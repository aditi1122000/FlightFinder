"""FastAPI bridge: fare plan for the web app, plus scraper refresh jobs."""

from __future__ import annotations

import logging
import math
import os
import re
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from src.services.axis_points import axis_cards
from src.services.flight_services import build_booking_url_for_flight, search_flights_api
from src.services.offers import estimate_saving, offers_for_fare, route_segment, suggest_cards
from src.services.points_transfer import best_card_net, best_transfer, transfer_options, wallet_cards
from src.services.supabase_persistence import (
    insert_price_alert,
    load_active_price_alerts,
    load_card_offers,
    load_card_point_rates,
    load_credit_cards,
    load_health_events,
    load_health_latest,
)

logger = logging.getLogger(__name__)

HOST = os.getenv("PLAN_API_HOST", "127.0.0.1")
PORT = int(os.getenv("PLAN_API_PORT", "8780"))
ADMIN_API_TOKEN = (os.getenv("ADMIN_API_TOKEN") or "").strip()
ROOT = Path(__file__).resolve().parents[1]

_jobs_lock = threading.Lock()
_jobs: Dict[str, Dict[str, Any]] = {}


def _site_name(offer: Dict) -> str:
    blob = f"{offer.get('site') or ''} {offer.get('merchant') or ''}".lower()
    if "goibibo" in blob:
        return "Goibibo"
    if "makemytrip" in blob or re.search(r"\bmmt\b", blob):
        return "MakeMyTrip"
    if "cleartrip" in blob:
        return "Cleartrip"
    if "ixigo" in blob:
        return "ixigo"
    merchant = str(offer.get("merchant") or "").strip()
    return merchant or "the booking site"


def _offer_line(offer: Optional[Dict], fare: float) -> Tuple[str, str, str]:
    if not offer:
        return "", "", ""
    site = _site_name(offer)
    saving = estimate_saving(offer, fare) if fare else None
    if saving:
        label = f"about ₹{saving:,.0f} off"
    elif offer.get("max_amount"):
        label = f"up to ₹{float(offer['max_amount']):,.0f} off"
    elif offer.get("percent_off"):
        label = f"up to {float(offer['percent_off']):g}% off"
    else:
        label = str(offer.get("title") or "Card offer")
    terms = str(offer.get("terms_url") or offer.get("detail_url") or "")
    return site, label, terms


def _best_offer(offers: List[Dict], flight: Dict) -> Optional[Dict]:
    if not offers:
        return None
    origin = str(flight.get("origin_code") or "")
    dest = str(flight.get("destination_code") or "")
    try:
        fare = float(flight.get("price") or 0)
    except (TypeError, ValueError):
        fare = 0.0
    rows = offers_for_fare(
        offers,
        str(flight.get("airline") or ""),
        fare=fare,
        segment=route_segment(origin, dest) if origin and dest else None,
    )
    return rows[0] if rows else None


def _departure(raw: str) -> str:
    text = (raw or "").strip()
    today = date.today()
    full = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text)
    if full:
        picked = date(int(full.group(1)), int(full.group(2)), int(full.group(3)))
        if picked < today:
            raise ValueError("That departure date has already passed.")
        return picked.isoformat()
    month = re.fullmatch(r"(\d{4})-(\d{2})", text)
    if not month:
        raise ValueError("Choose a departure month.")
    year, mon = int(month.group(1)), int(month.group(2))
    try:
        fifteenth = date(year, mon, 15)
    except ValueError as exc:
        raise ValueError("Choose a departure month.") from exc
    if fifteenth >= today:
        return fifteenth.isoformat()
    if year == today.year and mon == today.month:
        nxt = today + timedelta(days=1)
        return (nxt if nxt.month == mon else today).isoformat()
    raise ValueError("That month has already passed.")


def _rate(value) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return None
    return rate if rate > 0 else None


def _wallet() -> List[Dict]:
    """The fare screen lists the same cards as card_point_rates."""
    rows = []
    for row in load_card_point_rates():
        rows.append({
            "id": row.get("slug") or "",
            "name": row.get("card_name") or "",
            "bank": row.get("bank") or "",
            "currency": row.get("points_name") or "",
            "inr_per_point": _rate(row.get("inr_per_point")),
            "source_url": row.get("source_url") or "",
        })
    if rows:
        rows.sort(key=lambda card: (
            0 if card["bank"] == "Axis Bank" else 1,
            card["bank"].lower(),
            card["inr_per_point"] is None,
            card["name"].lower(),
        ))
        return rows
    scraped = axis_cards()
    rest = [card for card in wallet_cards() if "axis" not in str(card.get("bank") or "").lower()]
    return scraped + rest


