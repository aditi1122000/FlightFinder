-- Health / failure log for scrapers and fare APIs.
-- Run once in the Supabase SQL editor.
-- The ops UI at GET /ops/health (plan_api) reads this table.

create table if not exists system_health_events (
  id            bigint generated always as identity primary key,
  created_at    timestamptz not null default now(),
  component     text not null,          -- scraper | fare_api | plan_api | refresh
  source        text not null,          -- cleartrip | goibibo | booking_com | axis_points | …
  status        text not null,          -- ok | fail | blocked | skip
  http_status   integer,
  message       text,
  detail        jsonb not null default '{}'::jsonb,
  run_id        text                    -- optional batch id linking events in one refresh
);

create index if not exists system_health_events_created_idx
  on system_health_events (created_at desc);

create index if not exists system_health_events_source_idx
  on system_health_events (component, source, created_at desc);

create index if not exists system_health_events_status_idx
  on system_health_events (status, created_at desc)
  where status in ('fail', 'blocked');

-- Latest status per source (ops UI summary).
create or replace view system_health_latest as
select distinct on (component, source)
  component,
  source,
  status,
  http_status,
  message,
  created_at,
  run_id
from system_health_events
order by component, source, created_at desc;

alter table system_health_events enable row level security;

-- Service role inserts from GitHub Actions / plan_api; ops UI uses the same key server-side.
