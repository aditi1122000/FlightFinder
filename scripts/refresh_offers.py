"""
Batch refresh scrapers (run separately or together).

Tasks (see docs/aggregator_expansion.md):
  offers   — public deal pages → card_offers (+ curated) + expire stale
  catalog  — travel credit card listings → credit_cards
  points   — Axis + catalogue pages → card_point_rates
  routes   — re-price recent selections → fare_discounts (needs RapidAPI)
  watch    — six watched routes → route_watch (needs RapidAPI)

Examples:
  ./venv/bin/python scripts/refresh_offers.py --only offers
  ./venv/bin/python scripts/refresh_offers.py --only catalog
  ./venv/bin/python scripts/refresh_offers.py --only points
  ./venv/bin/python scripts/refresh_offers.py --only routes,watch
  ./venv/bin/python scripts/refresh_offers.py            # all of: offers, catalog, points
  ./venv/bin/python scripts/refresh_offers.py --routes --watch   # all + fare APIs
  ./venv/bin/python scripts/refresh_offers.py --dry-run
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import uuid
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))

from src.services import offers as offers_mod  # noqa: E402
from src.services.scrape_http import ScrapeBlocked  # noqa: E402
from src.services.supabase_persistence import (  # noqa: E402
    _get_client,
    expire_offers,
    insert_route_watch,
    load_card_offers,
    load_credit_cards,
    record_health_event,
    record_refresh_run,
    upsert_card_offers,
    upsert_credit_cards,
    upsert_fare_discounts,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("refresh_offers")

# Pause between distinct offer/catalog sources (on top of per-host pacing).
SOURCE_GAP_S = float(os.getenv("SCRAPE_SOURCE_GAP_S") or "4.0")


def _health(component: str, source: str, status: str, exc: BaseException | None = None,
            run_id: str | None = None, **extra) -> None:
    http_status = getattr(exc, "status", None) if exc else None
    message = str(exc) if exc else extra.pop("message", "")
    record_health_event(
        component, source, status,
        http_status=http_status,
        message=message,
        detail=extra or None,
        run_id=run_id,
    )


def scrape_card_offers(dry_run: bool, known_cards: list | None = None,
                       run_id: str | None = None) -> tuple[int, list, list]:
    ok, failed, total = [], [], 0
    names = list(offers_mod.SCRAPE_SOURCES)
    for i, name in enumerate(names):
        if i:
            time.sleep(SOURCE_GAP_S)
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
            if not dry_run:
                _health("scraper", name, "ok", run_id=run_id, rows=len(rows))
        except ScrapeBlocked as exc:
            log.warning("%s blocked (%s); leaving prior rows, skipping host", name, exc)
            failed.append(name)
            if not dry_run:
                _health("scraper", name, "blocked", exc, run_id=run_id)
        except Exception as exc:
            log.warning("%s failed: %s", name, exc)
            failed.append(name)
            if not dry_run:
                _health("scraper", name, "fail", exc, run_id=run_id)
    curated = [{**r, "source": "curated"} for r in offers_mod.CURATED_CARD_OFFERS]
    if curated:
        log.info("curated: %d rows", len(curated))
        total += len(curated) if dry_run else upsert_card_offers(curated)
    return total, ok, failed


def refresh_card_catalog(dry_run: bool, run_id: str | None = None) -> tuple[int, list, list]:
    ok, failed, rows = [], [], []
    names = list(offers_mod.CARD_CATALOG_SOURCES)
    for i, name in enumerate(names):
        if i:
            time.sleep(SOURCE_GAP_S)
        try:
            got = offers_mod.fetch_catalog_source(name)
            log.info("%s: %d cards listed", name, len(got))
            rows += got
            ok.append(name)
            if not dry_run:
                _health("scraper", f"catalog:{name}", "ok", run_id=run_id, rows=len(got))
        except ScrapeBlocked as exc:
            log.warning("%s blocked (%s); keeping prior catalogue", name, exc)
            failed.append(name)
            if not dry_run:
                _health("scraper", f"catalog:{name}", "blocked", exc, run_id=run_id)
        except Exception as exc:
            log.warning("%s failed: %s", name, exc)
            failed.append(name)
            if not dry_run:
                _health("scraper", f"catalog:{name}", "fail", exc, run_id=run_id)
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


def refresh_point_rates(dry_run: bool, run_id: str | None = None) -> tuple[int, list, list]:
    """Fresh Axis scrape, then every credit_cards page into card_point_rates."""
    from src.services.axis_points import scrape_axis_cards, _write_cache
    from src.services.card_points import sync_card_point_rates

    ok, failed = [], []
    try:
        axis_rows = scrape_axis_cards()
        rated = sum(1 for row in axis_rows if row.get("inr_per_point") is not None)
        log.info("axis: %d cards, %d with a stated rupee rate", len(axis_rows), rated)
        if not dry_run:
            _write_cache(axis_rows)
            _health("scraper", "axis_points", "ok", run_id=run_id, rows=len(axis_rows), rated=rated)
        ok.append("axis_points")
    except ScrapeBlocked as exc:
        log.warning("axis_points blocked: %s", exc)
        failed.append("axis_points")
        if not dry_run:
            _health("scraper", "axis_points", "blocked", exc, run_id=run_id)
    except Exception as exc:
        log.warning("axis_points failed: %s", exc)
        failed.append("axis_points")
        if not dry_run:
            _health("scraper", "axis_points", "fail", exc, run_id=run_id)

    try:
        if dry_run:
            from src.services.supabase_persistence import load_credit_card_pages
            pages = load_credit_card_pages()
            log.info("card_point_rates dry-run: would scrape %d catalogue pages", len(pages))
            return len(pages), ok, failed
        rows = sync_card_point_rates()
        rated = sum(1 for row in rows if row.get("inr_per_point") is not None)
        log.info("card_point_rates: %d cards, %d with a stated rupee rate", len(rows), rated)
        ok.append("card_point_rates")
        _health("scraper", "card_point_rates", "ok", run_id=run_id, rows=len(rows), rated=rated)
        return len(rows), ok, failed
    except ScrapeBlocked as exc:
        log.warning("card_point_rates blocked: %s", exc)
        failed.append("card_point_rates")
        _health("scraper", "card_point_rates", "blocked", exc, run_id=run_id)
        return 0, ok, failed
    except Exception as exc:
        log.warning("card_point_rates failed: %s", exc)
        failed.append("card_point_rates")
        _health("scraper", "card_point_rates", "fail", exc, run_id=run_id)
        return 0, ok, failed


def refresh_fare_discounts(dry_run: bool, run_id: str | None = None) -> int:
    from src.services.flight_services import enabled_providers, search_flights_api

    if not enabled_providers():
        log.info("fare provider unavailable or on cooldown; skipping fare_discounts refresh")
        if not dry_run:
            _health("fare_api", "booking_com", "skip", run_id=run_id, message="provider unavailable or cooldown")
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
            if not dry_run:
                status = "blocked" if err in ("provider_quota",) else "fail"
                _health("fare_api", "booking_com", status, run_id=run_id, message=str(err),
                        origin=r["origin_code"], destination=r["destination_code"])
            if not enabled_providers():
                break
            continue
        rows = offers_mod.fare_discount_rows(flights)
        log.info("%s-%s %s: %d fares, %d with a discount", r["origin_code"], r["destination_code"], r["departure_date"], len(flights), len(rows))
        total += len(rows) if dry_run else upsert_fare_discounts(rows)
    return total


TASKS = ("offers", "catalog", "points", "routes", "watch")
DEFAULT_BATCH = ("offers", "catalog", "points")


def parse_tasks(raw: str | None, *, routes: bool, watch: bool, skip_points: bool) -> list[str]:
    """Resolve --only / legacy flags into an ordered task list."""
    if raw:
        wanted = []
        for part in raw.replace(" ", "").split(","):
            if not part:
                continue
            if part not in TASKS:
                raise SystemExit(f"Unknown task {part!r}; choose from: {', '.join(TASKS)}")
            if part not in wanted:
                wanted.append(part)
        return wanted
    tasks = list(DEFAULT_BATCH)
    if skip_points and "points" in tasks:
        tasks.remove("points")
    if routes and "routes" not in tasks:
        tasks.append("routes")
    if watch and "watch" not in tasks:
        tasks.append("watch")
    return tasks


def run_tasks(tasks: list[str], dry_run: bool = False) -> dict:
    """Run selected batch scrapers. Returns counts + ok/failed source names."""
    card_rows = catalog_rows = point_rows = fare_rows = watch_rows = 0
    ok: list = []
    failed: list = []
    task_set = set(tasks)
    run_id = uuid.uuid4().hex[:12]
    if not dry_run:
        _health("refresh", "batch", "ok", run_id=run_id, message=f"start tasks={','.join(tasks)}")

    if "catalog" in task_set:
        catalog_rows, ok2, failed2 = refresh_card_catalog(dry_run, run_id=run_id)
        ok += ok2
        failed += failed2

    if "offers" in task_set:
        # Prefer names already in the table (fresh if catalog ran above).
        known_cards = [c["card_name"] for c in load_credit_cards()]
        card_rows, ok1, failed1 = scrape_card_offers(dry_run, known_cards, run_id=run_id)
        ok += ok1
        failed += failed1
        if not dry_run:
            expire_offers()

    if "points" in task_set:
        point_rows, ok3, failed3 = refresh_point_rates(dry_run, run_id=run_id)
        ok += ok3
        failed += failed3

    if "routes" in task_set:
        fare_rows = refresh_fare_discounts(dry_run, run_id=run_id)

    if "watch" in task_set:
        watch_rows = watch_routes(dry_run)

    if not dry_run:
        note_bits = [f"tasks={','.join(tasks)}", f"run_id={run_id}"]
        if "watch" in task_set:
            note_bits.append(f"route_watch={watch_rows}")
        if "points" in task_set:
            note_bits.append(f"point_rates={point_rows}")
        record_refresh_run(
            fare_rows, card_rows, ok, failed, catalog_rows=catalog_rows, note="; ".join(note_bits),
        )
        batch_status = "fail" if failed and not ok else ("ok" if not failed else "ok")
        if failed and ok:
            batch_status = "ok"  # partial — still ok at batch level; sources logged separately
        if failed and not ok:
            batch_status = "fail"
        _health(
            "refresh", "batch", batch_status, run_id=run_id,
            message=f"done ok={ok} failed={failed}",
            card_offers=card_rows, credit_cards=catalog_rows, point_rates=point_rows,
        )

    result = {
        "tasks": tasks,
        "run_id": run_id,
        "card_offers": card_rows,
        "credit_cards": catalog_rows,
        "point_rates": point_rows,
        "fare_discounts": fare_rows,
        "route_watch": watch_rows,
        "ok": ok,
        "failed": failed,
    }
    log.info(
        "done: tasks=%s card_offers=%d credit_cards=%d point_rates=%d fare_discounts=%d route_watch=%d ok=%s failed=%s",
        tasks, card_rows, catalog_rows, point_rows, fare_rows, watch_rows, ok, failed,
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run one or more batch scrapers (offers, catalog, points, routes, watch).",
    )
    parser.add_argument(
        "--only",
        metavar="TASKS",
        help="comma-separated: offers,catalog,points,routes,watch "
        "(default without this flag: offers,catalog,points)",
    )
    parser.add_argument("--routes", action="store_true", help="legacy: also run fare_discounts")
    parser.add_argument("--watch", action="store_true", help="legacy: also run route_watch")
    parser.add_argument("--skip-points", action="store_true", help="legacy: omit points from default set")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    tasks = parse_tasks(args.only, routes=args.routes, watch=args.watch, skip_points=args.skip_points)
    result = run_tasks(tasks, dry_run=args.dry_run)
    failed = result["failed"]
    ok = result["ok"]
    return 0 if not failed or ok else 1


if __name__ == "__main__":
    sys.exit(main())
