-- User trip price alerts. Ops / route_watch can email when cheapest ≤ target.
-- Run once in the Supabase SQL editor.

create table if not exists price_alerts (
  id               bigint generated always as identity primary key,
  created_at       timestamptz not null default now(),
  email            text not null,
  origin           char(3) not null,
  destination      char(3) not null,
  depart_date      date not null,
  return_date      date,
  cabin_class      text not null default 'economy',
  target_price     numeric(12,2) not null,
  currency         text not null default 'INR',
  baseline_price   numeric(12,2),          -- cash fare when they set the alert
  active           boolean not null default true,
  last_checked_at  timestamptz,
  last_price       numeric(12,2),
  triggered_at     timestamptz,
  note             text
);

create index if not exists price_alerts_active_route
  on price_alerts (active, origin, destination, depart_date)
  where active;

create index if not exists price_alerts_email
  on price_alerts (email);

alter table price_alerts enable row level security;
-- Inserts via service role from Streamlit / plan_api.
