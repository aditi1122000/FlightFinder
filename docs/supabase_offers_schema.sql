-- Offers: discounts that already sit inside a fare, plus card deals from public bank pages.
-- Refreshed three times a day by scripts/refresh_offers.py. Run after supabase_fares_schema.sql.

create table if not exists fare_discounts (
  id               bigint generated always as identity primary key,
  provider         text not null default 'booking_com',
  origin           char(3) not null,
  destination      char(3) not null,
  depart_date      date not null,
  cabin_class      text not null default 'economy',
  airline          text,
  flight_number    text,
  price            numeric(12,2) not null,
  list_price       numeric(12,2),
  discount         numeric(12,2) not null default 0,
  labels           text[] not null default '{}',   -- applied discount labels, badge text
  genius           boolean not null default false,
  fare_name        text,
  offer_token      text,
  fetched_at       timestamptz not null default now(),
  expires_at       timestamptz not null default now() + interval '1 day'
);
-- Plain unique constraint (PostgREST upsert can't target an expression index). '' stands in for null.
alter table fare_discounts drop constraint if exists fare_discounts_dedupe;
drop index if exists fare_discounts_dedupe;
update fare_discounts set flight_number = '' where flight_number is null;
update fare_discounts set offer_token = '' where offer_token is null;
alter table fare_discounts alter column flight_number set default '';
alter table fare_discounts alter column flight_number set not null;
alter table fare_discounts alter column offer_token set default '';
alter table fare_discounts alter column offer_token set not null;
alter table fare_discounts add constraint fare_discounts_dedupe
  unique (provider, origin, destination, depart_date, cabin_class, flight_number, offer_token);
create index if not exists fare_discounts_route
  on fare_discounts (origin, destination, depart_date, discount desc);
create index if not exists fare_discounts_expires_brin
  on fare_discounts using brin (expires_at);

-- Bank and card offers. source='scraped' rows come from public offer pages
-- (bank deal listings). source='curated' rows are entered by hand and only expire.
create table if not exists card_offers (
  id               bigint generated always as identity primary key,
  source           text not null check (source in ('scraped', 'curated')),
  bank             text not null,                  -- 'Axis Bank'
  card_name        text,                           -- 'Axis Bank Magnus', null = any card from this bank
  card_kind        text,                           -- 'credit', 'debit', 'emi'
  merchant         text not null,                  -- 'IndiGo', 'MakeMyTrip', 'Air India', 'Goibibo'
  title            text not null,
  code             text not null default '',  -- '' when the offer has no code; keeps the unique key free of nulls
  percent_off      numeric(5,2),
  max_amount       numeric(12,2),
  flat_amount      numeric(12,2),
  min_spend        numeric(12,2),
  applies_to       text not null default 'flights', -- 'flights', 'flights+hotels', 'travel'
  terms_url        text,
  valid_from       date,
  valid_until      date,
  fetched_at       timestamptz not null default now(),
  active           boolean not null default true
);
-- Plain unique constraint, not an expression index: PostgREST upsert matches ON CONFLICT
-- against a constraint, and coalesce() indexes are invisible to it.
alter table card_offers drop constraint if exists card_offers_dedupe;
drop index if exists card_offers_dedupe;
update card_offers set code = '' where code is null;
alter table card_offers alter column code set default '';
alter table card_offers alter column code set not null;
-- Which site listed it, which leg it covers, and which cards (if the T&C names any).
alter table card_offers add column if not exists site text not null default 'unknown';          -- 'axis_grab_deals', 'goibibo', 'ixigo', 'cleartrip'
alter table card_offers add column if not exists segment text not null default 'any';           -- 'domestic', 'international', 'any'
alter table card_offers add column if not exists eligible_cards text[] not null default '{}';   -- 'Flipkart SBI Credit Card'; empty = all cards of the bank
alter table card_offers add column if not exists excluded_cards text[] not null default '{}';   -- cards the T&C rules out, e.g. the bank's co-brand card
alter table card_offers add column if not exists detail_url text;                               -- offer page with the full table / T&C
alter table card_offers add column if not exists airline text;                                  -- set when the offer is for one airline's fares only
alter table card_offers add constraint card_offers_dedupe unique (site, bank, merchant, segment, code, title);
create index if not exists card_offers_lookup
  on card_offers (active, merchant, bank) where active;

-- Travel credit card catalogue. Refreshed from public comparison listings; one row per card.
-- Joins to card_offers on bank (and card_name when an offer is card-specific).
create table if not exists credit_cards (
  id               bigint generated always as identity primary key,
  slug             text not null,
  card_name        text not null,                  -- 'Axis Atlas Credit Card'
  bank             text not null,                  -- 'Axis Bank'
  co_brand         text,                           -- 'IndiGo', 'Vistara', 'MakeMyTrip', 'Marriott Bonvoy'
  co_brand_kind    text,                           -- 'airline', 'ota', 'hotel'
  tier             text,                           -- 'entry', 'mid', 'premium', 'super-premium' (by annual fee)
  joining_fee      numeric(10,2),
  annual_fee       numeric(10,2),
  categories       text[] not null default '{}',   -- 'Travel', 'Lounge Access', 'Rewards', 'Fuel'...
  highlights       text[] not null default '{}',   -- short bullets as listed
  lounge_access    boolean not null default false,
  listing_url      text,                           -- where we read it
  terms_url        text,                           -- bank's own MITC / T&C
  image_url        text,
  source           text not null default 'paisabazaar',
  fetched_at       timestamptz not null default now(),
  active           boolean not null default true
);
-- Plain-language summary written by the refresh: fee, lounge, earn rate, co-brand, live offers.
alter table credit_cards add column if not exists pitch text;
-- From the card's own PaisaBazaar page: section bullets, fees table, review rating.
alter table credit_cards add column if not exists details jsonb;                 -- {rewards[], travel_benefits[], welcome[], get_if[], eligibility[], pros[], cons[], about[], fees{}}
alter table credit_cards add column if not exists rating numeric(2,1);           -- Paisabazaar overall rating /5
alter table credit_cards add column if not exists pros text[] not null default '{}';
alter table credit_cards add column if not exists cons text[] not null default '{}';
alter table credit_cards add column if not exists finance_charge text;           -- '3.75% per month'
alter table credit_cards drop constraint if exists credit_cards_dedupe;
drop index if exists credit_cards_dedupe;
alter table credit_cards add constraint credit_cards_dedupe unique (bank, card_name);
create index if not exists credit_cards_bank on credit_cards (bank) where active;
create index if not exists credit_cards_cobrand on credit_cards (co_brand) where co_brand is not null;

-- Six watched routes (domestic, India->abroad, abroad->India), re-priced each refresh.
-- One row per route per refresh: cheapest fare seen, the airline, and the offers that applied.
create table if not exists route_watch (
  id               bigint generated always as identity primary key,
  category         text not null,                  -- 'domestic', 'outbound', 'inbound'
  origin           char(3) not null,
  destination      char(3) not null,
  depart_date      date not null,
  provider         text,                           -- 'booking_com'; null when no provider answered
  status           text not null,                  -- 'ok', 'provider_quota', 'no_fares', 'error'
  cheapest_price   numeric(12,2),
  currency         text,
  airline          text,
  flight_number    text,
  duration_minutes integer,
  stops            integer,
  fare_count       integer not null default 0,
  fare_discount    numeric(12,2) not null default 0,  -- discount already inside the cheapest fare
  card_offer_ids   bigint[] not null default '{}',    -- card_offers rows that apply to this route
  deep_link        text,
  fetched_at       timestamptz not null default now()
);
create index if not exists route_watch_route on route_watch (origin, destination, fetched_at desc);

create or replace function expire_card_offers() returns void language sql as $$
  update card_offers set active = false
  where active and valid_until is not null and valid_until < current_date;
  delete from fare_discounts where expires_at < now() - interval '1 hour';
$$;

-- Every refresh writes one row so the job can be watched.
create table if not exists offer_refresh_runs (
  id                bigint generated always as identity primary key,
  started_at        timestamptz not null default now(),
  finished_at       timestamptz,
  fare_rows         integer not null default 0,
  card_rows         integer not null default 0,
  catalog_rows      integer not null default 0,
  sources_ok        text[] not null default '{}',
  sources_failed    text[] not null default '{}',
  note              text
);

alter table offer_refresh_runs add column if not exists catalog_rows integer not null default 0;

alter table credit_cards      enable row level security;
alter table route_watch       enable row level security;
alter table fare_discounts    enable row level security;
alter table card_offers       enable row level security;
alter table offer_refresh_runs enable row level security;

-- pg_cron, three times a day (IST 07:00, 15:00, 21:00 = UTC 01:30, 09:30, 15:30):
--   select cron.schedule('offers-expire', '30 1,9,15 * * *', 'select expire_card_offers()');
-- The scrape and the fare-discount copy run from scripts/refresh_offers.py on the same schedule.
