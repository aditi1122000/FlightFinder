-- Rupees per point, one row per card. Run after supabase_offers_schema.sql.
-- A null inr_per_point means the bank's own page did not state a rate.

create table if not exists card_point_rates (
  id             bigint generated always as identity primary key,
  slug           text not null,                 -- 'axis-bank-atlas'
  bank           text not null,                 -- 'Axis Bank'
  card_name      text not null,
  points_name    text,                          -- 'EDGE Mile'; null when the page names none
  inr_per_point  numeric(8,2),                  -- null when the page states no rupee value
  source_url     text,                          -- the card page the rate was read from
  fetched_at     timestamptz not null default now(),
  active         boolean not null default true
);

alter table card_point_rates drop constraint if exists card_point_rates_dedupe;
alter table card_point_rates add constraint card_point_rates_dedupe unique (bank, card_name);
create index if not exists card_point_rates_bank on card_point_rates (bank) where active;

alter table card_point_rates enable row level security;
