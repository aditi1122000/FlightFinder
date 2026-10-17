"""
Points-transfer engine: wallet cards, partner ratios, and net fare after
bank-offer savings and redeemable points.

  you_pay = cash_fare - bank_offer_saving - points_value

cash_fare already includes Booking.com taxes/fees (price, base_fare, tax,
fee, discount, list_price).

Ratios and INR-per-source-point are curated 2026 values (points.casa /
cardadvisor). Static tables only — no network.
"""
from __future__ import annotations

import logging
import math
import re
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

EstimateSaving = Callable[[Dict, float], Optional[float]]


# --------------------------------------------------------------------------- #
# Catalogues
# --------------------------------------------------------------------------- #

_PROGRAMS: Dict[str, Dict] = {
    "indigo-bluchip": {
        "program_id": "indigo-bluchip",
        "program_name": "IndiGo BluChip",
        "kind": "airline",
        "airlines": ["IndiGo"],
    },
    "krisflyer": {
        "program_id": "krisflyer",
        "program_name": "KrisFlyer",
        "kind": "airline",
        "airlines": ["Singapore Airlines", "Scoot"],
    },
    "qatar-avios": {
        "program_id": "qatar-avios",
        "program_name": "Qatar Avios",
        "kind": "airline",
        "airlines": ["Qatar Airways", "Qatar"],
    },
    "ba-avios": {
        "program_id": "ba-avios",
        "program_name": "BA Avios",
        "kind": "airline",
        "airlines": ["British Airways"],
    },
    "asia-miles": {
        "program_id": "asia-miles",
        "program_name": "Asia Miles",
        "kind": "airline",
        "airlines": ["Cathay Pacific", "Cathay"],
    },
    "virgin": {
        "program_id": "virgin",
        "program_name": "Virgin Atlantic Flying Club",
        "kind": "airline",
        "airlines": ["Virgin Atlantic"],
    },
    "etihad": {
        "program_id": "etihad",
        "program_name": "Etihad Guest",
        "kind": "airline",
        "airlines": ["Etihad Airways", "Etihad"],
    },
    "maharaja": {
        "program_id": "maharaja",
        "program_name": "Maharaja Club",
        "kind": "airline",
        "airlines": ["Air India"],
    },
    "spiceclub": {
        "program_id": "spiceclub",
        "program_name": "SpiceClub",
        "kind": "airline",
        "airlines": ["SpiceJet"],
    },
    "airasia": {
        "program_id": "airasia",
        "program_name": "AirAsia",
        "kind": "airline",
        "airlines": ["AirAsia"],
    },
    "emirates": {
        "program_id": "emirates",
        "program_name": "Emirates Skywards",
        "kind": "airline",
        "airlines": ["Emirates"],
    },
    "flying-blue": {
        "program_id": "flying-blue",
        "program_name": "Flying Blue",
        "kind": "airline",
        "airlines": ["Air France", "KLM"],
    },
    "aeroplan": {
        "program_id": "aeroplan",
        "program_name": "Aeroplan",
        "kind": "airline",
        "airlines": ["Air Canada"],
    },
    "jal": {
        "program_id": "jal",
        "program_name": "JAL Mileage Bank",
        "kind": "airline",
        "airlines": ["Japan Airlines", "JAL"],
    },
    "united": {
        "program_id": "united",
        "program_name": "United MileagePlus",
        "kind": "airline",
        "airlines": ["United"],
    },
    "qantas": {
        "program_id": "qantas",
        "program_name": "Qantas Frequent Flyer",
        "kind": "airline",
        "airlines": ["Qantas"],
    },
    "hilton": {
        "program_id": "hilton",
        "program_name": "Hilton Honors",
        "kind": "hotel",
        "airlines": [],
    },
    "marriott": {
        "program_id": "marriott",
        "program_name": "Marriott Bonvoy",
        "kind": "hotel",
        "airlines": [],
    },
    "accor": {
        "program_id": "accor",
        "program_name": "Accor ALL",
        "kind": "hotel",
        "airlines": [],
    },
}

