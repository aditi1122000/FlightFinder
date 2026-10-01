-- Fares, providers and selections. Run after supabase_schema.sql.
--
-- Shape of the read path, fastest first:
--   1. in-process cache in the app (10 min TTL)            ~0 ms
--   2. route_fare_calendar  (one row per route/day/cabin)  one index lookup, date strips
--   3. fare_offers          (fresh offers per search)      one index range scan, full lists
--   4. live provider fan-out, in parallel, per-provider timeout and cooldown
-- Writes from step 4 fill 2 and 3 so the next person on that route skips the provider.
--
-- No traveller names or contact details live here. Selections keep counts only.

-- Providers ---------------------------------------------------------------

create table if not exists fare_providers (
  id                text primary key,                 -- 'booking_com', 'amadeus', 'duffel', ...
  display_name      text not null,
  enabled           boolean not null default true,
  priority          smallint not null default 100,    -- lower is asked first / trusted on ties
  timeout_ms        integer not null default 6000,
  max_rps           numeric(6,2),
  monthly_quota     integer,
  quota_used        integer not null default 0,
  quota_resets_at   timestamptz,
  cooldown_until    timestamptz,                      -- set on 429 so nobody calls it until then
  supports_deep_link boolean not null default false,  -- can link to one exact offer
  created_at        timestamptz not null default now()
);

insert into fare_providers (id, display_name, priority, timeout_ms, supports_deep_link)
values ('booking_com', 'Booking.com (RapidAPI)', 10, 6000, false)
on conflict (id) do nothing;

-- Every provider call, for routing on real latency and error rates.
create table if not exists provider_calls (
  id           bigint generated always as identity primary key,
  provider_id  text not null references fare_providers(id),
  origin       char(3) not null,
  destination  char(3) not null,
  depart_date  date not null,
  status       smallint not null,
  latency_ms   integer not null,
  offer_count  smallint not null default 0,
  created_at   timestamptz not null default now()
);
create index if not exists provider_calls_recent
  on provider_calls (provider_id, created_at desc);

-- Airports and the cities they belong to ---------------------------------

create table if not exists airports (
  iata          char(3) primary key,
  name          text not null,
  city_code     char(3) not null,      -- metro code: LON for LHR/LGW/STN, NYC for JFK/EWR/LGA
  city_name     text not null,
  country_code  char(2) not null,
  lat           double precision,
  lon           double precision,
  timezone      text,
  is_primary    boolean not null default false
);
create index if not exists airports_city on airports (city_code);
create index if not exists airports_city_name on airports (lower(city_name));

-- Searches ---------------------------------------------------------------

-- query_hash = sha1(origin|destination|depart|return|cabin|adults|children_ages).
-- Identical searches share one hash, so a cache hit is a single equality lookup.
create table if not exists search_requests (
  id              uuid primary key default gen_random_uuid(),
  query_hash      text not null,
  conversation_id uuid,
  origin          char(3) not null,
  destination     char(3) not null,
  depart_date     date not null,
  return_date     date,
  cabin_class     text not null default 'ECONOMY',
  adults          smallint not null default 1,
  children_ages   smallint[] not null default '{}',
  providers       text[] not null default '{}',
  result_count    smallint not null default 0,
  latency_ms      integer,
  served_from     text not null default 'live' check (served_from in ('memory', 'calendar', 'offers', 'live')),
  created_at      timestamptz not null default now()
);
create index if not exists search_requests_hash_recent
  on search_requests (query_hash, created_at desc);

-- Offers -----------------------------------------------------------------

-- Prices in minor units (paise) so sorting and min() stay integer.
create table if not exists fare_offers (
  id               bigint generated always as identity primary key,
  query_hash       text not null,
  provider_id      text not null references fare_providers(id),
  origin           char(3) not null,
  destination      char(3) not null,
  depart_date      date not null,
  depart_at        timestamptz,
  arrive_at        timestamptz,
  airline          text,
  flight_numbers   text[] not null default '{}',   -- {'6E 1407','6E 87'} for connections
  stops            smallint not null default 0,
  duration_min     integer,
  cabin_class      text not null,
  fare_brand       text,
  price_minor      bigint not null,
  list_price_minor bigint,
  currency         char(3) not null default 'INR',
  offer_token      text,                            -- provider handle for this exact offer
  deep_link        text,
  fetched_at       timestamptz not null default now(),
  expires_at       timestamptz not null default now() + interval '15 minutes'
);
-- "Give me this search, cheapest first" and the sort pills (cheapest / fastest).
create index if not exists fare_offers_query_price on fare_offers (query_hash, price_minor);
create index if not exists fare_offers_query_duration on fare_offers (query_hash, duration_min);
-- Route browsing across days without a hash.
create index if not exists fare_offers_route_day
  on fare_offers (origin, destination, depart_date, cabin_class, price_minor);
