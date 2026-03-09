"""
Shared flight-finder logic: LLM calls, parsing, validation, search, formatting.
No Streamlit imports — safe to use from graph nodes and CLI.
"""
import os
import time
import random
import re
import json
import requests
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from src.config import (
    MISTRAL_API_KEY,
    AIRPORT_API_KEY,
    AIRPORT_API_BASE_URL,
    RAPIDAPI_KEY,
    RAPIDAPI_HOST,
    MODEL_NAME,
    MAX_HISTORY,
    RETRIES,
    BASE_DELAY,
    PROTECTIVE_SLEEP,
    ERROR_MESSAGES,
)

logger = logging.getLogger(__name__)

_client = None


def _to_str(x):
    """Normalize slot value to string; LLM may return list (e.g. multiple airport codes)."""
    if x is None:
        return ""
    if isinstance(x, list):
        x = x[0] if x else ""
    return str(x).strip()


def _slot_codes_list(slot: dict, max_codes: int = 5) -> List[str]:
    """Return list of airport codes from a slot (origin or destination). Supports single code or list from LLM."""
    if not slot or not isinstance(slot, dict):
        return []
    raw = slot.get("airport_code") or slot.get("city")
    if raw is None:
        return []
    if isinstance(raw, list):
        codes = [str(c).strip().upper()[:3] for c in raw if c]
    else:
        codes = [str(raw).strip().upper()[:3]] if str(raw).strip() else []
    seen = set()
    out = []
    for c in codes[:max_codes]:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _get_client():
    global _client
    if _client is None:
        from mistralai import Mistral
        _client = Mistral(api_key=MISTRAL_API_KEY)
    return _client


def clean_json_text(text: str) -> str:
    if not isinstance(text, str):
        return text
    cleaned = text.replace("```json", "").replace("```", "").strip()
    cleaned = cleaned.replace("undefined", "null")
    first = cleaned.find("{")
    last = cleaned.rfind("}")
    if first != -1 and last != -1 and last > first:
        cleaned = cleaned[first : last + 1]
    cleaned = re.sub(r',(\s*[}\]])', r'\1', cleaned)
    return cleaned.strip()


def _find_balanced_json(text: str, start: int = 0) -> Tuple[Optional[int], Optional[int]]:
    i = text.find("{", start)
    if i == -1:
        return None, None
    depth = 0
    in_string = None
    escape = False
    j = i
    while j < len(text):
        c = text[j]
        if escape:
            escape = False
            j += 1
            continue
        if c == "\\" and in_string:
            escape = True
            j += 1
            continue
        if in_string:
            if c == in_string:
                in_string = None
            j += 1
            continue
        if c in ('"', "'"):
            in_string = c
            j += 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i, j + 1
        j += 1
    return None, None


def extract_json_from_response(text: str) -> Optional[Dict]:
    if not (text or isinstance(text, str)):
        return None
    json_match = re.search(r'<json_data>(.*?)</json_data>', text, re.DOTALL | re.IGNORECASE)
    if json_match:
        raw = json_match.group(1).strip()
        cleaned = clean_json_text(raw)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass
    start, end = _find_balanced_json(text)
    if start is not None and end is not None:
        raw = text[start:end]
        cleaned = clean_json_text(raw)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass
    return None