# Longest token first so "air france" wins over a stray "air".
_AIRLINE_TO_PROGRAM: List[Tuple[str, str]] = sorted(
    (
        ("singapore airlines", "krisflyer"),
        ("virgin atlantic", "virgin"),
        ("japan airlines", "jal"),
        ("british airways", "ba-avios"),
        ("qatar airways", "qatar-avios"),
        ("cathay pacific", "asia-miles"),
        ("etihad airways", "etihad"),
        ("air canada", "aeroplan"),
        ("air france", "flying-blue"),
        ("air india", "maharaja"),
        ("air asia", "airasia"),
        ("airasia", "airasia"),
        ("indigo", "indigo-bluchip"),
        ("singapore", "krisflyer"),
        ("scoot", "krisflyer"),
        ("qatar", "qatar-avios"),
        ("cathay", "asia-miles"),
        ("virgin", "virgin"),
        ("etihad", "etihad"),
        ("spicejet", "spiceclub"),
        ("emirates", "emirates"),
        ("aeroplan", "aeroplan"),
        ("qantas", "qantas"),
        ("united", "united"),
        ("klm", "flying-blue"),
        ("jal", "jal"),
        ("hilton", "hilton"),
        ("marriott", "marriott"),
        ("accor", "accor"),
    ),
    key=lambda kv: len(kv[0]),
    reverse=True,
)

# (program_id, from_pts, to_pts, days, inr_per_src_pt)
_Route = Tuple[str, int, int, int, float]


def _r(program_id: str, frm: int, to: int, inr: float, days: int = 1) -> _Route:
    return (program_id, frm, to, days, inr)


# Amex: no Accor, IndiGo, SpiceJet, or Maharaja.
_AMEX_MR: Tuple[_Route, ...] = (
    _r("virgin", 5, 4, 1.02),
    _r("hilton", 2, 3, 0.69),
    _r("krisflyer", 2, 1, 0.63),
    _r("marriott", 1, 1, 0.63),
    _r("asia-miles", 2, 1, 0.62),
    _r("qatar-avios", 2, 1, 0.61),
    _r("ba-avios", 2, 1, 0.59),
)

# Axis Olympus EDGE Miles: 1:4 to most airlines (~₹5.0–5.5); Accor 1:2.
_AXIS_OLYMPUS: Tuple[_Route, ...] = (
    _r("aeroplan", 1, 4, 5.50),
    _r("jal", 1, 4, 5.40),
    _r("krisflyer", 1, 4, 5.25),
    _r("flying-blue", 1, 4, 5.20),
    _r("qantas", 1, 4, 5.20),
    _r("etihad", 1, 4, 5.15),
    _r("united", 1, 4, 5.10),
    _r("emirates", 1, 4, 5.10),
    _r("indigo-bluchip", 1, 4, 5.00),
    _r("maharaja", 1, 4, 5.00),
    _r("airasia", 1, 4, 4.90),
    _r("spiceclub", 1, 4, 4.80),
    _r("accor", 1, 2, 4.34),
    _r("asia-miles", 1, 2, 2.48),
    _r("qatar-avios", 1, 2, 2.44),
    _r("ba-avios", 1, 2, 2.36),
)

# Axis Atlas EDGE Miles: typical 1:2 to airlines; Accor / Avios-family 2:1.
_AXIS_ATLAS: Tuple[_Route, ...] = (
    _r("aeroplan", 1, 2, 2.75),
    _r("jal", 1, 2, 2.70),
    _r("krisflyer", 1, 2, 2.63),
    _r("flying-blue", 1, 2, 2.60),
    _r("qantas", 1, 2, 2.60),
    _r("etihad", 1, 2, 2.58),
    _r("united", 1, 2, 2.55),
    _r("emirates", 1, 2, 2.55),
    _r("indigo-bluchip", 1, 2, 2.50),
    _r("maharaja", 1, 2, 2.50),
    _r("airasia", 1, 2, 2.45),
    _r("spiceclub", 1, 2, 2.40),
    _r("asia-miles", 2, 1, 1.24),
    _r("qatar-avios", 2, 1, 1.22),
    _r("ba-avios", 2, 1, 1.18),
    _r("accor", 2, 1, 1.09),
)

# Axis Horizon EDGE Miles: typical 1:1 to airlines; Accor 2:1.
_AXIS_HORIZON: Tuple[_Route, ...] = (
    _r("aeroplan", 1, 1, 1.38),
    _r("jal", 1, 1, 1.35),
    _r("krisflyer", 1, 1, 1.31),
    _r("flying-blue", 1, 1, 1.30),
    _r("qantas", 1, 1, 1.30),
    _r("etihad", 1, 1, 1.29),
    _r("united", 1, 1, 1.28),
    _r("emirates", 1, 1, 1.28),
    _r("indigo-bluchip", 1, 1, 1.25),
    _r("maharaja", 1, 1, 1.25),
    _r("airasia", 1, 1, 1.23),
    _r("spiceclub", 1, 1, 1.20),
    _r("asia-miles", 2, 1, 1.24),
    _r("qatar-avios", 2, 1, 1.22),
    _r("ba-avios", 2, 1, 1.18),
    _r("accor", 2, 1, 1.09),
)

