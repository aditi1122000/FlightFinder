"""Sample fares for /staging. No model and no live search."""

from __future__ import annotations

import re
from datetime import date, timedelta

_MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

_CITIES = {
    "delhi": ("Delhi", "DEL"),
    "new delhi": ("Delhi", "DEL"),
    "mumbai": ("Mumbai", "BOM"),
    "bombay": ("Mumbai", "BOM"),
    "bengaluru": ("Bengaluru", "BLR"),
    "bangalore": ("Bengaluru", "BLR"),
    "hyderabad": ("Hyderabad", "HYD"),
    "chennai": ("Chennai", "MAA"),
    "madras": ("Chennai", "MAA"),
    "kolkata": ("Kolkata", "CCU"),
    "calcutta": ("Kolkata", "CCU"),
    "goa": ("Goa", "GOI"),
    "singapore": ("Singapore", "SIN"),
    "dubai": ("Dubai", "DXB"),
    "london": ("London", "LHR"),
    "bangkok": ("Bangkok", "BKK"),
    "paris": ("Paris", "CDG"),
    "new york": ("New York", "JFK"),
}


def _place(raw: str) -> dict:
    text = re.sub(r"\s+", " ", (raw or "").strip(" .,")).strip()
    known = _CITIES.get(text.lower())
    if known:
        city, code = known
        return {"city": city, "airport_code": code}
    city = text.title() if text else "Delhi"
    letters = re.sub(r"[^A-Za-z]", "", city).upper()
    code = (letters[:3] or "XXX")
    return {"city": city, "airport_code": code}


_MONTH_WORD = re.compile(
    r"\b(?:in\s+|on\s+)?(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\b",
    re.IGNORECASE,
)


def _dated(year: int, month: int, day: int) -> date | None:
    try:
        found = date(year, month, day)
    except ValueError:
        return None
    if found < date.today():
        try:
            found = date(year + 1, month, day)
        except ValueError:
            return None
    return found


def _find_date(text: str) -> date | None:
    match = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})", text or "")
    if match:
        day = int(match.group(1))
        month = _MONTHS.get(match.group(2).lower())
        if month and 1 <= day <= 31:
            found = _dated(date.today().year, month, day)
            if found:
                return found
    month_only = _MONTH_WORD.search(text or "")
    if not month_only:
        return None
    month = _MONTHS.get(month_only.group(1).lower())
    if not month:
        return None
    return _dated(date.today().year, month, 15)


def _city_bit(raw: str) -> str:
    text = re.sub(
        r"\b(?:on\s+)?\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9}\b",
        " ",
        raw or "",
        flags=re.IGNORECASE,
    )
    text = _MONTH_WORD.sub(" ", text)
    text = re.sub(r"\b(?:in|on)\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"^(?:fly\s+)?from\s+", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return re.split(r"[,.]", text)[0].strip()


def slots_from_query(text: str) -> dict:
    """Read a route out of plain words. Anything else still gets a sample trip."""
    raw = (text or "").strip()
    parts = re.split(r"\s+to\s+", raw, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) == 2:
        origin = _place(_city_bit(parts[0]))
        dest = _place(_city_bit(parts[1]))
    else:
        origin = _place("Delhi")
        dest = _place(_city_bit(raw) or "Singapore")
    depart = _find_date(raw) or (date.today() + timedelta(days=30))
    return {
        "origin": origin,
        "destination": dest,
        "departure_date": depart.isoformat(),
        "return_date": None,
        "trip_type": "one_way",
        "passengers": {"adults": 1, "children": 0, "infants": 0},
        "cabin_class": "economy",
        "preferences": {"airlines": None},
    }


def flights_for(slots: dict) -> list[dict]:
    """Four fixed fares on the route in the slots. Business is the same trips, priced up."""
    origin = (slots or {}).get("origin") or {}
    dest = (slots or {}).get("destination") or {}
    o_code = origin.get("airport_code") or "DEL"
    d_code = dest.get("airport_code") or "SIN"
    cabin = "business" if "bus" in str((slots or {}).get("cabin_class") or "").lower() else "economy"
    scale = 2.7 if cabin == "business" else 1.0
    rows = [
        ("IndiGo", "6E", "6E 231", "06:10", "14:40", 510, 0, True, 8420, 900),
        ("Air India", "AI", "AI 382", "09:25", "17:55", 550, 0, True, 9780, 0),
        ("Singapore Airlines", "SQ", "SQ 403", "23:15", "07:40", 595, 0, True, 14150, 1200),
        ("IndiGo", "6E", "6E 446", "13:05", "22:30", 505, 1, False, 7650, 0),
    ]
    flights = []
    for airline, carrier, number, depart, arrive, minutes, stops, non_stop, cash, cut in rows:
        price = int(round(cash * scale))
        discount = int(round(cut * scale))
        tax = int(round(price * 0.18))
        fee = int(round(price * 0.06))
        base = max(price - tax - fee, 0)
        flights.append({
            "airline": airline,
            "carrier_code": carrier,
            "flight_number": number,
            "departure_time": depart,
            "arrival_time": arrive,
            "duration_minutes": minutes,
            "stops": stops,
            "non_stop": non_stop,
            "price": float(price),
            "base_fare": float(base),
            "tax": float(tax),
            "fee": float(fee),
            "discount": float(discount),
            "list_price": float(price + discount),
            "show_strikethrough": discount > 0,
            "cabin_class": cabin,
            "origin_code": o_code,
            "destination_code": d_code,
            "departure_date": (slots or {}).get("departure_date"),
            "provider": "sample",
            "source_url": "#",
            "offer_token": f"sample-{number.replace(' ', '')}-{cabin}",
            "badges": [{"text": "Sample fare", "type": "sample"}],
            "fare_name": "Flex" if cabin == "economy" else "Business Flex",
        })
    return flights
