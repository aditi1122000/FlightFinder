"""
Streamlit UI for the flight finder.
Run with: streamlit run src/app.py  (from project root)
"""
import os
import sys

# Ensure project root is on path when running as streamlit run src/app.py
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import html
import json
import logging
import random
import re
import threading
import time
import uuid

import streamlit as st

# Status messages shown at random while processing
STATUS_MESSAGES = [
    "Looking at the route…",
    "Lining up a few options…",
    "Checking times and stops…",
]

# Wide travel scenes with open sky. One is chosen per browser session.
# No cabin windows or interior frames — those fight the card in the middle.
BACKGROUNDS = [
    "https://images.unsplash.com/photo-1507525428034-b723cf961d3e?auto=format&fit=crop&w=2000&q=80",
    "https://images.unsplash.com/photo-1509316785289-025f5b846b35?auto=format&fit=crop&w=2000&q=80",
    "https://images.unsplash.com/photo-1469474968028-56623f02e42e?auto=format&fit=crop&w=2000&q=80",
    "https://images.unsplash.com/photo-1499346030926-9a72daac6c63?auto=format&fit=crop&w=2000&q=80",
]

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# Streamlit Cloud injects secrets into the environment. Always prefer
# st.secrets so a fixed key overwrites a stale/mashed env value from an
# earlier bad Secrets paste.
try:
    for key, value in st.secrets.items():
        if isinstance(value, str) and value.strip():
            os.environ[key] = value.strip()
except Exception:
    pass

from src.config import (
    DEFAULT_SLOTS,
    MODEL_NAME,
    MAX_HISTORY,
    MAX_TOKENS_LLM,
    SYSTEM_PROMPT,
    ERROR_MESSAGES,
    ENABLE_USER_SUMMARY_UPDATE,
    RATE_LIMIT_COOLDOWN_SECONDS,
    GARUDAX_DEBUG_USERS,
)
from src.services.flight_services import (
    GenerationCancelled,
    RateLimitError,
    call_mistral_with_backoff,
    request_stop,
    reset_stop,
    extract_conversational_message,
    extract_json_from_response,
    extract_misc_from_response,
    fill_slots_from_last_search,
    format_departure_date_display,
    format_missing_slots,
    flights_to_csv,
    clean_json_text,
    format_booking_details,
    validate_slots,
    search_flights_api,
    calculate_price_stats,
    suggest_alternatives,
    find_nearby_airports,
    generate_flexible_date_range,
    format_flight_price,
    format_price_range,
    resolve_airport_code,
    explore_open_window,
    build_booking_url_for_flight,
    booking_slots_for_flight,
    route_codes_from_flight,
    ota_search_url_for_offer,
    sibling_airports,
    apply_open_window_slot_policy,
    should_explore_window,
    try_local_followup,
)
from src.services.summarisation import ensure_user_summary_updated, get_user_summary
from src.services.offers import offers_for_fare, estimate_saving, route_segment, suggest_cards
from src.services.supabase_persistence import load_card_offers, load_credit_cards
from src.services.route_experience import (
    build_route_snapshot,
    leaflet_route_html,
    _format_local,
    _weather_label,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("flight_finder.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


def _persist_selection_later(row: dict) -> None:
    def worker() -> None:
        try:
            from src.services.supabase_persistence import persist_flight_selection
            persist_flight_selection(row)
        except Exception as exc:
            logger.warning("selection persist failed: %s", exc)

    threading.Thread(target=worker, daemon=True).start()


def _persist_message_later(
    conversation_id: str,
    role: str,
    content: str,
    slots: dict,
    turn_index: int,
    user_name: str | None,
) -> None:
    """Persist without making a UI transition wait on the network."""
    def worker() -> None:
        try:
            from src.services.supabase_persistence import persist_message
            persist_message(
                conversation_id,
                role,
                content,
                slots,
                turn_index=turn_index,
                user_name=user_name,
            )
        except Exception as exc:
            logger.debug("Supabase persist skipped: %s", exc)

    threading.Thread(target=worker, daemon=True).start()


def _append_message(role: str, content: str) -> None:
    """Append to chat_history and persist to Supabase if configured. Uses turn_index and user_name; increments after assistant."""
    turn_index = st.session_state.get("turn_index", 1)
    user_name = st.session_state.get("user_name") or None
    st.session_state.chat_history.append({"role": role, "content": content})
    _persist_message_later(
        st.session_state.conversation_id,
        role,
        content,
        dict(st.session_state.slots),
        turn_index,
        user_name,
    )
    if role == "assistant":
        st.session_state.turn_index = turn_index + 1


LANGGRAPH_AVAILABLE = False
create_flight_finder_graph = None
try:
    from src.graph.workflow import create_flight_finder_graph as _create
    create_flight_finder_graph = _create
    LANGGRAPH_AVAILABLE = True
    logger.info("LangGraph imported successfully")
except ImportError as e:
    logger.warning("LangGraph not available: %s. Using manual fallback.", e)


def _apply_graph_result(final_state: dict) -> None:
    """Apply LangGraph final_state to session state and persist last message."""
    chat_len = len(final_state.get("chat_history") or [])
    st.session_state.chat_history = final_state["chat_history"]
    st.session_state.slots = final_state["slots"]
    logger.info("_apply_graph_result: applied chat_history len=%d status=%s", chat_len, final_state.get("status"))
    if final_state["chat_history"]:
        last_msg = final_state["chat_history"][-1]
        turn_index = st.session_state.get("turn_index", 1)
        user_name = st.session_state.get("user_name") or None
        _persist_message_later(
            st.session_state.conversation_id,
            last_msg["role"],
            last_msg["content"],
            dict(final_state["slots"]),
            turn_index,
            user_name,
        )
        st.session_state.turn_index = turn_index + 1
    st.session_state.last_search_results = final_state.get("last_search_results")
    st.session_state.last_search_params = final_state.get("last_search_params")
    st.session_state.price_stats = final_state.get("price_stats")
    st.session_state.error_context = final_state.get("error_context")
    st.session_state.suggested_alternatives = final_state.get("suggested_alternatives")
    results = final_state.get("last_search_results") or []
    if results:
        st.session_state.window_scan = None
    else:
        st.session_state.window_scan = final_state.get("window_scan")
    _attach_turn_steps(final_state)
    st.session_state.agent_trace = {
        "status": final_state.get("status"),
        "missing_slots": final_state.get("missing_slots") or [],
        "user_message": final_state.get("user_message"),
        "searched": bool(results),
        "result_count": len(results),
        "model": MODEL_NAME,
        "error": final_state.get("error_message"),
        "live_fares": bool(results),
    }


def _build_initial_state(user_input: str) -> dict:
    return {
        "status": "clarification_needed",
        "user_message": user_input,
        "chat_history": list(st.session_state.chat_history),
        "slots": st.session_state.slots,
        "conversational_message": None,
        "missing_slots": [],
        "flights": [],
        "last_search_results": st.session_state.get("last_search_results"),
        "last_search_params": st.session_state.get("last_search_params"),
        "price_stats": st.session_state.get("price_stats"),
        "error_context": st.session_state.get("error_context"),
        "error_message": None,
        "suggested_alternatives": st.session_state.get("suggested_alternatives"),
        "search_history": list(st.session_state.get("search_history") or []),
        "user_summary": st.session_state.get("user_summary"),
    }


def _is_generation_cancelled(exc: BaseException | None) -> bool:
    seen: set[int] = set()

    def walk(cur: BaseException | None) -> bool:
        if cur is None or id(cur) in seen:
            return False
        seen.add(id(cur))
        if isinstance(cur, GenerationCancelled):
            return True
        grouped = getattr(cur, "exceptions", None)
        if grouped and any(walk(item) for item in grouped):
            return True
        return walk(cur.__cause__) or walk(cur.__context__)

    return walk(exc)


def _job_worker(graph, initial_state: dict, user_name: str | None, box: dict) -> None:
    """Run summary + graph off the Streamlit thread. Never touch st.* here."""
    try:
        summary_thread = None
        user_turns = sum(1 for msg in (initial_state.get("chat_history") or []) if msg.get("role") == "user")
        local = try_local_followup(
            initial_state.get("user_message") or "",
            initial_state.get("chat_history") or [],
            initial_state.get("slots") or {},
            initial_state.get("last_search_results"),
            bool(initial_state.get("last_search_params")),
        )
        if local is not None:
            if local.get("fare_sort"):
                box["fare_sort"] = local["fare_sort"]
            merged = {**initial_state, **local}
            box["card_picks"] = _card_picks_for(merged)
            box["result"] = merged
            logger.info("job: local follow-up, no new model or fare search")
            return
        if ENABLE_USER_SUMMARY_UPDATE and user_name and user_turns <= 1:

            def _summary_worker() -> None:
                try:
                    ensure_user_summary_updated(user_name)
                except GenerationCancelled:
                    raise
                except RateLimitError as e:
                    logger.warning("User summary skipped due to rate limit: %s", e)
                except Exception as e:
                    logger.debug("User summary update skipped: %s", e)

            summary_thread = threading.Thread(target=_summary_worker, daemon=True)
            summary_thread.start()
        try:
            row = get_user_summary(user_name) or {}
            summary = (row.get("summary_text") or "").strip() or None
            box["user_summary"] = summary
            initial_state["user_summary"] = summary
        except Exception as e:
            logger.debug("User summary read skipped: %s", e)
        if graph is None:
            box["needs_manual"] = True
            box["user_input"] = initial_state.get("user_message")
            return
        logger.info("job: invoking graph user_message=%s...", (initial_state.get("user_message") or "")[:60])
        result = graph.invoke(initial_state)
        if box.get("stopped"):
            return
        status = (result or {}).get("status")
        searched = (result or {}).get("last_search_results") or []
        if should_explore_window(result):
            box["status_label"] = "Checking fares across those dates…"

            def _scan_progress(partial: dict) -> None:
                if box.get("stopped"):
                    return
                box["window_scan"] = partial

            try:
                scan = explore_open_window(
                    result.get("slots") or {},
                    result.get("user_message") or "",
                    hint=result.get("conversational_message") or "",
                    on_progress=_scan_progress,
                )
            except GenerationCancelled:
                raise
            except Exception:
                logger.exception("explore_open_window failed")
                scan = None
            if box.get("stopped"):
                return
            if scan:
                result["window_scan"] = scan
                box["window_scan"] = scan
        if summary_thread is not None:
            summary_thread.join(timeout=0.1)
            try:
                row = get_user_summary(user_name) or {}
                summary = (row.get("summary_text") or "").strip() or None
                if summary:
                    box["user_summary"] = summary
            except Exception:
                pass
        box["card_picks"] = _card_picks_for(result)
        box["result"] = result
        logger.info("job: graph done status=%s", status)
    except GenerationCancelled:
        box["stopped"] = True
    except Exception as e:
        if _is_generation_cancelled(e):
            box["stopped"] = True
        else:
            logger.exception("Background turn failed")
            box["error"] = e
    finally:
        box["done"] = True


def _start_job(user_input: str) -> None:
    reset_stop()
    graph = st.session_state.get("flight_graph") if LANGGRAPH_AVAILABLE else None
    box = {
        "done": False,
        "stopped": False,
        "result": None,
        "error": None,
        "user_summary": None,
        "needs_manual": False,
        "status_label": random.choice(STATUS_MESSAGES) if STATUS_MESSAGES else "Looking…",
    }
    st.session_state.job = box
    st.session_state.is_calling_model = True
    threading.Thread(
        target=_job_worker,
        args=(graph, _build_initial_state(user_input), st.session_state.get("user_name"), box),
        daemon=True,
    ).start()


def _consume_job() -> None:
    """Apply a finished background turn on the main thread. A stopped turn leaves the user line."""
    job = st.session_state.pop("job", None)
    st.session_state.is_calling_model = False
    if not job:
        return
    if job.get("user_summary") is not None:
        st.session_state.user_summary = job["user_summary"]
    if job.get("fare_sort"):
        st.session_state.fare_sort = job["fare_sort"]
    result = job.get("result") or {}
    if result.get("fare_sort"):
        st.session_state.fare_sort = result["fare_sort"]
    if job.get("card_picks"):
        st.session_state.card_picks = job["card_picks"]
    err = job.get("error")
    if job.get("stopped") or _is_generation_cancelled(err):
        st.session_state.stopped_note = True
        return
    st.session_state.stopped_note = False
    if err is not None and (_exception_is_rate_limit(err) or isinstance(err, RateLimitError)):
        st.session_state.rate_limit_until = time.time() + RATE_LIMIT_COOLDOWN_SECONDS
        msg = ERROR_MESSAGES.get("rate_limit") or str(err)
        st.session_state.chat_history.append({"role": "assistant", "content": msg})
        return
    if err is not None:
        logger.warning("turn failed: %s", err)
        _append_message(
            "assistant",
            "That one didn't go through on our side. Give it another try, or change the trip a little.",
        )
        return
    if job.get("needs_manual"):
        process_manual_fallback(job.get("user_input") or "")
        return
    result = job.get("result")
    if result:
        _apply_graph_result(result)


@st.fragment(run_every=0.6)
def _watch_job() -> None:
    """Poll the background turn; refresh when the month scan or the graph updates."""
    job = st.session_state.get("job")
    if not job:
        return
    partial = job.get("window_scan")
    if partial and partial != st.session_state.get("window_scan"):
        st.session_state.window_scan = partial
        st.rerun(scope="app")
    if job.get("done"):
        st.rerun(scope="app")


def _exception_is_rate_limit(exc: BaseException) -> bool:
    """True if this exception (or its cause chain) is a Mistral rate limit."""
    cur = exc
    seen = set()
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, RateLimitError):
            return True
        text = str(cur).lower()
        if any(k in text for k in ("429", "rate_limited", "rate limit", "too many requests")):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def process_manual_fallback(user_input: str) -> None:
    """Manual status-based handling when LangGraph is not used."""
    history = st.session_state.chat_history[:-1]
    recent = history[-MAX_HISTORY:] if len(history) > MAX_HISTORY else history
    conversation_messages = [{"role": m["role"], "content": m["content"]} for m in recent]
    slots_context = json.dumps(st.session_state.slots, indent=2)
    user_summary = (st.session_state.get("user_summary") or "").strip()
    user_summary_block = user_summary if user_summary else "None"
    user_message_with_context = f"""[User profile summary (from past chats for this user_name): {user_summary_block}]
[Current booking state: {slots_context}]

User: {user_input}"""

    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT}
        ] + conversation_messages + [
            {"role": "user", "content": user_message_with_context}
        ],
        "temperature": 0.2,
        "max_tokens": MAX_TOKENS_LLM,
    }

    with st.spinner(random.choice(STATUS_MESSAGES) if STATUS_MESSAGES else "Thinking..."):
        response = call_mistral_with_backoff(payload)
    raw_reply = response.choices[0].message.content
    conversational_msg = extract_conversational_message(raw_reply)
    json_data = extract_json_from_response(raw_reply)
    conversational_msg = re.sub(r"<[^>]+>", "", conversational_msg).strip()
    misc = extract_misc_from_response(raw_reply)
    if misc:
        conversational_msg = (conversational_msg + "\n\n" + misc).strip()

    status = json_data.get("status", "error") if json_data else "error"
    logger.info("process_manual_fallback: status=%s", status)

    if json_data is None:
        try:
            json_data = json.loads(clean_json_text(raw_reply))
            conversational_msg = json_data.get("message", "I understand. Let me help you with that.")
        except Exception:
            conversational_msg = extract_conversational_message(raw_reply) or "I understand. Let me help you with that."
            json_data = {"status": "error", "slots": st.session_state.slots, "missing_slots": []}

    if "slots" in json_data and isinstance(json_data["slots"], dict):
        st.session_state.slots = fill_slots_from_last_search(
            json_data["slots"], st.session_state.get("last_search_params")
        )

    policy = {
        "user_message": user_input,
        "slots": st.session_state.slots,
        "missing_slots": json_data.get("missing_slots") or [],
        "status": json_data.get("status", "error"),
        "conversational_message": conversational_msg,
    }
    apply_open_window_slot_policy(policy)
    json_data["status"] = policy["status"]
    json_data["missing_slots"] = policy["missing_slots"]
    st.session_state.slots = policy["slots"]
    conversational_msg = policy["conversational_message"]

    status = json_data.get("status", "error")

    if status == "ready_for_search":
        is_valid, error_msg, error_details = validate_slots(st.session_state.slots)
        if not is_valid:
            st.session_state.error_context = error_details
            missing_line = format_missing_slots(
                list(error_details.keys()) if error_details else [],
                error_details,
            )
            combined = f"{conversational_msg}\n\n{error_msg}"
            if missing_line:
                combined = combined + "\n\n" + missing_line
            _append_message("assistant", combined)
        else:
            flights, api_error, api_error_details = search_flights_api(st.session_state.slots, max_results=10)
            st.session_state.last_search_results = flights
            st.session_state.last_search_params = st.session_state.slots.copy()
            if flights:
                st.session_state.price_stats = calculate_price_stats(flights)
            combined = conversational_msg + "\n\n"
            if api_error:
                combined += f"⚠️ {api_error}\n\n"
                st.session_state.error_context = api_error_details
                sugg = suggest_alternatives(st.session_state.slots)
                if sugg["suggestion_message"]:
                    combined += f"{sugg['suggestion_message']}\n\nWould you like to try any of these alternatives?"
                    st.session_state.suggested_alternatives = sugg
            elif not flights:
                combined += "I couldn't find any flights matching your exact criteria.\n\n"
                sugg = suggest_alternatives(st.session_state.slots)
                if sugg["suggestion_message"]:
                    combined += f"{sugg['suggestion_message']}\n\nWould you like to try any of these alternatives?"
                    st.session_state.suggested_alternatives = sugg
            else:
                combined += "**Flight Search Results:**\n\n"
                if st.session_state.price_stats:
                    combined += f"*{format_price_range(st.session_state.price_stats)}*\n\n"
                for i, f in enumerate(flights[:10], 1):
                    fn = f.get("flight_number")
                    airline_display = f"{f['airline']} ({fn})" if fn else f["airline"]
                    date_display = format_departure_date_display(f.get("departure_date") or st.session_state.slots.get("departure_date"))
                    if date_display:
                        airline_display = f"{airline_display} — {date_display}"
                    route = ""
                    if f.get("origin_code") and f.get("destination_code"):
                        route = f"   {f['origin_code']} → {f['destination_code']}\n"
                    combined += f"**{i}. {airline_display}** — {format_flight_price(f.get('price'))}\n"
                    if route:
                        combined += route
                    combined += f"   Departure: {f['departure_time']} | Arrival: {f['arrival_time']}\n"
                    combined += f"   {'Non-stop' if f.get('non_stop') else 'With stops'}\n"
                    combined += "\n"
                combined += "\n*Data source: Real flight API*"
            combined = re.sub(r"<[^>]+>", "", combined).strip()
            _append_message("assistant", combined)

    elif status == "refining_search":
        preferences = st.session_state.slots.get("preferences") or {}
        refinement_type = None
        fd = preferences.get("flexible_dates")
        flexible_dates_enabled = fd is True or (isinstance(fd, dict) and fd.get("enabled"))
        user_lower = (user_input or "").lower()
        price_words = ["cheaper", "cheapest", "budget", "low price", "affordable", "lowest", "minimum"]
        user_wants_cheaper = any(w in user_lower for w in price_words)
        if preferences.get("nearby_airports"):
            refinement_type = "nearby_airports"
        elif user_wants_cheaper or preferences.get("max_price"):
            refinement_type = "price_filter"
        elif flexible_dates_enabled:
            refinement_type = "flexible_dates"
        else:
            refinement_type = "price_filter"
        refined = []
        msg_extra = ""
        if refinement_type == "price_filter" and st.session_state.last_search_results:
            price_stats = st.session_state.price_stats or calculate_price_stats(st.session_state.last_search_results)
            if price_stats:
                threshold = price_stats["avg_price"]
                if any(w in user_input.lower() for w in ["cheapest", "lowest", "minimum"]):
                    threshold = price_stats["min_price"] + (price_stats["avg_price"] - price_stats["min_price"]) * 0.3
                filtered = [f for f in st.session_state.last_search_results if f.get("price", 0) <= threshold]
                search_slots = {**st.session_state.slots, "preferences": {**(st.session_state.slots.get("preferences") or {}), "max_price": threshold}}
                new_f, _, _ = search_flights_api(search_slots, max_results=10)
                seen = set()
                refined = []
                for f in filtered + new_f:
                    k = (f.get("airline"), f.get("departure_time"), f.get("price"))
                    if k not in seen:
                        refined.append(f)
                        seen.add(k)
                refined.sort(key=lambda x: x.get("price", 0))
                msg_extra = f"Found {len(refined)} flights within your budget (≤ ₹{threshold:,.0f})"
        elif refinement_type == "nearby_airports":
            _oc = (st.session_state.slots.get("origin") or {}).get("airport_code")
            _dc = (st.session_state.slots.get("destination") or {}).get("airport_code")
            o_code = (_oc[0] if isinstance(_oc, list) and _oc else _oc) or ""
            d_code = (_dc[0] if isinstance(_dc, list) and _dc else _dc) or ""
            all_f = []
            for airport in (find_nearby_airports(o_code) if o_code else [])[:2] + (find_nearby_airports(d_code) if d_code else [])[:2]:
                ss = dict(st.session_state.slots)
                ss.setdefault("origin", {})
                ss.setdefault("destination", {})
                if airport.get("airport_code") in [a.get("airport_code") for a in (find_nearby_airports(o_code) or [])[:2]]:
                    ss["origin"] = {**ss["origin"], "airport_code": airport["airport_code"]}
                else:
                    ss["destination"] = {**ss["destination"], "airport_code": airport["airport_code"]}
                fl, _, _ = search_flights_api(ss, max_results=5)
                all_f.extend(fl)
            refined = all_f[:10]
            msg_extra = f"Found {len(refined)} flights from nearby airports"
        elif refinement_type == "flexible_dates":
            base = st.session_state.slots.get("departure_date") or (st.session_state.get("last_search_params") or {}).get("departure_date")
            dates = generate_flexible_date_range(base) if base else []
            all_f = []
            for d in dates[:5]:
                fl, _, _ = search_flights_api({**st.session_state.slots, "departure_date": d}, max_results=3)
                all_f.extend(fl)
            refined = all_f[:10]
            msg_extra = f"Found {len(refined)} flights with flexible dates"
        combined = conversational_msg + "\n\n"
        if refined:
            combined += f"**{msg_extra}:**\n\n"
            st.session_state.price_stats = calculate_price_stats(refined)
            if st.session_state.price_stats:
                combined += f"*{format_price_range(st.session_state.price_stats)}*\n\n"
            for i, f in enumerate(refined[:10], 1):
                fn = f.get("flight_number")
                airline_display = f"{f['airline']} ({fn})" if fn else f["airline"]
                date_display = format_departure_date_display(f.get("departure_date") or st.session_state.slots.get("departure_date"))
                if date_display:
                    airline_display = f"{airline_display} — {date_display}"
                route = f"   {f['origin_code']} → {f['destination_code']}\n" if f.get("origin_code") and f.get("destination_code") else ""
                combined += f"**{i}. {airline_display}** — {format_flight_price(f.get('price'))}\n"
                if route:
                    combined += route
                combined += f"   Departure: {f['departure_time']} | Arrival: {f['arrival_time']}\n"
                combined += f"   {'Non-stop' if f.get('non_stop') else 'With stops'}\n"
                combined += "\n"
        else:
            combined += "I couldn't find any refined options. Would you like to try different criteria?"
        combined = re.sub(r"<[^>]+>", "", combined).strip()
        _append_message("assistant", combined)

    elif status == "awaiting_confirmation":
        slots = st.session_state.slots
        suggestions = []
        origin = slots.get("origin") or {}
        destination = slots.get("destination") or {}
        if (origin.get("city") or "").lower() and not origin.get("airport_code"):
            suggestions.extend(resolve_airport_code((origin.get("city") or "").lower()))
        if (destination.get("city") or "").lower() and not destination.get("airport_code"):
            suggestions.extend(resolve_airport_code((destination.get("city") or "").lower()))
        if suggestions:
            conversational_msg += "\n\n**Suggested alternatives:**\n"
            for i, sug in enumerate(suggestions[:3], 1):
                conversational_msg += f"{i}. {sug['city']} ({sug['airport_code']})"
                if sug.get("distance_km", 0) > 0:
                    conversational_msg += f" - {sug['distance_km']}km away"
                conversational_msg += f"\n   {sug.get('reason', '')}\n"
            st.session_state.suggested_alternatives = {"airports": suggestions}
        if st.session_state.get("error_context"):
            conversational_msg += "\n\nI encountered an issue with your request. "
            if isinstance(st.session_state.error_context, dict):
                conversational_msg += f"Please check: {', '.join(st.session_state.error_context.keys())}"
        conversational_msg = re.sub(r"<[^>]+>", "", conversational_msg).strip()
        _append_message("assistant", conversational_msg)

    else:
        conversational_msg = re.sub(r"<[^>]+>", "", conversational_msg).strip()
        missing = (json_data or {}).get("missing_slots") or []
        if missing:
            conversational_msg = (conversational_msg.rstrip() + "\n\n" + format_missing_slots(missing)).strip()
        _append_message("assistant", conversational_msg)