# Axis Magnus EDGE Reward Points: airlines 5:2; Accor / Avios-family 5:1.
_AXIS_MAGNUS: Tuple[_Route, ...] = (
    _r("aeroplan", 5, 2, 0.55),
    _r("jal", 5, 2, 0.54),
    _r("krisflyer", 5, 2, 0.53),
    _r("flying-blue", 5, 2, 0.52),
    _r("qantas", 5, 2, 0.52),
    _r("etihad", 5, 2, 0.52),
    _r("united", 5, 2, 0.51),
    _r("emirates", 5, 2, 0.51),
    _r("indigo-bluchip", 5, 2, 0.50),
    _r("maharaja", 5, 2, 0.50),
    _r("airasia", 5, 2, 0.49),
    _r("spiceclub", 5, 2, 0.48),
    _r("asia-miles", 5, 1, 0.25),
    _r("qatar-avios", 5, 1, 0.24),
    _r("ba-avios", 5, 1, 0.24),
    _r("accor", 5, 1, 0.43),
)

# HDFC SmartBuy: a few days to post.
_HDFC_INF: Tuple[_Route, ...] = (
    _r("krisflyer", 1, 1, 1.25, 3),
    _r("flying-blue", 1, 1, 1.19, 3),
    _r("etihad", 1, 1, 1.10, 3),
    _r("maharaja", 1, 1, 1.00, 3),
    _r("accor", 2, 1, 1.00, 3),
    _r("spiceclub", 1, 1, 0.90, 3),
)

_HDFC_REGALIA: Tuple[_Route, ...] = (
    _r("krisflyer", 2, 1, 0.63, 3),
    _r("maharaja", 2, 1, 0.50, 3),
    _r("accor", 2, 1, 0.50, 3),
    _r("spiceclub", 2, 1, 0.45, 3),
)

_HSBC_T1: Tuple[_Route, ...] = (
    _r("krisflyer", 1, 1, 1.20),
    _r("flying-blue", 1, 1, 1.15),
    _r("asia-miles", 1, 1, 1.10),
    _r("maharaja", 1, 1, 1.00),
    _r("accor", 1, 1, 2.17),
)

_CARDS: Tuple[Dict, ...] = (
    {
        "id": "amex-mr",
        "name": "Amex Membership Rewards",
        "bank": "American Express",
        "currency": "MR",
        "aliases": (
            "membership rewards",
            "platinum travel",
            "amex platinum",
            "american express",
            "centurion",
            "amex mr",
            "amex",
        ),
        "routes": _AMEX_MR,
    },
    {
        "id": "axis-olympus",
        "name": "Axis Olympus",
        "bank": "Axis Bank",
        "currency": "EDGE Miles",
        "aliases": ("olympus", "axis olympus"),
        "routes": _AXIS_OLYMPUS,
    },
    {
        "id": "axis-atlas",
        "name": "Axis Atlas",
        "bank": "Axis Bank",
        "currency": "EDGE Miles",
        "aliases": ("atlas", "axis atlas"),
        "routes": _AXIS_ATLAS,
    },
    {
        "id": "axis-horizon",
        "name": "Axis Horizon",
        "bank": "Axis Bank",
        "currency": "EDGE Miles",
        "aliases": ("horizon", "axis horizon"),
        "routes": _AXIS_HORIZON,
    },
    {
        "id": "axis-magnus",
        "name": "Axis Magnus",
        "bank": "Axis Bank",
        "currency": "EDGE RP",
        "aliases": ("magnus", "axis magnus"),
        "routes": _AXIS_MAGNUS,
    },
    {
        "id": "hdfc-infinia",
        "name": "HDFC Infinia",
        "bank": "HDFC Bank",
        "currency": "RP",
        "aliases": ("infinia", "hdfc infinia"),
        "routes": _HDFC_INF,
    },
    {
        "id": "hdfc-diners-black",
        "name": "HDFC Diners Black",
        "bank": "HDFC Bank",
        "currency": "RP",
        "aliases": ("diners club black", "diners black", "hdfc diners"),
        "routes": _HDFC_INF,
    },
    {
        "id": "hdfc-regalia-gold",
        "name": "HDFC Regalia Gold",
        "bank": "HDFC Bank",
        "currency": "RP",
        "aliases": ("regalia gold", "hdfc regalia gold"),
        "routes": _HDFC_REGALIA,
    },
    {
        "id": "hsbc-travelone",
        "name": "HSBC TravelOne",
        "bank": "HSBC",
        "currency": "RP",
        "aliases": ("travelone", "travel one", "hsbc travelone"),
        "routes": _HSBC_T1,
    },
)

