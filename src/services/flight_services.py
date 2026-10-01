"""
Shared flight-finder logic: LLM calls, parsing, validation, search, formatting.
No Streamlit imports — safe to use from graph nodes and CLI.
"""
import calendar
import csv
import io
import os
import socket
import time
import random
import re
import json
import threading
import requests
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import urllib3.connection as _u3conn

from src.config import (
    MISTRAL_API_KEY,
    GEMINI_API_KEY,
    AIRPORT_API_KEY,
    AIRPORT_API_BASE_URL,
    RAPIDAPI_KEY,
    RAPIDAPI_HOST,
    MODEL_NAME,
    LLM_PROVIDER,
    GEMINI_MODEL,
    GEMINI_FALLBACK_MODELS,
    MAX_HISTORY,
    RETRIES,
    BASE_DELAY,
    PROTECTIVE_SLEEP,
    RATE_LIMIT_RETRIES,
    RATE_LIMIT_WAIT_SECONDS,
    ERROR_MESSAGES,
    MISSING_SLOT_LABELS,
)


class RateLimitError(Exception):
    """Raised when the LLM returns 429 / rate_limited. Callers must not retry another path."""


class GenerationCancelled(Exception):
    """The user stopped the in-flight model or search request."""


_cancel = threading.Event()
_http_lock = threading.Lock()
_active_session: Optional[requests.Session] = None
_live_socks: List[socket.socket] = []
_track_sockets = False


def _track_connected_socket(cls) -> None:
    """Remember the socket after connect so Stop can shut it down."""
    orig = cls.connect
    if getattr(orig, "_gx_tracked", False):
        return

    def connect(self, *args, **kwargs):
        orig(self, *args, **kwargs)
        sock = getattr(self, "sock", None)
        if sock is not None and _track_sockets:
            with _http_lock:
                _live_socks.append(sock)

    connect._gx_tracked = True  # type: ignore[attr-defined]
    cls.connect = connect


_track_connected_socket(_u3conn.HTTPConnection)
_track_connected_socket(_u3conn.HTTPSConnection)


def _abort_live_sockets() -> None:
    """session.close() does not unblock a request already waiting on a socket."""
    with _http_lock:
        socks = list(_live_socks)
    for sock in socks:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass


def reset_stop() -> None:
    """Clear a previous Stop so the next turn can run."""
    _cancel.clear()


def request_stop() -> None:
    """Abort the in-flight HTTP call. Safe to call from the UI thread."""
    _cancel.set()
    _abort_live_sockets()
    with _http_lock:
        session = _active_session
    if session is not None:
        try:
            session.close()
        except Exception:
            pass


def _raise_if_stopped() -> None:
    if _cancel.is_set():
        raise GenerationCancelled("Stopped")


def _sleep_or_stop(seconds: float) -> None:
    if _cancel.wait(timeout=max(0.0, seconds)):
        raise GenerationCancelled("Stopped")


_inflight = 0


def _http_request(method: str, url: str, **kwargs):
    """requests call that Stop can abort by shutting down the socket.

    Several of these run at once during fan-out, so socket tracking is reference
    counted: the list is only cleared when the last request finishes.
    """
    global _track_sockets, _active_session, _inflight
    _raise_if_stopped()
    session = requests.Session()
    with _http_lock:
        _inflight += 1
        _track_sockets = True
        _active_session = session
    try:
        resp = session.request(method, url, **kwargs)
        _raise_if_stopped()
        return resp
    except GenerationCancelled:
        raise
    except requests.RequestException as e:
        if _cancel.is_set():
            raise GenerationCancelled("Stopped") from e
        raise
    finally:
        with _http_lock:
            _inflight = max(0, _inflight - 1)
            if _active_session is session:
                _active_session = None
            if _inflight == 0:
                _track_sockets = False
                _live_socks.clear()
        try:
            session.close()
        except Exception:
            pass


class _SimpleMessage:
    def __init__(self, content: str):
        self.content = content


class _SimpleChoice:
    def __init__(self, content: str):
        self.message = _SimpleMessage(content)


class _SimpleChatResponse:
    """Mistral-compatible response shape: response.choices[0].message.content"""

    def __init__(self, content: str):
        self.choices = [_SimpleChoice(content)]


logger = logging.getLogger(__name__)

_client = None


def fill_slots_from_last_search(slots: Dict, last_params: Optional[Dict]) -> Dict:
    """When the user doesn't repeat date/route, fill missing slot values from last search (session history)."""
    if not last_params:
        return slots
    out = dict(slots)
    if not out.get("departure_date") and last_params.get("departure_date"):
        out["departure_date"] = last_params["departure_date"]
    if not out.get("return_date") and last_params.get("return_date"):
        out["return_date"] = last_params["return_date"]
    for key in ("origin", "destination"):
        existing = out.get(key) or {}
        last_val = last_params.get(key)
        if last_val and isinstance(existing, dict) and isinstance(last_val, dict):
            merged = dict(existing)
            if not merged.get("city") and last_val.get("city"):
                merged["city"] = last_val["city"]
            if not merged.get("airport_code") and last_val.get("airport_code"):
                merged["airport_code"] = last_val["airport_code"]
            if merged != existing:
                out[key] = merged
    return out


def format_missing_slots(missing_keys: List[str], details: Optional[Dict] = None) -> str:
    """Build a user-facing line listing what's missing (e.g. 'Still needed: • A specific departure date...')."""
    if not missing_keys:
        return ""
    details = details or {}
    bullets = []
    for key in missing_keys:
        label = details.get(key) or MISSING_SLOT_LABELS.get(key, key.replace("_", " ").lower())
        bullets.append(str(label))
    if len(bullets) == 1:
        return f"Still open: {bullets[0]}."
    return "Still open: " + ", ".join(bullets[:-1]) + f" and {bullets[-1]}."


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
    # Prefer live env (Streamlit secrets) over the value captured at import time
    api_key = (os.getenv("MISTRAL_API_KEY") or MISTRAL_API_KEY or "").strip()
    if not api_key:
        raise ValueError(
            "MISTRAL_API_KEY is missing. Set it in Streamlit Secrets or .env."
        )
    if _client is None:
        from mistralai.client import Mistral
        _client = Mistral(api_key=api_key)
        _client._garudax_api_key = api_key  # type: ignore[attr-defined]
    else:
        # Rebuild client if secrets changed after a reboot/hot-reload
        prev = getattr(_client, "_garudax_api_key", None)
        if prev != api_key:
            from mistralai.client import Mistral
            _client = Mistral(api_key=api_key)
            _client._garudax_api_key = api_key  # type: ignore[attr-defined]
    return _client


def _gemini_api_key() -> str:
    return (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or GEMINI_API_KEY or "").strip()


def _messages_to_gemini(messages: List[Dict]) -> Tuple[Optional[str], List[Dict]]:
    """Convert OpenAI/Mistral-style messages to Gemini contents + system instruction."""
    system_parts: List[str] = []
    contents: List[Dict] = []
    for m in messages or []:
        role = (m.get("role") or "user").strip().lower()
        content = m.get("content")
        if content is None:
            continue
        text = content if isinstance(content, str) else str(content)
        if not text.strip():
            continue
        if role == "system":
            system_parts.append(text)
            continue
        gemini_role = "model" if role in ("assistant", "model") else "user"
        # Gemini requires alternating user/model; merge consecutive same-role turns
        if contents and contents[-1]["role"] == gemini_role:
            contents[-1]["parts"][0]["text"] += "\n\n" + text
        else:
            contents.append({"role": gemini_role, "parts": [{"text": text}]})
    system_instruction = "\n\n".join(system_parts).strip() or None
    return system_instruction, contents


def _extract_gemini_text(data: Dict) -> str:
    candidates = data.get("candidates") or []
    if not candidates:
        # Blocked / empty
        feedback = data.get("promptFeedback") or data.get("error") or data
        raise Exception(f"Gemini returned no candidates: {feedback}")
    parts = (((candidates[0] or {}).get("content") or {}).get("parts")) or []
    texts = []
    for p in parts:
        if isinstance(p, dict) and p.get("text"):
            texts.append(p["text"])
    text = "\n".join(texts).strip()
    if not text:
        raise Exception(f"Gemini returned empty text: {candidates[0]}")
    return text


