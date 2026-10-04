"""
Refresh the offers tables. Meant to run three times a day.

  ./venv/bin/python scripts/refresh_offers.py            # scrape card deals + expire stale rows
  ./venv/bin/python scripts/refresh_offers.py --routes   # also re-price saved routes to refresh fare_discounts
  ./venv/bin/python scripts/refresh_offers.py --dry-run  # print what would be written

What it does:
  1. Fetches each public bank deal page in SCRAPE_SOURCES, parses travel offers, upserts card_offers.
  2. Upserts CURATED_CARD_OFFERS (hand-entered rows).
  3. Refreshes the travel credit card catalogue (credit_cards) from public comparison listings.
  4. Marks expired card offers inactive and deletes stale fare_discounts.
  5. With --routes: re-runs the fare search for recently selected routes (flight_selections,
     next 60 days) and copies any discounts found into fare_discounts. Each route is one
     provider call, so this is skipped while the provider is on quota cooldown.

  6. With --watch: prices the six watched routes (domestic, India->abroad, abroad->India) and
     records cheapest fare + applicable offers in route_watch.

Schedule (IST 07:00 / 15:00 / 21:00):
  30 1,9,15 * * *  cd /path/to/flightFinder && ./venv/bin/python scripts/refresh_offers.py --routes --watch >> offers_refresh.log 2>&1
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))

from src.services import offers as offers_mod  # noqa: E402
from src.services.supabase_persistence import (  # noqa: E402
    _get_client,
    expire_offers,
    insert_route_watch,
    load_card_offers,
    load_credit_cards,
    record_refresh_run,
    upsert_card_offers,
    upsert_credit_cards,
    upsert_fare_discounts,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("refresh_offers")


def scrape_card_offers(dry_run: bool, known_cards: list | None = None) -> tuple[int, list, list]:
    ok, failed, total = [], [], 0
    for name in offers_mod.SCRAPE_SOURCES:
        try:
            rows = offers_mod.fetch_source(name, known_cards)
            log.info("%s: %d flight offers", name, len(rows))
            for r in rows[:4]:
                log.info("  %-20s %-13s %-5s %-14s %s%s", r["bank"], r["segment"], r.get("card_kind"), r.get("code") or "-",
                         r["title"][:60], f"  cards={r['eligible_cards']}" if r.get("eligible_cards") else "")
            if not dry_run:
                total += upsert_card_offers(rows)
            else:
                total += len(rows)
            ok.append(name)
        except Exception as exc:
            log.warning("%s failed: %s", name, exc)
            failed.append(name)
    curated = [{**r, "source": "curated"} for r in offers_mod.CURATED_CARD_OFFERS]
    if curated:
        log.info("curated: %d rows", len(curated))
        total += len(curated) if dry_run else upsert_card_offers(curated)
    return total, ok, failed


def refresh_card_catalog(dry_run: bool) -> tuple[int, list, list]:
    ok, failed, rows = [], [], []
    for name in offers_mod.CARD_CATALOG_SOURCES:
        try:
            got = offers_mod.fetch_catalog_source(name)
            log.info("%s: %d cards listed", name, len(got))
            rows += got
            ok.append(name)
        except Exception as exc:
            log.warning("%s failed: %s", name, exc)
            failed.append(name)
    cards = [c for c in offers_mod.merge_catalog(rows) if offers_mod.is_travel_card(c)]
    enriched = offers_mod.enrich_card_details(cards)
    log.info("card detail pages: %d of %d read (rewards, travel benefits, pros/cons, fees, rating)", enriched, len(cards))
    # Attach the bank's live offers so each card's pitch says what it gets you right now.
    by_bank = offers_mod.offers_by_bank(load_card_offers())
    for c in cards:
        c["pitch"] = offers_mod.card_pitch(c, by_bank.get(offers_mod.bank_key(c["bank"])))
    log.info("catalogue: %d travel cards across %d banks", len(cards), len({c["bank"] for c in cards}))
    for c in sorted(cards, key=lambda c: (c["bank"], c["card_name"]))[:4]:
        log.info("  %-22s %-42s\n      %s", c["bank"], c["card_name"][:42], c["pitch"].replace("\n", "\n      "))
    return (len(cards) if dry_run else upsert_credit_cards(cards)), ok, failed


def watch_routes(dry_run: bool) -> int:
    """Price the six watched routes and record cheapest fare + applicable offers."""
    from src.services.flight_services import enabled_providers, search_flights_api

    depart = (date.today() + timedelta(days=offers_mod.WATCH_DAYS_AHEAD)).isoformat()
    card_rows = load_card_offers()
    rows = []
    for route in offers_mod.WATCH_ROUTES:
        flights, err = [], None
        if enabled_providers():
            slots = {
                "origin": {"airport_code": route["origin"]},
                "destination": {"airport_code": route["destination"]},
                "departure_date": depart,
                "cabin_class": "economy",
                "passengers": {"adults": 1, "children": 0, "infants": 0},
            }
            try:
                flights, msg, details = search_flights_api(slots, max_results=30)
                if msg:  # msg is user-facing prose; keep the short reason code in the table
                    err = (details or {}).get("reason") or "error"
            except Exception as exc:
                err, flights = "error", []
                log.warning("%s-%s search raised: %s", route["origin"], route["destination"], exc)
        else:
            err = "provider_quota"
        row = offers_mod.watch_route_row(route, depart, flights or [], err, card_rows)
        rows.append(row)
        log.info("%-8s %s-%s %s: %s%s  offers=%d", route["category"], route["origin"], route["destination"], depart,
                 row["status"], f" ₹{row['cheapest_price']:,.0f} {row.get('airline')}" if row.get("cheapest_price") else "",
                 len(row["card_offer_ids"]))
    return len(rows) if dry_run else insert_route_watch(rows)


def recent_routes(limit: int = 12) -> list[dict]:
    """Routes people actually chose, departing in the next 60 days."""
    client = _get_client()
    if client is None:
        return []
    today = date.today()
    try:
        res = (
            client.table("flight_selections")
            .select("origin_code,destination_code,departure_date,cabin_class")
            .gte("departure_date", today.isoformat())
            .lte("departure_date", (today + timedelta(days=60)).isoformat())
            .order("selected_at", desc=True)
            .limit(200)
            .execute()
        )
    except Exception as exc:
        log.warning("flight_selections read failed: %s", exc)
        return []
    seen, routes = set(), []
    for row in res.data or []:
        key = (row.get("origin_code"), row.get("destination_code"), row.get("departure_date"), row.get("cabin_class") or "economy")
        if None in key[:3] or key in seen:
            continue
        seen.add(key)
        routes.append(row)
        if len(routes) >= limit:
            break
    return routes


def refresh_fare_discounts(dry_run: bool) -> int:
    from src.services.flight_services import enabled_providers, search_flights_api

    if not enabled_providers():
        log.info("fare provider unavailable or on cooldown; skipping fare_discounts refresh")
        return 0
    total = 0
    for r in recent_routes():
        slots = {
            "origin": {"airport_code": r["origin_code"]},
            "destination": {"airport_code": r["destination_code"]},
            "departure_date": r["departure_date"],
            "cabin_class": r.get("cabin_class") or "economy",
            "passengers": {"adults": 1, "children": 0, "infants": 0},
        }
        flights, err, _ = search_flights_api(slots, max_results=30)
        if err:
            log.info("%s-%s %s: %s", r["origin_code"], r["destination_code"], r["departure_date"], err)
            if not enabled_providers():
                break
            continue
        rows = offers_mod.fare_discount_rows(flights)
        log.info("%s-%s %s: %d fares, %d with a discount", r["origin_code"], r["destination_code"], r["departure_date"], len(flights), len(rows))
        total += len(rows) if dry_run else upsert_fare_discounts(rows)
    return total


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--routes", action="store_true", help="also refresh fare_discounts for recent routes")
    parser.add_argument("--watch", action="store_true", help="price the six watched routes into route_watch")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    # Catalogue first: its card names let the offer scrapers tag card-specific deals.
    known_cards = [c["card_name"] for c in load_credit_cards()]
    card_rows, ok, failed = scrape_card_offers(args.dry_run, known_cards)
    catalog_rows, ok2, failed2 = refresh_card_catalog(args.dry_run)
    ok += ok2
    failed += failed2
    fare_rows = refresh_fare_discounts(args.dry_run) if args.routes else 0
    watch_rows = watch_routes(args.dry_run) if args.watch else 0
    if not args.dry_run:
        expire_offers()
        record_refresh_run(fare_rows, card_rows, ok, failed, catalog_rows=catalog_rows,
                           note=f"route_watch={watch_rows}" if args.watch else "")
    log.info("done: card_offers=%d credit_cards=%d fare_discounts=%d route_watch=%d ok=%s failed=%s",
             card_rows, catalog_rows, fare_rows, watch_rows, ok, failed)
    return 0 if not failed or ok else 1


if __name__ == "__main__":
    sys.exit(main())