def _debug_allowed(user_name: str) -> bool:
    """Debug is only for names listed in GARUDAX_DEBUG_USERS (fragment match)."""
    name = (user_name or "").strip().lower()
    if not name or not GARUDAX_DEBUG_USERS:
        return False
    return any(token in name for token in GARUDAX_DEBUG_USERS)


def _inject_styles() -> None:
    st.markdown(
        """
        <style>
          @import url('https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700&display=swap');
          .stApp, .stMarkdown, .stMarkdown p, .stMarkdown li, .stMarkdown strong,
          [data-testid="stChatMessageContent"], [data-testid="stWidgetLabel"],
          [data-testid="stCaptionContainer"], [data-testid="stTextInput"] input,
          [data-testid="stChatInput"] textarea, .stButton > button,
          [data-testid="stExpander"] summary, .gx-mark, .gx-hero, .gx-hero h1, .gx-hero p {
            font-family: "Manrope", "Avenir Next", "Segoe UI", sans-serif !important;
            letter-spacing: -0.005em;
          }
          .stMarkdown, .stMarkdown p, .stMarkdown li, [data-testid="stChatMessageContent"],
          [data-testid="stWidgetLabel"], [data-testid="stCaptionContainer"] {
            font-weight: 500;
            color: #302C27;
          }
          .stMarkdown strong { font-weight: 560; }
          [data-testid="stSidebar"],
          [data-testid="stSidebarCollapsedControl"],
          [data-testid="collapsedControl"] {
            display: none !important;
            width: 0 !important;
            min-width: 0 !important;
            flex: 0 0 0px !important;
          }
          [data-testid="stAppViewContainer"] { overflow-x: hidden; }
          [data-testid="stMain"] {
            margin: 0 !important;
            padding: 0 !important;
            width: 100% !important;
            max-width: 100vw !important;
            min-width: 0 !important;
            flex: 1 1 auto !important;
          }
          [data-testid="stMainBlockContainer"],
          .block-container {
            width: 100% !important;
            max-width: none !important;
            min-height: 0;
            box-sizing: border-box !important;
            padding: 1.1rem 3.2rem 1.5rem !important;
            background: transparent;
            border-radius: 0;
            box-shadow: none;
            margin: 0 !important;
            position: relative;
            left: auto !important;
            transform: none !important;
          }
          .gx-banner {
            height: 280px;
            margin: -1.1rem -3.2rem 1.2rem -3.2rem;
            background-size: cover;
            background-position: center 40%;
            border-radius: 0 0 32px 32px;
          }
          [data-testid="stMain"] [data-testid="stVerticalBlock"] { gap: 0.45rem; }
          [data-testid="stChatMessage"] { padding-top: 0.15rem; padding-bottom: 0.15rem; }
          #MainMenu, footer, header[data-testid="stHeader"],
          [data-testid="stToolbar"], [data-testid="stDecoration"],
          [data-testid="stStatusWidget"] {
            display: none !important;
            visibility: hidden !important;
            height: 0 !important;
          }
          a.gx-home { text-decoration: none; color: inherit; }
          .gx-beta {
            background: #F5C400; color: #4A453E; font-size: 0.68rem; font-weight: 560;
            letter-spacing: 0.04em; text-transform: uppercase; border-radius: 999px;
            padding: 3px 8px; margin-left: 8px; border: 1px solid #E6C56A;
          }
          div.st-key-start { margin-top: 0.4rem; }
          .gx-hero h1 {
            font-size: 2.15rem; line-height: 1.2; letter-spacing: -0.015em;
            font-weight: 560; margin: 0 0 0.4rem 0; color: #3F3A34;
          }
          .gx-hero p { color: #6B645C; font-size: 1.05rem; font-weight: 450; margin: 0 0 1rem 0; }
          .gx-tile-title { font-weight: 520; color: #4A453E; margin-bottom: 0.15rem; }
          .gx-tile-body { color: #7A736A; font-size: 0.92rem; font-weight: 450; }
          .st-key-thread .st-key-composer { margin-top: 0.4rem; }
          .st-key-composer [data-testid="stForm"] {
            background: #fff;
            border: 1px solid #E4D7C2;
            border-radius: 999px;
            padding: 4px 6px 4px 18px;
            box-shadow: 0 10px 28px rgba(26, 23, 20, 0.12);
            gap: 0;
          }
          .st-key-composer [data-testid="stVerticalBlock"],
          .st-key-composer [data-testid="stHorizontalBlock"] { gap: 0.25rem; align-items: center; }
          .st-key-composer [data-testid="stTextInput"] { margin-bottom: 0; }
          .st-key-composer [data-testid="InputInstructions"] { display: none; }
          .st-key-composer [data-testid="stTextInput"] input,
          .st-key-composer [data-baseweb="input"],
          .st-key-composer [data-baseweb="base-input"] {
            border: none !important;
            background: transparent !important;
            box-shadow: none !important;
          }
          .st-key-composer [data-testid="stTextInput"] input {
            font-size: 1.05rem;
            font-weight: 450;
            color: #4A453E;
            min-height: 48px;
          }
          .st-key-composer [data-testid="stFormSubmitButton"] {
            display: flex;
            justify-content: flex-end;
          }
          .st-key-composer [data-testid="stFormSubmitButton"] button {
            width: 46px !important;
            height: 46px !important;
            min-width: 46px !important;
            min-height: 46px !important;
            padding: 0 !important;
            border: none !important;
            border-radius: 999px !important;
            background: #F5C400 !important;
            color: transparent !important;
            font-size: 0 !important;
            position: relative;
            box-shadow: none !important;
          }
          .st-key-composer [data-testid="stFormSubmitButton"] button p,
          .st-key-composer [data-testid="stFormSubmitButton"] button span {
            position: absolute !important;
            width: 1px !important;
            height: 1px !important;
            overflow: hidden !important;
            clip: rect(0 0 0 0) !important;
          }
          .st-key-composer [data-testid="stFormSubmitButton"] button::before {
            content: "";
            width: 11px;
            height: 11px;
            border: 2px solid #3F3A34;
            border-radius: 999px;
            position: absolute;
            left: 14px;
            top: 13px;
          }
          .st-key-composer [data-testid="stFormSubmitButton"] button::after {
            content: "";
            width: 6px;
            height: 2px;
            background: #3F3A34;
            border-radius: 2px;
            position: absolute;
            left: 24px;
            top: 26px;
            transform: rotate(45deg);
          }
          .st-key-ideas { margin-top: 0.85rem; }
          .st-key-ideas .stButton > button {
            background: #fff;
            color: #4A453E;
            border: 1px solid #E4D7C2;
            border-radius: 999px;
            min-height: 46px;
            font-weight: 500;
            text-align: center;
          }
          .st-key-ideas .stButton > button:hover {
            background: #F5C400;
            border-color: #E6C56A;
            color: #4A453E;
          }
          [data-testid="stTextInput"] [data-testid="stWidgetLabel"],
          [data-testid="stTextInput"] [data-testid="InputInstructions"] {
            display: none !important;
            height: 0 !important;
            margin: 0 !important;
            padding: 0 !important;
          }
          [data-testid="stTextInput"] [data-baseweb="input"],
          [data-testid="stTextInput"] [data-baseweb="base-input"] {
            background: #fff !important;
            border: 1px solid #E6D9C4 !important;
            border-radius: 16px !important;
            min-height: 52px;
            overflow: hidden;
            box-shadow: none !important;
          }
          .st-key-cover [data-testid="stTextInput"] [data-baseweb="input"],
          .st-key-cover [data-testid="stTextInput"] [data-baseweb="base-input"] {
            background: transparent !important;
            border: none !important;
            border-radius: 0 !important;
            min-height: 48px !important;
          }
          [data-testid="stTextInput"] input {
            background: transparent !important;
            border: none !important;
            border-radius: 16px !important;
            box-shadow: none !important;
            color: #4A453E !important;
            font-weight: 450 !important;
            min-height: 50px;
            height: 50px;
            padding: 0 14px !important;
            line-height: 50px !important;
            font-size: 1.02rem;
          }
          [data-testid="stBottom"],
          [data-testid="stBottom"] > div {
            background: transparent !important;
          }
          [data-testid="stChatInput"] {
            background: #fff !important;
            border: 1px solid #E4D7C2 !important;
            border-radius: 999px !important;
            box-shadow: 0 8px 22px rgba(74, 69, 62, 0.08);
          }
          [data-testid="stChatInput"] textarea {
            background: #fff !important;
            color: #4A453E !important;
            font-size: 1.05rem !important;
            font-weight: 450 !important;
            caret-color: #4A453E;
          }
          [data-testid="stChatInputSubmitButton"] button {
            background: #F5C400 !important;
            color: #4A453E !important;
            border-radius: 999px !important;
            font-weight: 500 !important;
          }
          .stApp:has([data-testid="stChatInput"]) .block-container { padding-bottom: 1.25rem; }
          .st-key-thread {
            max-width: 760px;
            margin: 0 auto;
          }
          .gx-q {
            margin: 1rem 0 0.2rem auto;
            width: fit-content;
            max-width: 80%;
            background: #F5C400;
            color: #3F3A34;
            border-radius: 18px 18px 6px 18px;
            padding: 10px 14px;
          }
          .gx-wait {
            display: flex;
            align-items: center;
            gap: 10px;
            margin: 0.15rem 0 0.45rem;
            min-height: 0;
            color: #8A8175;
            font-weight: 450;
            font-size: 0.98rem;
          }
          .gx-pulse {
            width: 8px;
            height: 8px;
            border-radius: 999px;
            background: #F5C400;
            flex: 0 0 auto;
            animation: gx-pulse 1.4s ease-out infinite;
          }
          @keyframes gx-pulse {
            0% { box-shadow: 0 0 0 0 rgba(245, 196, 0, 0.55); }
            70% { box-shadow: 0 0 0 7px rgba(245, 196, 0, 0); }
            100% { box-shadow: 0 0 0 0 rgba(245, 196, 0, 0); }
          }
          .st-key-liveq [data-testid="stVerticalBlock"] {
            align-items: flex-end !important;
          }
          .st-key-liveq [data-testid="stHorizontalBlock"] {
            display: flex !important;
            justify-content: flex-end;
            align-items: center;
            gap: 8px !important;
            width: fit-content !important;
            max-width: 80%;
            margin: 0.7rem 0 0.35rem auto;
            padding: 6px 8px 6px 14px;
            background: #F6D86B !important;
            border-radius: 18px 18px 6px 18px;
          }
          .st-key-liveq [data-testid="stColumn"] {
            width: auto !important;
            flex: 0 0 auto !important;
            min-width: 0 !important;
            padding: 0 !important;
          }
          .st-key-liveq .gx-q,
          .st-key-sheet .st-key-liveq .gx-q,
          .st-key-liveq .gx-q p,
          .st-key-sheet .st-key-liveq .gx-q p {
            margin: 0;
            max-width: none;
            background: transparent !important;
            box-shadow: none !important;
            border-radius: 0;
            padding: 4px 2px 4px 0;
            color: #1F2328 !important;
            -webkit-text-fill-color: #1F2328 !important;
          }
          .st-key-liveq .st-key-stop_processing {
            width: 28px !important;
            height: 28px !important;
            min-height: 0 !important;
          }
          .st-key-liveq .st-key-stop_processing button {
            width: 28px !important;
            height: 28px !important;
            min-width: 28px !important;
            min-height: 28px !important;
            padding: 0 !important;
            border: 1px solid rgba(31, 35, 40, 0.08) !important;
            border-radius: 999px !important;
            background: #fff !important;
            color: transparent !important;
            position: relative;
            box-shadow: 0 1px 2px rgba(0, 0, 0, 0.06) !important;
          }
          .st-key-liveq .st-key-stop_processing button p,
          .st-key-liveq .st-key-stop_processing button span {
            position: absolute !important;
            width: 1px !important;
            height: 1px !important;
            overflow: hidden !important;
            clip: rect(0 0 0 0) !important;
          }
          .st-key-liveq .st-key-stop_processing button::after {
            content: "" !important;
            display: block !important;
            width: 8px;
            height: 8px;
            border-radius: 2px;
            background: #1F2328;
            position: absolute;
            left: 50%;
            top: 50%;
            transform: translate(-50%, -50%);
          }
          .st-key-homebar [data-testid="stForm"] {
            background: #fff;
            border: 1px solid #E6D9C4;
            border-radius: 999px;
            padding: 4px 5px 4px 16px;
          }
          .st-key-homebar [data-testid="stHorizontalBlock"] { gap: 0.2rem; align-items: center; }
          .st-key-homebar [data-testid="stColumn"] { padding: 0 !important; }
          .st-key-homebar [data-testid="stTextInput"] { margin-bottom: 0; }
          .st-key-homebar [data-testid="InputInstructions"] { display: none; }
          .st-key-homebar [data-testid="stTextInput"] [data-baseweb="input"],
          .st-key-homebar [data-testid="stTextInput"] [data-baseweb="base-input"] {
            border: none !important;
            background: transparent !important;
            box-shadow: none !important;
            min-height: 44px !important;
            border-radius: 999px !important;
          }
          .st-key-homebar [data-testid="stTextInput"] input {
            border: none !important;
            background: transparent !important;
            box-shadow: none !important;
            min-height: 44px !important;
            height: 44px !important;
            line-height: 44px !important;
            padding: 0 4px !important;
          }
          .st-key-homebar [data-testid="stFormSubmitButton"] {
            display: flex;
            justify-content: flex-end;
          }
          .st-key-homebar [data-testid="stFormSubmitButton"] button {
            width: 44px !important;
            height: 44px !important;
            min-width: 44px !important;
            min-height: 44px !important;
            padding: 0 !important;
            border: none !important;
            border-radius: 999px !important;
            background: #F5C400 !important;
            color: transparent !important;
            font-size: 0 !important;
            position: relative;
            box-shadow: none !important;
          }
          .st-key-homebar [data-testid="stFormSubmitButton"] button p,
          .st-key-homebar [data-testid="stFormSubmitButton"] button span {
            position: absolute !important;
            width: 1px !important;
            height: 1px !important;
            overflow: hidden !important;
            clip: rect(0 0 0 0) !important;
          }
          .st-key-homebar [data-testid="stFormSubmitButton"] button::after {
            content: "";
            width: 8px;
            height: 8px;
            border-right: 2.5px solid #3F3A34;
            border-top: 2.5px solid #3F3A34;
            position: absolute;
            left: 15px;
            top: 17px;
            transform: rotate(45deg);
          }
          [class*="st-key-reply"] {
            background: #fff;
            border: 1px solid #EFE4CC;
            border-radius: 6px 18px 18px 18px;
            padding: 4px 16px 8px;
            margin: 1.35rem 0 1.5rem 0;
          }
          [class*="st-key-reply"] [data-testid="stExpander"] details {
            background: transparent;
            border: none !important;
            border-radius: 10px;
          }
          .gx-section { margin: 0.8rem 0 0.35rem 0; font-weight: 520; color: #4A453E; }
          .gx-note { color: #8A8175; font-size: 0.82rem; font-weight: 450; margin-bottom: 0.45rem; }
          .gx-card {
            background: #fff; border: 1px solid #F0E2B8; border-radius: 14px;
            padding: 12px 14px; margin-bottom: 8px;
            display: flex; justify-content: space-between; gap: 12px; align-items: center;
          }
          .gx-card .who { font-weight: 520; }
          .gx-card .meta { color: #7A736A; font-size: 0.9rem; font-weight: 450; }
          .gx-card .fare { font-weight: 520; white-space: nowrap; }
          .gx-chart { display: block; margin: 0.2rem 0 0.9rem 0; }
          .gx-bars { display: flex; align-items: flex-end; gap: 6px; height: 120px; margin: 0.4rem 0 0.35rem 0; }
          .gx-fare-helpers {
            display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 8px; margin: 0.85rem 0 0.2rem 0; clear: both;
          }
          @media (max-width: 720px) {
            .gx-fare-helpers { grid-template-columns: 1fr; }
          }
          .gx-bar { flex: 1; background: #1A1714; border-radius: 6px 6px 2px 2px; min-height: 8px; }
          .gx-bar.alt { background: #8A8175; }
          .gx-bar.on { background: #F5C400; outline: 1px solid #C89200; }
          .gx-bar.pending { background: #E3E7EB; opacity: 0.85; }
          .gx-months { display: flex; gap: 6px; }
          .gx-months span { flex: 1; text-align: center; font-size: 0.68rem; color: #6B6458; }
          .gx-split { display: flex; height: 14px; gap: 3px; margin: 0.45rem 0 0.2rem 0; }
          .gx-split div { height: 14px; border-radius: 999px; min-width: 6px; }
          .gx-route-photo {
            height: 220px; border-radius: 18px; background-size: cover; background-position: center;
            box-shadow: 0 12px 32px rgba(26, 23, 20, 0.14);
          }
          .gx-route-stats {
            display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin-top: 10px;
          }
          @media (min-width: 720px) {
            .gx-route-stats { grid-template-columns: repeat(4, minmax(0, 1fr)); }
          }
          .gx-route-stat {
            background: #fff; border: 1px solid #E7DCC8; border-radius: 14px;
            padding: 10px 12px;
          }
          .gx-route-stat .k { font-size: 0.68rem; letter-spacing: 0.08em; text-transform: uppercase; color: #8A8175; }
          .gx-route-stat .v { font-size: 0.95rem; font-weight: 520; color: #3F3A34; margin-top: 2px; }
          .gx-route-stat .s { font-size: 0.82rem; color: #6B6458; margin-top: 2px; }
          [data-testid="stAppViewContainer"] {
            background-color: #F6F3EC;
          }
          .stButton > button {
            border-radius: 999px; border: 1px solid #D9CBB2; background: #fff;
            color: #4A453E; font-weight: 500;
          }
          .stButton > button[kind="primary"] {
            background: #4A453E; color: #fff; border-radius: 999px; border: none; font-weight: 500;
          }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _apply_background() -> None:
    """Pick one travel photo per session so sign-ins don't all look the same."""
    if st.session_state.get("bg_url") not in BACKGROUNDS:
        st.session_state.bg_url = random.choice(BACKGROUNDS)
    url = st.session_state.bg_url
    st.markdown(
        f"""
        <style>
          [data-testid="stAppViewContainer"] {{
            background-image:
              linear-gradient(180deg, rgba(20,16,12,0.06) 0%, rgba(20,16,12,0.12) 100%),
              url("{url}");
            background-size: cover;
            background-position: center 40%;
            background-attachment: fixed;
          }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def _queue_turn(text: str, *, rerun: bool = True) -> None:
    """Show the user's line immediately, then let the next run call the agent."""
    text = (text or "").strip()
    if not text or st.session_state.get("pending_turn"):
        return
    running = st.session_state.get("job")
    if running and not running.get("done"):
        running["stopped"] = True
        running["done"] = True
        request_stop()
    st.session_state.stopped_note = False
    _append_message("user", text)
    st.session_state.pending_turn = text
    st.session_state.pending_shown = False
    if rerun:
        st.rerun()


def _route_label(slots: dict) -> str:
    origin = (slots or {}).get("origin") or {}
    dest = (slots or {}).get("destination") or {}
    left = origin.get("city") or origin.get("airport_code") or "From"
    right = dest.get("city") or dest.get("airport_code") or "To"
    return f"{left} → {right}"


def _focus_month(slots: dict):
    date = (slots or {}).get("departure_date") or ""
    if isinstance(date, str) and len(date) >= 7 and date[5:7].isdigit():
        idx = int(date[5:7]) - 1
        if 0 <= idx < 12:
            return MONTHS[idx]
    return None


def _sample_trend(anchor) -> list:
    mid = int(anchor or 18000)
    shape = [1.15, 0.92, 0.84, 0.9, 1.05, 1.22, 1.28, 1.18, 0.95, 0.88, 1.02, 1.2]
    return [max(4000, int(mid * s)) for s in shape]


def _cheapest_point(series: dict):
    priced = [p for p in (series.get("points") or []) if (p.get("price") or 0) > 0]
    if not priced:
        return None
    return min(priced, key=lambda p: p["price"])


def _date_bars(points: list, alt: bool) -> None:
    if not points:
        return
    priced = [p for p in points if (p.get("price") or 0) > 0]
    peak = max((p["price"] for p in priced), default=1) or 1
    cheapest = min((p["price"] for p in priced), default=0)
    bars = []
    labels = []
    for point in points:
        day = point["date"][8:10].lstrip("0")
        try:
            from datetime import datetime
            parsed = datetime.strptime(point["date"][:10], "%Y-%m-%d")
            if len(points) <= 10:
                day = parsed.strftime("%a")[:2] + " " + day
        except ValueError:
            pass
        price = point.get("price") or 0
        klass = "gx-bar"
        if alt:
            klass += " alt"
        if price > 0 and price == cheapest:
            klass += " on"
        elif not price:
            klass += " pending"
        height = max(8, int(118 * price / peak)) if price > 0 else 6
        bars.append(f'<div class="{klass}" style="height:{height}px"></div>')
        labels.append(f"<span>{html.escape(day)}</span>")
    st.markdown(
        f'<div class="gx-bars">{"".join(bars)}</div><div class="gx-months">{"".join(labels)}</div>',
        unsafe_allow_html=True,
    )


def _render_window_scan(slots: dict, scan: dict) -> None:
    """Partial trip: dates inside the month they named, for each possible city."""
    series = scan.get("series") or []
    cities = ", ".join(item.get("city") or item.get("code") or "" for item in series)
    month = scan.get("month_label") or "that month"
    st.markdown(f'<div class="gx-section">{html.escape(_route_label(slots))}</div>', unsafe_allow_html=True)
    if scan.get("specific_date"):
        scope = f"{format_departure_date_display(scan['specific_date'])} · {cities}"
    else:
        scope = f"{month} · {cities}"
    st.markdown(f'<div class="gx-note">{html.escape(scope)}</div>', unsafe_allow_html=True)

    heading = "Fares on that day" if scan.get("specific_date") else f"Fares across {month.split()[0]}"
    st.markdown(f'<div class="gx-section">{html.escape(heading)}</div>', unsafe_allow_html=True)
    if scan.get("specific_date"):
        note = "One price check for each city. The yellow bar is the cheaper one."
    elif scan.get("sampled"):
        note = "A few days across the month, not every day. Yellow is the cheapest of these. Name a date, or ask for the cheapest."
    else:
        note = "Every day in that window. Yellow is the cheapest day."
    if scan.get("partial") and not scan.get("live"):
        note = "Filling in live prices…"
    elif scan.get("notice") and not scan.get("live"):
        note = scan["notice"]
    st.markdown(f'<div class="gx-note">{html.escape(note)}</div>', unsafe_allow_html=True)

    reads = []
    for index, item in enumerate(series):
        best = _cheapest_point(item)
        label = item.get("city") or item.get("code") or "City"
        st.markdown(f'<div class="gx-note">{html.escape(label)}</div>', unsafe_allow_html=True)
        _date_bars(item.get("points") or [], alt=index > 0)
        if best:
            reads.append(
                f"{label} is lowest at {format_flight_price(best['price'])} on {format_departure_date_display(best['date'])}"
            )
    if reads:
        st.markdown(f'<div class="gx-note">{html.escape(". ".join(reads))}.</div>', unsafe_allow_html=True)


def _has_fare_cut(flight: dict) -> bool:
    if (flight.get("discount") or 0) > 0 or flight.get("applied_discounts"):
        return True
    if flight.get("show_strikethrough") or flight.get("genius_airline") or flight.get("genius_self"):
        return True
    return False


def _fare_sentence(flight: dict) -> str:
    who = flight.get("airline") or "This fare"
    parts = [f"{who} is {format_flight_price(flight.get('price'))}."]
    bits = []
    if (flight.get("base_fare") or 0) > 0:
        bits.append(f"{format_flight_price(flight['base_fare'])} is the fare")
    if (flight.get("tax") or 0) > 0:
        bits.append(f"{format_flight_price(flight['tax'])} is tax")
    if (flight.get("fee") or 0) > 0:
        bits.append(f"{format_flight_price(flight['fee'])} is fees")
    if bits:
        parts.append(" and ".join(bits) + "." if len(bits) < 3 else ", ".join(bits) + ".")
    discount = flight.get("discount") or 0
    applied = flight.get("applied_discounts") or []
    if discount > 0:
        line = f"A fare discount of {format_flight_price(discount)} is already in that price"
        before = flight.get("list_price") or 0
        if before > discount:
            line += f", down from {format_flight_price(before)}"
        parts.append(line + ".")
    elif applied:
        names = ", ".join(row.get("label") or "a discount" for row in applied)
        parts.append(f"Applied on this offer: {names}.")
    elif flight.get("genius_airline") or flight.get("genius_self"):
        parts.append("A Genius campaign is flagged on this offer.")
    else:
        parts.append(
            "No fare discount came back on this offer. Bank-card cashback is not in this search, so it is not estimated."
        )
    return " ".join(parts)


def _fare_helpers(flight: dict) -> list:
    cards = [("Listed fare", format_flight_price(flight.get("price")))]
    if (flight.get("discount") or 0) > 0:
        body = f"{format_flight_price(flight['discount'])} off"
        before = flight.get("list_price") or 0
        if before:
            body += f", from {format_flight_price(before)}"
        cards.append(("Fare discount", body))
    for row in flight.get("applied_discounts") or []:
        body = row.get("label") or "Discount"
        if row.get("amount"):
            body = f"{body}: {format_flight_price(row['amount'])}"
        cards.append(("Applied discount", body))
    if flight.get("genius_airline"):
        cards.append(("Genius", "Airline-funded campaign on this offer."))
    elif flight.get("genius_self"):
        cards.append(("Genius", "Campaign flagged on this offer."))
    for badge in flight.get("badges") or []:
        if badge.get("text"):
            cards.append(("Deal badge", badge["text"]))
    if flight.get("fare_name"):
        cards.append(("Fare family", str(flight["fare_name"])))
    return cards[:6]


def _fare_split_bar(flight: dict) -> None:
    parts = [
        ("Fare", float(flight.get("base_fare") or 0), "#1A1714"),
        ("Tax", float(flight.get("tax") or 0), "#C4B8A5"),
        ("Fees", float(flight.get("fee") or 0), "#8A8175"),
    ]
    parts = [(name, value, color) for name, value, color in parts if value > 0]
    total = sum(value for _, value, _ in parts)
    if total <= 0:
        return
    segs = "".join(
        f'<div style="width:{max(4, 100 * value / total):.1f}%;background:{color}"></div>'
        for _, value, color in parts
    )
    legend = " · ".join(f"{name} {format_flight_price(value)}" for name, value, _ in parts)
    if (flight.get("discount") or 0) > 0:
        legend += f" · Discount −{format_flight_price(flight['discount'])}"
    st.markdown(
        f'<div class="gx-split">{segs}</div><div class="gx-note">{html.escape(legend)}</div>',
        unsafe_allow_html=True,
    )


def _fare_compare_bars(flights: list, picked: dict) -> None:
    prices = [float(f.get("price") or 0) for f in flights]
    peak = max(prices) or 1
    bars = []
    labels = []
    for flight, price in zip(flights, prices):
        klass = "gx-bar"
        if flight is picked:
            klass += " on"
        elif _has_fare_cut(flight):
            klass += " alt"
        height = max(8, int(118 * price / peak)) if price else 8
        bars.append(f'<div class="{klass}" style="height:{height}px"></div>')
        short = ((flight.get("airline") or "Fare").split() or ["Fare"])[0][:10]
        labels.append(f"<span>{html.escape(short)}</span>")
    st.markdown(
        '<div class="gx-note">Taller is a higher total. Yellow is the fare you are reading.</div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        f'<div class="gx-chart"><div class="gx-bars">{"".join(bars)}</div>'
        f'<div class="gx-months">{"".join(labels)}</div></div>',
        unsafe_allow_html=True,
    )


def _render_fare_desk(flights: list) -> None:
    """Filters, a fare split, and helpers from the search payload. No guessed card rates."""
    st.markdown('<div class="gx-section">What changes the fare</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="gx-note">From this search: the fare split, any listed discount, deal badges, and what is already included.</div>',
        unsafe_allow_html=True,
    )
    lens = st.pills(
        "Fare lens",
        ["All", "Discounted", "Deal badges", "Non-stop"],
        default="All",
        key="fare_lens",
        label_visibility="collapsed",
    ) or "All"
    pool = list(flights[:8])
    if lens == "Discounted":
        pool = [f for f in pool if _has_fare_cut(f)]
    elif lens == "Deal badges":
        pool = [f for f in pool if f.get("badges")]
    elif lens == "Non-stop":
        pool = [f for f in pool if f.get("non_stop")]
    if not pool:
        st.markdown(
            '<div class="gx-note">None of these fares match that filter.</div>',
            unsafe_allow_html=True,
        )
        return
    labels = []
    for flight in pool:
        number = str(flight.get("flight_number") or "").strip()
        who = flight.get("airline") or "Flight"
        label = f"{who} · {number} · {format_flight_price(flight.get('price'))}" if number else f"{who} · {format_flight_price(flight.get('price'))}"
        labels.append(label)
    pick = st.selectbox("Fare", labels, label_visibility="collapsed", key="fare_pick")
    flight = pool[labels.index(pick)] if pick in labels else pool[0]
    st.markdown(f'<div class="gx-note">{html.escape(_fare_sentence(flight))}</div>', unsafe_allow_html=True)
    _fare_split_bar(flight)
    if len(pool) > 1:
        _fare_compare_bars(pool, flight)
    cards = "".join(
        f'<div class="gx-card" style="display:block"><div class="gx-tile-title">{html.escape(title)}</div>'
        f'<div class="gx-tile-body">{html.escape(body)}</div></div>'
        for title, body in _fare_helpers(flight)
    )
    if cards:
        st.markdown(f'<div class="gx-fare-helpers">{cards}</div>', unsafe_allow_html=True)
    included = [str(item) for item in (flight.get("included") or []) if item]
    if included:
        st.markdown(
            f'<div class="gx-note">Already in this fare: {html.escape(", ".join(included))}.</div>',
            unsafe_allow_html=True,
        )


@st.cache_data(ttl=900, show_spinner=False)
def _cached_route(origin_code: str, dest_code: str, dest_city: str) -> dict | None:
    """Cache weather + path; slots are rebuilt from codes for stability."""
    slots = {
        "origin": {"city": None, "airport_code": origin_code},
        "destination": {"city": dest_city, "airport_code": dest_code},
    }
    snap = build_route_snapshot(slots, dest_code=dest_code or None)
    if not snap:
        return None
    return {
        "origin_label": snap.origin.label,
        "dest_label": snap.destination.label,
        "origin_lat": snap.origin.lat,
        "origin_lon": snap.origin.lon,
        "dest_lat": snap.destination.lat,
        "dest_lon": snap.destination.lon,
        "path": snap.path,
        "distance_km": snap.distance_km,
        "flight_hours": snap.flight_hours,
        "photo_url": snap.photo_url,
        "origin_weather": snap.origin_weather,
        "dest_weather": snap.dest_weather,
        "google_maps_url": snap.google_maps_url,
    }


def _render_route_experience(slots: dict, dest_code: str | None = None) -> None:
    import streamlit.components.v1 as components
    from src.services.route_experience import RoutePoint, RouteSnapshot

    origin = slots.get("origin") if isinstance(slots.get("origin"), dict) else {}
    dest = slots.get("destination") if isinstance(slots.get("destination"), dict) else {}
    o_code = (origin.get("airport_code") or origin.get("city") or "")[:8]
    d_city = (dest.get("city") or "")[:40]
    d_code = dest_code or (dest.get("airport_code") or "")[:8]
    if not o_code or not (d_code or d_city):
        return
    raw = _cached_route(str(o_code), str(d_code), d_city)
    if not raw:
        return
    snap = RouteSnapshot(
        origin=RoutePoint("", "", raw["origin_label"], raw["origin_lat"], raw["origin_lon"]),
        destination=RoutePoint("", "", raw["dest_label"], raw["dest_lat"], raw["dest_lon"]),
        path=raw["path"],
        distance_km=raw["distance_km"],
        flight_hours=raw["flight_hours"],
        photo_url=raw["photo_url"],
        origin_weather=raw["origin_weather"],
        dest_weather=raw["dest_weather"],
        google_maps_url=raw["google_maps_url"],
    )
    ow, dw = snap.origin_weather, snap.dest_weather
    st.markdown('<div class="gx-section">The route</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="gx-note">Google Maps, live weather, and local time.</div>',
        unsafe_allow_html=True,
    )
    photo, map_col = st.columns([1, 1.35])
    with photo:
        st.markdown(
            f'<div class="gx-route-photo" style="background-image:url({html.escape(snap.photo_url, quote=True)})"></div>',
            unsafe_allow_html=True,
        )
        st.caption(f"Mood shot · {snap.destination.label.split('(')[0].strip()}")
    with map_col:
        components.html(leaflet_route_html(snap, height=220), height=228, scrolling=False)
    stats = [
        ("From", snap.origin.label, _weather_line(ow)),
        ("To", snap.destination.label, _weather_line(dw)),
        ("Distance", f"~{snap.distance_km:,} km", f"About {snap.flight_hours} h in the air"),
        ("Coordinates", _coord(snap.origin), _coord(snap.destination)),
    ]
    cards = "".join(
        f'<div class="gx-route-stat"><div class="k">{html.escape(title)}</div>'
        f'<div class="v">{html.escape(main)}</div>'
        f'<div class="s">{html.escape(sub)}</div></div>'
        for title, main, sub in stats
    )
    st.markdown(f'<div class="gx-route-stats">{cards}</div>', unsafe_allow_html=True)
    route = f"{snap.origin.label.split('(')[0].strip()} to {snap.destination.label.split('(')[0].strip()}"
    st.markdown(
        f'<a class="gx-maps" href="{html.escape(snap.google_maps_url, quote=True)}" target="_blank" rel="noopener">'
        '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="#E85D4C" d="M12 2.2C8.3 2.2 5.3 5.2 5.3 8.9c0 4.9 6.7 12.9 6.7 12.9s6.7-8 6.7-12.9c0-3.7-3-6.7-6.7-6.7z"/>'
        '<circle cx="12" cy="8.8" r="2.3" fill="#fff"/></svg>'
        f'<span>Open in Google Maps</span><em>{html.escape(route)}</em></a>',
        unsafe_allow_html=True,
    )


def _weather_line(w: dict) -> str:
    if not w:
        return "Weather unavailable"
    temp = w.get("temp_c")
    t = f"{temp:.0f}°C" if isinstance(temp, (int, float)) else "—"
    sky = _weather_label(w.get("weather_code"))
    when = _format_local(w.get("local_time") or "")
    tz = (w.get("timezone") or "").split("/")[-1].replace("_", " ")
    return f"{t} · {sky} · {when}" + (f" ({tz})" if tz else "")


def _coord(pt) -> str:
    return f"{pt.lat:.2f}°, {pt.lon:.2f}°"


def _minutes(hhmm: str) -> int | None:
    try:
        h, m = str(hhmm or "").strip()[:5].split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return None


def _duration_min(flight: dict) -> int | None:
    dep = _minutes(flight.get("departure_time"))
    arr = _minutes(flight.get("arrival_time"))
    if dep is None or arr is None:
        return None
    span = arr - dep
    if span <= 0:
        span += 24 * 60
    return span


def _flight_line(flight: dict, date: str | None = None) -> str:
    airline = str(flight.get("airline") or "Flight")
    number = str(flight.get("flight_number") or "").strip()
    who = airline + (f" {number}" if number else "")
    dep = str(flight.get("departure_time") or "")
    arr = str(flight.get("arrival_time") or "")
    stops = "direct" if flight.get("non_stop") else "with stops"
    cabin = str(flight.get("cabin_class") or "economy").replace("_", " ").title()
    when = f"{format_departure_date_display(date)} · " if date else ""
    return f"{when}{who} · {dep} → {arr} · {stops} · {cabin}"


def _paths_from_scan(slots: dict, scan: dict) -> tuple[str, list]:
    """Partial trip: one card per possible city, each with the cheapest priced day."""
    origin = (slots.get("origin") or {}).get("city") or (slots.get("origin") or {}).get("airport_code") or "there"
    cards = []
    for item in scan.get("series") or []:
        best = _cheapest_point(item)
        if not best:
            continue
        flight = best.get("flight") or {}
        city = item.get("city") or item.get("code") or "City"
        cards.append({
            "title": city,
            "price": best["price"],
            "why": _flight_line(flight, best["date"]),
            "ask": f"Fly {origin} to {city} on {best['date']}",
            "button": f"Go with {city}",
        })
    cards.sort(key=lambda c: c["price"])
    if not cards:
        return "", []
    top = cards[0]
    month = (scan.get("month_label") or "that month").split()[0]
    verdict = (
        f"{top['title']} is the cheapest way in this {month}, at {format_flight_price(top['price'])}. "
        "Pick one and the real search runs on that day."
    )
    return verdict, cards[:3]


def _paths_from_flights(flights: list) -> tuple[str, list]:
    """Full search: one card per way of sorting the list. Each card is a filter, not a booking."""
    ranked = {mode: _rank_fares(flights, mode) for mode in ("Cheapest", "Fastest", "Earliest")}
    if not ranked["Cheapest"]:
        return "", []
    cheapest = ranked["Cheapest"][0]
    quickest = ranked["Fastest"][0]
    earliest = ranked["Earliest"][0]

    picks = [("Cheapest", "Cheapest", cheapest, "Lowest total on this day.")]
    if quickest is not cheapest:
        picks.append(("Quickest", "Fastest", quickest, f"{_duration_label(quickest)} · {_stops_label(quickest)}."))
    if earliest is not cheapest and earliest is not quickest:
        picks.append(("Early start", "Earliest", earliest, "Leaves first, so you land with the day ahead."))

    cards = [
        {
            "title": title,
            "sort": sort,
            "price": flight["price"],
            "why": _flight_line(flight),
            "note": note,
            "flight": flight,
        }
        for title, sort, flight, note in picks
    ]
    gap = (quickest["price"] - cheapest["price"]) if quickest is not cheapest else 0
    verdict = f"{cheapest.get('airline') or 'The cheapest fare'} at {format_flight_price(cheapest['price'])} is the one to beat."
    if gap > 0:
        verdict += f" Quicker costs {format_flight_price(gap)} more."
    return verdict, cards


def _set_fare_sort(mode: str) -> None:
    st.session_state.fare_sort = mode
    st.session_state.fares_shown = FARE_PAGE


def _render_paths() -> bool:
    """The answer first: a one-line verdict and cards you can act on. Returns True if drawn."""
    slots = st.session_state.get("slots") or {}
    flights = st.session_state.get("last_search_results") or []
    scan = st.session_state.get("window_scan") or {}
    if flights:
        verdict, cards = _paths_from_flights(flights)
        mode = "book"
    elif scan.get("live"):
        verdict, cards = _paths_from_scan(slots, scan)
        mode = "pick"
    else:
        return False
    if not cards:
        return False

    st.markdown(f'<div class="gx-verdict">{html.escape(verdict)}</div>', unsafe_allow_html=True)
    current = st.session_state.get("fare_sort") or "Best"
    cols = st.columns(len(cards))
    for i, (col, card) in enumerate(zip(cols, cards)):
        active = card.get("sort") == current if mode == "book" else i == 0
        with col:
            with st.container(key="pathactive" if active else f"path{i}"):
                pick = ' <span class="gx-path-badge">Our pick</span>' if i == 0 else ""
                note = card.get("note") or ""
                st.markdown(
                    f'<div class="gx-path"><div class="t">{html.escape(card["title"])}{pick}</div>'
                    f'<div class="p">{html.escape(format_flight_price(card["price"]))}</div>'
                    f'<div class="w">{html.escape(card["why"])}</div>'
                    + (f'<div class="n">{html.escape(note)}</div>' if note else "")
                    + "</div>",
                    unsafe_allow_html=True,
                )
                if mode == "pick":
                    if st.button(card["button"], key=f"path_go_{i}", use_container_width=True):
                        _queue_turn(card["ask"])
    return True


def _duration_label(flight: dict) -> str:
    mins = flight.get("duration_minutes") or _duration_min(flight) or 0
    return f"{mins // 60}h {mins % 60:02d}m" if mins else ""


def _stops_label(flight: dict) -> str:
    stops = flight.get("stops")
    if stops is None:
        stops = 0 if flight.get("non_stop") else 1
    return "Direct" if not stops else f"{stops} stop" + ("s" if stops > 1 else "")


def _close_booking() -> None:
    st.session_state.selected_flight = None
    st.session_state.booking_ready_url = None


def _price_stack(flight: dict) -> str:
    """Discount above the struck original, then the price you pay."""
    pay = float(flight.get("price") or 0)
    cut = float(flight.get("discount") or 0)
    listed = float(flight.get("list_price") or 0)
    if listed <= pay and cut > 0:
        listed = pay + cut
    if cut > 0 and listed > pay:
        return (
            '<span class="gx-price">'
            f'<small>{html.escape(format_flight_price(cut))} off</small>'
            f'<s>{html.escape(format_flight_price(listed))}</s>'
            f'<b>{html.escape(format_flight_price(pay))}</b>'
            "</span>"
        )
    return f'<span class="gx-price"><b>{html.escape(format_flight_price(pay))}</b></span>'


def _ticket_html(flight: dict) -> str:
    airline = html.escape(str(flight.get("airline") or "Flight"))
    number = html.escape(str(flight.get("flight_number") or "").strip())
    cabin = str(flight.get("cabin_class") or "economy").replace("_", " ").title()
    fare_name = str(flight.get("fare_name") or "").strip()
    when = format_departure_date_display(flight.get("departure_date"))
    meta = " · ".join(html.escape(p) for p in (when, cabin, fare_name.title()) if p)
    middle = " · ".join(p for p in (_duration_label(flight), _stops_label(flight)) if p)
    return (
        '<div class="gx-ticket">'
        f'<div class="top"><span class="who">{airline}'
        + (f' <em>{number}</em>' if number else "")
        + f'</span>{_price_stack(flight)}</div>'
        '<div class="leg">'
        f'<div><b>{html.escape(str(flight.get("departure_time") or ""))}</b>'
        f'<span>{html.escape(str(flight.get("origin_code") or ""))}</span></div>'
        f'<div class="line"><i></i><small>{html.escape(middle)}</small></div>'
        f'<div class="end"><b>{html.escape(str(flight.get("arrival_time") or ""))}</b>'
        f'<span>{html.escape(str(flight.get("destination_code") or ""))}</span></div>'
        "</div>"
        f'<div class="meta">{meta}</div>'
        "</div>"
    )


@st.cache_data(ttl=8 * 3600, show_spinner=False)
def _card_offers_cached() -> list:
    """Card deals table. Refreshed by scripts/refresh_offers.py three times a day."""
    try:
        return load_card_offers()
    except Exception:
        return []


@st.cache_data(ttl=8 * 3600, show_spinner=False)
def _credit_cards_cached() -> list:
    """Travel credit card catalogue. Refreshed with the offers."""
    try:
        return load_credit_cards()
    except Exception:
        return []


def _aggregator_label(offer: dict) -> str:
    site = (offer.get("site") or offer.get("merchant") or "").lower()
    if "goibibo" in site:
        return "Goibibo"
    if "cleartrip" in site:
        return "Cleartrip"
    if "ixigo" in site:
        return "ixigo"
    if "axis" in site:
        return offer.get("merchant") or "Axis"
    return offer.get("merchant") or "the booking site"


def _offer_amount(offer: dict, fare: float) -> str:
    saving = estimate_saving(offer, fare) if fare else None
    if saving:
        return f"about ₹{saving:,.0f} off"
    if offer.get("max_amount"):
        return f"up to ₹{float(offer['max_amount']):,.0f} off"
    if offer.get("percent_off"):
        return f"up to {float(offer['percent_off']):g}%"
    return ""


FARE_COLS = [5.2, 2, 1.3]


def _lead_site(offers: list) -> str:
    """The booking site the card button will open. Fares stay on Booking.com."""
    for offer in offers or []:
        where = _aggregator_label(offer)
        if where and where != "Booking.com":
            return where
    return ""


def _offers_for_site(offers: list, site: str) -> list:
    if not site:
        return []
    out, seen = [], set()
    for offer in offers or []:
        if _aggregator_label(offer) != site:
            continue
        key = (offer.get("code"), offer.get("bank"), _short_offer_title(offer.get("title") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(offer)
    return out


def _offer_html(
    offer: dict,
    fare: float = 0.0,
    *,
    trip_url: str = "",
) -> str:
    amount = _offer_amount(offer, fare)
    code = str(offer.get("code") or "").strip()
    where = _aggregator_label(offer)
    title = _short_offer_title(offer.get("title") or "")
    if code and code in title:
        title = re.sub(rf"\s*[·\-–]?\s*{re.escape(code)}\s*", " ", title).strip(" ·-")
    ref = offer.get("detail_url") or offer.get("terms_url") or ""
    tags = []
    if offer.get("card_kind") == "emi":
        tags.append("EMI")
    if offer.get("min_spend"):
        tags.append(f"min ₹{float(offer['min_spend']):,.0f}")
    links = []
    if ref:
        links.append(f'<a class="gx-ref" href="{html.escape(ref)}" target="_blank" rel="noopener">Terms</a>')
    if trip_url and where != "Booking.com":
        links.append(
            f'<a class="gx-ref gx-ref-trip" href="{html.escape(trip_url)}" target="_blank" rel="noopener">'
            f'{html.escape(where)}</a>'
        )
    name = offer.get("bank") or where
    code_bit = f' · <b>{html.escape(code)}</b>' if code else ""
    return (
        f'<div class="gx-offer">'
        f'<span class="gx-offer-bank">{html.escape(name)}{code_bit}</span>'
        f'<span class="gx-offer-amt">{html.escape(amount)}</span>'
        f'<span class="gx-offer-text">{html.escape(title)}</span>'
        f'<span class="gx-offer-meta">'
        + (html.escape(" · ".join(tags)) if tags else "")
        + ((" · " if tags else "") + " · ".join(links) if links else "")
        + "</span></div>"
    )


def _clip_text(text: str, limit: int = 140) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    if len(t) <= limit:
        return t
    return t[: limit - 1].rsplit(" ", 1)[0] + "…"


def _render_card_offers(flight: dict) -> None:
    """Deals that might cut this fare at payment. Listed, not promised."""
    airline = str(flight.get("airline") or "")
    try:
        fare = float(flight.get("price") or 0)
    except (TypeError, ValueError):
        fare = 0.0
    all_offers = _card_offers_cached()
    if not all_offers:
        return
    cards = _credit_cards_cached()
    chosen = None
    base_slots = st.session_state.get("slots") or {}
    origin_code, dest_code = route_codes_from_flight(flight, base_slots)
    segment = route_segment(origin_code, dest_code)
    book_slots = booking_slots_for_flight(flight, base_slots, dest_code)
    bank = None

    raw = offers_for_fare(
        all_offers, airline, bank=None, fare=fare, segment=segment,
    )
    site = _lead_site(raw)
    matches = _offers_for_site(raw, site)[:4]
    fare_cut = float(flight.get("discount") or 0)
    items = []
    if fare_cut > 0:
        items.append(
            f'<div class="gx-offer"><span class="gx-offer-bank">In this fare</span>'
            f'<span class="gx-offer-text">₹{fare_cut:,.0f} already taken off the list price</span>'
            f'<span class="gx-offer-meta">no code needed</span></div>'
        )
    for o in matches:
        trip_url = ota_search_url_for_offer(o, origin_code, dest_code, book_slots, flight=flight)
        items.append(_offer_html(o, fare, trip_url=trip_url))
    if chosen and chosen.get("co_brand") and chosen["co_brand"].lower() == airline.lower():
        items.append(
            f'<div class="gx-offer"><span class="gx-offer-bank">{html.escape(chosen["bank"])}</span>'
            f'<span class="gx-offer-text">{html.escape(chosen["card_name"])} earns its {html.escape(airline)} rate on top</span>'
            f'<span class="gx-offer-meta">co-branded card</span></div>'
        )
    if not items and chosen:
        items.append(
            f'<div class="gx-offer"><span class="gx-offer-bank">{html.escape(chosen["bank"])}</span>'
            f'<span class="gx-offer-text">No listed offer on this fare right now</span></div>'
        )
    if items:
        st.markdown(
            f'<div class="gx-offers gx-coupons"><div class="gx-offers-h"><span class="gx-coupon-mark">%</span> Bank offers and coupons on {html.escape(site or "this fare")}</div>'
            + "".join(items)
            + "</div>",
            unsafe_allow_html=True,
        )

    # Section 2: cards the traveller doesn't hold that would cut this fare. Opt-in, not a pitch.
    if cards:
        _render_card_suggestions(cards, all_offers, airline, fare, exclude_bank=bank, segment=segment)


def _short_offer_title(title: str) -> str:
    """Scraped offer text can run long ('...; Min ATV – Rs. 7,500 + 3M NC EMI'). Keep the first clause."""
    t = re.split(r"[;|]| — | - (?=[A-Z])", title or "", maxsplit=1)[0].strip()
    t = re.sub(r"\s*\((?:on all|all) [^)]*\)", "", t)
    return (t[:90] + "…") if len(t) > 92 else t


def _render_card_suggestions(cards: list, all_offers: list, airline: str, fare: float,
                             exclude_bank, segment: str = None, limit: int = 3) -> None:
    from src.services.offers import bank_key, offers_by_bank
    by_bank = offers_by_bank(all_offers)
    ranked = []
    for c in cards:
        key = bank_key(c["bank"])
        if exclude_bank and key == bank_key(exclude_bank):
            continue
        bank_deals = offers_for_fare(by_bank.get(key, []), airline, card_name=c["card_name"], fare=fare, segment=segment)
        if not bank_deals:
            continue
        best = bank_deals[0]
        saving = estimate_saving(best, fare) if fare else None
        cobrand = 1 if (c.get("co_brand") or "").lower() == airline.lower() else 0
        ranked.append((cobrand, saving or 0, -(float(c.get("annual_fee") or 0)), c, best, saving))
    ranked.sort(key=lambda t: (t[0], t[1], t[2]), reverse=True)
    picks, seen_banks = [], set()
    for t in ranked:  # one card per bank, so the list reads as choices rather than a catalogue
        key = bank_key(t[3]["bank"])
        if key in seen_banks:
            continue
        seen_banks.add(key)
        picks.append(t)
        if len(picks) >= limit:
            break
    if not picks:
        return
    rows = []
    for _, _, _, c, best, saving in picks:
        fee = c.get("annual_fee")
        fee_txt = "no annual fee" if fee is not None and float(fee) == 0 else (f"₹{float(fee):,.0f}/yr" if fee is not None else "")
        gain = f"about ₹{saving:,.0f} off" if saving else _offer_amount(best, fare)
        listing = c.get("listing_url") or ""
        terms = c.get("terms_url") or listing
        offer_ref = best.get("detail_url") or ""
        refs = []
        if listing:
            refs.append(f'<a class="gx-ref" href="{html.escape(listing)}" target="_blank" rel="noopener">Card</a>')
        if terms and terms != listing:
            refs.append(f'<a class="gx-ref" href="{html.escape(terms)}" target="_blank" rel="noopener">Bank terms</a>')
        if offer_ref:
            refs.append(f'<a class="gx-ref" href="{html.escape(offer_ref)}" target="_blank" rel="noopener">Offer</a>')
        rows.append(
            f'<div class="gx-card">'
            f'<span class="gx-card-name">{html.escape(c["card_name"])}</span>'
            f'<span class="gx-card-gain">{html.escape(gain)}</span>'
            f'<span class="gx-card-meta">{html.escape(fee_txt)}'
            + (" · lounge" if c.get("lounge_access") else "")
            + (f' · apply on {_aggregator_label(best)}' if best else "")
            + (f' · {" · ".join(refs)}' if refs else "")
            + "</span></div>"
        )
    top = picks[0]
    top_card, top_offer, top_saving = top[3], top[4], top[5]
    gain = f"about ₹{top_saving:,.0f} off" if top_saving else _offer_amount(top_offer, fare)
    with st.expander(
        f"{top_card['card_name']} · {gain} on {_aggregator_label(top_offer)}",
        expanded=False,
    ):
        st.markdown('<div class="gx-cards">' + "".join(rows) + "</div>", unsafe_allow_html=True)


@st.dialog("Book this flight", width="medium", on_dismiss=_close_booking)
def _passenger_dialog() -> None:
    """Asked only after a fare is chosen. One screen, one button."""
    flight = st.session_state.get("selected_flight") or {}
    if not flight:
        return
    base_slots = st.session_state.get("slots") or {}
    origin_code, dest_code = route_codes_from_flight(flight, base_slots)
    st.markdown(_ticket_html(flight), unsafe_allow_html=True)
    _render_card_offers(flight)

    booking_dest = dest_code
    city, siblings = sibling_airports(dest_code)
    if len(siblings) > 1:
        names = dict(siblings)
        booking_dest = st.segmented_control(
            f"Arriving in {city}",
            [code for code, _ in siblings],
            default=dest_code,
            key="book_dest_airport",
        ) or dest_code
        if booking_dest == dest_code:
            st.caption(f"{names[dest_code]}. This flight lands here.")
        else:
            st.caption(
                f"{names[booking_dest]}. This flight lands at {dest_code}, "
                f"so Booking.com will show {booking_dest} fares instead."
            )

    existing = (st.session_state.get("slots") or {}).get("passengers") or {}
    with st.container(key="paxrow"):
        a_col, c_col, i_col = st.columns(3)
        adults = a_col.number_input(
            "Adults", 1, 9, max(1, int(existing.get("adults") or 1)), key="book_adults"
        )
        children = c_col.number_input(
            "Children 2–11", 0, 8, max(0, int(existing.get("children") or 0)), key="book_children"
        )
        infants = i_col.number_input(
            "Infants", 0, 4, min(4, max(0, int(existing.get("infants") or 0))), key="book_infants"
        )

    labels = (
        [f"Adult {i + 1}" for i in range(int(adults))]
        + [f"Child {i + 1}" for i in range(int(children))]
        + [f"Infant {i + 1}" for i in range(int(infants))]
    )
    def _touch_name() -> None:
        return None

    with st.container(key="paxnames"):
        names = [
            st.text_input(
                label,
                placeholder=label,
                key=f"traveller_name_{i}",
                label_visibility="collapsed",
                autocomplete="off",
                on_change=_touch_name,
            ).strip()
            for i, label in enumerate(labels)
        ]
    lap_ok = int(infants) <= int(adults)
    filled = [n.casefold() for n in names if n]
    names_ok = len(filled) == len(set(filled))
    if not lap_ok:
        st.caption("Each infant needs an adult lap, so add an adult or remove an infant.")
    elif not names_ok:
        st.caption("Each traveller needs a different name.")

    passengers = {"adults": int(adults), "children": int(children), "infants": int(infants)}
    slots = booking_slots_for_flight(
        flight,
        {**base_slots, "passengers": passengers},
        booking_dest,
    )
    ready = bool(origin_code and booking_dest and slots.get("departure_date") and lap_ok and names_ok)
    url = build_booking_url_for_flight(flight, slots, booking_dest) if ready else ""

    st.session_state.slots = slots
    st.session_state.confirmed_travellers = [n for n in names if n]
    st.session_state.booking_ready_url = url
    if ready and st.session_state.get("conversation_id"):
        signature = (flight.get("offer_token") or flight.get("flight_number"), booking_dest, tuple(passengers.values()))
        if st.session_state.get("selection_saved") != signature:
            st.session_state.selection_saved = signature
            from src.services.supabase_persistence import selection_row
            _persist_selection_later(
                selection_row(st.session_state.conversation_id, flight, passengers, booking_dest, url)
            )

    number = str(flight.get("flight_number") or "").strip()
    with st.container(key="bookcta"):
        st.link_button(
            "This fare on Booking.com",
            url or "#",
            type="primary",
            disabled=not ready,
            width="stretch",
        )
        best = offers_for_fare(
            _card_offers_cached(),
            str(flight.get("airline") or ""),
            fare=float(flight.get("price") or 0),
            segment=route_segment(origin_code, booking_dest),
        )
        best = next((o for o in best if _aggregator_label(o) != "Booking.com"), None)
        if best and ready:
            ota = ota_search_url_for_offer(best, origin_code, booking_dest, slots, flight=flight)
            where = _aggregator_label(best)
            if where != "Booking.com":
                flight_bit = number or (flight.get("airline") or "this flight")
                st.link_button(
                    f"{flight_bit} on {where}",
                    ota,
                    type="secondary",
                    width="stretch",
                )
    if number:
        st.caption(f"Both open {origin_code} → {booking_dest} on {slots.get('departure_date')}. On the site, book {number} at {flight.get('departure_time')}.")


FARE_PAGE = 5
SORT_MODES = ["Best", "Cheapest", "Fastest", "Earliest"]


def _rank_fares(flights: list, mode: str) -> list:
    priced = [f for f in flights if (f.get("price") or 0) > 0]
    if not priced:
        return []

    def minutes(f: dict) -> int:
        return f.get("duration_minutes") or _duration_min(f) or 24 * 60

    def stops(f: dict) -> int:
        s = f.get("stops")
        return s if s is not None else (0 if f.get("non_stop") else 1)

    if mode == "Cheapest":
        return sorted(priced, key=lambda f: (f["price"], minutes(f)))
    if mode == "Fastest":
        return sorted(priced, key=lambda f: (minutes(f), f["price"]))
    if mode == "Earliest":
        return sorted(priced, key=lambda f: (_minutes(f.get("departure_time")) or 24 * 60, f["price"]))
    if any(f.get("fit_score") is not None for f in priced):
        return sorted(priced, key=lambda f: (-(f.get("fit_score") or 0), f["price"]))
    low_p = min(f["price"] for f in priced)
    low_m = min(minutes(f) for f in priced)
    # Price matters most; a long or broken journey has to be much cheaper to win.
    return sorted(
        priced,
        key=lambda f: f["price"] / low_p + 0.6 * minutes(f) / low_m + 0.15 * stops(f),
    )


def _render_fare_list(flights: list) -> None:
    signature = (len(flights), (flights[0] or {}).get("offer_token") if flights else None)
    if st.session_state.get("fares_for") != signature:
        st.session_state.fares_for = signature
        st.session_state.fares_shown = FARE_PAGE

    head, sort_col = st.columns([3, 2], vertical_alignment="center")
    with head:
        st.markdown('<div class="gx-sort-label">Sort</div>', unsafe_allow_html=True)
    with sort_col:
        if "fare_sort" not in st.session_state:
            st.session_state.fare_sort = "Best"
        mode = st.segmented_control(
            "Sort fares", SORT_MODES, key="fare_sort", label_visibility="collapsed",
        ) or st.session_state.fare_sort

    ranked = _rank_fares(flights, mode)
    shown = ranked[: st.session_state.fares_shown]
    for i, f in enumerate(shown):
        number = str(f.get("flight_number") or "").strip()
        cabin = str(f.get("cabin_class") or "economy").replace("_", " ").title()
        score = f.get("fit_score")
        band = str(f.get("fit_band") or "")
        score_bit = f"{score}/10 {band}" if score is not None else ""
        cut = float(f.get("discount") or 0)
        deal = next((str(b.get("text") or b) for b in (f.get("badges") or []) if b), "")
        extra = []
        if cut > 0:
            extra.append(f"{format_flight_price(cut)} off")
        if deal:
            extra.append(deal)
        extra.append("Booking.com")
        sub = " · ".join(p for p in (_duration_label(f), _stops_label(f), cabin, score_bit, *extra) if p)
        with st.container(key=f"farerow{i}"):
            info, price_col, act = st.columns(FARE_COLS, vertical_alignment="center")
            with info:
                st.markdown(
                    '<div class="gx-row">'
                    f'<div class="when"><b>{html.escape(str(f.get("departure_time") or ""))}</b>'
                    f'<span>→</span><b>{html.escape(str(f.get("arrival_time") or ""))}</b></div>'
                    f'<div class="sub">{html.escape(str(f.get("airline") or "Flight"))}'
                    + (f' · {html.escape(number)}' if number else "")
                    + f' · {html.escape(sub)}</div></div>',
                    unsafe_allow_html=True,
                )
            with price_col:
                st.markdown(_price_stack(f), unsafe_allow_html=True)
            with act:
                if st.button("Book", key=f"choose_fare_{i}", type="tertiary"):
                    st.session_state.selected_flight = dict(f)
                    st.session_state.booking_ready_url = None
                    st.rerun()

    remaining = len(ranked) - len(shown)
    if remaining > 0:
        with st.container(key="faremore"):
            if st.button(f"{remaining} more flights", key="fares_more", type="tertiary"):
                st.session_state.fares_shown += FARE_PAGE
                st.rerun()


def _card_picks_for(state: dict) -> dict:
    """Top three cards for what this person has said so far. Off the reply path's model call."""
    try:
        cards = load_credit_cards()
        offers = load_card_offers()
    except Exception:
        return {}
    if not cards:
        return {}
    said = "\n".join(
        m.get("content") or "" for m in (state.get("chat_history") or []) if m.get("role") == "user"
    )
    said = f"{said}\n{state.get('user_message') or ''}"
    slots = state.get("slots") or {}
    origin = ((slots.get("origin") or {}).get("airport_code")) or ""
    dest = ((slots.get("destination") or {}).get("airport_code")) or ""
    flights = state.get("last_search_results") or []
    priced = [f for f in flights if f.get("price")]
    cheapest = min(priced, key=lambda f: float(f["price"])) if priced else {}
    try:
        fare = float(cheapest.get("price") or 0)
    except (TypeError, ValueError):
        fare = 0.0
    return suggest_cards(
        said, cards, offers,
        airline=str(cheapest.get("airline") or ""),
        fare=fare,
        segment=route_segment(str(origin), str(dest)) if origin and dest else "any",
    )


def _render_card_picks() -> None:
    """The card agent's answer, under the fares it was picked against."""
    data = st.session_state.get("card_picks") or {}
    picks = data.get("picks") or []
    if not picks:
        return
    rows = []
    flights = st.session_state.get("last_search_results") or []
    priced = [f for f in flights if f.get("price")]
    anchor = min(priced, key=lambda f: float(f["price"])) if priced else {}
    slots = st.session_state.get("slots") or {}
    o_code, d_code = route_codes_from_flight(anchor, slots) if anchor else ("", "")
    book_slots = booking_slots_for_flight(anchor, slots, d_code) if anchor else slots
    for i, p in enumerate(picks, 1):
        fee = p.get("annual_fee")
        fee_txt = "no annual fee" if fee is not None and float(fee) == 0 else (f"₹{float(fee):,.0f}/yr" if fee is not None else "")
        link = p.get("listing_url") or ""
        refs = []
        if link:
            refs.append(f'<a class="gx-ref" href="{html.escape(link)}" target="_blank" rel="noopener">Card</a>')
        if p.get("terms_url"):
            refs.append(f'<a class="gx-ref" href="{html.escape(p["terms_url"])}" target="_blank" rel="noopener">Bank terms</a>')
        if p.get("merchant") and o_code and d_code:
            offer_stub = {"merchant": p.get("merchant"), "code": p.get("code")}
            trip = ota_search_url_for_offer(offer_stub, o_code, d_code, book_slots, flight=anchor)
            where = _aggregator_label(offer_stub)
            if where != "Booking.com":
                refs.append(
                    f'<a class="gx-ref" href="{html.escape(trip)}" target="_blank" rel="noopener">Open {html.escape(where)}</a>'
                )
        rows.append(
            f'<div class="gx-pick-row">'
            f'<span class="gx-pick-n">{i}</span>'
            f'<span class="gx-pick-name">{html.escape(p["card_name"])}</span>'
            f'<span class="gx-pick-fee">{html.escape(fee_txt)}</span>'
            f'<span class="gx-pick-why">{html.escape(_clip_text(p.get("reason") or ""))}'
            + (f' · {" · ".join(refs)}' if refs else "")
            + "</span></div>"
        )
    top = picks[0]
    saving = top.get("saving")
    gain = f"about ₹{float(saving):,.0f} off" if saving else "a live discount"
    where = _aggregator_label({"merchant": top.get("merchant")}) if top.get("merchant") else "the booking site"
    with st.expander(f"{top['card_name']} · {gain} on {where}", expanded=False):
        st.markdown('<div class="gx-picks">' + "".join(rows) + "</div>", unsafe_allow_html=True)


def _render_search_offers(flights: list) -> None:
    """Coupons for one site, on the same columns as the fare rows above."""
    priced = [f for f in flights if f.get("price")]
    if not priced:
        return
    cheapest = min(priced, key=lambda f: float(f["price"]))
    fare = float(cheapest.get("price") or 0)
    airline = str(cheapest.get("airline") or "")
    origin = str(cheapest.get("origin_code") or "")
    dest = str(cheapest.get("destination_code") or "")
    ranked = offers_for_fare(
        _card_offers_cached(), airline, fare=fare, segment=route_segment(origin, dest),
    )
    site = _lead_site(ranked)
    matches = _offers_for_site(ranked, site)[:4]
    if not matches:
        return
    base_slots = st.session_state.get("slots") or {}
    slots = booking_slots_for_flight(cheapest, base_slots, dest)
    st.markdown(
        f'<div class="gx-section">Bank offers and coupons on {html.escape(site)}</div>',
        unsafe_allow_html=True,
    )
    for i, offer in enumerate(matches):
        code = str(offer.get("code") or "").strip()
        title = _short_offer_title(offer.get("title") or "")
        if code and code in title:
            title = re.sub(rf"\s*[·\-–]?\s*{re.escape(code)}\s*", " ", title).strip(" ·-")
        amount = _offer_amount(offer, fare)
        ref = offer.get("detail_url") or offer.get("terms_url") or ""
        trip = ota_search_url_for_offer(offer, origin, dest, slots, flight=cheapest)
        with st.container(key=f"couponrow{i}"):
            info, price_col, act = st.columns(FARE_COLS, vertical_alignment="center")
            with info:
                st.markdown(
                    '<div class="gx-row">'
                    f'<div class="when"><b>{html.escape(offer.get("bank") or site)}</b>'
                    + (f'<span class="gx-code">{html.escape(code)}</span>' if code else "")
                    + "</div>"
                    f'<div class="sub">{html.escape(title)}</div></div>',
                    unsafe_allow_html=True,
                )
            with price_col:
                st.markdown(
                    f'<div class="gx-price"><b>{html.escape(amount)}</b></div>',
                    unsafe_allow_html=True,
                )
            with act:
                if trip:
                    st.link_button(site, trip, type="tertiary")
                elif ref:
                    st.link_button("Terms", ref, type="tertiary")


def _render_trip_board() -> None:
    slots = st.session_state.get("slots") or {}
    flights = st.session_state.get("last_search_results") or []
    scan = st.session_state.get("window_scan")
    origin = (slots.get("origin") or {}).get("city") or (slots.get("origin") or {}).get("airport_code")
    dest = (slots.get("destination") or {}).get("city") or (slots.get("destination") or {}).get("airport_code")
    dest_code = None
    if scan and (scan.get("series") or []):
        dest_code = scan["series"][0].get("code")
    if not flights and scan:
        _render_window_scan(slots, scan)
        if origin and dest:
            _render_route_experience(slots, dest_code=dest_code)
        return
    if not flights:
        if origin and dest:
            _render_route_experience(slots, dest_code=dest_code)
        return

    date = slots.get("departure_date")
    ret = slots.get("return_date")
    when = date or "Dates still open"
    if ret:
        when = f"{date} → {ret}"
    st.markdown(
        f'<div class="gx-section">Fares <span class="gx-count">{len(flights)}</span></div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        f'<div class="gx-note">{html.escape(_route_label(slots))} · {html.escape(str(when))} · prices from Booking.com</div>',
        unsafe_allow_html=True,
    )
    _render_fare_list(flights)
    _render_search_offers(flights)
    _render_card_picks()
    if origin and dest:
        _render_route_experience(slots, dest_code=dest_code)


def _place_label(slots: dict, key: str) -> str:
    node = (slots or {}).get(key) or {}
    if isinstance(node, dict):
        return node.get("city") or node.get("airport_code") or "not set"
    return str(node or "not set")


def _turn_steps(final_state: dict) -> list:
    """One dropdown per step, the same shape Insight Agent uses for a thought trace."""
    slots = final_state.get("slots") or {}
    missing = final_state.get("missing_slots") or []
    status = final_state.get("status") or "unknown"
    summary = (final_state.get("user_summary") or "").strip()
    results = final_state.get("last_search_results") or []
    steps = [
        {
            "title": "Read the request",
            "body": final_state.get("user_message") or "No message on this turn.",
        },
        {
            "title": "Memory",
            "body": summary or "Nothing saved for this name. A sample city is not read from a profile.",
        },
        {
            "title": "Slots",
            "body": (
                f"From {_place_label(slots, 'origin')}. "
                f"To {_place_label(slots, 'destination')}. "
                f"Date {slots.get('departure_date') or 'not set'}."
            ),
        },
    ]
    if status == "clarification_needed":
        need = ", ".join(missing) if missing else "a route or a date"
        steps.append({
            "title": "Ask for what's missing",
            "body": f"No search on this turn. Still need {need}. The note under this list is that question.",
        })
    elif results:
        steps.append({
            "title": "Search flights",
            "body": f"Live search returned {len(results)} flights.",
        })
    elif status == "error":
        steps.append({
            "title": "Could not read the reply",
            "body": final_state.get("error_message") or "The model reply did not include a usable plan.",
        })
    else:
        steps.append({"title": "Decide", "body": f"Status {status}. No live flight list on this turn."})
    return steps


def _attach_turn_steps(final_state: dict) -> None:
    history = final_state.get("chat_history") or []
    if not history or history[-1].get("role") != "assistant":
        return
    history[-1]["steps"] = _turn_steps(final_state)
    history[-1]["trace"] = {
        "model": MODEL_NAME,
        "status": final_state.get("status"),
        "missing_slots": final_state.get("missing_slots") or [],
        "slots": final_state.get("slots"),
        "user_summary": final_state.get("user_summary"),
        "error": final_state.get("error_message"),
        "result_count": len(final_state.get("last_search_results") or []),
    }


def _screen() -> str:
    raw = st.query_params.get("screen")
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else ""
    return raw or ""


def _layout_for(photo_url: str) -> None:
    """Full-bleed cover (Momentum-style), then a sheet that slides over it."""
    safe_photo = html.escape(photo_url, quote=True)
    st.markdown(
        f"""
        <style>
          [data-testid="stAppViewContainer"] {{
            background: #0e0c0a !important;
            overflow: hidden !important;
            height: 100dvh !important;
          }}
          [data-testid="stMain"] {{
            height: 100dvh !important;
            overflow: hidden !important;
            overscroll-behavior-y: contain;
            scrollbar-width: none;
          }}
          [data-testid="stMain"]::-webkit-scrollbar {{ display: none; }}
          [data-testid="stMainBlockContainer"],
          .block-container {{
            width: 100% !important;
            max-width: none !important;
            min-height: 0 !important;
            margin: 0 !important;
            padding: 0 !important;
            background: transparent !important;
            box-shadow: none !important;
          }}
          .st-key-cover {{
            position: sticky;
            top: 0;
            isolation: isolate;
            min-height: 100dvh;
            height: 100dvh;
            scroll-snap-align: start;
            overflow: hidden;
          }}
          .st-key-cover::before {{
            content: "";
            position: absolute;
            inset: -12%;
            z-index: 0;
            background:
              linear-gradient(180deg,
                rgba(8,6,4,0.55) 0%,
                rgba(8,6,4,0.22) 38%,
                rgba(8,6,4,0.62) 100%),
              url("{safe_photo}") center 42% / cover no-repeat;
            transform: scale(1.035);
            transform-origin: center center;
          }}
          .st-key-cover {{
            display: flex !important;
            flex-direction: column !important;
            justify-content: center !important;
            align-items: center !important;
          }}
          .st-key-cover > div {{ width: 100%; }}
          .st-key-cover [data-testid="stVerticalBlock"] {{
            position: relative;
            z-index: 1;
            align-items: center !important;
            gap: 0 !important;
            padding: 0 1.25rem 7vh !important;
          }}
          .st-key-cover .element-container {{
            width: min(980px, 94vw) !important;
            margin-left: auto !important;
            margin-right: auto !important;
          }}
          .st-key-cover::after {{
            content: "";
            position: absolute;
            inset: 0;
            z-index: 0;
            pointer-events: none;
            background: radial-gradient(ellipse 62% 48% at 50% 46%, rgba(8,6,4,0.46), rgba(8,6,4,0) 100%);
          }}
          .gx-cover-copy {{
            text-align: center;
            color: #fff !important;
            padding: 0 0.5rem;
          }}
          .gx-kicker {{
            display: inline-flex;
            letter-spacing: 0.22em;
            text-transform: uppercase;
            font-size: 0.72rem;
            font-weight: 700;
            margin: 0 0 1.2rem 0;
            color: #fff !important;
            padding: 0.42rem 0.72rem;
            border: 1px solid rgba(255,255,255,0.3);
            border-radius: 999px;
            background: rgba(10,10,10,0.18);
            backdrop-filter: blur(10px);
            text-shadow: 0 2px 14px rgba(0,0,0,0.9);
          }}
          .gx-cover-copy h1 {{
            color: #fff !important;
            font-size: clamp(2.15rem, 4.4vw, 3.65rem);
            font-weight: 700;
            letter-spacing: -0.055em;
            margin: 0;
            line-height: 1.02;
            white-space: nowrap;
            text-shadow: 0 5px 30px rgba(0,0,0,0.9);
            font-family: "Manrope", "Avenir Next", sans-serif !important;
          }}
          .gx-sub {{
            margin: 0.85rem auto 0;
            max-width: none;
            color: #fff !important;
            font-size: 1.05rem;
            font-weight: 600;
            line-height: 1.4;
            white-space: nowrap;
            text-shadow: 0 2px 14px rgba(0,0,0,0.95);
          }}
          .gx-split-title {{
            display: flex;
            justify-content: center;
            align-items: baseline;
            gap: 0.28em;
            flex-wrap: nowrap;
            white-space: nowrap;
          }}
          .gx-half {{
            display: inline-block;
          }}
          .gx-asked {{
            display: inline-block;
            margin: 1.1rem auto 0;
            max-width: 30rem;
            color: #fff;
            font-size: 1rem;
            font-weight: 450;
            line-height: 1.4;
            background: rgba(255,255,255,0.14);
            border: 1px solid rgba(255,255,255,0.28);
            border-radius: 999px;
            padding: 6px 14px;
            backdrop-filter: blur(6px);
          }}
          .gx-cover-note {{
            color: #fff;
            text-align: center;
            font-size: 0.92rem;
            margin-top: 0.6rem;
            text-shadow: 0 1px 10px rgba(0,0,0,0.6);
          }}
          .st-key-coverhint {{
            position: absolute;
            left: 50%;
            bottom: 72px;
            transform: translateX(-50%);
            z-index: 1;
            width: auto !important;
          }}
          .st-key-coverhint [data-testid="stVerticalBlock"] {{
            padding: 0 !important;
          }}
          .gx-scroll-hint {{
            margin: 0;
            color: rgba(255,255,255,0.88);
            font-size: 0.74rem;
            letter-spacing: 0.14em;
            text-transform: uppercase;
            pointer-events: none;
            text-shadow: 0 1px 8px rgba(0,0,0,0.6);
            animation: gx-bob 2.2s ease-in-out infinite;
          }}
          .gx-scroll-hint::after {{
            content: "";
            display: block;
            width: 1px;
            height: 28px;
            margin: 8px auto 0;
            background: linear-gradient(180deg, rgba(255,255,255,0.7), transparent);
          }}
          @keyframes gx-bob {{
            0%, 100% {{ transform: translateX(-50%) translateY(0); }}
            50% {{ transform: translateX(-50%) translateY(6px); }}
          }}
          .st-key-cover .st-key-homebar {{
            width: min(520px, 92vw) !important;
            margin: 1.75rem auto 0 !important;
          }}
          .st-key-cover .st-key-homebar [data-testid="stVerticalBlock"] {{
            min-height: 0 !important;
            padding: 0 !important;
          }}
          .st-key-cover .st-key-homebar [data-testid="stForm"] {{
            background: transparent !important;
            border: none !important;
            border-bottom: 1px solid rgba(255,255,255,0.55) !important;
            border-radius: 0 !important;
            box-shadow: none !important;
            padding: 0 44px 4px 0 !important;
            position: relative;
          }}
          .st-key-cover .st-key-homebar [data-testid="stHorizontalBlock"] {{
            width: 100% !important;
            gap: 0 !important;
          }}
          .st-key-cover .st-key-homebar [data-testid="column"]:last-child {{
            position: absolute !important;
            right: 0;
            top: 50%;
            transform: translateY(-50%);
            width: auto !important;
            flex: 0 0 auto !important;
          }}
          .st-key-cover .st-key-homebar [data-testid="stTextInput"] input {{
            color: #fff !important;
            -webkit-text-fill-color: #fff !important;
            text-align: center;
            font-size: 1.15rem !important;
            font-weight: 600 !important;
            background: transparent !important;
            caret-color: #F5C400;
          }}
          .st-key-cover .st-key-homebar [data-testid="stTextInput"] input::placeholder {{
            color: rgba(255,255,255,0.9) !important;
            opacity: 1 !important;
          }}
          .st-key-cover .st-key-homebar [data-baseweb="input"],
          .st-key-cover .st-key-homebar [data-baseweb="base-input"],
          .st-key-cover .st-key-homebar [data-testid="stTextInput"],
          .st-key-cover .st-key-homebar [data-testid="stTextInput"] > div,
          .st-key-cover .st-key-homebar [data-testid="stTextInput"] div {{
            background: transparent !important;
            border: none !important;
            box-shadow: none !important;
            min-height: 48px !important;
          }}
          .st-key-cover .st-key-homebar [data-testid="stFormSubmitButton"] button {{
            background: #F5C400 !important;
            border: none !important;
            box-shadow: 0 6px 18px rgba(0,0,0,0.25) !important;
          }}
          .st-key-cover .st-key-homebar [data-testid="stFormSubmitButton"] button::after {{
            border-color: #3F3A34 !important;
          }}
          .st-key-sheet {{
            position: fixed;
            inset: 0;
            z-index: 1001;
            min-height: 100dvh;
            margin: 0;
            padding: 0;
            background: #F4F6F8;
            overflow-x: hidden;
            overflow-y: auto;
            overscroll-behavior-y: contain;
            scrollbar-width: thin;
            scrollbar-color: #C9CED6 transparent;
          }}
          .st-key-sheet::before {{
            content: "";
            position: fixed;
            inset: 0 auto 0 0;
            width: 30%;
            background:
              linear-gradient(180deg, rgba(8,12,16,0.22), rgba(8,12,16,0.82)),
              url("{safe_photo}") center / cover no-repeat;
          }}
          .st-key-sheet .st-key-thread {{
            position: relative;
            width: 70%;
            max-width: none;
            height: auto;
            min-height: 100dvh;
            margin: 0 0 0 30%;
            padding: 2rem clamp(2rem, 5vw, 5.5rem) 2.5rem;
            overflow: visible;
            color: #302C27;
            background:
              radial-gradient(circle at 92% 2%, rgba(245,196,0,0.13), transparent 28%),
              #F4F6F8;
          }}
          .gx-journey-rail {{
            position: fixed;
            z-index: 3;
            left: clamp(1.5rem, 3vw, 3.2rem);
            bottom: clamp(2rem, 7vh, 5rem);
            width: min(22%, 260px);
            color: #fff;
          }}
          .gx-journey-rail .k {{
            font-size: 0.68rem;
            font-weight: 600;
            letter-spacing: 0.16em;
            text-transform: uppercase;
          }}
          .gx-journey-rail .n {{
            margin-top: 0.8rem;
            font-family: "Manrope", "Avenir Next", sans-serif;
            font-size: clamp(1.55rem, 2.5vw, 2.65rem);
            font-weight: 700;
            letter-spacing: -0.045em;
            line-height: 1.05;
          }}
          .gx-journey-rail .s {{
            margin-top: 0.8rem;
            color: rgba(255,255,255,0.82);
            font-size: 0.85rem;
            line-height: 1.5;
          }}
          .gx-live-chip {{
            display: inline-flex;
            align-items: center;
            gap: 0.45rem;
            margin-top: 1.1rem;
            padding: 0.42rem 0.65rem;
            border: 1px solid rgba(255,255,255,0.24);
            border-radius: 999px;
            background: rgba(255,255,255,0.1);
            font-size: 0.7rem;
            font-weight: 600;
            backdrop-filter: blur(10px);
          }}
          .gx-live-chip::before {{
            content: "";
            width: 7px;
            height: 7px;
            border-radius: 50%;
            background: #F5C400;
            box-shadow: 0 0 0 4px rgba(245,196,0,0.16);
          }}
          .st-key-sheet .gx-mark {{
            color: #111827;
            font-weight: 700;
          }}
          .st-key-sheet .st-key-composer {{
            position: sticky;
            bottom: 1rem;
            z-index: 8;
          }}
          @media (max-width: 720px) {{
            .st-key-sheet {{ padding-top: 210px; }}
            .st-key-sheet::before {{ width: 100%; height: 210px; bottom: auto; }}
            .st-key-sheet .st-key-thread {{
              width: 100%; min-height: calc(100dvh - 210px); margin: 0;
              padding: 1.2rem 1.15rem 2rem;
            }}
            .gx-journey-rail {{
              position: absolute; top: 88px; bottom: auto; left: 1.4rem; width: 70%;
            }}
            .gx-journey-rail .n {{ font-size: 2rem; }}
          }}
          .gx-verdict {{
            font-size: clamp(1.35rem, 2.6vw, 1.8rem);
            font-weight: 560;
            letter-spacing: -0.02em;
            line-height: 1.25;
            color: #3F3A34;
            margin: 1.4rem 0 0.9rem 0;
            max-width: 34rem;
            animation: gx-rise 0.6s cubic-bezier(0.22, 1, 0.36, 1) both;
          }}
          .gx-path {{
            background: #fff;
            border: 1px solid #E2E6EA;
            border-radius: 20px;
            padding: 1rem 1.1rem 0.9rem;
            min-height: 150px;
            animation: gx-rise 0.6s cubic-bezier(0.22, 1, 0.36, 1) both;
            transition: transform 0.2s ease, box-shadow 0.2s ease;
          }}
          .gx-path:hover {{ transform: translateY(-3px); box-shadow: 0 16px 38px rgba(17,24,39,0.1); }}
          .st-key-path1 .gx-path {{ animation-delay: 0.06s; }}
          .st-key-path2 .gx-path {{ animation-delay: 0.12s; }}
          .st-key-pathactive .gx-path {{ border-color: #E6C56A; box-shadow: 0 10px 28px rgba(230, 168, 0, 0.14); }}
          .gx-path .t {{ font-size: 0.8rem; letter-spacing: 0.08em; text-transform: uppercase; color: #8A8175; }}
          .gx-path-badge {{
            background: #F5C400; color: #3F3A34; border-radius: 999px; padding: 2px 8px;
            font-size: 0.66rem; letter-spacing: 0.06em; margin-left: 6px; vertical-align: middle;
            display: inline-block;
          }}
          .gx-path .p {{ font-size: 1.85rem; font-weight: 560; letter-spacing: -0.03em; color: #1A1714; margin: 0.25rem 0 0.2rem; }}
          .gx-path .w {{ font-size: 0.9rem; color: #5C564E; line-height: 1.4; }}
          .gx-path .n {{ font-size: 0.82rem; color: #8A8175; margin-top: 0.35rem; }}
          [class*="st-key-path"] .stButton > button,
          [class*="st-key-path"] [data-testid="stLinkButton"] a {{
            width: 100%;
            background: #1A1714 !important;
            color: #fff !important;
            border: none !important;
            border-radius: 14px !important;
            min-height: 44px;
            font-weight: 520 !important;
            margin-top: 0.5rem;
          }}
          .st-key-pathactive .stButton > button,
          .st-key-pathactive [data-testid="stLinkButton"] a {{
            background: #F4C84A !important;
            color: #252A30 !important;
          }}
          /* Workspace polish: quiet surfaces, softer controls, stronger hierarchy. */
          .st-key-sheet .st-key-thread {{
            color: #252A30;
          }}
          .st-key-sheet [class*="st-key-reply"] {{
            margin: 1.15rem 0 1.35rem;
            padding: 0.7rem 0.9rem 0.8rem;
            background: rgba(255,255,255,0.88);
            border: 1px solid #E5E9EE;
            border-radius: 18px;
            box-shadow: 0 8px 24px rgba(30,41,59,0.045);
          }}
          .st-key-sheet .gx-q,
          .st-key-sheet .gx-q p {{
            background: #F6D86B;
            color: #1F2328 !important;
            -webkit-text-fill-color: #1F2328 !important;
            border-radius: 16px 16px 5px 16px;
            box-shadow: 0 5px 14px rgba(128,96,0,0.09);
          }}
          .st-key-sheet .gx-q p {{
            margin: 0;
            background: transparent;
            box-shadow: none;
          }}
          .st-key-sheet .gx-verdict {{
            max-width: 39rem;
            color: #20262D;
            font-size: clamp(1.28rem, 2.2vw, 1.65rem);
            font-weight: 650;
            letter-spacing: -0.025em;
            line-height: 1.32;
            margin-top: 1.55rem;
          }}
          .st-key-sheet .gx-path {{
            border: 1px solid #E2E7EC;
            border-radius: 18px;
            box-shadow: 0 7px 22px rgba(30,41,59,0.045);
          }}
          .st-key-sheet .st-key-pathactive .gx-path {{
            border-color: #E8D38A;
            box-shadow: 0 8px 24px rgba(150,112,0,0.08);
          }}
          .st-key-sheet .gx-path .t {{
            color: #7A828C;
            font-weight: 650;
          }}
          .st-key-sheet .gx-path .p {{
            color: #20262D;
            font-size: 1.65rem;
            font-weight: 650;
          }}
          .st-key-sheet .gx-path .w {{ color: #59626D; }}
          .st-key-sheet .gx-path .n {{ color: #7A828C; }}
          [class*="st-key-path"] .stButton > button,
          [class*="st-key-path"] [data-testid="stLinkButton"] a {{
            min-height: 42px;
            border-radius: 12px !important;
            background: #2C333A !important;
            box-shadow: 0 5px 14px rgba(17,24,39,0.1) !important;
            font-size: 0.84rem !important;
            font-weight: 600 !important;
            transition: transform 0.18s ease, box-shadow 0.18s ease, background 0.18s ease;
          }}
          [class*="st-key-path"] .stButton > button:hover,
          [class*="st-key-path"] [data-testid="stLinkButton"] a:hover {{
            transform: translateY(-1px);
            background: #20262D !important;
            box-shadow: 0 8px 20px rgba(17,24,39,0.14) !important;
          }}
          .st-key-sheet .st-key-pathactive .stButton > button,
          .st-key-sheet .st-key-pathactive [data-testid="stLinkButton"] a {{
            background: #F4C84A !important;
            color: #252A30 !important;
            box-shadow: 0 5px 14px rgba(165,126,0,0.13) !important;
          }}
          .st-key-sheet .gx-section {{
            margin-top: 1.15rem;
            color: #303740;
            font-weight: 650;
          }}
          .st-key-sheet .gx-note {{
            color: #77808A;
            font-size: 0.8rem;
            line-height: 1.5;
          }}
          .st-key-sheet .gx-route-photo {{
            background-color: #E9EDF1;
            border: 1px solid #E1E6EB;
            border-radius: 16px;
            box-shadow: 0 8px 24px rgba(30,41,59,0.07);
          }}
          .st-key-sheet .gx-route-stat,
          .st-key-sheet .gx-card {{
            background: rgba(255,255,255,0.9);
            border-color: #E2E7EC;
            border-radius: 14px;
            box-shadow: 0 4px 14px rgba(30,41,59,0.035);
          }}
          .st-key-sheet .gx-card {{
            padding: 0.78rem 0.9rem;
            margin-bottom: 0.45rem;
          }}
          .st-key-sheet .gx-card .who,
          .st-key-sheet .gx-card .fare {{ color: #2C333A; font-weight: 600; }}
          .st-key-sheet .gx-card .meta {{ color: #69727D; }}
          .st-key-sheet [data-testid="stExpander"] details {{
            background: rgba(255,255,255,0.55);
            border: 1px solid #E0E5EA !important;
            border-radius: 13px;
          }}
          .st-key-sheet [data-testid="stExpander"] summary {{
            color: #424A54;
            font-size: 0.88rem;
            font-weight: 550;
          }}
          .gx-maps {{
            display: inline-flex;
            align-items: center;
            gap: 0.45rem;
            margin: 0.15rem 0 0.2rem;
            padding: 0.28rem 0.15rem;
            color: #5C5348 !important;
            font-size: 0.84rem;
            font-weight: 600;
            text-decoration: none !important;
          }}
          .gx-maps svg {{ width: 15px; height: 15px; flex: none; }}
          .gx-maps em {{
            font-style: normal;
            color: #A3988C;
            font-weight: 500;
            font-size: 0.78rem;
          }}
          .gx-maps em::before {{ content: "·"; margin-right: 0.45rem; }}
          .gx-maps:hover span {{ text-decoration: underline; text-underline-offset: 3px; }}
          .st-key-sheet [class*="st-key-trace"] [data-testid="stExpander"] details {{
            background: #FFFCF8 !important;
            border: 1px solid #F3D2B0 !important;
            border-radius: 12px !important;
            box-shadow: none !important;
          }}
          .st-key-sheet [class*="st-key-trace"] [data-testid="stExpander"] details details {{
            background: #FFFFFF !important;
            border: 1px solid #F6E0C8 !important;
            margin: 0.28rem 0.15rem;
          }}
          .st-key-sheet [class*="st-key-trace"] [data-testid="stExpander"] summary {{
            color: #4A4036 !important;
            font-size: 0.82rem !important;
            font-weight: 550 !important;
            min-height: 0 !important;
          }}
          .st-key-sheet [class*="st-key-trace"] [data-testid="stExpander"] summary p {{
            font-size: 0.82rem !important;
            font-weight: 550 !important;
            color: #4A4036 !important;
          }}
          .st-key-sheet [class*="st-key-trace"] [data-testid="stExpander"] summary svg {{
            color: #E08A4F !important;
          }}
          .gx-step {{
            color: #6B6258;
            font-size: 0.84rem;
            font-weight: 500;
            line-height: 1.5;
            padding: 0.15rem 0.1rem 0.35rem;
          }}
          .st-key-sheet [class*="st-key-trace"] [data-testid="stJson"] {{
            background: #FBF7F2 !important;
            border: 1px solid #F3E6D6 !important;
            border-radius: 10px !important;
          }}
          .st-key-sheet [data-testid="stDownloadButton"] button,
          .st-key-sheet [data-testid="stLinkButton"] a {{
            border-color: #D9DFE5;
            color: #3D4650;
            box-shadow: none;
          }}
          .st-key-sheet .st-key-composer [data-testid="stForm"] {{
            border: 1px solid #DEE4EA;
            background: rgba(255,255,255,0.94);
            box-shadow: 0 10px 30px rgba(30,41,59,0.09);
            backdrop-filter: blur(16px);
          }}
          .st-key-sheet .st-key-composer [data-testid="stFormSubmitButton"] button {{
            background: #F4C84A !important;
            box-shadow: 0 5px 14px rgba(165,126,0,0.14) !important;
          }}
          .st-key-sheet .st-key-compare_cabin button {{
            width: auto !important;
            min-height: 36px;
            padding: 0 0.85rem;
            background: rgba(255,255,255,0.7) !important;
            border: 1px solid #DDE3E9 !important;
            color: #46505B !important;
            font-size: 0.78rem;
            box-shadow: none !important;
          }}
          div[role="dialog"] {{
            border-radius: 24px !important;
            border: 1px solid #E1E6EB !important;
            box-shadow: 0 28px 80px rgba(17,24,39,0.18) !important;
          }}
          div[role="dialog"] [data-testid="stWidgetLabel"],
          div[role="dialog"] [data-testid="stWidgetLabel"] p {{
            display: block !important;
            height: auto !important;
            margin: 0 0 0.25rem !important;
            color: #3D4650 !important;
            font-size: 0.82rem !important;
            font-weight: 600 !important;
          }}
          button, input,
          [data-testid="stLinkButton"] a,
          [data-testid="stButtonGroup"] button,
          div[role="dialog"] h2 {{
            font-family: "Manrope", "Avenir Next", "Segoe UI", sans-serif !important;
            -webkit-font-smoothing: antialiased;
          }}
          div[role="dialog"] {{ padding-top: 0.4rem; }}
          div[role="dialog"] h2 {{
            font-size: 1.02rem !important;
            font-weight: 650 !important;
            color: #20262D !important;
            letter-spacing: -0.01em;
          }}
          .gx-ticket {{
            border: 1px solid #E6EAEE;
            border-radius: 16px;
            padding: 0.9rem 1rem 0.8rem;
            background: linear-gradient(180deg, #FFFFFF 0%, #FAFBFC 100%);
          }}
          .gx-offers {{
            margin-top: 0.6rem; padding: 0.85rem 1.05rem 0.7rem;
            border: 1px solid #F3D2B0; border-radius: 14px; background: #FFFCF8;
          }}
          .gx-coupon-mark {{
            display: inline-flex; align-items: center; justify-content: center;
            width: 1.15rem; height: 1.15rem; margin-right: 0.35rem;
            border-radius: 999px; background: #E7F6EE; color: #1F8A4C;
            font-size: 0.72rem; font-weight: 750;
          }}
          .gx-offers-h {{ font-size: 0.84rem; font-weight: 650; letter-spacing: 0; text-transform: none; color: #20262D; margin-bottom: 0.45rem; }}
          .gx-offer {{ display: grid; grid-template-columns: 1fr auto; column-gap: 0.7rem; row-gap: 0.08rem; padding: 0.45rem 0; border-top: 1px dashed #F3D2B0; align-items: baseline; }}
          .gx-offer:first-of-type {{ border-top: 0; }}
          .gx-offer-bank {{ font-size: 0.8rem; font-weight: 650; color: #20262D; }}
          .gx-offer-amt {{ font-size: 0.78rem; font-weight: 650; color: #2E8B57; text-align: right; white-space: nowrap; }}
          .gx-offer-text {{ grid-column: 1 / -1; font-size: 0.82rem; color: #20262D; }}
          .gx-offer-meta {{ grid-column: 1 / -1; font-size: 0.74rem; color: #8A929B; }}
          .gx-ref {{ color: #C56A2D !important; font-weight: 650; text-decoration: none !important; }}
          .gx-ref:hover {{ text-decoration: underline !important; text-underline-offset: 2px; }}
          .gx-sort-label {{ font-size: 0.72rem; font-weight: 650; letter-spacing: 0.04em; text-transform: uppercase; color: #8A929B; }}
          .gx-offer-note {{ margin-top: 0.4rem; font-size: 0.72rem; color: #8A929B; }}
          .gx-cards {{
            margin-top: 0.15rem; padding: 0.35rem 0.35rem 0.15rem;
            border: 0; background: transparent;
          }}
          .gx-cards-h {{ color: #8A929B; }}
          .gx-card {{
            display: grid; grid-template-columns: 1fr auto; column-gap: 0.6rem; row-gap: 0.1rem;
            padding: 0.45rem 0; border-top: 1px solid #F0F2F4; text-decoration: none !important; color: inherit;
          }}
          .gx-card:first-of-type {{ border-top: 0; }}
          .gx-card:hover .gx-card-name {{ color: #E08A4F; }}
          .gx-card-name {{ font-size: 0.86rem; font-weight: 650; color: #20262D; }}
          .gx-card-gain {{ font-size: 0.78rem; font-weight: 650; color: #2E8B57; white-space: nowrap; text-align: right; }}
          .gx-card-pitch {{ grid-column: 1 / -1; font-size: 0.78rem; color: #4B535C; line-height: 1.35; }}
          .gx-card-meta {{ grid-column: 1 / -1; font-size: 0.72rem; color: #8A929B; }}
          .gx-picks, .gx-stepbox {{
            margin: 0.85rem 0 0.4rem; padding: 0.75rem 0.95rem 0.6rem;
            border: 1px solid #E6EAEE; border-radius: 14px; background: #FFFFFF;
          }}
          .gx-pick-line {{ font-size: 0.78rem; color: #8A929B; margin: -0.1rem 0 0.2rem; }}
          .gx-picks .gx-pick-row {{
            display: grid; grid-template-columns: 1.15rem 1fr auto; column-gap: 0.6rem; row-gap: 0.12rem;
            padding: 0.55rem 0; border-top: 1px solid #F0F2F4; align-items: baseline;
            background: transparent !important; border-radius: 0 !important;
          }}
          .gx-picks .gx-pick-row:first-of-type {{ border-top: 0; }}
          .gx-trip-hint {{ color: #69727D; font-weight: 550; }}
          .gx-ref-trip {{ font-weight: 700 !important; }}
          .gx-pick-n {{ font-size: 0.74rem; font-weight: 650; color: #E08A4F; }}
          .gx-pick-name {{ font-size: 0.88rem; font-weight: 650; color: #20262D; }}
          .gx-pick-fee {{ font-size: 0.74rem; color: #8A929B; white-space: nowrap; text-align: right; }}
          .gx-pick-why {{ grid-column: 2 / -1; font-size: 0.78rem; color: #4B535C; line-height: 1.4; }}
          .gx-ticket .top {{ display: flex; justify-content: space-between; align-items: baseline; }}
          .gx-ticket .who {{ font-weight: 650; color: #20262D; font-size: 0.92rem; }}
          .gx-ticket .who em {{ font-style: normal; color: #8A929B; font-weight: 550; margin-left: 0.25rem; }}
          .gx-price {{
            display: flex; flex-direction: column; align-items: flex-end; line-height: 1.15;
          }}
          .gx-price small {{ font-size: 0.7rem; color: #2F7D4F; font-weight: 650; }}
          .gx-price s {{ font-size: 0.75rem; color: #9AA2AB; font-weight: 550; text-decoration: line-through; }}
          .gx-price b {{ font-size: 1rem; font-weight: 700; color: #20262D; }}
          .gx-ticket .gx-price b {{ font-size: 1.12rem; }}
          .gx-ticket .leg {{
            display: grid; grid-template-columns: auto 1fr auto; gap: 0.8rem;
            align-items: center; margin: 0.75rem 0 0.55rem;
          }}
          .gx-ticket .leg b {{ display: block; font-size: 1.25rem; font-weight: 650; color: #20262D; line-height: 1.1; }}
          .gx-ticket .leg span {{ font-size: 0.74rem; color: #7A828C; font-weight: 600; letter-spacing: 0.06em; }}
          .gx-ticket .leg .end {{ text-align: right; }}
          .gx-ticket .line {{ position: relative; text-align: center; }}
          .gx-ticket .line i {{ display: block; height: 1px; background: #D5DBE1; margin: 0 0 0.3rem; }}
          .gx-ticket .line small {{ font-size: 0.72rem; color: #7A828C; font-weight: 550; }}
          .gx-ticket .meta {{
            border-top: 1px dashed #E3E7EB; padding-top: 0.55rem;
            font-size: 0.76rem; color: #69727D; font-weight: 550;
          }}
          div[role="dialog"] [data-testid="stCaptionContainer"] p {{
            font-size: 0.74rem !important; color: #7A828C !important; line-height: 1.45;
          }}
          div[role="dialog"] [data-testid="stNumberInput"] input {{
            text-align: center; font-weight: 600; color: #20262D; height: 36px;
          }}
          div[role="dialog"] [data-testid="stNumberInputContainer"] {{
            border-radius: 11px !important; border-color: #E1E6EB !important; background: #FFFFFF;
          }}
          div[role="dialog"] [data-testid="stNumberInput"] button {{ background: transparent !important; }}
          .st-key-paxnames [data-testid="stTextInput"] > div {{ margin-bottom: -0.35rem; }}
          .st-key-paxnames [data-baseweb="input"] {{
            border-radius: 11px !important; border-color: #E1E6EB !important; background: #FFFFFF !important;
          }}
          .st-key-paxnames input {{ font-size: 0.86rem !important; height: 40px; }}
          .st-key-paxnames input::placeholder {{ color: #A0A7AF; }}
          [data-testid="stButtonGroup"] button {{
            min-height: 32px !important;
            padding: 0 0.85rem !important;
            border-color: #E1E6EB !important;
            background: #FFFFFF !important;
            color: #4A535D !important;
            font-size: 0.78rem !important;
            font-weight: 600 !important;
          }}
          [data-testid="stButtonGroup"] button[kind*="Active"] {{
            background: #20262D !important;
            border-color: #20262D !important;
            color: #FFFFFF !important;
          }}
          .st-key-bookcta [data-testid="stLinkButton"]:first-of-type a {{
            min-height: 46px;
            border: 0 !important;
            border-radius: 13px !important;
            background: #20262D !important;
            color: #FFFFFF !important;
            font-weight: 650 !important;
            font-size: 0.9rem !important;
            box-shadow: 0 8px 20px rgba(32,38,45,0.18) !important;
            transition: transform 0.16s ease, box-shadow 0.16s ease;
          }}
          .st-key-bookcta [data-testid="stLinkButton"]:first-of-type a:hover {{
            transform: translateY(-1px);
            box-shadow: 0 12px 26px rgba(32,38,45,0.22) !important;
          }}
          .st-key-bookcta [data-testid="stLinkButton"]:not(:first-of-type) a {{
            min-height: 44px;
            border: 1px solid #D5DCE3 !important;
            border-radius: 13px !important;
            background: #FFFFFF !important;
            color: #20262D !important;
            font-weight: 600 !important;
            font-size: 0.86rem !important;
            box-shadow: none !important;
          }}
          .st-key-bookcta [data-testid="stLinkButton"] a[disabled],
          .st-key-bookcta [data-testid="stLinkButton"] a[aria-disabled="true"] {{
            background: #EEF1F4 !important; color: #9AA2AB !important; box-shadow: none !important;
            border-color: #EEF1F4 !important;
          }}
          div[role="dialog"] .gx-offers,
          div[role="dialog"] .gx-cards,
          div[role="dialog"] .gx-picks {{
            padding-left: 0.15rem;
            padding-right: 0.35rem;
          }}
          div[role="dialog"] [data-testid="stExpander"] details {{
            border-color: #E6EAEE !important;
            background: #FFFFFF;
          }}
          div[role="dialog"] [data-testid="stExpander"] summary {{
            font-size: 0.86rem !important;
            font-weight: 650 !important;
            color: #20262D !important;
          }}
          .gx-count {{
            display: inline-block; margin-left: 0.35rem; padding: 0.05rem 0.5rem;
            border-radius: 999px; background: #EAEEF2; color: #5B646E;
            font-size: 0.72rem; font-weight: 650; vertical-align: 0.1rem;
          }}
          .st-key-sheet [class*="st-key-farerow"] {{
            background: transparent;
            border: 0;
            border-bottom: 1px solid #EEF1F4;
            border-radius: 0;
            padding: 0.15rem 0 0.15rem;
            margin: 0;
            box-shadow: none;
          }}
          .st-key-sheet [class*="st-key-farerow"]:hover {{
            border-color: #EEF1F4;
            box-shadow: none;
            background: transparent;
          }}
          .gx-row .when {{ display: flex; gap: 0.45rem; align-items: baseline; }}
          .gx-row .when b {{ font-size: 1rem; font-weight: 650; color: #20262D; }}
          .gx-row .when span {{ color: #A0A7AF; font-size: 0.8rem; }}
          .gx-code {{
            margin-left: 0.45rem;
            padding: 0.05rem 0.4rem;
            border-radius: 6px;
            background: #F4F6F8;
            color: #3D4650 !important;
            font-size: 0.72rem !important;
            font-weight: 700;
            letter-spacing: 0.03em;
          }}
          .gx-row .sub {{ font-size: 0.76rem; color: #6F7882; font-weight: 550; margin-top: 0.1rem; }}
          .st-key-sheet .gx-price {{ white-space: nowrap; }}
          .st-key-sheet [class*="st-key-farerow"] .stButton > button {{
            min-height: 0 !important;
            height: auto !important;
            padding: 0.15rem 0 !important;
            border: 0 !important;
            border-radius: 0 !important;
            background: transparent !important;
            color: #C56A2D !important;
            font-size: 0.84rem !important;
            font-weight: 650 !important;
            box-shadow: none !important;
            justify-content: flex-end;
          }}
          .st-key-sheet [class*="st-key-farerow"] .stButton > button:hover {{
            background: transparent !important;
            color: #20262D !important;
            text-decoration: underline;
            text-underline-offset: 3px;
          }}
          .st-key-sheet [class*="st-key-couponrow"] {{
            background: transparent;
            border: 0;
            border-bottom: 1px solid #EEF1F4;
            border-radius: 0;
            padding: 0.15rem 0;
            margin: 0;
          }}
          .st-key-sheet [class*="st-key-couponrow"] [data-testid="stLinkButton"] a {{
            min-height: 0 !important;
            height: auto !important;
            padding: 0.15rem 0 !important;
            border: 0 !important;
            background: transparent !important;
            color: #C56A2D !important;
            font-size: 0.84rem !important;
            font-weight: 650 !important;
            box-shadow: none !important;
            justify-content: flex-end;
          }}
          .st-key-sheet [class*="st-key-couponrow"] .gx-price b {{
            color: #2E8B57;
            font-size: 0.92rem;
          }}
          .st-key-sheet .st-key-faremore {{ display: flex; justify-content: flex-start; margin: 0.1rem 0 0.2rem; }}
          .st-key-sheet .st-key-faremore button {{
            min-height: 0 !important; padding: 0.15rem 0 !important;
            background: transparent !important; border: 0 !important; box-shadow: none !important;
            color: #8A929B !important; font-size: 0.8rem !important; font-weight: 600 !important;
          }}
          .st-key-sheet .st-key-faremore button:hover {{ color: #20262D !important; }}
          @keyframes gx-rise {{
            from {{ opacity: 0; transform: translateY(24px); }}
            to {{ opacity: 1; transform: none; }}
          }}
          .st-key-snap, .st-key-snap iframe,
          .st-key-depart, .st-key-depart iframe,
          .st-key-transitionclear, .st-key-transitionclear iframe {{
            height: 0 !important;
            min-height: 0 !important;
            overflow: hidden !important;
          }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def _split_name_and_trip(text: str) -> tuple[str, str]:
    """'Aditi · Hanoi in November' → name, trip. A bare name stays on the cover."""
    raw = (text or "").strip()
    for sep in (" · ", "·", ",", " — ", " - "):
        if sep in raw:
            left, right = raw.split(sep, 1)
            left, right = left.strip(), right.strip()
            if left and right and len(left.split()) <= 3:
                return left, right
    if len(raw.split()) <= 2:
        return raw, ""
    return "", ""


def _greeting() -> str:
    hour = time.localtime().tm_hour
    return "Good morning" if hour < 12 else ("Good afternoon" if hour < 18 else "Good evening")


def _split_heading(name: str) -> str:
    """A time-aware welcome makes giving a name immediately rewarding."""
    greeting = _greeting()
    if name:
        return (
            f'<h1 class="gx-split-title">'
            f'<span class="gx-half left">{greeting},</span>'
            f'<span class="gx-half right">{html.escape(name)}.</span></h1>'
        )
    return (
        '<h1 class="gx-split-title">'
        f'<span class="gx-half left">{greeting}.</span></h1>'
    )


def _first_ask() -> str:
    for msg in st.session_state.get("chat_history") or []:
        if msg.get("role") == "user" and (msg.get("content") or "").strip():
            return msg["content"].strip()
    return ""


def _begin_from_cover(text: str) -> None:
    text = (text or "").strip()
    if not text:
        return
    name = (st.session_state.user_name or "").strip()
    query = text
    if not name:
        # Name capture is its own small moment on the cover. It is never parsed
        # as a trip, so the next field can be unambiguously about travel.
        st.session_state.user_name = text[:40].strip()
        st.session_state.cover_note = ""
        st.rerun()
        return
    else:
        st.session_state.cover_note = ""
        # "Aditi · Hanoi in November" from a signed-in user updates the name too.
        maybe_name, maybe_trip = _split_name_and_trip(text)
        if maybe_name and maybe_trip and "·" in text:
            st.session_state.user_name = maybe_name
            query = maybe_trip
    if not query:
        st.rerun()
        return
    # This form is above the workspace in the same run, so execution can
    # continue directly into the sheet without paying for a second rerun.
    _queue_turn(query, rerun=False)


def _arm_cover_departure(name: str, photo_url: str) -> None:
    """Paint the workspace immediately while Streamlit completes its rerun."""
    import streamlit.components.v1 as components

    name_js = json.dumps(name).replace("<", "\\u003c")
    greeting_js = json.dumps(_greeting())
    photo_js = json.dumps(photo_url).replace("<", "\\u003c")
    with st.container(key="depart"):
        components.html(
            f"""
            <script>
            const doc = window.parent.document;
            const form = doc.querySelector('.st-key-homebar form');
            if (form && !form.dataset.gxLeave) {{
              form.dataset.gxLeave = '1';
              form.addEventListener('submit', () => {{
                const query = form.querySelector('input')?.value?.trim();
                if (!query || doc.getElementById('gx-transition-shell')) return;
                if (!doc.getElementById('gx-transition-style')) {{
                  const style = doc.createElement('style');
                  style.id = 'gx-transition-style';
                  style.textContent = `
                    #gx-transition-shell{{position:fixed;inset:0;z-index:1000;display:grid;
                      grid-template-columns:30% 70%;background:#f4f6f8;
                      animation:gxTsIn .55s cubic-bezier(.16,1,.3,1) both;font-family:Manrope,Arial,sans-serif}}
                    @keyframes gxTsIn{{from{{transform:translateY(100%)}}to{{transform:none}}}}
                    #gx-transition-shell .rail{{position:relative;background-size:cover;background-position:center;overflow:hidden}}
                    #gx-transition-shell .rail:after{{content:"";position:absolute;inset:0;background:linear-gradient(180deg,rgba(8,12,16,.2),rgba(8,12,16,.82))}}
                    #gx-transition-shell .railcopy{{position:absolute;z-index:1;left:10%;right:10%;bottom:9%;color:white}}
                    #gx-transition-shell .eyebrow{{font-size:11px;font-weight:700;letter-spacing:.16em;text-transform:uppercase}}
                    #gx-transition-shell .name{{font-size:clamp(24px,3vw,42px);font-weight:700;line-height:1.05;letter-spacing:-.045em;margin-top:12px}}
                    #gx-transition-shell .desk{{padding:34px clamp(32px,5vw,88px);background:radial-gradient(circle at 92% 2%,rgba(245,196,0,.14),transparent 28%),#f4f6f8}}
                    #gx-transition-shell .brand{{font-size:14px;font-weight:700;color:#111827}}
                    #gx-transition-shell .load{{height:75%;display:flex;flex-direction:column;justify-content:center;max-width:520px}}
                    #gx-transition-shell .query{{width:max-content;max-width:100%;padding:8px 12px;border-radius:12px;background:#f5c400;color:#302c27;font-size:14px}}
                    #gx-transition-shell h2{{font-size:clamp(28px,4vw,48px);line-height:1.05;letter-spacing:-.045em;color:#111827;margin:24px 0 10px}}
                    #gx-transition-shell p{{color:#667085;margin:0}}
                    #gx-transition-shell .pulse{{width:44px;height:4px;border-radius:9px;background:#dfe3e8;margin-top:28px;overflow:hidden}}
                    #gx-transition-shell .pulse:after{{content:"";display:block;width:55%;height:100%;background:#f5c400;animation:gxTsPulse 1s ease-in-out infinite alternate}}
                    @keyframes gxTsPulse{{to{{transform:translateX(82%)}}}}
                    @media(max-width:720px){{#gx-transition-shell{{grid-template-columns:1fr;grid-template-rows:210px 1fr}}#gx-transition-shell .desk{{padding:24px}}}}
                  `;
                  doc.head.appendChild(style);
                }}
                const shell = doc.createElement('div');
                shell.id = 'gx-transition-shell';
                shell.innerHTML = '<aside class="rail"><div class="railcopy"><div class="eyebrow">Your journey</div><div class="name"></div></div></aside>'
                  + '<main class="desk"><div class="brand">GarudaX <small> BETA</small></div><div class="load"><div class="query"></div>'
                  + '<h2>Opening your flight desk…</h2><p>Getting the route ready while the live search starts.</p><div class="pulse"></div></div></main>';
                shell.querySelector('.rail').style.backgroundImage = `url("${{{photo_js}}}")`;
                shell.querySelector('.name').textContent = {greeting_js} + ', ' + {name_js} + '.';
                shell.querySelector('.query').textContent = query;
                doc.body.appendChild(shell);
                window.setTimeout(() => shell.remove(), 12000);
              }});
            }}
            </script>
            """,
            height=0,
            width=0,
        )


def _clear_transition_shell() -> None:
    """Remove the optimistic browser shell once the real workspace is mounted."""
    import streamlit.components.v1 as components

    with st.container(key="transitionclear"):
        components.html(
            """
            <script>
            const doc = window.parent.document;
            doc.getElementById('gx-transition-shell')?.remove();
            doc.getElementById('gx-transition-style')?.remove();
            </script>
            """,
            height=0,
            width=0,
        )


def _snap_scroll(which: str) -> None:
    """Smooth-scroll the search sheet or the cover. One shot, so later refreshes stay put."""
    import streamlit.components.v1 as components

    target = "sheet" if which == "sheet" else "cover"
    with st.container(key="snap"):
        components.html(
            f"""
            <script>
            const doc = window.parent.document;
            const main = doc.querySelector('[data-testid="stMain"]');
            const el = doc.querySelector('.st-key-{target}');
            if (main && el) {{
              const top = el.getBoundingClientRect().top - main.getBoundingClientRect().top + main.scrollTop;
              main.scrollTo({{ top: Math.max(0, top), behavior: 'smooth' }});
            }}
            </script>
            """,
            height=0,
            width=0,
        )


def _ask_box(form_key: str) -> None:
    """Composer under the thread. The circle on the right runs the search."""
    with st.container(key="composer"):
        with st.form(form_key, clear_on_submit=True, border=False):
            field, send = st.columns([12, 1], vertical_alignment="center")
            with field:
                draft = st.text_input(
                    "Where to, and when?",
                    placeholder="Where to, and when?",
                    label_visibility="collapsed",
                )
            with send:
                go = st.form_submit_button("Search")
    if go and (draft or "").strip():
        _queue_turn((draft or "").strip())


def _user_line(msg: dict, with_stop: bool) -> None:
    """User chip. While this turn is running, Stop sits on its right."""
    bubble = f'<div class="gx-q">{html.escape(msg["content"])}</div>'
    if not with_stop:
        st.markdown(bubble, unsafe_allow_html=True)
        return
    job = st.session_state.get("job") or {}
    with st.container(key="liveq"):
        text_col, stop_col = st.columns([8, 1], vertical_alignment="center")
        with text_col:
            st.markdown(bubble, unsafe_allow_html=True)
        with stop_col:
            if st.button("Stop", key="stop_processing"):
                job["stopped"] = True
                job["done"] = True
                request_stop()
                st.session_state.stopped_note = True
                st.session_state.pending_turn = None
                st.rerun()


def _render_turn_steps(msg: dict, idx: int, show_raw: bool) -> None:
    if not show_raw:
        return
    steps = msg.get("steps") or []
    if not steps and not msg.get("trace"):
        return
    with st.container(key=f"trace{idx}"):
        with st.expander(f"Thought · {len(steps)} steps", expanded=False):
            for step in steps:
                with st.expander(step["title"], expanded=False):
                    st.markdown(
                        f'<div class="gx-step">{html.escape(step["body"])}</div>',
                        unsafe_allow_html=True,
                    )
            if show_raw and msg.get("trace"):
                with st.expander("Raw JSON", expanded=False):
                    st.json(msg["trace"])


def main() -> None:
    st.set_page_config(
        page_title="GarudaX · Beta",
        page_icon="✦",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    _inject_styles()

    if "chat_history" not in st.session_state:
        st.session_state.chat_history = []
    if "conversation_id" not in st.session_state:
        st.session_state.conversation_id = str(uuid.uuid4())
    if "turn_index" not in st.session_state:
        st.session_state.turn_index = 1
    if "user_name" not in st.session_state:
        st.session_state.user_name = ""
    if "slots" not in st.session_state:
        st.session_state.slots = {**DEFAULT_SLOTS}
    if "is_calling_model" not in st.session_state:
        st.session_state.is_calling_model = False
    if "last_search_results" not in st.session_state:
        st.session_state.last_search_results = None
    if "last_search_params" not in st.session_state:
        st.session_state.last_search_params = None
    if "search_history" not in st.session_state:
        st.session_state.search_history = []
    if "error_context" not in st.session_state:
        st.session_state.error_context = None
    if "price_stats" not in st.session_state:
        st.session_state.price_stats = None
    if "window_scan" not in st.session_state:
        st.session_state.window_scan = None
    if "suggested_alternatives" not in st.session_state:
        st.session_state.suggested_alternatives = None
    if "user_summary" not in st.session_state:
        st.session_state.user_summary = None
    if "rate_limit_until" not in st.session_state:
        st.session_state.rate_limit_until = 0.0
    if "pending_turn" not in st.session_state:
        st.session_state.pending_turn = None
    if "agent_trace" not in st.session_state:
        st.session_state.agent_trace = None
    if "selected_flight" not in st.session_state:
        st.session_state.selected_flight = None
    if "booking_ready_url" not in st.session_state:
        st.session_state.booking_ready_url = None
    if "confirmed_travellers" not in st.session_state:
        st.session_state.confirmed_travellers = []
    if st.session_state.get("bg_url") not in BACKGROUNDS:
        st.session_state.bg_url = random.choice(BACKGROUNDS)
    if "cover_note" not in st.session_state:
        st.session_state.cover_note = ""
    if LANGGRAPH_AVAILABLE and "flight_graph" not in st.session_state:
        try:
            st.session_state.flight_graph = create_flight_finder_graph()
        except Exception as e:
            logger.error("Failed to init LangGraph: %s", e)
            st.warning("Could not initialize LangGraph. Using manual handling.")

    name = (st.session_state.user_name or "").strip()
    photo_url = st.session_state.bg_url
    _layout_for(photo_url)
    debug_ok = _debug_allowed(name)
    brand = '<span class="gx-mark">GarudaX</span><span class="gx-beta">Beta</span>'
    asked = _first_ask()
    asked_html = f'<p class="gx-asked">{html.escape(asked)}</p>' if asked else ""
    sub = (
        "Start with the trip in your own words. We’ll shape the rest together."
        if name
        else "What should we call you?"
    )
    with st.container(key="cover"):
        st.markdown(
            f'<div class="gx-cover-copy"><p class="gx-kicker">GarudaX</p>{_split_heading(name)}'
            f'<p class="gx-sub">{sub}</p>'
            f"{asked_html}</div>",
            unsafe_allow_html=True,
        )
        if not name:
            with st.container(key="homebar"):
                with st.form("home_name", clear_on_submit=True, border=False):
                    field, go_col = st.columns([12, 1], vertical_alignment="center")
                    with field:
                        name_input = st.text_input(
                            "Your name",
                            placeholder="Your first name",
                            label_visibility="collapsed",
                        )
                    with go_col:
                        go = st.form_submit_button("Continue")
            if go:
                _begin_from_cover(name_input or "")
        else:
            with st.container(key="homebar"):
                with st.form("trip_query", clear_on_submit=True, border=False):
                    field, go_col = st.columns([12, 1], vertical_alignment="center")
                    with field:
                        trip_input = st.text_input(
                            "Your trip",
                            placeholder="Try: Vietnam next month from Delhi",
                            label_visibility="collapsed",
                        )
                    with go_col:
                        trip_go = st.form_submit_button("Continue")
            if trip_go:
                _begin_from_cover(trip_input or "")
        if st.session_state.get("cover_note"):
            st.markdown(
                f'<p class="gx-cover-note">{html.escape(st.session_state.cover_note)}</p>',
                unsafe_allow_html=True,
            )
        if name:
            _arm_cover_departure(name, photo_url)

    sheet_empty = not (
        st.session_state.chat_history
        or st.session_state.pending_turn
        or st.session_state.get("job")
    )
    # The sheet is not a second landing page. It only exists after the first
    # request, so there is nothing to reveal by manually scrolling the cover.
    if sheet_empty:
        return

    with st.container(key="sheet"):
        _clear_transition_shell()
        st.markdown(
            f'<aside class="gx-journey-rail"><div class="k">Your journey</div>'
            f'<div class="n">{html.escape(_greeting())},<br>{html.escape(name)}.</div>'
            '<div class="s">Ask, compare and refine without leaving the trip.</div>'
            '<div class="gx-live-chip">Live workspace</div></aside>',
            unsafe_allow_html=True,
        )
        with st.container(key="thread"):
            bar_l, bar_r = st.columns([3, 2], vertical_alignment="center")
            with bar_l:
                st.markdown(f'<div>{brand}</div>', unsafe_allow_html=True)
            with bar_r:
                dev_col, chat_col = st.columns([1.15, 1], vertical_alignment="center")
                with dev_col:
                    if debug_ok:
                        st.toggle("DEV MODE", key="debug_on")
                    else:
                        st.toggle("DEV MODE", value=False, disabled=True, key="dev_mode_locked")
                with chat_col:
                    new_chat = st.button("New chat", key="new_chat", type="primary")
                if new_chat:
                    request_stop()
                    running = st.session_state.get("job")
                    if running is not None:
                        running["stopped"] = True
                    st.session_state.job = None
                    st.session_state.is_calling_model = False
                    st.session_state.chat_history = []
                    st.session_state.user_name = ""
                    st.session_state.slots = {**DEFAULT_SLOTS}
                    st.session_state.conversation_id = str(uuid.uuid4())
                    st.session_state.turn_index = 1
                    st.session_state.last_search_results = None
                    st.session_state.last_search_params = None
                    st.session_state.search_history = []
                    st.session_state.error_context = None
                    st.session_state.price_stats = None
                    st.session_state.window_scan = None
                    st.session_state.suggested_alternatives = None
                    st.session_state.pending_turn = None
                    st.session_state.pending_shown = False
                    st.session_state.stopped_note = False
                    st.session_state.agent_trace = None
                    st.session_state.card_picks = None
                    st.session_state.selected_flight = None
                    st.session_state.booking_ready_url = None
                    st.session_state.confirmed_travellers = []
                    st.rerun()

            pending = st.session_state.pending_turn
            already = st.session_state.get("job")
            if already and (already.get("stopped") or already.get("done")):
                if already.get("stopped"):
                    request_stop()
                    st.session_state.stopped_note = True
                _consume_job()
                already = None

            if pending and not already:
                now = time.time()
                cooldown_until = float(st.session_state.get("rate_limit_until") or 0)
                st.session_state.pending_turn = None
                st.session_state.pending_shown = False
                if now < cooldown_until:
                    remaining = int(cooldown_until - now) + 1
                    st.warning(f"Give it about {remaining}s, then try once more.")
                else:
                    logger.info("Processing user message (len=%d) on a background turn", len(pending))
                    _start_job(pending)

            job = st.session_state.get("job")
            job_running = bool(job) and not job.get("done") and not job.get("stopped")
            show_raw = bool(debug_ok and st.session_state.get("debug_on"))
            history = st.session_state.chat_history
            live_idx = None
            if job_running:
                for i in range(len(history) - 1, -1, -1):
                    if history[i]["role"] == "user":
                        live_idx = i
                        break
            for idx, msg in enumerate(history):
                if msg["role"] == "user":
                    _user_line(msg, with_stop=idx == live_idx)
                else:
                    with st.container(key=f"reply{idx}"):
                        _render_turn_steps(msg, idx, show_raw)
                        st.markdown(msg["content"])

            if not job_running:
                _render_paths()
            _render_trip_board()
            _results = st.session_state.get("last_search_results") or []
            if _results:
                st.download_button(
                    label="Download these flights",
                    data=flights_to_csv(_results),
                    mime="text/csv",
                    file_name="garudax_flights.csv",
                    key="download_flights_csv",
                )
            if job_running:
                _watch_job()
                label = (st.session_state.get("job") or {}).get("status_label") or "Looking…"
                st.markdown(
                    f'<p class="gx-wait"><span class="gx-pulse"></span>{html.escape(label)}</p>',
                    unsafe_allow_html=True,
                )
            elif st.session_state.get("stopped_note"):
                st.markdown(
                    '<p class="gx-wait">Stopped. Change the trip below and search again.</p>',
                    unsafe_allow_html=True,
                )
            if not job_running:
                _ask_box("reply")

    if st.session_state.get("selected_flight"):
        _passenger_dialog()

    st.session_state.pop("snap_down", None)
    st.session_state.pop("snap_up", None)


if __name__ == "__main__":
    main()