def _money(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _points_on_fare(price: float, saving: float, transfer: Optional[Dict], points: float) -> Tuple[float, float, float]:
    rate = 0.0
    if transfer and transfer.get("matches_airline"):
        rate = _money(transfer.get("inr_per_src_pt"))
    room = max(0.0, price - saving)
    if rate <= 0 or points <= 0 or room <= 0:
        return room, 0.0, 0.0
    used = min(points, math.ceil(room / rate))
    value = min(room, used * rate)
    return max(0.0, room - value), used, value


def _flight_title(flight: Dict) -> str:
    number = str(flight.get("flight_number") or "").strip()
    airline = str(flight.get("airline") or "Airline")
    return f"{airline} {number}".strip()


def _cash_strategy(flight: Dict, slots: Dict, offer: Optional[Dict]) -> Dict:
    price = _money(flight.get("price"))
    site, label, terms = _offer_line(offer, price)
    list_price = _money(flight.get("list_price"))
    show_list = bool(flight.get("show_strikethrough")) and list_price > price
    url = build_booking_url_for_flight(flight, slots, str(flight.get("destination_code") or ""))
    return {
        "id": f"cash-{flight.get('flight_number') or flight.get('airline')}",
        "method": "cash",
        "strategyName": _flight_title(flight),
        "description": "Cash fare from Booking.com. A card code does not apply on that site.",
        "cardName": "Booking.com",
        "airlineName": flight.get("airline") or "Airline",
        "pointsUsed": 0,
        "taxesFees": _money(flight.get("tax")),
        "totalOutCost": round(price),
        "cashSaved": 0,
        "valuePerPoint": 0,
        "transferTime": "",
        "bookingUrl": url or flight.get("source_url") or "",
        "listPrice": round(list_price) if show_list else None,
        "offerSite": site,
        "offerLabel": label,
        "offerTerms": terms,
    }


def _transfer_strategy(flight: Dict, slots: Dict, card: Dict, transfer: Dict, points: float, offer: Optional[Dict]) -> Optional[Dict]:
    price = _money(flight.get("price"))
    saving = _money(estimate_saving(offer, price)) if offer else 0.0
    you_pay, used, value = _points_on_fare(price, saving, transfer, points)
    if used <= 0 or value <= 0:
        return None
    url = build_booking_url_for_flight(flight, slots, str(flight.get("destination_code") or ""))
    days = int(transfer.get("days") or 0)
    timing = "Instant" if days <= 0 else f"{days} day" if days == 1 else f"{days} days"
    program = transfer.get("program_name") or "the airline"
    return {
        "id": f"transfer-{card['id']}-{transfer.get('program_id')}-{flight.get('flight_number') or flight.get('airline')}",
        "method": "transfer",
        "strategyName": f"Move points to {program}",
        "description": f"Transfer {int(used):,} {card.get('currency') or 'points'} to {program}, then pay the rest on {_flight_title(flight)}. {timing} transfer.",
        "cardName": card.get("name") or "",
        "airlineName": flight.get("airline") or program,
        "pointsUsed": int(used),
        "taxesFees": round(_money(flight.get("tax"))),
        "totalOutCost": round(you_pay),
        "cashSaved": round(max(0.0, price - you_pay)),
        "valuePerPoint": round(_money(transfer.get("inr_per_src_pt")), 2),
        "transferTime": timing,
        "bookingUrl": url or flight.get("source_url") or "",
        "pointProgram": card.get("currency") or "",
        "offerSite": "",
        "offerLabel": "",
        "offerTerms": "",
    }


def _mark_best(strategies: List[Dict]) -> None:
    cash = next((row for row in strategies if row.get("method") == "cash"), None)
    others = [row for row in strategies if row.get("method") != "cash"]
    if not cash or not others:
        return
    baseline = _money(cash.get("totalOutCost"))
    for row in others:
        row["cashSaved"] = round(max(0.0, baseline - _money(row.get("totalOutCost"))))
    best = min(others, key=lambda row: (_money(row.get("totalOutCost")), -_money(row.get("cashSaved"))))
    if _money(best.get("totalOutCost")) < baseline:
        best["badge"] = "best"


def _card_pick(flight: Dict, offers: List[Dict], cards: List[Dict]) -> Optional[Dict]:
    if cards:
        try:
            suggested = suggest_cards(
                "travel points card",
                cards,
                offers,
                airline=str(flight.get("airline") or ""),
                fare=_money(flight.get("price")),
                segment=route_segment(str(flight.get("origin_code") or ""), str(flight.get("destination_code") or "")),
                limit=1,
            )
        except Exception:
            logger.warning("suggest_cards failed", exc_info=True)
            suggested = {}
        pick = (suggested.get("picks") or [None])[0]
        if pick:
            return {
                "name": pick.get("card_name") or "",
                "bank": pick.get("bank") or "",
                "pointProgram": pick.get("bank") or "points",
                "pitch": pick.get("reason") or "",
                "listingUrl": pick.get("listing_url") or "",
                "termsUrl": pick.get("terms_url") or "",
            }
    ranked = best_card_net(flight, {}, 10000, estimate_saving)
    transfer = ranked.get("transfer") or {}
    if not transfer:
        return None
    return {
        "name": transfer.get("card_name") or "",
        "bank": "",
        "pointProgram": transfer.get("program_name") or "points",
        "pitch": f"{transfer.get('program_name') or 'Points'} can come off a {_flight_title(flight)} fare when the transfer matches the airline.",
        "listingUrl": "",
        "termsUrl": "",
    }


def build_plan(body: Dict) -> Tuple[int, Dict]:
    try:
        depart = _departure(str(body.get("depart") or ""))
    except ValueError as exc:
        return 400, {"error": str(exc), "strategies": [], "card": None}

    origin = str(body.get("originCode") or "").strip().upper()
    dest = str(body.get("destinationCode") or "").strip().upper()
    if len(origin) != 3 or len(dest) != 3 or origin == dest:
        return 400, {"error": "Choose a departure city and a different destination.", "strategies": [], "card": None}

    cabin = "business" if str(body.get("cabin") or "").lower() == "business" else "economy"
    mode = str(body.get("mode") or "points")
    if mode not in {"points", "cash", "card"}:
        mode = "points"
    points = _money(body.get("points"))
    card_id = str(body.get("cardId") or "")
    cards = {card["id"]: card for card in _wallet()}
    card = cards.get(card_id)

    slots = {
        "origin": {"city": body.get("originCity"), "airport_code": origin},
        "destination": {"city": body.get("destinationCity"), "airport_code": dest},
        "departure_date": depart,
        "return_date": None,
        "trip_type": "one_way",
        "passengers": {"adults": 1, "children": 0, "infants": 0},
        "cabin_class": cabin,
    }
    flights, err, _details = search_flights_api(slots, max_results=8)
    priced = [flight for flight in flights or [] if _money(flight.get("price")) > 0]
    priced.sort(key=lambda flight: flight.get("price") or 0)
    if not priced:
        return 200, {"error": err or "No fares came back for this date.", "strategies": [], "card": None}

    try:
        offers = load_card_offers()
    except Exception:
        logger.warning("offers unavailable", exc_info=True)
        offers = []
    try:
        catalog = load_credit_cards()
    except Exception:
        logger.warning("card catalogue unavailable", exc_info=True)
        catalog = []

    cheapest = priced[0]
    strategies = [_cash_strategy(cheapest, slots, _best_offer(offers, cheapest))]
    recommendation = None

    if mode == "points" and card and points > 0 and card.get("inr_per_point"):
        label = card.get("currency") or "points"
        rate = _money(card.get("inr_per_point"))
        transfer = {
            "program_id": "face-value",
            "program_name": label,
            "matches_airline": True,
            "inr_per_src_pt": rate,
            "days": 0,
        }
        row = _transfer_strategy(cheapest, slots, card, transfer, points, _best_offer(offers, cheapest))
        if row:
            shown = f"{rate:.2f}".rstrip("0").rstrip(".")
            row["method"] = "points"
            row["strategyName"] = f"{label} on this fare"
            row["description"] = f"{int(row['pointsUsed']):,} {label} × ₹{shown} comes off {_flight_title(cheapest)}."
            row["transferTime"] = ""
            row["termsUrl"] = card.get("source_url") or card.get("url") or ""
            strategies.append(row)
    elif mode == "points" and card and points > 0:
        seen = set()
        for flight in priced:
            airline = str(flight.get("airline") or "")
            if not airline or airline in seen:
                continue
            seen.add(airline)
            transfer = best_transfer(card_id, airline, points)
            if not transfer:
                continue
            row = _transfer_strategy(flight, slots, card, transfer, points, _best_offer(offers, flight))
            if row:
                strategies.append(row)
    elif mode == "card":
        recommendation = _card_pick(cheapest, offers, catalog)
        sample = 80000.0
        winner = best_card_net(cheapest, {}, sample, estimate_saving)
        transfer = winner.get("transfer") or {}
        winner_card = cards.get(transfer.get("card_id") or "")
        if winner_card and transfer.get("matches_airline"):
            row = _transfer_strategy(cheapest, slots, winner_card, transfer, sample, _best_offer(offers, cheapest))
            if row:
                row["description"] = (
                    f"With 80,000 {winner_card.get('currency') or 'points'} on {winner_card.get('name')}, "
                    f"this is the transfer that lowers {_flight_title(cheapest)}."
                )
                strategies.append(row)

    _mark_best(strategies)
    strategies.sort(key=lambda row: (0 if row.get("method") == "cash" else 1, row.get("totalOutCost") or 0))
    return 200, {"strategies": strategies, "card": recommendation}


# --------------------------------------------------------------------------- #
# FastAPI app
# --------------------------------------------------------------------------- #


class PlanBody(BaseModel):
    originCode: str = ""
    originCity: str = ""
    destinationCode: str = ""
    destinationCity: str = ""
    depart: str = ""
    cabin: str = "economy"
    mode: str = "points"
    cardId: str = ""
    points: float = 0


class RefreshBody(BaseModel):
    """Pick batch scrapers. Prefer `tasks`; legacy bools still work."""
    tasks: Optional[List[str]] = None
    routes: bool = False
    watch: bool = False
    skip_points: bool = False


class PriceAlertBody(BaseModel):
    email: str
    origin: str
    destination: str
    departDate: str
    returnDate: str = ""
    cabinClass: str = "economy"
    targetPrice: float
    baselinePrice: Optional[float] = None
    currency: str = "INR"


def _refresh_module():
    import importlib.util

    path = ROOT / "scripts" / "refresh_offers.py"
    spec = importlib.util.spec_from_file_location("refresh_offers_job", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load refresh_offers.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _require_admin(token: Optional[str]) -> None:
    if not ADMIN_API_TOKEN:
        # Local default: open when no token is configured.
        return
    if (token or "").strip() != ADMIN_API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid admin token")


def _set_job(job_id: str, **fields: Any) -> None:
    with _jobs_lock:
        row = _jobs.setdefault(job_id, {"id": job_id})
        row.update(fields)


def _resolve_tasks(body: RefreshBody) -> List[str]:
    mod = _refresh_module()
    allowed = set(getattr(mod, "TASKS", ("offers", "catalog", "points", "routes", "watch")))
    if body.tasks:
        out: List[str] = []
        for name in body.tasks:
            key = (name or "").strip().lower()
            if key not in allowed:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown task {name!r}; choose from: {', '.join(sorted(allowed))}",
                )
            if key not in out:
                out.append(key)
        if not out:
            raise HTTPException(status_code=400, detail="tasks must not be empty")
        return out
    return mod.parse_tasks(
        None, routes=body.routes, watch=body.watch, skip_points=body.skip_points,
    )


def _run_refresh(job_id: str, tasks: List[str]) -> None:
    _set_job(job_id, status="running", started_at=datetime.now(timezone.utc).isoformat(), tasks=tasks)
    try:
        mod = _refresh_module()
        result = mod.run_tasks(tasks, dry_run=False)
        _set_job(
            job_id,
            status="done",
            finished_at=datetime.now(timezone.utc).isoformat(),
            result=result,
        )
    except Exception as exc:
        logger.exception("refresh job %s failed", job_id)
        _set_job(
            job_id,
            status="failed",
            finished_at=datetime.now(timezone.utc).isoformat(),
            error=str(exc),
        )


def create_app() -> FastAPI:
    app = FastAPI(title="GarudaX API", version="1.0.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    def health() -> Dict[str, Any]:
        return {"ok": True, "service": "garudax-api"}

    @app.get("/api/cards")
    def cards() -> Dict[str, Any]:
        out = []
        for card in _wallet():
            rate = _rate(card.get("inr_per_point"))
            out.append({
                "id": card["id"],
                "name": card["name"],
                "bank": card["bank"],
                "currency": card.get("currency") or card.get("points_name") or "",
                "inrPerPoint": rate,
                "programs": 0 if rate else len(transfer_options(card["id"])),
            })
        return {"cards": out}

    @app.post("/api/plan")
    def plan(body: PlanBody):
        status, payload = build_plan(body.model_dump())
        if status >= 400:
            return JSONResponse(status_code=status, content=payload)
        return payload

    @app.get("/api/admin/scrapers")
    def list_scrapers(x_admin_token: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        _require_admin(x_admin_token)
        mod = _refresh_module()
        from src.services.aggregators import AGGREGATORS, BANK_OFFER_HUBS, scrape_targets

        return {
            "tasks": list(getattr(mod, "TASKS", ())),
            "offer_sources": list(getattr(mod.offers_mod, "SCRAPE_SOURCES", {}).keys()),
            "aggregators_book": [{"id": a.id, "name": a.name} for a in AGGREGATORS],
            "aggregators_scrape": [
                {"id": a.id, "name": a.name, "offer_url": a.offer_url}
                for a in scrape_targets()
            ],
            "bank_offer_hubs": [{"id": a.id, "name": a.name} for a in BANK_OFFER_HUBS],
            "catalog_sources": list(getattr(mod.offers_mod, "CARD_CATALOG_SOURCES", {}).keys()),
            "point_rates": ["axis_points", "card_point_rates"],
            "schedule": {
                "offers": "Scrape card offers — Mon/Wed/Fri 01:30 UTC (IST 07:00)",
                "catalog": "Scrape travel cards — Mon/Wed/Fri 02:00 UTC (IST 07:30)",
                "points": "Scrape point rates — Mon/Wed/Fri 03:00 UTC (IST 08:00)",
                "routes_watch": "Refresh route watch — Mon/Wed/Fri 02:45 UTC (IST 08:15)",
                "manual": "POST /api/admin/refresh with {\"tasks\": [\"offers\"]}",
            },
        }

    @app.post("/api/admin/refresh")
    def start_refresh(
        background: BackgroundTasks,
        body: RefreshBody = RefreshBody(),
        x_admin_token: Optional[str] = Header(default=None),
    ) -> Dict[str, Any]:
        _require_admin(x_admin_token)
        tasks = _resolve_tasks(body)
        with _jobs_lock:
            running = next((j for j in _jobs.values() if j.get("status") == "running"), None)
        if running:
            raise HTTPException(status_code=409, detail=f"Job {running['id']} is already running")
        job_id = uuid.uuid4().hex[:12]
        _set_job(
            job_id,
            status="queued",
            created_at=datetime.now(timezone.utc).isoformat(),
            tasks=tasks,
        )
        background.add_task(_run_refresh, job_id, tasks)
        return {"job_id": job_id, "status": "queued", "tasks": tasks}

    @app.get("/api/admin/refresh/{job_id}")
    def refresh_status(
        job_id: str,
        x_admin_token: Optional[str] = Header(default=None),
    ) -> Dict[str, Any]:
        _require_admin(x_admin_token)
        with _jobs_lock:
            job = _jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Unknown job")
        return job

    @app.get("/api/admin/refresh")
    def refresh_list(
        limit: int = Query(default=10, ge=1, le=50),
        x_admin_token: Optional[str] = Header(default=None),
    ) -> Dict[str, Any]:
        _require_admin(x_admin_token)
        with _jobs_lock:
            rows = sorted(
                _jobs.values(),
                key=lambda row: row.get("created_at") or "",
                reverse=True,
            )[:limit]
        return {"jobs": rows}

    @app.post("/api/alerts")
    def create_price_alert(body: PriceAlertBody) -> Dict[str, Any]:
        """Save a trip price alert (email when fare ≤ target)."""
        row = insert_price_alert({
            "email": body.email,
            "origin": body.origin,
            "destination": body.destination,
            "depart_date": body.departDate,
            "return_date": body.returnDate or None,
            "cabin_class": body.cabinClass,
            "target_price": body.targetPrice,
            "baseline_price": body.baselinePrice,
            "currency": body.currency or "INR",
        })
        if not row:
            raise HTTPException(status_code=400, detail="Need a valid email, route, date, and target price")
        return {"ok": True, "alert": row}

    @app.get("/api/alerts")
    def list_price_alerts(
        x_admin_token: Optional[str] = Header(default=None),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> Dict[str, Any]:
        _require_admin(x_admin_token)
        return {"alerts": load_active_price_alerts(limit=limit)}

    @app.get("/api/ops/health")
    def ops_health(
        limit: int = Query(default=80, ge=1, le=500),
        status: Optional[str] = Query(default=None),
        x_admin_token: Optional[str] = Header(default=None),
    ) -> Dict[str, Any]:
        """JSON for the ops health UI — reads system_health_events."""
        _require_admin(x_admin_token)
        latest = load_health_latest()
        events = load_health_events(limit=limit, status=status)
        bad = sum(1 for r in latest if r.get("status") in ("fail", "blocked"))
        return {
            "ok": bad == 0,
            "summary": {
                "sources": len(latest),
                "problems": bad,
                "events": len(events),
            },
            "latest": latest,
            "events": events,
        }

    @app.get("/api/ops/health/events")
    def ops_health_events(
        limit: int = Query(default=100, ge=1, le=500),
        status: Optional[str] = Query(default=None),
        component: Optional[str] = Query(default=None),
        source: Optional[str] = Query(default=None),
        x_admin_token: Optional[str] = Header(default=None),
    ) -> Dict[str, Any]:
        _require_admin(x_admin_token)
        return {
            "events": load_health_events(
                limit=limit, status=status, component=component, source=source,
            ),
        }

    @app.get("/ops/health", response_class=HTMLResponse)
    def ops_health_page(
        token: Optional[str] = Query(default=None),
        x_admin_token: Optional[str] = Header(default=None),
    ) -> str:
        """Simple ops dashboard synced to system_health_events (no Grafana)."""
        _require_admin(x_admin_token or token)
        return _OPS_HEALTH_HTML

    return app


_OPS_HEALTH_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>GarudaX · Ops health</title>
  <style>
    :root { --bg:#F4F3EF; --ink:#161616; --muted:#6E6C66; --line:#E4E2DC; --ok:#1E7A45; --bad:#B42318; --warn:#B54708; }
    * { box-sizing: border-box; }
    body { margin:0; font-family: Manrope, "Avenir Next", system-ui, sans-serif; background:var(--bg); color:var(--ink); }
    header { padding:1.25rem 1.5rem; border-bottom:1px solid var(--line); display:flex; gap:1rem; align-items:baseline; flex-wrap:wrap; }
    header h1 { margin:0; font-size:1.25rem; letter-spacing:-0.02em; }
    header .meta { color:var(--muted); font-size:0.9rem; }
    main { padding:1.25rem 1.5rem 3rem; display:grid; gap:1.25rem; }
    .cards { display:flex; gap:0.75rem; flex-wrap:wrap; }
    .card { background:#fff; border:1px solid var(--line); border-radius:14px; padding:0.85rem 1rem; min-width:8rem; }
    .card b { display:block; font-size:1.4rem; }
    .card span { color:var(--muted); font-size:0.78rem; letter-spacing:0.04em; text-transform:uppercase; }
    table { width:100%; border-collapse:collapse; background:#fff; border:1px solid var(--line); border-radius:14px; overflow:hidden; }
    th, td { text-align:left; padding:0.55rem 0.75rem; border-bottom:1px solid var(--line); font-size:0.9rem; vertical-align:top; }
    th { color:var(--muted); font-weight:650; font-size:0.72rem; letter-spacing:0.06em; text-transform:uppercase; background:#FAFAF8; }
    tr:last-child td { border-bottom:0; }
    .pill { display:inline-block; padding:0.12rem 0.45rem; border-radius:999px; font-size:0.75rem; font-weight:700; }
    .ok { background:#E7F6EC; color:var(--ok); }
    .fail { background:#FEEEED; color:var(--bad); }
    .blocked { background:#FFF1E5; color:var(--warn); }
    .skip { background:#F0F0EC; color:var(--muted); }
    .msg { color:var(--muted); max-width:28rem; }
    button { border:1px solid var(--line); background:#fff; border-radius:999px; padding:0.4rem 0.9rem; font-weight:650; cursor:pointer; }
    button:hover { background:#F3F1EC; }
    .err { color:var(--bad); }
  </style>
</head>
<body>
  <header>
    <h1>GarudaX ops health</h1>
    <div class="meta" id="stamp">Loading…</div>
    <button type="button" id="reload">Refresh</button>
  </header>
  <main>
    <div class="cards" id="summary"></div>
    <section>
      <h2 style="font-size:1rem;margin:0 0 0.5rem">Latest by source</h2>
      <table><thead><tr><th>Component</th><th>Source</th><th>Status</th><th>HTTP</th><th>When</th><th>Message</th></tr></thead>
      <tbody id="latest"></tbody></table>
    </section>
    <section>
      <h2 style="font-size:1rem;margin:0 0 0.5rem">Recent events</h2>
      <table><thead><tr><th>When</th><th>Component</th><th>Source</th><th>Status</th><th>HTTP</th><th>Message</th><th>Run</th></tr></thead>
      <tbody id="events"></tbody></table>
    </section>
  </main>
  <script>
    const params = new URLSearchParams(location.search);
    const token = params.get('token') || '';
    function headers() {
      const h = { 'Accept': 'application/json' };
      if (token) h['X-Admin-Token'] = token;
      return h;
    }
    function pill(status) {
      const s = (status || '').toLowerCase();
      return '<span class="pill ' + s + '">' + (status || '') + '</span>';
    }
    function esc(v) {
      return String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    }
    function when(iso) {
      if (!iso) return '';
      try { return new Date(iso).toLocaleString(); } catch { return iso; }
    }
    async function load() {
      const stamp = document.getElementById('stamp');
      try {
        const res = await fetch('/api/ops/health?limit=100', { headers: headers() });
        if (!res.ok) throw new Error(res.status + ' ' + (await res.text()));
        const data = await res.json();
        const s = data.summary || {};
        document.getElementById('summary').innerHTML =
          '<div class="card"><span>Sources</span><b>' + (s.sources||0) + '</b></div>' +
          '<div class="card"><span>Problems</span><b>' + (s.problems||0) + '</b></div>' +
          '<div class="card"><span>Events</span><b>' + (s.events||0) + '</b></div>';
        document.getElementById('latest').innerHTML = (data.latest||[]).map(r =>
          '<tr><td>'+esc(r.component)+'</td><td>'+esc(r.source)+'</td><td>'+pill(r.status)+'</td><td>'+esc(r.http_status??'')+
          '</td><td>'+esc(when(r.created_at))+'</td><td class="msg">'+esc(r.message)+'</td></tr>'
        ).join('') || '<tr><td colspan="6" class="msg">No rows yet — run a scrape or paste the health SQL in Supabase.</td></tr>';
        document.getElementById('events').innerHTML = (data.events||[]).map(r =>
          '<tr><td>'+esc(when(r.created_at))+'</td><td>'+esc(r.component)+'</td><td>'+esc(r.source)+'</td><td>'+pill(r.status)+
          '</td><td>'+esc(r.http_status??'')+'</td><td class="msg">'+esc(r.message)+'</td><td>'+esc(r.run_id||'')+'</td></tr>'
        ).join('') || '<tr><td colspan="7" class="msg">No events.</td></tr>';
        stamp.textContent = 'Synced ' + new Date().toLocaleTimeString() + (data.ok ? ' · all clear' : ' · issues open');
      } catch (e) {
        stamp.innerHTML = '<span class="err">' + esc(e.message) + '</span>';
      }
    }
    document.getElementById('reload').onclick = load;
    load();
    setInterval(load, 30000);
  </script>
</body>
</html>
"""


app = create_app()


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logger.info("GarudaX API on http://%s:%s", HOST, PORT)
    uvicorn.run("src.plan_api:app", host=HOST, port=PORT, reload=False)


if __name__ == "__main__":
    main()
