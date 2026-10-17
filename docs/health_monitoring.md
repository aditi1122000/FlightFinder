# Health monitoring (scrapers + fare API)

No Grafana / Loki. Scrapers and the fare API write to Supabase; a small ops page reads that table.

## 1. Create the table

Run once in the Supabase SQL editor:

[`docs/supabase_health_schema.sql`](supabase_health_schema.sql)

## 2. What gets logged

| Component | Source examples | Status |
|-----------|-----------------|--------|
| `scraper` | `cleartrip`, `goibibo`, `axis_points`, … | `ok` / `fail` / `blocked` |
| `fare_api` | `booking_com` | `blocked` (429/403), `fail`, `skip` |
| `refresh` | `batch` | start / finish of a refresh run |

`blocked` = HTTP 403/429 (or RapidAPI quota). Prior offer rows are left alone.

## 3. Ops UI (own endpoint)

With plan API running (`./venv/bin/python -m src.plan_api`):

| URL | Purpose |
|-----|---------|
| http://127.0.0.1:8780/ops/health | HTML dashboard (latest + recent events) |
| http://127.0.0.1:8780/api/ops/health | JSON: `{ latest, events }` |
| http://127.0.0.1:8780/api/ops/health/events?status=blocked | Filtered events |

If `ADMIN_API_TOKEN` is set, send header `X-Admin-Token: …` on the JSON routes (and open `/ops/health?token=…` or use the same header via a bookmarklet). Locally with no token, the page is open.

## 4. Why Supabase

GitHub Actions and local scrapers already use Supabase. One table means cron failures show up in the ops UI without a second logging stack.