-- Expiry sweeps scan by time; BRIN stays tiny on append-only data.
create index if not exists fare_offers_expires_brin on fare_offers using brin (expires_at);
-- One row per provider/flight/time per search, so re-fetches upsert instead of piling up.
create unique index if not exists fare_offers_dedupe
  on fare_offers (query_hash, provider_id, flight_numbers, depart_at, cabin_class);

-- Cheapest fare per route and day. This is what the date strip reads.
create table if not exists route_fare_calendar (
  origin         char(3) not null,
  destination    char(3) not null,
  depart_date    date not null,
  cabin_class    text not null,
  pax_key        text not null default '1',        -- '1', '2', '2+8', '1+0' (adults + child ages)
  min_price_minor bigint not null,
  currency       char(3) not null default 'INR',
  provider_id    text not null references fare_providers(id),
  airline        text,
  flight_numbers text[] not null default '{}',
  fetched_at     timestamptz not null default now(),
  expires_at     timestamptz not null default now() + interval '6 hours',
  primary key (origin, destination, depart_date, cabin_class, pax_key)
);

-- Keep the calendar row only when the new price is lower or the old one is stale.
create or replace function upsert_route_fare(
  p_origin char(3), p_destination char(3), p_day date, p_cabin text, p_pax text,
  p_price bigint, p_provider text, p_airline text, p_flights text[]
) returns void language sql as $$
  insert into route_fare_calendar as c
    (origin, destination, depart_date, cabin_class, pax_key, min_price_minor, provider_id, airline, flight_numbers)
  values (p_origin, p_destination, p_day, p_cabin, p_pax, p_price, p_provider, p_airline, p_flights)
  on conflict (origin, destination, depart_date, cabin_class, pax_key) do update
    set min_price_minor = excluded.min_price_minor,
        provider_id     = excluded.provider_id,
        airline         = excluded.airline,
        flight_numbers  = excluded.flight_numbers,
        fetched_at      = now(),
        expires_at      = now() + interval '6 hours'
  where c.expires_at < now() or excluded.min_price_minor < c.min_price_minor;
$$;

-- What the user picked ---------------------------------------------------

create table if not exists flight_selections (
  id                        uuid primary key default gen_random_uuid(),
  conversation_id           uuid not null,
  provider                  text not null,
  airline                   text,
  flight_number             text,
  origin_code               char(3),
  destination_code          char(3),
  booking_destination_code  char(3),             -- airport confirmed in the dialog
  departure_date            date,
  departure_at              text,
  arrival_at                text,
  cabin_class               text,
  fare_name                 text,
  price                     numeric(12,2),
  list_price                numeric(12,2),
  discount                  numeric(12,2),
  currency                  char(3) not null default 'INR',
  stops                     smallint,
  duration_minutes          integer,
  adults                    smallint not null default 1,
  children                  smallint not null default 0,
  infants                   smallint not null default 0,
  offer_token               text,
  deep_link                 text,
  selected_at               timestamptz not null default now()
);
create index if not exists flight_selections_conversation on flight_selections (conversation_id, selected_at desc);
create index if not exists flight_selections_route on flight_selections (origin_code, destination_code, departure_date);

-- Housekeeping -----------------------------------------------------------

-- Schedule with pg_cron:  select cron.schedule('fares-sweep', '*/15 * * * *', 'select sweep_expired_fares()');
create or replace function sweep_expired_fares() returns void language sql as $$
  delete from fare_offers where expires_at < now() - interval '1 hour';
  delete from route_fare_calendar where expires_at < now() - interval '7 days';
  delete from provider_calls where created_at < now() - interval '30 days';
$$;

-- Server-side only. The app writes with the service role key.
alter table fare_providers      enable row level security;
alter table provider_calls      enable row level security;
alter table airports            enable row level security;
alter table search_requests     enable row level security;
alter table fare_offers         enable row level security;
alter table route_fare_calendar enable row level security;
alter table flight_selections   enable row level security;