def _call_gemini_once(payload: Dict) -> _SimpleChatResponse:
    api_key = _gemini_api_key()
    if not api_key:
        raise ValueError(
            "GEMINI_API_KEY is missing. Set it in Streamlit Secrets or .env."
        )
    primary = (payload.get("model") or os.getenv("GEMINI_MODEL") or GEMINI_MODEL or MODEL_NAME).strip()
    candidates = [primary]
    for m in GEMINI_FALLBACK_MODELS:
        if m and m not in candidates:
            candidates.append(m)

    system_instruction, contents = _messages_to_gemini(payload.get("messages") or [])
    if not contents:
        raise ValueError("Gemini payload has no user/model messages")

    body: Dict = {
        "contents": contents,
        "generationConfig": {
            "temperature": float(payload.get("temperature", 0.2)),
            # Gemini 3.x thinking models burn tokens on "thoughts"; keep budget 0 for chat.
            "maxOutputTokens": max(int(payload.get("max_tokens") or 1500), 1024),
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    if system_instruction:
        body["systemInstruction"] = {"parts": [{"text": system_instruction}]}

    last_err: Optional[Exception] = None
    for model in candidates:
        _raise_if_stopped()
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        resp = _http_request(
            "POST",
            url,
            headers={
                "Content-Type": "application/json",
                "X-goog-api-key": api_key,
            },
            json=body,
            timeout=60,
        )
        if resp.status_code == 429:
            raise RateLimitError(
                f"Gemini rate limit exceeded ({model}). Wait ~1–2 minutes, then send one message. "
                f"Details: {resp.text}"
            )
        if resp.status_code in (503, 404) or (
            resp.status_code >= 400
            and any(k in (resp.text or "").lower() for k in ("high demand", "unavailable", "no longer available"))
        ):
            last_err = Exception(f"Gemini API error {resp.status_code} ({model}): {resp.text}")
            logger.warning("Gemini model %s failed (%s); trying next fallback", model, resp.status_code)
            continue
        if resp.status_code >= 400:
            raise Exception(f"Gemini API error {resp.status_code} ({model}): {resp.text}")
        data = resp.json()
        logger.info("Gemini response from model=%s", model)
        return _SimpleChatResponse(_extract_gemini_text(data))

    if last_err is not None:
        raise last_err
    raise Exception("Gemini call failed with no model candidates")


def _call_mistral_once(payload: Dict):
    client = _get_client()
    return client.chat.complete(**payload)


def call_mistral_with_backoff(payload: Dict, retries: int = RETRIES, base_delay: float = BASE_DELAY):
    """
    Call the configured LLM (Gemini or Mistral) with limited retries.

    Keeps the historical function name so workflow/app imports stay unchanged.
    Response always exposes: response.choices[0].message.content
    """
    provider = (os.getenv("LLM_PROVIDER") or LLM_PROVIDER or "gemini").strip().lower()
    last_exc = None
    rate_limit_attempts = 0

    for attempt in range(retries):
        try:
            _raise_if_stopped()
            if provider == "gemini":
                resp = _call_gemini_once(payload)
            else:
                resp = _call_mistral_once(payload)
            _sleep_or_stop(PROTECTIVE_SLEEP)
            _raise_if_stopped()
            return resp
        except GenerationCancelled:
            raise
        except RateLimitError:
            raise
        except Exception as e:
            last_exc = e
            if _is_rate_limit_error(e):
                if rate_limit_attempts < RATE_LIMIT_RETRIES:
                    rate_limit_attempts += 1
                    wait = RATE_LIMIT_WAIT_SECONDS + random.uniform(0, 0.5)
                    logger.warning(
                        "%s rate limited (attempt %d/%d); waiting %.1fs",
                        provider,
                        rate_limit_attempts,
                        RATE_LIMIT_RETRIES,
                        wait,
                    )
                    _sleep_or_stop(wait)
                    continue
                logger.warning("%s rate limited — stopping without further retries", provider)
                raise RateLimitError(
                    f"{provider} rate limit exceeded. Wait ~1–2 minutes, then send one message. "
                    f"Details: {e}"
                ) from e

            err_text = str(e).lower()
            if any(
                k in err_text
                for k in ("capacity", "overloaded", "timeout", "unavailable", "high demand", "503")
            ):
                backoff = base_delay * (2 ** attempt)
                _sleep_or_stop(backoff + random.uniform(0, backoff * 0.3))
                continue
            raise

    if last_exc is not None and _is_rate_limit_error(last_exc):
        raise RateLimitError(
            f"{provider} rate limit exceeded after retries. Last error: {last_exc}"
        ) from last_exc
    raise Exception(f"Failed after {retries} retries. Last error: {last_exc}")


def clean_json_text(text: str) -> str:
    if not isinstance(text, str):
        return text
    cleaned = text.replace("```json", "").replace("```", "").strip()
    cleaned = cleaned.replace("undefined", "null")
    first = cleaned.find("{")
    last = cleaned.rfind("}")
    if first != -1 and last != -1 and last > first:
        cleaned = cleaned[first : last + 1]
    # Gemini sometimes splits a key across a newline: "\nstatus"
    cleaned = re.sub(
        r'"\s*([A-Za-z_][A-Za-z0-9_]*)\s*"\s*:',
        r'"\1":',
        cleaned,
    )
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
    # Fallback for truncated response: no </json_data>, take from <json_data> to last }
    open_tag = re.search(r'<json_data>\s*', text, re.IGNORECASE)
    if open_tag:
        tail = text[open_tag.end():]
        first_brace = tail.find("{")
        last_brace = tail.rfind("}")
        if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
            raw = tail[first_brace : last_brace + 1]
            cleaned = clean_json_text(raw)
            try:
                return json.loads(cleaned)
            except json.JSONDecodeError:
                pass
    return None


def extract_misc_from_response(text: str) -> Optional[str]:
    """Extract optional <misc>...</misc> content for special requests (table, summary, etc.). Output as-is."""
    if not (text or isinstance(text, str)):
        return None
    misc_match = re.search(r'<misc>\s*(.*?)</misc>', text, re.DOTALL | re.IGNORECASE)
    if misc_match:
        out = misc_match.group(1).strip()
        return out if out else None
    # Truncated response: no </misc>, take from <misc> to end
    open_tag = re.search(r'<misc>\s*', text, re.IGNORECASE)
    if open_tag:
        out = text[open_tag.end():].strip()
        return out if out else None
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
    text = re.sub(r'<misc>.*?</misc>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<misc>.*', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', '', text, flags=re.DOTALL)
    text = re.sub(r'\{.*?\}', '', text, flags=re.DOTALL)
    text = re.sub(r'\n\s*\n\s*\n+', '\n\n', text)
    text = re.sub(r'[ \t]+', ' ', text).strip()
    return text if text else "I understand. Let me help you with that."


def _is_rate_limit_error(exc: Exception) -> bool:
    err_text = str(exc).lower()
    return any(
        k in err_text
        for k in ("429", "rate_limited", "rate limit", "too many requests")
    )


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
        logger.warning(
            "validate_slots: invalid slots=%s details=%s",
            errors,
            error_details,
        )
        return False, ERROR_MESSAGES["missing_info"], error_details
    _oc = _to_str(origin.get("airport_code")) or _to_str(origin.get("city"))
    _dc = _to_str(destination.get("airport_code")) or _to_str(destination.get("city"))
    logger.debug("validate_slots: ok origin=%s dest=%s date=%s", _oc or "(empty)", _dc or "(empty)", departure_date)
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
            response = _http_request(
                "GET",
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
        except GenerationCancelled:
            raise
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


def flights_to_csv(flights: List[Dict]) -> str:
    """Build a CSV string from flight list for download. Uses full data from last_search_results (no truncation)."""
    if not flights:
        return ""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([
        "#", "Airline", "Flight No.", "Date", "Price (₹)", "Route", "Departure", "Arrival", "Stops", "Booking Link",
    ])
    for i, f in enumerate(flights, 1):
        f = f if isinstance(f, dict) else {}
        price = f.get("price")
        try:
            price_str = f"{float(price):,.0f}" if price is not None else ""
        except (TypeError, ValueError):
            price_str = ""
        route = ""
        if f.get("origin_code") and f.get("destination_code"):
            route = f"{f['origin_code']} → {f['destination_code']}"
        stops = "Non-stop" if f.get("non_stop") else "With stops"
        date_display = format_departure_date_display(f.get("departure_date"))
        writer.writerow([
            i,
            (f.get("airline") or "").replace("\n", " "),
            (f.get("flight_number") or "").replace("\n", " "),
            date_display,
            price_str,
            route,
            (f.get("departure_time") or "").replace("\n", " "),
            (f.get("arrival_time") or "").replace("\n", " "),
            stops,
            f.get("source_url") or "",
        ])
    return out.getvalue()


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
    msg_parts = []
    if suggestions["nearby_origin_airports"]:
        msg_parts.append("leave from " + " or ".join(a["city"] for a in suggestions["nearby_origin_airports"][:2]))
    if suggestions["nearby_dest_airports"]:
        msg_parts.append("land at " + " or ".join(a["city"] for a in suggestions["nearby_dest_airports"][:2]))
    if suggestions["flexible_dates"] and len(suggestions["flexible_dates"]) > 1:
        first = format_departure_date_display(suggestions["flexible_dates"][0])
        last = format_departure_date_display(suggestions["flexible_dates"][-1])
        msg_parts.append(f"shift the day anywhere from {first} to {last}")
    if msg_parts:
        suggestions["suggestion_message"] = "A couple of ways to open it up: " + "; ".join(msg_parts) + "."
    else:
        suggestions["suggestion_message"] = ""
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


def _money(node) -> float:
    """Booking.com money objects are units plus nanos."""
    if isinstance(node, (int, float)):
        return float(node)
    if not isinstance(node, dict):
        return 0.0
    return float(node.get("units") or 0) + float(node.get("nanos") or 0) / 1e9


def _discount_rows(items) -> List[Dict]:
    rows = []
    if not isinstance(items, list):
        return rows
    for item in items:
        if not isinstance(item, dict):
            continue
        label = item.get("text") or item.get("label") or item.get("title") or item.get("name") or item.get("type") or "Discount"
        amount = 0.0
        for key in ("amount", "price", "discount", "value"):
            if key in item:
                amount = _money(item.get(key))
                break
        rows.append({"label": str(label), "amount": round(amount, 2)})
    return rows


def _included_labels(offer: dict) -> List[str]:
    features = (offer.get("brandedFareInfo") or {}).get("features") or []
    labels = []
    for feat in features:
        if not isinstance(feat, dict) or feat.get("availability") != "INCLUDED":
            continue
        label = (feat.get("label") or "").replace("\t", " ").strip()
        if label and label not in labels:
            labels.append(label)
        if len(labels) >= 3:
            break
    return labels


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
        pb = offer.get("priceBreakdown") or {}
        total_rounded = pb.get("totalRounded") or pb.get("total") or {}
        price = _money(total_rounded)
        list_price = _money(pb.get("totalWithoutDiscountRounded") or pb.get("totalWithoutDiscount"))
        badges = []
        for badge in offer.get("badges") or []:
            if isinstance(badge, dict) and badge.get("text"):
                badges.append({"text": str(badge["text"]), "type": str(badge.get("type") or "")})
        non_stop = len(legs) <= 1 and not any(leg.get("flightStops") for leg in legs)
        stops = max(0, len(legs) - 1) + sum(len(leg.get("flightStops") or []) for leg in legs)
        duration_minutes = 0
        if seg.get("totalTime"):
            duration_minutes = int(seg["totalTime"]) // 60
        elif dep_iso and arr_iso:
            try:
                delta = datetime.fromisoformat(arr_iso[:19]) - datetime.fromisoformat(dep_iso[:19])
                duration_minutes = max(0, int(delta.total_seconds() // 60))
            except ValueError:
                pass
        token = offer.get("token") or ""
        fare_name = (offer.get("brandedFareInfo") or {}).get("fareName") or ""
        return {
            "airline": airline_name,
            "departure_time": dep_str,
            "arrival_time": arr_str,
            "price": round(price, 2),
            "base_fare": round(_money(pb.get("baseFare")), 2),
            "tax": round(_money(pb.get("tax")), 2),
            "fee": round(_money(pb.get("fee")), 2),
            "discount": round(_money(pb.get("discount")), 2),
            "list_price": round(list_price, 2),
            "show_strikethrough": bool(pb.get("showPriceStrikethrough")),
            "applied_discounts": _discount_rows(offer.get("appliedDiscounts")),
            "badges": badges,
            "genius_airline": bool(offer.get("hasGeniusAirlineFundedCampaign")),
            "genius_self": bool(offer.get("hasGeniusSelfFundedCampaign")),
            "fare_name": str(fare_name),
            "included": _included_labels(offer),
            "non_stop": non_stop,
            "stops": stops,
            "duration_minutes": duration_minutes,
            "departure_at": dep_iso,
            "arrival_at": arr_iso,
            "provider": "booking_com",
            "source_url": "#",
            "offer_token": token,
            "flight_number": flight_number_str,
        }
    except Exception:
        return {"airline": "Unknown", "departure_time": "00:00", "arrival_time": "00:00", "price": 0, "non_stop": True, "source_url": "#", "flight_number": None}


DEFAULT_CHILD_AGE = 8


def passenger_ages(passengers: Optional[Dict]) -> List[int]:
    """Booking counts children by age, and an age of 0 is an infant on a lap."""
    passengers = passengers if isinstance(passengers, dict) else {}
    children = max(0, int(passengers.get("children") or 0))
    infants = max(0, int(passengers.get("infants") or 0))
    given = [int(a) for a in (passengers.get("children_ages") or []) if str(a).strip().isdigit()]
    ages = (given + [DEFAULT_CHILD_AGE] * children)[:children]
    return ages + [0] * infants


def build_booking_search_url(origin_code: str, dest_code: str, slots: Dict) -> str:
    """Booking.com route search with the details their public web flow accepts."""
    passengers = slots.get("passengers") or {}
    departure = _to_str(slots.get("departure_date")) or ""
    return_date = _to_str(slots.get("return_date")) or ""
    trip_type = "ROUNDTRIP" if return_date else "ONEWAY"
    params = {
        "type": trip_type,
        "cabinClass": (_to_str(slots.get("cabin_class")) or "ECONOMY").upper(),
        "adults": max(1, int(passengers.get("adults") or 1)),
        "sort": "BEST",
        "depart": departure,
        "from": f"{origin_code}.AIRPORT",
        "to": f"{dest_code}.AIRPORT",
        "ca_source": "flights_index_sb",
    }
    ages = passenger_ages(passengers)
    if ages:
        params["childrenAges"] = ",".join(str(a) for a in ages)
    if return_date:
        params["return"] = return_date
    route = f"{origin_code}.AIRPORT-{dest_code}.AIRPORT"
    return f"https://flights.booking.com/flights/{route}/?{urlencode(params)}"


FARE_CACHE_TTL_S = 600
PROVIDER_COOLDOWN_S = 900
_fare_cache: Dict[tuple, Tuple[float, List[Dict]]] = {}
_fare_cache_lock = threading.Lock()
_provider_cooldown: Dict[str, float] = {}


class ProviderQuotaExceeded(Exception):
    """The provider refused for quota; skip it until the cooldown ends."""


def _fare_cache_key(provider: str, origin_code: str, dest_code: str, slots: Dict) -> tuple:
    passengers = slots.get("passengers") or {}
    return (
        provider,
        origin_code,
        dest_code,
        _to_str(slots.get("departure_date")) or "",
        _to_str(slots.get("return_date")) or "",
        (_to_str(slots.get("cabin_class")) or "ECONOMY").upper(),
        max(1, int(passengers.get("adults") or 1)),
        tuple(passenger_ages(passengers)),
    )


def _cache_get(key: tuple) -> Optional[List[Dict]]:
    with _fare_cache_lock:
        hit = _fare_cache.get(key)
        if hit and hit[0] > time.monotonic():
            return [dict(f) for f in hit[1]]
        _fare_cache.pop(key, None)
    return None


def _cache_put(key: tuple, flights: List[Dict]) -> None:
    with _fare_cache_lock:
        if len(_fare_cache) > 2000:
            now = time.monotonic()
            for stale in [k for k, (exp, _) in _fare_cache.items() if exp <= now]:
                _fare_cache.pop(stale, None)
        _fare_cache[key] = (time.monotonic() + FARE_CACHE_TTL_S, [dict(f) for f in flights])


def _booking_com_route(origin_code: str, dest_code: str, slots: Dict, limit: int) -> List[Dict]:
    """booking-com15 on RapidAPI. Raises on errors so the registry can report them."""
    passengers = slots.get("passengers") or {}
    url = f"https://{RAPIDAPI_HOST.rstrip('/')}/api/v1/flights/searchFlights"
    params = {
        "fromId": f"{origin_code}.AIRPORT",
        "toId": f"{dest_code}.AIRPORT",
        "stops": "none",
        "pageNo": "1",
        "adults": str(max(1, int(passengers.get("adults") or 1))),
        "sort": "BEST",
        "cabinClass": (_to_str(slots.get("cabin_class")) or "ECONOMY").upper(),
        "currency_code": "INR",
        "departDate": _to_str(slots.get("departure_date")) or "",
    }
    ages = passenger_ages(passengers)
    if ages:
        params["children"] = ",".join(str(a) for a in ages)
    if slots.get("return_date"):
        params["returnDate"] = _to_str(slots.get("return_date"))
    headers = {"x-rapidapi-key": RAPIDAPI_KEY, "x-rapidapi-host": RAPIDAPI_HOST}
    response = _http_request("GET", url, headers=headers, params=params, timeout=(4, 20))
    if response.status_code == 429:
        raise ProviderQuotaExceeded((response.text or "")[:160])
    if response.status_code in (401, 403):
        raise PermissionError((response.text or "")[:160])
    if response.status_code != 200:
        logger.warning("booking_com: %s for %s-%s", response.status_code, origin_code, dest_code)
        return []
    data = response.json()
    inner = data.get("data") or {}
    offers = inner.get("flightOffers") or []
    if offers:
        return [_normalize_booking_flight_offer(o) for o in offers[:limit]]
    raw = data.get("data") or data.get("flights") or data.get("result") or []
    if isinstance(raw, dict):
        raw = raw.get("flights") or raw.get("data") or []
    return [_normalize_rapidapi_flight(f if isinstance(f, dict) else {}, i) for i, f in enumerate((raw or [])[:limit])]


# One entry per fare source. Each takes (origin, dest, slots, limit) and returns
# normalised offers. Adding a provider means adding a function and a row here.
FARE_PROVIDERS: Dict[str, Dict] = {
    "booking_com": {
        "search": _booking_com_route,
        "enabled": lambda: bool(RAPIDAPI_KEY and RAPIDAPI_HOST),
        "deep_link": build_booking_search_url,
    },
}


def enabled_providers() -> List[str]:
    now = time.monotonic()
    return [
        name for name, spec in FARE_PROVIDERS.items()
        if spec["enabled"]() and _provider_cooldown.get(name, 0) <= now
    ]


def _provider_route(
    provider: str, origin_code: str, dest_code: str, slots: Dict, limit: int
) -> Tuple[List[Dict], Optional[str]]:
    """One provider, one route. Cached; a quota refusal parks the provider."""
    key = _fare_cache_key(provider, origin_code, dest_code, slots)
    cached = _cache_get(key)
    if cached is not None:
        return cached[:limit], None
    spec = FARE_PROVIDERS[provider]
    _raise_if_stopped()
    try:
        # One page costs the same however much we keep, so cache the whole page.
        flights = spec["search"](origin_code, dest_code, slots, max(limit, 20))
    except ProviderQuotaExceeded:
        _provider_cooldown[provider] = time.monotonic() + PROVIDER_COOLDOWN_S
        logger.warning("%s: quota exhausted, pausing for %ds", provider, PROVIDER_COOLDOWN_S)
        return [], "provider_quota"
    except PermissionError:
        return [], "api_error_4xx"
    departure_date = _to_str(slots.get("departure_date"))
    cabin = (_to_str(slots.get("cabin_class")) or "economy").lower()
    link = spec["deep_link"](origin_code, dest_code, slots)
    for f in flights:
        f["origin_code"] = origin_code
        f["destination_code"] = dest_code
        f["departure_date"] = departure_date
        f["cabin_class"] = cabin
        f["provider"] = provider
        f["source_url"] = link
    _cache_put(key, flights)
    return flights[:limit], None


def _search_flights_single_route(
    origin_code: str,
    dest_code: str,
    slots: Dict,
    per_route_max: int,
) -> Tuple[List[Dict], Optional[str], Optional[Dict]]:
    """Every enabled provider for one route, merged. Returns (flights, error, details)."""
    providers = enabled_providers()
    if not providers:
        reason = "provider_quota" if _provider_cooldown else "provider_unconfigured"
        return [], ERROR_MESSAGES[reason], {"reason": reason}
    flights: List[Dict] = []
    errors: List[str] = []
    for name in providers:
        got, err = _provider_route(name, origin_code, dest_code, slots, per_route_max)
        flights.extend(got)
        if err:
            errors.append(err)
    if not flights and errors:
        return [], ERROR_MESSAGES[errors[0]], {"reason": errors[0]}
    return flights, None, None


def _fan_out(tasks: List[Tuple], max_workers: int = 8) -> List[Tuple[Tuple, Tuple]]:
    """Run route searches side by side. Stop still aborts every open socket."""
    if not tasks:
        return []
    if len(tasks) == 1:
        return [(tasks[0], _search_flights_single_route(*tasks[0]))]
    out = []
    with ThreadPoolExecutor(max_workers=min(max_workers, len(tasks))) as pool:
        futures = {pool.submit(_search_flights_single_route, *task): task for task in tasks}
        for future in as_completed(futures):
            task = futures[future]
            try:
                out.append((task, future.result()))
            except GenerationCancelled:
                for other in futures:
                    other.cancel()
                raise
            except Exception:
                logger.warning("fan-out: route %s-%s failed", task[0], task[1], exc_info=True)
                out.append((task, ([], None, None)))
    return out


def format_departure_date_display(date_str: Optional[str]) -> str:
    """Format YYYY-MM-DD as '12 Nov 2026' for display next to flight number."""
    if not date_str or not isinstance(date_str, str):
        return ""
    try:
        dt = datetime.strptime(date_str.strip()[:10], "%Y-%m-%d")
        return dt.strftime("%d %b %Y")
    except ValueError:
        return date_str[:10] if len(date_str) >= 10 else date_str


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
    logger.info(
        "search_flights_api: slots origin=%s dest=%s date=%s",
        origin.get("airport_code") or origin.get("city"),
        destination.get("airport_code") or destination.get("city"),
        slots.get("departure_date"),
    )
    if not origin_codes:
        origin_codes = [(_to_str(origin.get("airport_code")) or _to_str(origin.get("city")) or "???").upper()[:3] or "SRC"]
    if not dest_codes:
        dest_codes = [(_to_str(destination.get("airport_code")) or _to_str(destination.get("city")) or "???").upper()[:3] or "DST"]
    # Cap pairs to avoid too many API calls (e.g. 5×5 = 25)
    max_pairs = 25
    origin_codes = origin_codes[:5]
    dest_codes = dest_codes[:5]
    per_route_max = max(3, (max_results + len(origin_codes) * len(dest_codes) - 1) // (len(origin_codes) * len(dest_codes)))

    tasks = [(o, d, slots, per_route_max) for o in origin_codes for d in dest_codes if o and d and o != d]
    try:
        results = _fan_out(tasks)
    except GenerationCancelled:
        raise
    except requests.exceptions.Timeout:
        logger.warning("search_flights_api: provider timeout")
        return [], ERROR_MESSAGES["timeout"], None
    except requests.exceptions.RequestException as e:
        logger.warning("search_flights_api: provider request error: %s", e)
        return [], ERROR_MESSAGES["network_error"], {"error": str(e)}

    all_flights: List[Dict] = []
    api_error = None
    api_error_details = None
    for _, (flights, err, details) in results:
        all_flights.extend(flights)
        if err and not api_error:
            api_error, api_error_details = err, details
    if not all_flights:
        logger.info("search_flights_api: no live fares (%s)", (api_error_details or {}).get("reason", "empty"))
        return [], api_error, api_error_details

    best: Dict[tuple, Dict] = {}
    for f in all_flights:
        key = (
            f.get("flight_number") or f.get("airline"),
            f.get("departure_date"),
            f.get("departure_time"),
            f.get("origin_code"),
            f.get("destination_code"),
        )
        if key not in best or (f.get("price") or 0) < (best[key].get("price") or 0):
            best[key] = f
    unique = sorted(best.values(), key=lambda x: (x.get("price") or 0, x.get("departure_time") or ""))
    result = unique[:max_results]
    logger.info("search_flights_api: %d routes in parallel → %d fares", len(tasks), len(result))
    return result, None, None


_MONTH_NUMBERS = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

# A country or region in the request, not one airport. Keep this to the
# gateways a partial trip can actually compare.
_COUNTRY_GATEWAYS = {
    "vietnam": [("Hanoi", "HAN"), ("Ho Chi Minh City", "SGN")],
    "thailand": [("Bangkok", "BKK")],
    "japan": [("Tokyo", "HND"), ("Osaka", "KIX")],
    "uae": [("Dubai", "DXB"), ("Abu Dhabi", "AUH")],
    "united arab emirates": [("Dubai", "DXB"), ("Abu Dhabi", "AUH")],
    "singapore": [("Singapore", "SIN")],
    "malaysia": [("Kuala Lumpur", "KUL")],
    "indonesia": [("Jakarta", "CGK"), ("Bali", "DPS")],
    "sri lanka": [("Colombo", "CMB")],
    "nepal": [("Kathmandu", "KTM")],
    "maldives": [("Male", "MLE")],
    "france": [("Paris", "CDG")],
    "italy": [("Rome", "FCO"), ("Milan", "MXP")],
    "spain": [("Madrid", "MAD"), ("Barcelona", "BCN")],
    "uk": [("London", "LHR")],
    "united kingdom": [("London", "LHR")],
    "usa": [("New York", "JFK"), ("Los Angeles", "LAX")],
    "united states": [("New York", "JFK"), ("Los Angeles", "LAX")],
    "australia": [("Sydney", "SYD"), ("Melbourne", "MEL")],
    "south korea": [("Seoul", "ICN")],
    "korea": [("Seoul", "ICN")],
    "china": [("Beijing", "PEK"), ("Shanghai", "PVG")],
    "germany": [("Frankfurt", "FRA")],
    "turkey": [("Istanbul", "IST")],
    "qatar": [("Doha", "DOH")],
}

_CITY_IATA = {
    "delhi": "DEL",
    "new delhi": "DEL",
    "igi": "DEL",
    "mumbai": "BOM",
    "bangalore": "BLR",
    "bengaluru": "BLR",
    "hyderabad": "HYD",
    "chennai": "MAA",
    "kolkata": "CCU",
    "goa": "GOI",
    "kochi": "COK",
    "ahmedabad": "AMD",
    "pune": "PNQ",
    "jaipur": "JAI",
    "hanoi": "HAN",
    "ho chi minh city": "SGN",
    "ho chi minh": "SGN",
    "saigon": "SGN",
    "bangkok": "BKK",
    "dubai": "DXB",
    "abu dhabi": "AUH",
    "singapore": "SIN",
    "london": "LHR",
    "paris": "CDG",
    "tokyo": "HND",
    "osaka": "KIX",
    "bali": "DPS",
    "kuala lumpur": "KUL",
    "jakarta": "CGK",
    "sydney": "SYD",
    "melbourne": "MEL",
    "new york": "JFK",
    "los angeles": "LAX",
    "seoul": "ICN",
    "doha": "DOH",
    "istanbul": "IST",
}

_CODE_ALIAS = {"IGI": "DEL"}

# Cities where "the airport" is ambiguous. Each code maps to all of its siblings.
_MULTI_AIRPORT_CITIES = [
    ("Dubai", [("DXB", "Dubai International"), ("DWC", "Al Maktoum")]),
    ("London", [("LHR", "Heathrow"), ("LGW", "Gatwick"), ("STN", "Stansted"), ("LTN", "Luton"), ("LCY", "City")]),
    ("Bangkok", [("BKK", "Suvarnabhumi"), ("DMK", "Don Mueang")]),
    ("Tokyo", [("HND", "Haneda"), ("NRT", "Narita")]),
    ("Osaka", [("KIX", "Kansai"), ("ITM", "Itami")]),
    ("Paris", [("CDG", "Charles de Gaulle"), ("ORY", "Orly")]),
    ("New York", [("JFK", "John F. Kennedy"), ("EWR", "Newark"), ("LGA", "LaGuardia")]),
    ("Seoul", [("ICN", "Incheon"), ("GMP", "Gimpo")]),
    ("Shanghai", [("PVG", "Pudong"), ("SHA", "Hongqiao")]),
    ("Beijing", [("PEK", "Capital"), ("PKX", "Daxing")]),
    ("Istanbul", [("IST", "Istanbul"), ("SAW", "Sabiha Gökçen")]),
    ("Milan", [("MXP", "Malpensa"), ("LIN", "Linate"), ("BGY", "Bergamo")]),
    ("Rome", [("FCO", "Fiumicino"), ("CIA", "Ciampino")]),
    ("Kuala Lumpur", [("KUL", "KLIA"), ("SZB", "Subang")]),
    ("Jakarta", [("CGK", "Soekarno–Hatta"), ("HLP", "Halim")]),
    ("Moscow", [("SVO", "Sheremetyevo"), ("DME", "Domodedovo"), ("VKO", "Vnukovo")]),
    ("Los Angeles", [("LAX", "Los Angeles Intl"), ("BUR", "Burbank"), ("SNA", "John Wayne")]),
    ("Chicago", [("ORD", "O'Hare"), ("MDW", "Midway")]),
    ("Washington", [("IAD", "Dulles"), ("DCA", "Reagan National"), ("BWI", "Baltimore")]),
    ("Mumbai", [("BOM", "Chhatrapati Shivaji"), ("NMI", "Navi Mumbai")]),
]
_SIBLING_AIRPORTS = {
    code: (city, airports) for city, airports in _MULTI_AIRPORT_CITIES for code, _ in airports
}


def sibling_airports(code: Optional[str]) -> Tuple[str, List[Tuple[str, str]]]:
    """(city, [(code, name), ...]) when this airport shares its city with others."""
    hit = _SIBLING_AIRPORTS.get((code or "").upper())
    return hit if hit else ("", [])


def _iata(code: Optional[str]) -> str:
    raw = (_to_str(code) or "").strip().upper()
    raw = _CODE_ALIAS.get(raw, raw)
    if len(raw) == 3 and raw.isalpha():
        return raw
    return ""


def _city_iata(city: str) -> str:
    key = (city or "").strip().lower()
    if not key:
        return ""
    if key in _CITY_IATA:
        return _CITY_IATA[key]
    for name, code in sorted(_CITY_IATA.items(), key=lambda item: -len(item[0])):
        if name in key:
            return code
    return ""


def _week_dates(today: datetime, this_week: bool) -> List[datetime]:
    """Calendar week, Monday to Sunday. This week keeps only today onward."""
    monday = today - timedelta(days=today.weekday())
    start = monday if this_week else monday + timedelta(days=7)
    days = [start + timedelta(days=offset) for offset in range(7)]
    if this_week:
        days = [day for day in days if day.date() >= today.date()]
    return days


def infer_travel_window(
    user_message: str,
    slots: Optional[Dict] = None,
    today: Optional[datetime] = None,
    hint: str = "",
) -> Optional[Dict]:
    """The window the user named. A week keeps every day; a month stays that month."""
    today = today or datetime.now()
    slots = slots or {}
    text = f"{user_message or ''}\n{hint or ''}".lower()
    this_week = "this week" in text
    next_week = bool(re.search(r"\bnex\w{0,3}\s+week\b", text))
    if this_week or next_week:
        days = _week_dates(today, this_week=this_week and not next_week)
        if not days:
            return None
        first, last = days[0], days[-1]
        if first.month == last.month:
            label = f"{first.day}–{last.day} {first.strftime('%b %Y')}"
        else:
            label = f"{first.strftime('%-d %b')}–{last.strftime('%-d %b %Y')}"
        return {
            "year": first.year,
            "month": first.month,
            "specific_date": None,
            "dates": [day.strftime("%Y-%m-%d") for day in days],
            "label": label,
        }
    date = _to_str(slots.get("departure_date"))
    if date and len(date) >= 10:
        try:
            dt = datetime.strptime(date[:10], "%Y-%m-%d")
            return {"year": dt.year, "month": dt.month, "specific_date": dt.strftime("%Y-%m-%d")}
        except ValueError:
            pass
    # "nexr month" still means next month.
    if re.search(r"\bnex\w{0,3}\s+month\b", text):
        month = today.month + 1
        year = today.year
        if month > 12:
            month = 1
            year += 1
        return {"year": year, "month": month, "specific_date": None}
    if "this month" in text:
        return {"year": today.year, "month": today.month, "specific_date": None}
    for name, num in sorted(_MONTH_NUMBERS.items(), key=lambda item: -len(item[0])):
        if re.search(rf"\b{name}\b", text):
            year = today.year if num >= today.month else today.year + 1
            return {"year": year, "month": num, "specific_date": None}
    if re.search(r"\b(sakura|cherry blossom)\b", text):
        year = today.year if today.month <= 4 else today.year + 1
        return {"year": year, "month": 3, "specific_date": None}
    return None


def _sample_dates_in_month(year: int, month: int, today: datetime, count: int = 6) -> List[str]:
    last = calendar.monthrange(year, month)[1]
    days = []
    for day in range(1, last + 1):
        dt = datetime(year, month, day)
        if dt.date() >= today.date():
            days.append(dt)
    if not days:
        return []
    if len(days) <= count:
        return [d.strftime("%Y-%m-%d") for d in days]
    indexes = [round(i * (len(days) - 1) / (count - 1)) for i in range(count)]
    seen = []
    for idx in indexes:
        stamp = days[idx].strftime("%Y-%m-%d")
        if stamp not in seen:
            seen.append(stamp)
    return seen


_FLEXIBLE_DATE = re.compile(
    r"\b(any|every|each|whatever|whichever)\s+(date|day|weekday)s?\b"
    r"|\b(date|day)\s+(doesn't|does not|dont|do not)\s+matter\b"
    r"|\b(flexible|open)\s+(on\s+)?(dates?|days?)\b"
    r"|\bno\s+preference\s+(on|for)\s+(the\s+)?(date|day)\b",
    re.I,
)


def user_wants_flexible_date(text: str) -> bool:
    return bool(_FLEXIBLE_DATE.search(text or ""))


def _slots_have_places(slots: Dict) -> bool:
    origin = slots.get("origin") if isinstance(slots.get("origin"), dict) else {}
    dest = slots.get("destination") if isinstance(slots.get("destination"), dict) else {}
    return bool(
        _to_str(origin.get("airport_code")) or _to_str(origin.get("city"))
    ) and bool(_to_str(dest.get("airport_code")) or _to_str(dest.get("city")))


def apply_open_window_slot_policy(state: Dict) -> None:
    """Month or week without a fixed day is valid — don't block on departure_date."""
    user_message = state.get("user_message") or ""
    hint = state.get("conversational_message") or ""
    slots = state.get("slots") if isinstance(state.get("slots"), dict) else {}
    window = infer_travel_window(user_message, slots, hint=hint)
    if not window or not _slots_have_places(slots):
        return

    open_month = not window.get("specific_date")
    flexible = user_wants_flexible_date(user_message) or open_month
    if not flexible:
        return

    if user_wants_flexible_date(user_message) or (
        open_month
        and not re.search(
            r"\b(\d{1,2}(?:st|nd|rd|th)?|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
            user_message,
            re.I,
        )
    ):
        slots = {**slots, "departure_date": None}
        state["slots"] = slots

    missing = [m for m in (state.get("missing_slots") or []) if m != "departure_date"]
    state["missing_slots"] = missing

    if missing:
        return

    if state.get("status") in ("clarification_needed", "awaiting_confirmation"):
        state["status"] = "update"

    month_label = window.get("label") or calendar.month_name[window["month"]]
    origin = slots.get("origin") or {}
    dest = slots.get("destination") or {}
    o = _to_str(origin.get("city")) or _to_str(origin.get("airport_code")) or "your origin"
    d = _to_str(dest.get("city")) or _to_str(dest.get("airport_code")) or "your destination"
    if window.get("dates"):
        follow = "I'm checking each day in that week."
    else:
        follow = (
            "I'm checking a few days across the month, not every day. "
            "Name a date, or ask for the cheapest."
        )
    state["conversational_message"] = f"{o} to {d} in {month_label}. {follow}"


def _user_turn_count(history: Optional[List]) -> int:
    return sum(1 for msg in (history or []) if (msg or {}).get("role") == "user")


def _conversation_text(history: Optional[List], extra: str = "") -> str:
    lines = [(msg or {}).get("content") or "" for msg in (history or [])]
    if extra:
        lines.append(extra)
    return "\n".join(lines)


def _primary_place(node: Optional[Dict]) -> Dict:
    """A country becomes its main airport, so Tokyo/Osaka is not a second question."""
    node = node if isinstance(node, dict) else {}
    options = destination_options(node)
    if not options:
        return node
    current = _iata(node.get("airport_code"))
    codes = {code for _, code in options}
    if current in codes:
        return node
    city, code = options[0]
    return {"city": city, "airport_code": code}


def _anchor_date(text: str, slots: Dict, today: Optional[datetime] = None) -> str:
    today = today or datetime.now()
    window = infer_travel_window(text, slots, today)
    if window:
        dates = _dates_for_window_scan(window, today, 1)
        if dates:
            return dates[len(dates) // 2]
    specific = _to_str(slots.get("departure_date"))
    if specific and len(specific) >= 10:
        return specific[:10]
    return (today + timedelta(days=21)).strftime("%Y-%m-%d")


def _loose_date(text: str, today: Optional[datetime] = None) -> str:
    today = today or datetime.now()
    match = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]{3,9})\.?\s+(\d{4})\b", text or "", re.I)
    if not match:
        return ""
    day, month_name, year = match.groups()
    month = _MONTH_NUMBERS.get(month_name.lower()[:3]) or _MONTH_NUMBERS.get(month_name.lower())
    if not month:
        return ""
    try:
        return datetime(int(year), month, int(day)).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def score_offers(flights: List[Dict], text: str = "") -> List[Dict]:
    """0–4 weak, 5–8 ok, 9–10 best. Best is the balance of price and duration."""
    priced = [f for f in flights if (f.get("price") or 0) > 0]
    if not priced:
        return flights
    prices = [f["price"] for f in priced]
    durations = [f.get("duration_minutes") or 24 * 60 for f in priced]
    low_p, high_p = min(prices), max(prices)
    low_d, high_d = min(durations), max(durations)
    low = (text or "").lower()
    if any(word in low for word in ("cheap", "cheapest", "budget")):
        price_weight = 0.8
    elif any(word in low for word in ("fast", "fastest", "quick", "shortest")):
        price_weight = 0.25
    else:
        price_weight = 0.55
    span_p = high_p - low_p or 1
    span_d = high_d - low_d or 1
    ranked = []
    for flight in priced:
        price_norm = (flight["price"] - low_p) / span_p
        duration_norm = ((flight.get("duration_minutes") or 24 * 60) - low_d) / span_d
        penalty = price_weight * price_norm + (1 - price_weight) * duration_norm
        ranked.append((penalty, flight))
    best_pen = min(pen for pen, _ in ranked)
    worst_pen = max(pen for pen, _ in ranked)
    span = worst_pen - best_pen or 1
    for penalty, flight in ranked:
        score = int(round(10 * (worst_pen - penalty) / span))
        score = max(0, min(10, score))
        if score >= 9:
            band = "Best"
        elif score >= 5:
            band = "Ok"
        else:
            band = "Weak"
        flight["fit_score"] = score
        flight["fit_band"] = band
    return flights


def describe_scored_fares(flights: List[Dict], slots: Dict) -> str:
    priced = [f for f in flights if (f.get("price") or 0) > 0]
    when = format_departure_date_display(_to_str(slots.get("departure_date"))) or "that day"
    if not priced:
        return f"Nothing came back for {when}."
    cheapest = min(priced, key=lambda f: f["price"])
    best = max(priced, key=lambda f: (f.get("fit_score") or 0, -f["price"]))
    return (
        f"{len(priced)} fares for {when}, from one search. "
        f"Best balance is {best.get('airline') or 'one'} at {format_flight_price(best.get('price'))} "
        f"({best.get('fit_score')}/10). "
        f"Cheapest is {cheapest.get('airline') or 'one'} at {format_flight_price(cheapest.get('price'))}. "
        "9–10 is the best fit, 5–8 is ok, 0–4 is a weak fit. "
        "Ask for cheapest, fastest, or best and I'll re-sort these. I won't search again."
    )


def apply_two_turn_policy(state: Dict) -> None:
    """One follow-up at most, then a single search. A country uses its main airport."""
    slots = state.get("slots") if isinstance(state.get("slots"), dict) else {}
    slots = {**slots}
    if isinstance(slots.get("destination"), dict):
        slots["destination"] = _primary_place(slots.get("destination"))
    if isinstance(slots.get("origin"), dict):
        origin_options = destination_options(slots.get("origin"))
        origin_code = _iata((slots.get("origin") or {}).get("airport_code"))
        if origin_options and not origin_code:
            city, code = origin_options[0]
            slots["origin"] = {"city": city, "airport_code": code}
    state["slots"] = slots

    turns = _user_turn_count(state.get("chat_history"))
    text = _conversation_text(state.get("chat_history"), state.get("conversational_message") or "")
    has_places = _slots_have_places(slots)
    origin = slots.get("origin") if isinstance(slots.get("origin"), dict) else {}
    has_origin = bool(_to_str(origin.get("airport_code")) or _to_str(origin.get("city")))

    dest = slots.get("destination") or {}
    d = _to_str(dest.get("city")) or _to_str(dest.get("airport_code")) or ""
    has_dest = bool(d)

    if has_places:
        if not _to_str(slots.get("departure_date")):
            slots["departure_date"] = _anchor_date(text, slots)
            state["slots"] = slots
        when = format_departure_date_display(slots.get("departure_date"))
        state["status"] = "ready_for_search"
        state["missing_slots"] = []
        state["conversational_message"] = f"Searching {d} on {when} once."
        return

    if turns >= 2:
        state["status"] = "update"
        state["missing_slots"] = []
        state["conversational_message"] = (
            "I still don't have both cities, so I won't search. "
            "Name the missing city and I'll use what is already saved."
        )
        return

    state["status"] = "clarification_needed"
    if not has_origin and not has_dest:
        state["missing_slots"] = ["origin", "destination"]
        state["conversational_message"] = (
            "Which city are you leaving from, and which city are you flying to? "
            "One reply is enough, then I'll search once."
        )
    elif not has_origin:
        state["missing_slots"] = ["origin"]
        state["conversational_message"] = (
            f"Flying into {d}. Which city are you leaving from? "
            "That's the only thing I need, then I'll search once."
        )
    else:
        state["missing_slots"] = ["destination"]
        state["conversational_message"] = (
            "Which city are you flying to? That's the only thing I need, then I'll search once."
        )


_REFINE_WORDS = re.compile(
    r"\b(ok|okay|yes|yeah|sure|fine|cheapest|cheap|fastest|fast|quickest|best|sort)\b",
    re.I,
)


def _is_refine_message(text: str) -> bool:
    if _loose_date(text):
        return True
    return bool(_REFINE_WORDS.search(text or ""))


def _fill_named_city(text: str, slots: Dict) -> Dict:
    slots = {**slots}
    found = []
    low = (text or "").lower()
    for name, code in sorted(_CITY_IATA.items(), key=lambda item: -len(item[0])):
        if re.search(rf"\b{re.escape(name)}\b", low):
            found.append((name.title() if name != "ho chi minh city" else "Ho Chi Minh City", code))
    if not found:
        return slots
    origin = slots.get("origin") if isinstance(slots.get("origin"), dict) else {}
    dest = slots.get("destination") if isinstance(slots.get("destination"), dict) else {}
    has_origin = bool(_to_str(origin.get("airport_code")) or _to_str(origin.get("city")))
    has_dest = bool(_to_str(dest.get("airport_code")) or _to_str(dest.get("city")))
    city, code = found[0]
    if not has_origin:
        slots["origin"] = {"city": city, "airport_code": code}
    elif not has_dest:
        slots["destination"] = {"city": city, "airport_code": code}
    return slots


def try_local_followup(
    user_message: str,
    history: List,
    slots: Dict,
    saved_flights: Optional[List],
    searched_before: bool,
) -> Optional[Dict]:
    """After the first search, or on the second short reply, skip the model and the fare API."""
    turns = _user_turn_count(history)
    saved = list(saved_flights or [])
    text = user_message or ""

    def finish(message: str, flights: List, next_slots: Dict, sort_mode: str = "") -> Dict:
        thread = list(history or [])
        thread.append({"role": "assistant", "content": message})
        return {
            "status": "update",
            "slots": next_slots,
            "chat_history": thread,
            "conversational_message": message,
            "last_search_results": flights,
            "last_search_params": next_slots,
            "missing_slots": [],
            "fare_sort": sort_mode,
            "window_scan": None,
        }

    if saved and (turns >= 3 or _is_refine_message(text)):
        wanted = _loose_date(text)
        pool = saved
        note = ""
        sort_mode = ""
        if wanted:
            matched = [f for f in saved if (f.get("departure_date") or "")[:10] == wanted]
            saved_day = format_departure_date_display(_to_str((slots or {}).get("departure_date")))
            if matched:
                pool = matched
            else:
                note = f"I only searched {saved_day or 'the saved day'}, so I can't switch to {format_departure_date_display(wanted)} without a new search. "
        low = text.lower()
        if "cheap" in low:
            sort_mode = "Cheapest"
        elif any(word in low for word in ("fast", "quick")):
            sort_mode = "Fastest"
        elif "best" in low or low.strip() in {"ok", "okay", "yes", "yeah", "sure", "fine"}:
            sort_mode = "Best"
        score_offers(pool, _conversation_text(history))
        message = note + describe_scored_fares(pool, {**(slots or {}), "departure_date": (pool[0].get("departure_date") if pool else None) or (slots or {}).get("departure_date")})
        if sort_mode:
            message = f"Sorted by {sort_mode.lower()}. " + message
        return finish(message, pool, slots or {}, sort_mode)

    if turns >= 3 and searched_before:
        return finish(
            "I already used the one search. Ask for cheapest, fastest, or best and I'll re-sort what came back.",
            saved,
            slots or {},
        )

    if turns == 2 and not saved:
        filled = _fill_named_city(text, slots or {})
        filled["destination"] = _primary_place(filled.get("destination"))
        if _slots_have_places(filled):
            filled["departure_date"] = _anchor_date(_conversation_text(history), filled)
            flights, api_error, details = search_flights_api(filled, max_results=30)
            score_offers(flights, _conversation_text(history))
            if api_error and not flights:
                message = api_error
            else:
                message = describe_scored_fares(flights, filled)
            done = finish(message, flights, filled)
            done["status"] = "ready_for_search" if flights else "update"
            done["error_context"] = details
            return done
    return None


def should_explore_window(result: Dict) -> bool:
    """Run the month/week fare strip when we know the route but not a single day."""
    if result.get("last_search_results"):
        return False
    status = result.get("status") or ""
    if status not in ("clarification_needed", "awaiting_confirmation", "update"):
        return False
    slots = result.get("slots") if isinstance(result.get("slots"), dict) else {}
    if not _slots_have_places(slots):
        return False
    missing = set(result.get("missing_slots") or [])
    if missing & {"origin", "destination"}:
        return False
    msg = result.get("user_message") or ""
    hint = result.get("conversational_message") or ""
    window = infer_travel_window(msg, slots, hint=hint)
    if not window:
        return False
    if slots.get("departure_date") and "departure_date" not in missing and not user_wants_flexible_date(msg):
        if window.get("specific_date"):
            return False
    return True


def _dates_for_window_scan(window: Dict, today: datetime, dest_count: int) -> List[str]:
    """A named week keeps every day. A month is a short sample, not all 30 days."""
    if window.get("dates"):
        return list(window["dates"])
    if window.get("specific_date"):
        return [window["specific_date"]]
    # Two cities means two calls per day, so the sample stays smaller.
    count = 3 if dest_count > 1 else 4
    return _sample_dates_in_month(window["year"], window["month"], today, count=count)


def _parse_min_price_payload(data: dict, wanted_dates: set) -> Dict[str, float]:
    """Best-effort parse for getMinPrice-style responses."""
    prices: Dict[str, float] = {}

    def walk(node):
        if isinstance(node, dict):
            day = (
                _to_str(node.get("departureDate"))
                or _to_str(node.get("departDate"))
                or _to_str(node.get("date"))
                or _to_str(node.get("departure_date"))
            )
            if day and len(day) >= 10:
                day = day[:10]
            amount = None
            for key in ("price", "minPrice", "amount", "total"):
                if key in node:
                    amount = _money(node.get(key))
                    break
            if not amount and isinstance(node.get("priceBreakdown"), dict):
                amount = _money((node["priceBreakdown"] or {}).get("totalRounded"))
            if day and amount and day in wanted_dates:
                prices[day] = min(prices.get(day, amount), amount)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return prices


def _booking_min_prices_for_dates(
    origin_code: str, dest_code: str, slots: Dict, dates: List[str]
) -> Dict[str, float]:
    """One getMinPrice call per route anchor when the provider supports it."""
    if not dates or not (RAPIDAPI_KEY and RAPIDAPI_HOST):
        return {}
    wanted = set(dates)
    url = f"https://{RAPIDAPI_HOST.rstrip('/')}/api/v1/flights/getMinPrice"
    passengers = slots.get("passengers") or {}
    params = {
        "fromId": f"{origin_code}.AIRPORT",
        "toId": f"{dest_code}.AIRPORT",
        "departDate": dates[len(dates) // 2],
        "cabinClass": (_to_str(slots.get("cabin_class")) or "ECONOMY").upper(),
        "currency_code": "INR",
        "adults": str(max(1, int(passengers.get("adults") or 1))),
    }
    ages = passenger_ages(passengers)
    if ages:
        params["children"] = ",".join(str(a) for a in ages)
    headers = {"x-rapidapi-key": RAPIDAPI_KEY, "x-rapidapi-host": RAPIDAPI_HOST}
    try:
        response = _http_request("GET", url, headers=headers, params=params, timeout=(4, 12))
    except GenerationCancelled:
        raise
    except Exception:
        return {}
    if response.status_code == 429:
        raise ProviderQuotaExceeded((response.text or "")[:160])
    if response.status_code != 200:
        return {}
    try:
        return _parse_min_price_payload(response.json(), wanted)
    except Exception:
        return {}


def destination_options(destination: Optional[Dict]) -> List[Tuple[str, str]]:
    """Airports worth pricing when the destination is still a country or a city."""
    node = destination if isinstance(destination, dict) else {}
    city = _to_str(node.get("city")) or ""
    key = city.strip().lower()
    if key in _COUNTRY_GATEWAYS:
        return list(_COUNTRY_GATEWAYS[key])
    code = _iata(node.get("airport_code")) or _city_iata(city)
    if code:
        return [(city or code, code)]
    if not city:
        return []
    found = resolve_airport_code(city)
    options = []
    for item in found[:2]:
        item_code = _iata(item.get("airport_code"))
        if item_code:
            options.append((item.get("city") or city, item_code))
    return options


def explore_open_window(
    slots: Dict,
    user_message: str,
    today: Optional[datetime] = None,
    hint: str = "",
    on_progress: Optional[Callable[[Dict], None]] = None,
    max_workers: int = 6,
) -> Optional[Dict]:
    """Price a spread of days in the month the user named, for each possible city.

    A full search still waits for a city and a day. This only fills the open window
    so the board can show that month, not a fake year.
    """
    today = today or datetime.now()
    window = infer_travel_window(user_message, slots, today, hint=hint)
    if not window:
        return None
    origin = slots.get("origin") if isinstance(slots.get("origin"), dict) else {}
    origin_city = _to_str(origin.get("city")) or ""
    origin_code = _iata(origin.get("airport_code")) or _city_iata(origin_city)
    dests = destination_options(slots.get("destination"))[:2]
    if not origin_code or not dests:
        logger.info(
            "explore_open_window: skipped origin=%s dests=%s",
            origin_code or origin_city or "(empty)",
            [code for _, code in dests],
        )
        return None
    dates = _dates_for_window_scan(window, today, len(dests))
    if not dates:
        return None
    month_label = window.get("label") or f"{calendar.month_name[window['month']]} {window['year']}"
    logger.info(
        "explore_open_window: %s from %s to %s across %d dates",
        month_label,
        origin_code,
        ",".join(code for _, code in dests),
        len(dates),
    )

    def empty_point(date: str) -> Dict:
        return {"date": date, "price": None, "flight": None}

    series = [
        {"city": city, "code": code, "points": [empty_point(d) for d in dates]}
        for city, code in dests
    ]

    def build_payload(*, live: bool, partial: bool, notice: str = "") -> Dict:
        priced = sum(1 for item in series for p in item["points"] if (p.get("price") or 0) > 0)
        return {
            "month_label": month_label,
            "year": window["year"],
            "month": window["month"],
            "origin_label": origin_city or origin_code,
            "origin_code": origin_code,
            "specific_date": window.get("specific_date"),
            "dates": dates,
            "series": series,
            "live": live,
            "partial": partial,
            "priced_count": priced,
            "sampled": not window.get("dates") and not window.get("specific_date"),
            "notice": notice,
        }

    def emit(*, live: bool, partial: bool, notice: str = "") -> None:
        if on_progress:
            on_progress(build_payload(live=live, partial=partial, notice=notice))

    emit(live=False, partial=True)

    if not (RAPIDAPI_KEY and RAPIDAPI_HOST):
        return build_payload(live=False, partial=False, notice=ERROR_MESSAGES["provider_unconfigured"])

    notice = ""
    point_index = {(item["code"], p["date"]): (item, idx) for item in series for idx, p in enumerate(item["points"])}

    def set_point(code: str, date: str, price: Optional[float], flight: Optional[Dict]) -> None:
        item, idx = point_index[(code, date)]
        item["points"][idx] = {"date": date, "price": price, "flight": flight}

    # Try calendar min-price first (one call per destination).
    calendar_ok = False
    try:
        for city, code in dests:
            day_prices = _booking_min_prices_for_dates(origin_code, code, slots, dates)
            if not day_prices:
                continue
            calendar_ok = True
            for day, price in day_prices.items():
                set_point(code, day, price, {"price": price, "destination_code": code, "destination_city": city})
            emit(live=True, partial=True)
    except ProviderQuotaExceeded:
        notice = ERROR_MESSAGES["provider_quota"]
        return build_payload(live=False, partial=False, notice=notice)

    if calendar_ok and all((p.get("price") or 0) > 0 for item in series for p in item["points"]):
        return build_payload(live=True, partial=False, notice="")

    # Fall back to per-day searchFlights, streaming results as each finishes.
    tasks = []
    for _, code in dests:
        for date in dates:
            item, idx = point_index[(code, date)]
            if (item["points"][idx].get("price") or 0) > 0:
                continue
            tasks.append((origin_code, code, {**slots, "departure_date": date}, 1))
    if not tasks:
        live = any(point.get("price") for item in series for point in item["points"])
        return build_payload(live=live, partial=False, notice=notice)

    if len(tasks) == 1:
        batch = [(tasks[0], _search_flights_single_route(*tasks[0]))]
    else:
        batch = _fan_out(tasks, max_workers=max_workers)

    reasons = set()
    for task, (flights, err, details) in batch:
        code = task[1]
        date = task[2]["departure_date"]
        city = next((c for c, d in dests if d == code), code)
        if err and details:
            reasons.add((details or {}).get("reason") or "")
        priced = [f for f in flights if (f.get("price") or 0) > 0]
        flight = min(priced, key=lambda f: f.get("price") or 0) if priced else None
        if flight is not None:
            flight = {**flight, "departure_date": date, "destination_city": city}
            set_point(code, date, flight.get("price"), flight)
        emit(live=bool(flight), partial=True)

    if not notice:
        notice = next((ERROR_MESSAGES[r] for r in ("provider_quota", "provider_unconfigured") if r in reasons), "")

    live = any(point.get("price") for item in series for point in item["points"])
    return build_payload(live=live, partial=False, notice="" if live else notice)