def extract_conversational_message(text: str) -> str:
    if not text:
        return "I understand. Let me help you with that."
    conv_match = re.search(
        r'<conversational_message>(.*?)</conversational_message>',
        text, re.DOTALL | re.IGNORECASE
    )
    if conv_match:
        text = conv_match.group(1).strip()
    else:
        text = re.sub(r'<conversational_message>', '', text, flags=re.IGNORECASE)
        text = re.sub(r'</conversational_message>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'<json_data>.*?</json_data>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', '', text, flags=re.DOTALL)
    text = re.sub(r'\{.*?\}', '', text, flags=re.DOTALL)
    text = re.sub(r'\n\s*\n\s*\n+', '\n\n', text)
    text = re.sub(r'[ \t]+', ' ', text).strip()
    return text if text else "I understand. Let me help you with that."


def call_mistral_with_backoff(payload: Dict, retries: int = RETRIES, base_delay: float = BASE_DELAY):
    client = _get_client()
    last_exc = None
    for attempt in range(retries):
        try:
            resp = client.chat.complete(**payload)
            time.sleep(PROTECTIVE_SLEEP)
            return resp
        except Exception as e:
            last_exc = e
            err_text = str(e).lower()
            if any(k in err_text for k in ("capacity", "rate", "limit")):
                backoff = base_delay * (2 ** attempt)
                time.sleep(backoff + random.uniform(0, backoff * 0.3))
                continue
            raise
    raise Exception(f"Failed after {retries} retries. Last error: {last_exc}")


def format_booking_details(slots: Dict) -> str:
    details = []
    origin = slots.get("origin") or {}
    destination = slots.get("destination") or {}
    if not isinstance(origin, dict):
        origin = {}
    if not isinstance(destination, dict):
        destination = {}
    oc, dc = _to_str(origin.get("airport_code")) or _to_str(origin.get("city")), _to_str(destination.get("airport_code")) or _to_str(destination.get("city"))
    if _to_str(origin.get("city")) or oc:
        details.append(f"**From:** {_to_str(origin.get('city', ''))} ({oc})".strip(" ()"))
    if _to_str(destination.get("city")) or dc:
        details.append(f"**To:** {_to_str(destination.get('city', ''))} ({dc})".strip(" ()"))
    if slots.get("departure_date"):
        details.append(f"**Departure:** {slots.get('departure_date')}")
    if slots.get("return_date"):
        details.append(f"**Return:** {slots.get('return_date')}")
    passengers = slots.get("passengers", {})
    pax_list = []
    if passengers.get("adults", 0) > 0:
        pax_list.append(f"{passengers['adults']} adult(s)")
    if passengers.get("children", 0) > 0:
        pax_list.append(f"{passengers['children']} child(ren)")
    if passengers.get("infants", 0) > 0:
        pax_list.append(f"{passengers['infants']} infant(s)")
    if pax_list:
        details.append(f"**Passengers:** {', '.join(pax_list)}")
    if slots.get("cabin_class"):
        details.append(f"**Class:** {slots.get('cabin_class').replace('_', ' ').title()}")
    if slots.get("trip_type"):
        details.append(f"**Trip Type:** {slots.get('trip_type').replace('_', ' ').title()}")
    if not details:
        return "No booking details yet. Start a conversation to search for flights!"
    return "\n\n".join(details)


def validate_slots(slots: Dict) -> Tuple[bool, Optional[str], Optional[Dict]]:
    errors = []
    error_details = {}
    origin = slots.get("origin") or {}
    destination = slots.get("destination") or {}
    if not isinstance(origin, dict):
        origin = {}
    if not isinstance(destination, dict):
        destination = {}
    if not _to_str(origin.get("airport_code")) and not _to_str(origin.get("city")):
        errors.append("origin")
        error_details["origin"] = "Origin airport or city is required"
    if not _to_str(destination.get("airport_code")) and not _to_str(destination.get("city")):
        errors.append("destination")
        error_details["destination"] = "Destination airport or city is required"
    departure_date = _to_str(slots.get("departure_date"))
    if not departure_date:
        errors.append("departure_date")
        error_details["departure_date"] = "Departure date is required"
    else:
        try:
            date_obj = datetime.strptime(departure_date, "%Y-%m-%d")
            if date_obj < datetime.now().replace(hour=0, minute=0, second=0, microsecond=0):
                errors.append("departure_date")
                error_details["departure_date"] = "Departure date must be in the future"
        except ValueError:
            errors.append("departure_date")
            error_details["departure_date"] = "Invalid date format"
    passengers = slots.get("passengers", {})
    if passengers.get("adults", 0) < 1:
        errors.append("passengers")
        error_details["passengers"] = "At least 1 adult passenger is required"
    if errors:
        return False, ERROR_MESSAGES["missing_info"], error_details
    return True, None, None


def resolve_airport_code(city_name: str) -> List[Dict]:
    city_to_airport_mapping = {
        "kota": [{"city": "Jaipur", "airport_code": "JAI", "distance_km": 250, "reason": "Kota doesn't have an airport. Jaipur (JAI) is the nearest major airport."}],
        "varanasi": [{"city": "Varanasi", "airport_code": "VNS", "distance_km": 0, "reason": "Varanasi has an airport."}],
    }
    city_lower = city_name.lower().strip()
    if city_lower in city_to_airport_mapping:
        return city_to_airport_mapping[city_lower]
    if AIRPORT_API_KEY and AIRPORT_API_BASE_URL:
        try:
            response = requests.get(
                f"{AIRPORT_API_BASE_URL}/airports",
                params={"search": city_name, "access_key": AIRPORT_API_KEY},
                timeout=5,
            )
            if response.status_code == 200:
                data = response.json()
                if data.get("data"):
                    return [{
                        "city": airport.get("city_name", city_name),
                        "airport_code": airport.get("iata_code"),
                        "distance_km": 0,
                        "reason": f"Found airport: {airport.get('airport_name')}",
                    } for airport in data["data"][:3]]
        except Exception as e:
            logger.warning("resolve_airport_code: API failed city=%s error=%s", city_name, e)
    return []


def find_nearby_airports(airport_code: str, radius_km: int = 100) -> List[Dict]:
    if not airport_code:
        return []
    nearby_airports_db = {
        "DEL": [{"airport_code": "JAI", "city": "Jaipur", "distance_km": 280}, {"airport_code": "AGR", "city": "Agra", "distance_km": 200}],
        "BOM": [{"airport_code": "PNQ", "city": "Pune", "distance_km": 150}, {"airport_code": "GOI", "city": "Goa", "distance_km": 400}],
        "HYD": [{"airport_code": "VGA", "city": "Vijayawada", "distance_km": 250}],
        "BLR": [{"airport_code": "MAA", "city": "Chennai", "distance_km": 350}],
    }
    if airport_code in nearby_airports_db:
        return [a for a in nearby_airports_db[airport_code] if a["distance_km"] <= radius_km]
    return []


def generate_flexible_date_range(base_date: str, days_before: int = 3, days_after: int = 3) -> List[str]:
    try:
        base = datetime.strptime(base_date, "%Y-%m-%d")
        dates = []
        for i in range(-days_before, days_after + 1):
            date = base + timedelta(days=i)
            if date >= datetime.now().replace(hour=0, minute=0, second=0, microsecond=0):
                dates.append(date.strftime("%Y-%m-%d"))
        return dates
    except Exception:
        return [base_date]


def format_flight_price(price: Optional[float]) -> str:
    if price is None or price == 0:
        return "Price on request"
    return f"₹{price:,.0f}"


def format_price_range(stats: Optional[Dict]) -> str:
    if not stats or (stats.get("max_price") or 0) == 0:
        return "Price on request"
    return f"₹{stats['min_price']:,.0f} - ₹{stats['max_price']:,.0f} (Avg: ₹{stats['avg_price']:,.0f})"


def calculate_price_stats(flights: List[Dict]) -> Optional[Dict]:
    if not flights:
        return None
    prices = [f.get("price", 0) for f in flights if f.get("price")]
    if not prices:
        return None
    return {
        "min_price": min(prices),
        "max_price": max(prices),
        "avg_price": sum(prices) / len(prices),
    }


def suggest_alternatives(slots: Dict, empty_reason: str = "no_flights") -> Dict:
    suggestions = {
        "nearby_origin_airports": [],
        "nearby_dest_airports": [],
        "flexible_dates": [],
        "suggestion_message": "",
    }
    origin = slots.get("origin") or {}
    destination = slots.get("destination") or {}
    if not isinstance(origin, dict):
        origin = {}
    if not isinstance(destination, dict):
        destination = {}
    origin_code = _to_str(origin.get("airport_code")) or _to_str(origin.get("city"))
    dest_code = _to_str(destination.get("airport_code")) or _to_str(destination.get("city"))
    departure_date = _to_str(slots.get("departure_date"))
    if origin_code:
        suggestions["nearby_origin_airports"] = find_nearby_airports(origin_code)
    if dest_code:
        suggestions["nearby_dest_airports"] = find_nearby_airports(dest_code)
    if departure_date:
        suggestions["flexible_dates"] = generate_flexible_date_range(departure_date)
    msg_parts = ["I couldn't find flights for your exact route. Here are some alternatives:"]
    if suggestions["nearby_origin_airports"]:
        msg_parts.append("• Try nearby origin airports: " + ", ".join([f"{a['city']} ({a['airport_code']})" for a in suggestions["nearby_origin_airports"][:2]]))
    if suggestions["nearby_dest_airports"]:
        msg_parts.append("• Try nearby destination airports: " + ", ".join([f"{a['city']} ({a['airport_code']})" for a in suggestions["nearby_dest_airports"][:2]]))
    if suggestions["flexible_dates"]:
        msg_parts.append(f"• Try flexible dates: {suggestions['flexible_dates'][0]} to {suggestions['flexible_dates'][-1]}")
    suggestions["suggestion_message"] = "\n".join(msg_parts)
    return suggestions


def _parse_iso_time(iso_str: Optional[str]) -> str:
    if not iso_str:
        return "00:00"
    try:
        if "T" in iso_str:
            return iso_str.split("T")[1][:5] if len(iso_str.split("T")[1]) >= 5 else "00:00"
        return "00:00"
    except Exception:
        return "00:00"


def _normalize_rapidapi_flight(item: dict, index: int) -> dict:
    if isinstance(item, dict):
        dep = item.get("departure") or item.get("departureTime") or item.get("departure_time") or {}
        arr = item.get("arrival") or item.get("arrivalTime") or item.get("arrival_time") or {}
        if isinstance(dep, dict):
            dep_str = dep.get("scheduled") or dep.get("time") or dep.get("date") or "00:00"
        else:
            dep_str = str(dep)[:5] if dep else "00:00"
        if isinstance(arr, dict):
            arr_str = arr.get("scheduled") or arr.get("time") or arr.get("date") or "00:00"
        else:
            arr_str = str(arr)[:5] if arr else "00:00"
        if isinstance(dep_str, str) and "T" in dep_str:
            dep_str = _parse_iso_time(dep_str)
        if isinstance(arr_str, str) and "T" in arr_str:
            arr_str = _parse_iso_time(arr_str)
        air = item.get("airline")
        airline_name = (air.get("name") if isinstance(air, dict) else air) or "Unknown"
        carrier_code = None
        if isinstance(air, dict) and air.get("code"):
            carrier_code = air.get("code")
        flight_num = item.get("flight_number") or item.get("flightNumber")
        if flight_num is not None:
            flight_num = str(flight_num).strip()
        flight_number_str = None
        if carrier_code and flight_num:
            flight_number_str = f"{carrier_code} {flight_num}"
        elif flight_num:
            flight_number_str = str(flight_num)
        return {
            "airline": airline_name if isinstance(airline_name, str) else "Unknown",
            "departure_time": dep_str if isinstance(dep_str, str) else "00:00",
            "arrival_time": arr_str if isinstance(arr_str, str) else "00:00",
            "price": float(item.get("price") or item.get("fare") or 0),
            "non_stop": item.get("non_stop", item.get("nonStop", True)),
            "source_url": item.get("booking_url") or item.get("source_url") or item.get("deepLink") or "#",
            "flight_number": flight_number_str,
        }
    return {"airline": "Unknown", "departure_time": "00:00", "arrival_time": "00:00", "price": 0, "non_stop": True, "source_url": "#", "flight_number": None}


def _normalize_booking_flight_offer(offer: dict) -> dict:
    try:
        segments = offer.get("segments") or []
        if not segments:
            return {"airline": "Unknown", "departure_time": "00:00", "arrival_time": "00:00", "price": 0, "non_stop": True, "source_url": "#", "flight_number": None}
        seg = segments[0]
        dep_iso = seg.get("departureTime") or ""
        arr_iso = seg.get("arrivalTime") or ""
        dep_str = _parse_iso_time(dep_iso) if dep_iso else "00:00"
        arr_str = _parse_iso_time(arr_iso) if arr_iso else "00:00"
        airline_name = "Unknown"
        flight_number_str = None
        legs = seg.get("legs") or []
        if legs:
            leg0 = legs[0]
            carriers_data = leg0.get("carriersData") or []
            carrier_code = ""
            if carriers_data and isinstance(carriers_data[0], dict):
                airline_name = carriers_data[0].get("name") or airline_name
                carrier_code = carriers_data[0].get("code") or ""
            flight_info = leg0.get("flightInfo") or {}
            fn = flight_info.get("flightNumber")
            if fn is not None and carrier_code:
                flight_number_str = f"{carrier_code} {fn}"
            elif fn is not None:
                flight_number_str = str(fn)
            # For multi-leg, append other flight numbers (e.g. "AA 293 / AA 3199")
            if len(legs) > 1:
                parts = [flight_number_str] if flight_number_str else []
                for leg in legs[1:]:
                    fi = leg.get("flightInfo") or {}
                    cdata = (leg.get("carriersData") or [{}])[0] if leg.get("carriersData") else {}
                    code = cdata.get("code", "") if isinstance(cdata, dict) else ""
                    fn = fi.get("flightNumber")
                    if code and fn is not None:
                        parts.append(f"{code} {fn}")
                    elif fn is not None:
                        parts.append(str(fn))
                if parts:
                    flight_number_str = " / ".join(parts)
        price = 0
        pb = offer.get("priceBreakdown") or {}
        total_rounded = pb.get("totalRounded") or pb.get("total") or {}
        if isinstance(total_rounded, dict):
            price = float(total_rounded.get("units") or 0) + float(total_rounded.get("nanos") or 0) / 1e9
        elif isinstance(total_rounded, (int, float)):
            price = float(total_rounded)
        non_stop = len(legs) <= 1 and not any(leg.get("flightStops") for leg in legs)
        token = offer.get("token") or ""
        source_url = f"https://www.booking.com/flights?token={token}" if token else "#"
        return {
            "airline": airline_name,
            "departure_time": dep_str,
            "arrival_time": arr_str,
            "price": round(price, 2),
            "non_stop": non_stop,
            "source_url": source_url,
            "flight_number": flight_number_str,
        }
    except Exception:
        return {"airline": "Unknown", "departure_time": "00:00", "arrival_time": "00:00", "price": 0, "non_stop": True, "source_url": "#", "flight_number": None}


def _search_flights_single_route(
    origin_code: str,
    dest_code: str,
    slots: Dict,
    per_route_max: int,
) -> Tuple[List[Dict], Optional[str], Optional[Dict]]:
    """Search flights for one origin–destination pair. Returns (flights with origin_code/destination_code set, error, details)."""
    departure_date = _to_str(slots.get("departure_date"))
    url = f"https://{RAPIDAPI_HOST.rstrip('/')}/api/v1/flights/searchFlights"
    params = {
        "fromId": f"{origin_code}.AIRPORT",
        "toId": f"{dest_code}.AIRPORT",
        "stops": "none",
        "pageNo": "1",
        "adults": str((slots.get("passengers") or {}).get("adults", 1)),
        "children": str((slots.get("passengers") or {}).get("children", 0)),
        "sort": "BEST",
        "cabinClass": (slots.get("cabin_class") or "ECONOMY").upper() or "ECONOMY",
        "currency_code": "INR",
        "departDate": departure_date or "",
    }
    headers = {"x-rapidapi-key": RAPIDAPI_KEY, "x-rapidapi-host": RAPIDAPI_HOST}
    response = requests.get(url, headers=headers, params=params, timeout=30)
    data = response.json() if response.ok else {}
    if response.status_code != 200:
        if response.status_code in (401, 403):
            return [], ERROR_MESSAGES["api_error_4xx"], {"message": data.get("message", "Invalid RapidAPI key")}
        return [], None, None
    inner = data.get("data") or {}
    offers = inner.get("flightOffers") or []
    if offers:
        flights = [_normalize_booking_flight_offer(o) for o in offers[:per_route_max]]
    else:
        raw = data.get("data") or data.get("flights") or data.get("result") or []
        if isinstance(raw, dict):
            raw = raw.get("flights") or raw.get("data") or []
        flights = [_normalize_rapidapi_flight(f if isinstance(f, dict) else {}, i) for i, f in enumerate((raw or [])[:per_route_max])]
    for f in flights:
        f["origin_code"] = origin_code
        f["destination_code"] = dest_code
    return flights, None, None


def search_flights_api(slots: Dict, max_results: int = 10) -> Tuple[List[Dict], Optional[str], Optional[Dict]]:
    """Search across all origin–destination airport pairs from slots (e.g. NYC airports × LA airports)."""
    origin = slots.get("origin") or {}
    destination = slots.get("destination") or {}
    if not isinstance(origin, dict):
        origin = {}
    if not isinstance(destination, dict):
        destination = {}
    origin_codes = _slot_codes_list(origin)
    dest_codes = _slot_codes_list(destination)
    if not origin_codes:
        origin_codes = [(_to_str(origin.get("airport_code")) or _to_str(origin.get("city")) or "???").upper()[:3] or "SRC"]
    if not dest_codes:
        dest_codes = [(_to_str(destination.get("airport_code")) or _to_str(destination.get("city")) or "???").upper()[:3] or "DST"]
    # Cap pairs to avoid too many API calls (e.g. 5×5 = 25)
    max_pairs = 25
    origin_codes = origin_codes[:5]
    dest_codes = dest_codes[:5]
    per_route_max = max(3, (max_results + len(origin_codes) * len(dest_codes) - 1) // (len(origin_codes) * len(dest_codes)))

    if RAPIDAPI_KEY and RAPIDAPI_HOST:
        try:
            all_flights = []
            api_error = None
            api_error_details = None
            for o in origin_codes:
                for d in dest_codes:
                    if o and d:
                        flights, err, details = _search_flights_single_route(o, d, slots, per_route_max)
                        if err:
                            api_error = api_error or err
                            api_error_details = api_error_details or details
                        all_flights.extend(flights)
            if all_flights:
                # Dedupe by (airline, departure_time, price, route), sort by price
                seen = set()
                unique = []
                for f in all_flights:
                    key = (f.get("airline"), f.get("departure_time"), f.get("price"), f.get("origin_code"), f.get("destination_code"))
                    if key not in seen:
                        seen.add(key)
                        unique.append(f)
                unique.sort(key=lambda x: (x.get("price") or 0, x.get("departure_time") or ""))
                result = unique[:max_results]
                logger.info("search_flights_api: multi-airport search %d pairs → %d flights", len(origin_codes) * len(dest_codes), len(result))
                return result, api_error, api_error_details
            if api_error:
                return [], api_error, api_error_details
            # Write last response for debugging if we have one
            _base = os.path.dirname(os.path.abspath(__file__))
            _aviation_path = os.path.join(_base, "aviation_response.json")
            try:
                with open(_aviation_path, "w", encoding="utf-8") as f:
                    json.dump({}, f)
            except Exception:
                pass
        except requests.exceptions.Timeout:
            return [], ERROR_MESSAGES["timeout"], None
        except requests.exceptions.RequestException as e:
            return [], ERROR_MESSAGES["network_error"], {"error": str(e)}
        except Exception:
            logger.exception("search_flights_api: RapidAPI unexpected error")

    return mock_search_flights(slots, max_results), None, None


def mock_search_flights(slots: Dict, max_results: int = 3) -> List[Dict]:
    origin = slots.get("origin") or {}
    destination = slots.get("destination") or {}
    if not isinstance(origin, dict):
        origin = {}
    if not isinstance(destination, dict):
        destination = {}
    origin_codes = _slot_codes_list(origin) or [_to_str(origin.get("airport_code")) or _to_str(origin.get("city")) or "SRC"]
    dest_codes = _slot_codes_list(destination) or [_to_str(destination.get("airport_code")) or _to_str(destination.get("city")) or "DST"]
    origin_codes = origin_codes[:5]
    dest_codes = dest_codes[:5]
    sample_airlines = ["IndiGo", "Vistara", "Akasa Air", "Air India", "SpiceJet"]
    booking_urls = {
        "IndiGo": "https://www.goindigo.in/",
        "Vistara": "https://www.airvistara.com/",
        "Akasa Air": "https://www.akasaair.com/",
        "Air India": "https://www.airindia.in/",
        "SpiceJet": "https://www.spicejet.com/",
    }
    flights = []
    idx = 0
    base_price = 4000
    codes = ["6E", "UK", "QP", "AI", "SG"]
    for o in origin_codes:
        for d in dest_codes:
            if not o or not d:
                continue
            for j in range(max(1, max_results // (len(origin_codes) * len(dest_codes)))):
                if idx >= max_results:
                    break
                airline = sample_airlines[idx % len(sample_airlines)]
                code = codes[idx % len(codes)]
                hour = (6 + idx * 3) % 24
                arr_hour = (hour + 2 + (idx % 2)) % 24
                flights.append({
                    "airline": airline,
                    "departure_time": f"{hour:02d}:00",
                    "arrival_time": f"{arr_hour:02d}:10",
                    "price": base_price + idx * 900,
                    "non_stop": True,
                    "source_url": booking_urls.get(airline, "#"),
                    "flight_number": f"{code} {1000 + idx}",
                    "origin_code": o,
                    "destination_code": d,
                })
                idx += 1
        if idx >= max_results:
            break
    flights.sort(key=lambda x: (x.get("price") or 0, x.get("departure_time") or ""))
    return flights[:max_results]