_CARDS_BY_ID: Dict[str, Dict] = {c["id"]: c for c in _CARDS}

# Longest alias first so "diners club black" wins over a shorter token.
_ALIAS_INDEX: List[Tuple[str, str]] = sorted(
    ((alias, c["id"]) for c in _CARDS for alias in c["aliases"]),
    key=lambda kv: len(kv[0]),
    reverse=True,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _fold(text: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _num(value: object) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _card(card_id: Optional[str]) -> Optional[Dict]:
    if not card_id:
        return None
    found = _CARDS_BY_ID.get(card_id.strip().lower())
    if found is None:
        logger.warning("unknown wallet card_id %s", card_id)
    return found


def _program_copy(program_id: str) -> Dict:
    p = _PROGRAMS[program_id]
    return {
        "program_id": p["program_id"],
        "program_name": p["program_name"],
        "kind": p["kind"],
        "airlines": list(p["airlines"]),
    }


def _option_from_route(route: _Route) -> Dict:
    program_id, frm, to, days, inr = route
    meta = _program_copy(program_id)
    meta.update({
        "from_pts": int(frm),
        "to_pts": int(to),
        "days": int(days),
        "inr_per_src_pt": float(inr),
    })
    return meta


def _same_airline(left: str, right: str) -> bool:
    a, b = _fold(left), _fold(right)
    if not a or not b:
        return False
    return a == b or a in b or b in a


def _route_matches_airline(option: Dict, airline: str) -> bool:
    wanted = match_program_for_airline(airline)
    if wanted and wanted["program_id"] == option["program_id"]:
        return True
    return any(_same_airline(airline, name) for name in option.get("airlines") or [])


def _resolve_saving(estimate_saving: Optional[EstimateSaving], offer: Dict, cash: float) -> float:
    if estimate_saving is None:
        from src.services.offers import estimate_saving as estimate_saving  # local import
    try:
        got = estimate_saving(offer, cash)
    except Exception as exc:
        logger.warning("estimate_saving failed: %s", exc)
        return 0.0
    return float(got or 0)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def wallet_cards() -> List[Dict]:
    """Cards that can transfer points. aliases match credit_cards.card_name / bank."""
    return [
        {
            "id": c["id"],
            "name": c["name"],
            "bank": c["bank"],
            "currency": c["currency"],
            "aliases": list(c["aliases"]),
        }
        for c in _CARDS
    ]


def transfer_options(card_id: str) -> List[Dict]:
    """Partner routes for a wallet card, highest estimated INR/source-point first."""
    card = _card(card_id)
    if not card:
        return []
    options = [_option_from_route(r) for r in card["routes"]]
    options.sort(key=lambda o: o["inr_per_src_pt"], reverse=True)
    return options


def match_program_for_airline(airline: str) -> Optional[Dict]:
    """Map an airline name onto a loyalty program, or None if unknown."""
    low = _fold(airline)
    if not low:
        return None
    padded = f" {low} "
    for token, program_id in _AIRLINE_TO_PROGRAM:
        if f" {token} " in padded:
            return _program_copy(program_id)
    return None


def _transfer_from_pick(card: Dict, pick: Dict, points: float, matches: bool) -> Dict:
    pts = float(points or 0)
    received = math.floor(pts * pick["to_pts"] / pick["from_pts"]) if pick["from_pts"] else 0
    raw_value = pts * pick["inr_per_src_pt"]
    return {
        "card_id": card["id"],
        "card_name": card["name"],
        "program_id": pick["program_id"],
        "program_name": pick["program_name"],
        "from_pts": pick["from_pts"],
        "to_pts": pick["to_pts"],
        "days": pick["days"],
        "points": pts,
        "received": received,
        "value_inr": round(raw_value, 0) if matches else 0,
        "matches_airline": matches,
        "inr_per_src_pt": pick["inr_per_src_pt"],
        "raw_value_inr": raw_value,
    }


def best_transfer(
    card_id: str,
    airline: str,
    points: float,
    program_id: Optional[str] = None,
) -> Optional[Dict]:
    """Best partner route for this card + flight.

    Prefer a route whose airlines list matches the flight. program_id pins
    the route the person chose. If none match, return the highest
    inr_per_src_pt airline route with matches_airline=False. value_inr is 0
    on a mismatch so it is not subtracted from the fare; raw_value_inr still
    holds the alternate transfer's estimate.
    """
    card = _card(card_id)
    if not card:
        return None
    options = transfer_options(card_id)
    if not options:
        return None

    if program_id:
        chosen = next((o for o in options if o["program_id"] == program_id), None)
        if chosen is None:
            meta = _PROGRAMS.get(program_id) or {}
            return {
                "card_id": card["id"],
                "card_name": card["name"],
                "program_id": program_id,
                "program_name": meta.get("program_name") or program_id,
                "from_pts": 0,
                "to_pts": 0,
                "days": 0,
                "points": float(points or 0),
                "received": 0,
                "value_inr": 0,
                "matches_airline": False,
                "inr_per_src_pt": 0,
                "raw_value_inr": 0,
            }
        return _transfer_from_pick(
            card, chosen, points, _route_matches_airline(chosen, airline),
        )

    matched = [o for o in options if _route_matches_airline(o, airline)]
    if matched:
        pick = max(matched, key=lambda o: o["inr_per_src_pt"])
        matches = True
    else:
        airline_routes = [o for o in options if o["kind"] == "airline"]
        if not airline_routes:
            return None
        pick = max(airline_routes, key=lambda o: o["inr_per_src_pt"])
        matches = False
    return _transfer_from_pick(card, pick, points, matches)


def fare_parts(flight: Dict) -> Dict:
    """Split a Booking.com fare. cash is price (taxes/fees already inside)."""
    cash = _num((flight or {}).get("price"))
    base_fare = _num((flight or {}).get("base_fare"))
    tax = _num((flight or {}).get("tax"))
    fee = _num((flight or {}).get("fee"))
    if base_fare == 0 and tax == 0 and fee == 0:
        base_fare = cash
    return {
        "base_fare": base_fare,
        "tax": tax,
        "fee": fee,
        "cash": cash,
        "list_price": _num((flight or {}).get("list_price")),
        "discount": _num((flight or {}).get("discount")),
    }


def net_price(
    flight: Dict,
    offer: Optional[Dict],
    card_id: Optional[str],
    points: float,
    estimate_saving: EstimateSaving,
    program_id: Optional[str] = None,
) -> Dict:
    """Flat receipt: fare parts, offer saving, points value, and you_pay (>= 0)."""
    parts = fare_parts(flight or {})
    cash = parts["cash"]
    offer_saving = _resolve_saving(estimate_saving, offer, cash) if offer else 0.0
    transfer = None
    if card_id and _num(points) > 0:
        transfer = best_transfer(
            card_id, (flight or {}).get("airline") or "", float(points), program_id=program_id,
        )
    points_value = 0.0
    if transfer and transfer["matches_airline"]:
        points_value = float(transfer["value_inr"] or 0)
    you_pay = max(0.0, cash - offer_saving - points_value)
    return {
        **parts,
        "offer_saving": offer_saving,
        "points_value": points_value,
        "transfer": transfer,
        "you_pay": you_pay,
        "offer": offer,
        "card_id": card_id,
    }


def best_card_net(
    flight: Dict,
    offer_by_card: Dict[str, Optional[Dict]],
    points: float,
    estimate_saving: EstimateSaving,
    program_id: Optional[str] = None,
) -> Dict:
    """Lowest you_pay across wallet cards. Used when the row is on All cards."""
    ranked: List[Dict] = []
    for card in _CARDS:
        offer = (offer_by_card or {}).get(card["id"])
        ranked.append(
            net_price(flight, offer, card["id"], points, estimate_saving, program_id=program_id)
        )
    if not ranked:
        return net_price(flight, None, None, 0, estimate_saving)
    ranked.sort(key=lambda n: (
        n["you_pay"],
        0 if (n.get("transfer") or {}).get("matches_airline") else 1,
        -(n.get("points_value") or 0),
        -(n.get("offer_saving") or 0),
    ))
    return ranked[0]


def match_wallet_card(credit_card_row: Dict) -> Optional[str]:
    """Map a credit_cards row (card_name, bank) onto a wallet card id."""
    row = credit_card_row or {}
    blob = _fold(f"{row.get('card_name') or ''} {row.get('bank') or ''}")
    if not blob:
        return None
    padded = f" {blob} "
    for alias, card_id in _ALIAS_INDEX:
        if f" {_fold(alias)} " in padded:
            return card_id
    return None
