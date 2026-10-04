"""
Persist conversation messages to Supabase (optional).
Set SUPABASE_URL and one of the keys below in .env to enable.

Preferred (elevated, server-only): SUPABASE_SECRET_KEY or SUPABASE_SERVICE_ROLE_KEY.
Optional (low-privilege, respects RLS): SUPABASE_PUBLISHABLE_KEY or SUPABASE_ANON_KEY.
"""
import os
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_SUPABASE_CLIENT = None
_SUPABASE_DISABLED = None


def _get_client():
    global _SUPABASE_CLIENT, _SUPABASE_DISABLED
    if _SUPABASE_DISABLED is True:
        return None
    if _SUPABASE_CLIENT is not None:
        return _SUPABASE_CLIENT
    url = (os.getenv("SUPABASE_URL") or "").strip()
    # Prefer elevated keys (bypass RLS) for server-side inserts; fall back to publishable/anon
    key = (
        os.getenv("SUPABASE_SECRET_KEY")
        or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        or os.getenv("SUPABASE_PUBLISHABLE_KEY")
        or os.getenv("SUPABASE_ANON_KEY")
        or ""
    ).strip()
    if not url or not key:
        _SUPABASE_DISABLED = True
        logger.debug("Supabase not configured (SUPABASE_URL or key missing). Skipping persistence.")
        return None
    try:
        from supabase import create_client
        _SUPABASE_CLIENT = create_client(url, key)
        return _SUPABASE_CLIENT
    except Exception as e:
        logger.warning("Supabase client init failed: %s. Skipping persistence.", e)
        _SUPABASE_DISABLED = True
        return None


TABLE_NAME = "conversation_messages"


def persist_message(
    conversation_id: str,
    role: str,
    content: str,
    slots: Optional[dict] = None,
    turn_index: Optional[int] = None,
    user_name: Optional[str] = None,
) -> bool:
    """
    Insert one message row into Supabase. No-op if Supabase is not configured.
    turn_index pairs user and assistant messages for the same turn (same value for both).
    user_name tracks which user sent the message (optional).
    Returns True if persisted, False if skipped or failed.
    """
    client = _get_client()
    if client is None:
        return False
    try:
        row = {
            "conversation_id": conversation_id,
            "role": role,
            "content": content,
        }
        if turn_index is not None:
            row["turn_index"] = turn_index
        if user_name is not None and (user_name or "").strip():
            row["user_name"] = (user_name or "").strip()
        if slots is not None:
            row["slots"] = slots
        client.table(TABLE_NAME).insert(row).execute()
        logger.debug("Persisted message to Supabase: conversation_id=%s role=%s turn_index=%s user_name=%s", conversation_id, role, turn_index, user_name)
        return True
    except Exception as e:
        logger.warning("Supabase persist_message failed: %s", e)
        return False


SELECTION_TABLE = "flight_selections"


def selection_row(conversation_id: str, flight: dict, passengers: dict, booking_airport: str, deep_link: str) -> dict:
    """What we keep about a chosen flight. Counts only: traveller names never leave the session."""
    return {
        "conversation_id": conversation_id,
        "provider": flight.get("provider") or "booking_com",
        "airline": flight.get("airline"),
        "flight_number": flight.get("flight_number"),
        "origin_code": flight.get("origin_code"),
        "destination_code": flight.get("destination_code"),
        "booking_destination_code": booking_airport or flight.get("destination_code"),
        "departure_date": flight.get("departure_date") or None,
        "departure_at": flight.get("departure_at") or None,
        "arrival_at": flight.get("arrival_at") or None,
        "cabin_class": flight.get("cabin_class"),
        "fare_name": flight.get("fare_name") or None,
        "price": flight.get("price"),
        "list_price": flight.get("list_price") or None,
        "discount": flight.get("discount") or None,
        "currency": "INR",
        "stops": flight.get("stops"),
        "duration_minutes": flight.get("duration_minutes") or None,
        "adults": int(passengers.get("adults") or 1),
        "children": int(passengers.get("children") or 0),
        "infants": int(passengers.get("infants") or 0),
        "offer_token": flight.get("offer_token") or None,
        "deep_link": deep_link,
    }


def persist_flight_selection(row: dict) -> bool:
    client = _get_client()
    if client is None:
        return False
    try:
        client.table(SELECTION_TABLE).insert(row).execute()
        return True
    except Exception as e:
        logger.warning("Supabase persist_flight_selection failed: %s", e)
        return False


# Offers ---------------------------------------------------------------------

FARE_DISCOUNTS_TABLE = "fare_discounts"
CARD_OFFERS_TABLE = "card_offers"
REFRESH_RUNS_TABLE = "offer_refresh_runs"


