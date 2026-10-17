# Scheduling scrapers and covering more aggregators

## FastAPI control plane

The plan API (`src/plan_api.py`) is FastAPI on port **8780** (Vite proxies `/api`).

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/health` | Liveness |
| GET | `/api/cards` | Wallet / point-rate cards for the web app |
| POST | `/api/plan` | Live fare plan (same as before) |
| GET | `/api/admin/scrapers` | List offer / catalogue / point scrape sources |
| POST | `/api/admin/refresh` | Start a background refresh job (`tasks` list) |
| GET | `/api/admin/refresh` | Recent jobs |
| GET | `/api/admin/refresh/{job_id}` | Job status |

```bash
# start API
./venv/bin/python -m src.plan_api

# one batch task (set ADMIN_API_TOKEN in .env in production)
curl -X POST http://127.0.0.1:8780/api/admin/refresh \
  -H 'Content-Type: application/json' \
  -H "X-Admin-Token: $ADMIN_API_TOKEN" \
  -d '{"tasks": ["offers"]}'

# or several
curl -X POST http://127.0.0.1:8780/api/admin/refresh \
  -H 'Content-Type: application/json' \
  -H "X-Admin-Token: $ADMIN_API_TOKEN" \
  -d '{"tasks": ["catalog", "points"]}'
```

GitHub Actions remains the preferred schedule. FastAPI is for local / ops triggers and the web app.

## What already runs on a schedule

Each batch scraper from the prod diagram is its **own** GitHub Actions workflow (separate task in the Actions tab):

| Workflow | Task | Cron (UTC) | When (IST) | Script |
|----------|------|------------|------------|--------|
| **Scrape card offers** | `offers` | `30 1 * * 1,3,5` | Mon/Wed/Fri 07:00 | `--only offers` |
| **Scrape travel cards** | `catalog` | `0 2 * * 1,3,5` | Mon/Wed/Fri 07:30 | `--only catalog` |
| **Scrape point rates** | `points` | `0 3 * * 1,3,5` | Mon/Wed/Fri 08:00 | `--only points` |
| **Refresh route watch** | `routes,watch` | `45 2 * * 1,3,5` | Mon/Wed/Fri 08:15 | `--only routes,watch` |
| **Refresh all batch scrapers** | all jobs | — | manual only | one workflow, separate jobs |

**Resolution:** each scraper runs **3× per week** (one shot on Mon/Wed/Fri), staggered ~30 min apart so GitHub’s IP is not hammering every hub on the same minute. Manual **Run workflow** still works any day.

Live flight search is **not** a cron job — Booking.com via RapidAPI on each search.

### Secrets required in the GitHub repo

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `RAPIDAPI_KEY` / `RAPIDAPI_HOST` (only for route watch)

Without Supabase secrets, the job runs but cannot persist rows.

Mac `crontab` is optional and weaker (machine must be awake). Prefer Actions.

---

## Two products, two strategies

Do **not** treat “top 10 aggregators” as one scrape for everything.

| Job | Good approach | Bad approach |
|-----|---------------|--------------|
| Live **flight prices** | Fare APIs / affiliates, parallel, cache | Browser-scrape 10 result pages per search |
| **Card / bank offers** | Batch scrape public offer hubs a few times/day | Scrape offer pages on every user click |
| **Book** | Build search URLs (+ coupon) | Scrape a deep link from the results HTML |

---

## Top 20 aggregators (both green boxes)

Registry: `src/services/aggregators.py` (`AGGREGATORS` = 20 OTAs/metas).

| Diagram box | Behaviour | Coverage |
|-------------|-----------|----------|
| **Card offers – batch scrape** | Cron → public deal pages → `card_offers` | All scrape-enabled hubs (~18): dedicated parsers for Goibibo / Cleartrip / ixigo / Axis; generic parser for EaseMyTrip, Yatra, HappyEasyGo, Via, Expedia, Trip.com, SOTC, Thomas Cook, Akbar, bank hubs, … |
| **Booking links – built live** | On Book → search URL + coupon | **All 20** via `build_search_url` — **no scrape** on click |

MakeMyTrip is book-only (`scrape_hard`); Goibibo covers most of that group’s coupons. Metas without a public deal wall (Skyscanner, Kayak, Agoda, …) still get book URLs.

**Cadence (anti-blacklist):** Mon/Wed/Fri, paced GETs, abort host on 403/429. Health → `system_health_events`.

### Flight prices (yellow box — not green)

Booking.com RapidAPI on search. Do **not** scrape OTA fare HTML on the request path.

---

## Ways to grow further

### Option 1 — Provider registry (recommended for flights)

```
FARE_PROVIDERS = {
  booking_com: search_fn + enabled?,
  amadeus: …,
  …
}
```

- Parallel fan-out, merge, dedupe, sort by price.
- Each provider has its own quota / cooldown.
- UI always has Booking cash first; others are “also seen on X” when useful.

### Option 2 — Offer hub expansion (recommended for coupons)

Add one parser per public offers URL into the existing scrape map. Cron stays the same. App matching logic already filters by airline / segment / fare.

### Option 3 — Browser scrape of fare results (avoid for prod search)

Playwright against 10 OTAs per search: 15–90+ s, fragile, ToS risk. Use only for offline research or rare audits, never on the Book path.

### Option 4 — Hybrid “nightly price check”

Cron builds search URLs for watched routes, optionally browser-scrapes **one** secondary OTA into a cache table for comparison. User search still uses APIs.

---

## Rollout order

1. Confirm Actions secrets and that each dedicated workflow succeeds (Actions → Run workflow).
2. Add 2–3 more **offer** sources (HDFC / Amex / EaseMyTrip offers) — same `offers` cron.
3. Add a second **flight API** provider if you get keys — same live path as Booking.
4. Only then consider a background scrape experiment for one OTA on watched routes.

---

## What “done” looks like

- Scrapers: one Actions workflow per batch lane (offers / catalogue / point rates), logged in `offer_refresh_runs` + `system_health_events` (see `docs/health_monitoring.md`).
- Live search: 1 primary + N optional APIs, under a few seconds.
- Offers: 8–10 bank/OTA offer hubs in the table, refreshed 3×/week.
- Book: constructed URLs for every site you show an offer for — still no fare HTML scrape.
