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

import json
import logging
import random
import re
import uuid

import streamlit as st

# Status messages shown at random while processing
STATUS_MESSAGES = [
    "Working on the magic…",
    "Finding all possible flights across airports…",
    "Checking the best routes for you…",
    "Almost there — polishing results…",
]

try:
    for key, value in st.secrets.items():
        if isinstance(value, str) and key not in os.environ:
            os.environ[key] = value
except Exception:
    pass

from src.config import DEFAULT_SLOTS, MODEL_NAME, MAX_HISTORY, MAX_TOKENS_LLM, SYSTEM_PROMPT
from src.services.flight_services import (
    call_mistral_with_backoff,
    extract_conversational_message,
    extract_json_from_response,
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


def _append_message(role: str, content: str) -> None:
    """Append to chat_history and persist to Supabase if configured. Uses turn_index and user_name; increments after assistant."""
    turn_index = st.session_state.get("turn_index", 1)
    user_name = st.session_state.get("user_name") or None
    st.session_state.chat_history.append({"role": role, "content": content})
    try:
        from src.services.supabase_persistence import persist_message
        persist_message(
            st.session_state.conversation_id,
            role,
            content,
            st.session_state.slots,
            turn_index=turn_index,
            user_name=user_name,
        )
    except Exception as e:
        logger.debug("Supabase persist skipped: %s", e)
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
    st.session_state.chat_history = final_state["chat_history"]
    st.session_state.slots = final_state["slots"]
    if final_state["chat_history"]:
        last_msg = final_state["chat_history"][-1]
        turn_index = st.session_state.get("turn_index", 1)
        user_name = st.session_state.get("user_name") or None
        try:
            from src.services.supabase_persistence import persist_message
            persist_message(
                st.session_state.conversation_id,
                last_msg["role"],
                last_msg["content"],
                final_state["slots"],
                turn_index=turn_index,
                user_name=user_name,
            )
        except Exception:
            pass
        st.session_state.turn_index = turn_index + 1
    st.session_state.last_search_results = final_state.get("last_search_results")
    st.session_state.last_search_params = final_state.get("last_search_params")
    st.session_state.price_stats = final_state.get("price_stats")
    st.session_state.error_context = final_state.get("error_context")
    st.session_state.suggested_alternatives = final_state.get("suggested_alternatives")


def handle_user_message_with_graph(user_input: str) -> bool:
    """Process user message via LangGraph (runs in main thread so result is always applied)."""
    if not LANGGRAPH_AVAILABLE or "flight_graph" not in st.session_state:
        return False
    try:
        initial_state = {
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
            "search_history": st.session_state.get("search_history", []),
        }
        msg = random.choice(STATUS_MESSAGES) if STATUS_MESSAGES else "Processing..."
        with st.spinner(msg):
            final_state = st.session_state.flight_graph.invoke(initial_state)
        _apply_graph_result(final_state)
        return True
    except Exception as e:
        logger.error("LangGraph error: %s", e, exc_info=True)
        st.error(f"LangGraph error: {e}")
        return False


def process_manual_fallback(user_input: str) -> None:
    """Manual status-based handling when LangGraph is not used."""
    history = st.session_state.chat_history[:-1]
    recent = history[-MAX_HISTORY:] if len(history) > MAX_HISTORY else history
    conversation_messages = [{"role": m["role"], "content": m["content"]} for m in recent]
    slots_context = json.dumps(st.session_state.slots, indent=2)
    user_message_with_context = f"""[Current booking state: {slots_context}]

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

    if json_data is None:
        try:
            json_data = json.loads(clean_json_text(raw_reply))
            conversational_msg = json_data.get("message", "I understand. Let me help you with that.")
        except Exception:
            conversational_msg = extract_conversational_message(raw_reply) or "I understand. Let me help you with that."
            json_data = {"status": "error", "slots": st.session_state.slots, "missing_slots": []}

    if "slots" in json_data and isinstance(json_data["slots"], dict):
        st.session_state.slots = json_data["slots"]

    status = json_data.get("status", "error")

    if status == "ready_for_search":
        is_valid, error_msg, error_details = validate_slots(st.session_state.slots)
        if not is_valid:
            st.session_state.error_context = error_details
            _append_message(
                "assistant",
                f"{conversational_msg}\n\n{error_msg}\n\nMissing or invalid: {', '.join(error_details.keys())}",
            )
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
                    route = ""
                    if f.get("origin_code") and f.get("destination_code"):
                        route = f"   {f['origin_code']} → {f['destination_code']}\n"
                    combined += f"**{i}. {airline_display}** — {format_flight_price(f.get('price'))}\n"
                    if route:
                        combined += route
                    combined += f"   Departure: {f['departure_time']} | Arrival: {f['arrival_time']}\n"
                    combined += f"   {'Non-stop' if f.get('non_stop') else 'With stops'}\n"
                    if f.get("source_url") and f["source_url"] != "#":
                        combined += f"   [Book here]({f['source_url']})\n"
                    combined += "\n"
                combined += "\n*Data source: Real flight API*"
            combined = re.sub(r"<[^>]+>", "", combined).strip()
            _append_message("assistant", combined)

    elif status == "refining_search":
        preferences = st.session_state.slots.get("preferences") or {}
        refinement_type = None
        if preferences.get("nearby_airports"):
            refinement_type = "nearby_airports"
        elif (preferences.get("flexible_dates") or {}).get("enabled"):
            refinement_type = "flexible_dates"
        elif preferences.get("max_price") or any(w in user_input.lower() for w in ["cheaper", "budget", "low price", "affordable"]):
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
            base = st.session_state.slots.get("departure_date")
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
                route = f"   {f['origin_code']} → {f['destination_code']}\n" if f.get("origin_code") and f.get("destination_code") else ""
                combined += f"**{i}. {airline_display}** — {format_flight_price(f.get('price'))}\n"
                if route:
                    combined += route
                combined += f"   Departure: {f['departure_time']} | Arrival: {f['arrival_time']}\n"
                combined += f"   {'Non-stop' if f.get('non_stop') else 'With stops'}\n"
                if f.get("source_url") and f["source_url"] != "#":
                    combined += f"   [Book here]({f['source_url']})\n"
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
        _append_message("assistant", conversational_msg)


def main() -> None:
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
    if "suggested_alternatives" not in st.session_state:
        st.session_state.suggested_alternatives = None
    if LANGGRAPH_AVAILABLE and "flight_graph" not in st.session_state:
        try:
            st.session_state.flight_graph = create_flight_finder_graph()
        except Exception as e:
            logger.error("Failed to init LangGraph: %s", e)
            st.warning("Could not initialize LangGraph. Using manual handling.")

    st.title("✈️ GarudaX - Your Flight Finder")

    # UI gate: user must enter name before using chat (no agent prompt for name)
    has_name = bool((st.session_state.user_name or "").strip())
    if not has_name:
        st.markdown("Enter your name to continue. Your name is only used to tag your searches.")
        name_input = st.text_input("Your name", key="user_name_input", placeholder="e.g. Alex", label_visibility="collapsed")
        if st.button("Continue", key="name_continue"):
            name = (name_input or "").strip()
            if name:
                st.session_state.user_name = name
                st.rerun()
            else:
                st.warning("Please enter your name.")
        st.caption(f"Conversation ID: `{st.session_state.conversation_id}`")
        return

    # First-time after name: show intro once (no "what should I call you?")
    if len(st.session_state.chat_history) == 0:
        greeting = f"""Hi **{st.session_state.user_name}**! I'm **GarudaX**, your flight-finding assistant. ✈️

I'm here to help you search for flights in plain language. Tell me where you want to go, when, and any preferences—I'll ask a few questions if I need more detail, then show you options.

**I can help with things like:**
- *"Flights from New York to Los Angeles next Friday"*
- *"I need to fly to London in March, preferably direct"*
- *"Cheapest options from JFK to Paris for 2 adults"*
- *"Weekend trips from Chicago to Miami"*"""
        st.session_state.chat_history.append({"role": "assistant", "content": greeting})
        try:
            from src.services.supabase_persistence import persist_message
            persist_message(
                st.session_state.conversation_id,
                "assistant",
                greeting,
                slots=st.session_state.slots,
                turn_index=st.session_state.get("turn_index", 1),
                user_name=st.session_state.user_name,
            )
        except Exception:
            pass

    st.caption(f"Conversation ID: `{st.session_state.conversation_id}`")

    if st.button("🆕 New Chat", key="new_chat"):
        st.session_state.chat_history = []
        st.session_state.slots = {**DEFAULT_SLOTS}
        st.session_state.conversation_id = str(uuid.uuid4())
        st.session_state.turn_index = 1
        st.session_state.last_search_results = None
        st.session_state.last_search_params = None
        st.session_state.search_history = []
        st.session_state.error_context = None
        st.session_state.price_stats = None
        st.session_state.suggested_alternatives = None
        st.rerun()

    with st.expander("📋 Current Booking Details (click to view)"):
        st.markdown(format_booking_details(st.session_state.slots))
        with st.expander("🔧 Raw Data (for debugging)"):
            st.json(st.session_state.slots)

    for msg in st.session_state.chat_history:
        with st.chat_message(msg["role"]):
            st.write(msg["content"])

    user_input = st.chat_input("Type your message here...")
    if not user_input:
        return

    last = st.session_state.chat_history[-1] if st.session_state.chat_history else None
    if last and last.get("role") == "user" and (last.get("content") or "").strip() == user_input.strip():
        return

    if st.session_state.is_calling_model:
        st.warning("Processing previous request — please wait.")
        return

    st.session_state.is_calling_model = True
    _append_message("user", user_input)

    try:
        if handle_user_message_with_graph(user_input):
            st.rerun()
        else:
            with st.spinner(random.choice(STATUS_MESSAGES) if STATUS_MESSAGES else "Thinking..."):
                process_manual_fallback(user_input)
            st.rerun()
    except Exception as e:
        logger.exception("Error processing message")
        _append_message("assistant", f"I encountered an error: {e}. Please try again.")
        st.error(str(e))
        st.rerun()
    finally:
        st.session_state.is_calling_model = False


if __name__ == "__main__":
    main()