def upsert_fare_discounts(rows: list) -> int:
    """Discounts already inside fares. Same row twice in a day is one row."""
    client = _get_client()
    if client is None or not rows:
        return 0
    seen, unique = set(), []
    for r in rows:
        r = {**r, "flight_number": r.get("flight_number") or "", "offer_token": r.get("offer_token") or ""}
        key = (r["provider"], r["origin"], r["destination"], r["depart_date"], r["cabin_class"], r["flight_number"], r["offer_token"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(r)
    try:
        client.table(FARE_DISCOUNTS_TABLE).upsert(
            unique,
            on_conflict="provider,origin,destination,depart_date,cabin_class,flight_number,offer_token",
        ).execute()
        return len(unique)
    except Exception as e:
        logger.warning("Supabase upsert_fare_discounts failed: %s", e)
        return 0


def upsert_card_offers(rows: list) -> int:
    client = _get_client()
    if client is None or not rows:
        return 0
    clean = []
    for r in rows:
        r = dict(r)
        for key in ("valid_from", "valid_until"):
            v = r.get(key)
            if hasattr(v, "isoformat"):
                r[key] = v.isoformat()
        r["code"] = r.get("code") or ""
        r.setdefault("site", "unknown")
        r.setdefault("segment", "any")
        r.setdefault("eligible_cards", [])
        r.setdefault("excluded_cards", [])
        r.setdefault("airline", None)
        r.setdefault("active", True)
        clean.append(r)
    # Postgres refuses a batch that hits the same unique key twice; keep the first of each.
    seen, unique = set(), []
    for r in clean:
        key = (r["site"], r["bank"], r["merchant"], r["segment"], r["code"], r["title"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(r)
    try:
        client.table(CARD_OFFERS_TABLE).upsert(
            unique, on_conflict="site,bank,merchant,segment,code,title"
        ).execute()
        return len(unique)
    except Exception as e:
        logger.warning("Supabase upsert_card_offers failed: %s", e)
        return 0


CREDIT_CARDS_TABLE = "credit_cards"


def upsert_credit_cards(rows: list) -> int:
    client = _get_client()
    if client is None or not rows:
        return 0
    try:
        client.table(CREDIT_CARDS_TABLE).upsert(
            [{**r, "active": True} for r in rows], on_conflict="bank,card_name"
        ).execute()
        return len(rows)
    except Exception as e:
        logger.warning("Supabase upsert_credit_cards failed: %s", e)
        return 0


ROUTE_WATCH_TABLE = "route_watch"


def insert_route_watch(rows: list) -> int:
    client = _get_client()
    if client is None or not rows:
        return 0
    try:
        client.table(ROUTE_WATCH_TABLE).insert(rows).execute()
        return len(rows)
    except Exception as e:
        logger.warning("Supabase insert_route_watch failed: %s", e)
        return 0


def load_latest_route_watch() -> list:
    """Most recent row per watched route."""
    client = _get_client()
    if client is None:
        return []
    try:
        res = (
            client.table(ROUTE_WATCH_TABLE)
            .select("*")
            .order("fetched_at", desc=True)
            .limit(60)
            .execute()
        )
        latest = {}
        for r in res.data or []:
            latest.setdefault((r["origin"], r["destination"]), r)
        return list(latest.values())
    except Exception as e:
        logger.warning("Supabase load_latest_route_watch failed: %s", e)
        return []


def load_credit_cards() -> list:
    client = _get_client()
    if client is None:
        return []
    try:
        res = (
            client.table(CREDIT_CARDS_TABLE)
            .select("card_name,bank,co_brand,co_brand_kind,tier,annual_fee,lounge_access,highlights,pitch,listing_url,terms_url,rating,pros,cons")
            .eq("active", True)
            .order("bank")
            .execute()
        )
        return list(res.data or [])
    except Exception as e:
        logger.warning("Supabase load_credit_cards failed: %s", e)
        return []


def load_card_offers() -> list:
    client = _get_client()
    if client is None:
        return []
    try:
        res = client.table(CARD_OFFERS_TABLE).select("*").eq("active", True).execute()
        return list(res.data or [])
    except Exception as e:
        logger.warning("Supabase load_card_offers failed: %s", e)
        return []


def expire_offers() -> None:
    client = _get_client()
    if client is None:
        return
    try:
        client.rpc("expire_card_offers").execute()
    except Exception as e:
        logger.warning("Supabase expire_card_offers failed: %s", e)


def record_refresh_run(fare_rows: int, card_rows: int, ok: list, failed: list, note: str = "",
                       catalog_rows: int = 0) -> None:
    client = _get_client()
    if client is None:
        return
    try:
        from datetime import datetime, timezone
        client.table(REFRESH_RUNS_TABLE).insert({
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "fare_rows": fare_rows,
            "card_rows": card_rows,
            "catalog_rows": catalog_rows,
            "sources_ok": ok,
            "sources_failed": failed,
            "note": note or None,
        }).execute()
    except Exception as e:
        logger.warning("Supabase record_refresh_run failed: %s", e)
