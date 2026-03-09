"""
LangGraph workflow for the flight finder.
Imports from services and state only (no Streamlit) so the graph can run without the UI.
"""
import json
import logging
import re
from datetime import datetime
from typing import Literal

from langgraph.graph import StateGraph, END

from src.config import MODEL_NAME, SYSTEM_PROMPT, MAX_HISTORY, MAX_TOKENS_LLM, ERROR_MESSAGES
from src.state import FlightState
from src.services.flight_services import (
    call_mistral_with_backoff,
    extract_conversational_message,
    extract_json_from_response,
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

logger = logging.getLogger(__name__)


def _current_date_context() -> str:
    """Return current date string for LLM context (so it uses the correct year/date)."""
    now = datetime.now()
    return now.strftime("%Y-%m-%d (%A, %B %d, %Y)")


def parse_llm_response(state: FlightState) -> FlightState:
    """Entry node: call LLM, parse response, update state with status and slots."""
    user_msg = (state.get("user_message") or "")[:80]
    logger.info("parse_llm: user_message=%s...", user_msg)
    conversation_messages = state["chat_history"][-MAX_HISTORY:]
    slots_dict = state["slots"]
    slots_context = json.dumps(slots_dict, indent=2)
    today = _current_date_context()
    user_message_with_context = f"""[Current date: {today}]
[Current booking state: {slots_context}]

User: {state["user_message"]}"""

    system_content = (
        SYSTEM_PROMPT
        + "\n\nToday's date (use this when interpreting relative dates like 'next Friday', 'in March', or 'tomorrow'): "
        + today
        + ". Always use this date's year and calendar when resolving relative dates; do not assume 2024."
    )
    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": system_content}
        ] + conversation_messages + [
            {"role": "user", "content": user_message_with_context}
        ],
        "temperature": 0.2,
        "max_tokens": MAX_TOKENS_LLM,
    }

    logger.info("parse_llm: calling Mistral (messages=%d)", len(payload.get("messages", [])))
    response = call_mistral_with_backoff(payload)
    raw_reply = response.choices[0].message.content
    logger.info("parse_llm: response length=%d", len(raw_reply or ""))

    try:
        with open("last_llm_raw_reply.txt", "w", encoding="utf-8") as f:
            f.write(raw_reply or "")
    except Exception as e:
        logger.warning("parse_llm: could not save raw_reply to file: %s", e)

    conversational_msg = extract_conversational_message(raw_reply)
    json_data = extract_json_from_response(raw_reply)
    conversational_msg = re.sub(r'<[^>]+>', '', conversational_msg).strip()

    state["conversational_message"] = conversational_msg

    if json_data:
        status = json_data.get("status", "error")
        state["status"] = status
        logger.info("parse_llm: status=%s missing_slots=%s", status, json_data.get("missing_slots", []))
        if "slots" in json_data:
            state["slots"] = json_data["slots"]
        state["missing_slots"] = json_data.get("missing_slots", [])
    else:
        state["status"] = "error"
        state["error_message"] = "Failed to parse LLM response"
        logger.warning("parse_llm: no JSON, status=error. Snippet: %s...", (raw_reply or "")[:500].replace("\n", " "))
        # Strict format: only accept valid JSON; show short error, do not use unparsed text
        state["chat_history"].append({
            "role": "assistant",
            "content": ERROR_MESSAGES.get("format_error", "I couldn't process that. Please try again."),
        })

    return state


def handle_clarification(state: FlightState) -> FlightState:
    """Handle clarification_needed: add assistant message to chat."""
    msg = state["conversational_message"]
    state["chat_history"].append({"role": "assistant", "content": msg})
    return state


def handle_update(state: FlightState) -> FlightState:
    """Handle update: add assistant message to chat."""
    msg = state["conversational_message"]
    state["chat_history"].append({"role": "assistant", "content": msg})
    return state


def handle_ready_for_search(state: FlightState) -> FlightState:
    """Handle ready_for_search: validate slots, call search API, format and append results."""
    slots_dict = state["slots"]
    is_valid, error_msg, error_details = validate_slots(slots_dict)

    if not is_valid:
        state["status"] = "awaiting_confirmation"
        state["error_context"] = error_details
        state["error_message"] = error_msg
        state["conversational_message"] += f"\n\n{error_msg}"
        return state

    flights, api_error, api_error_details = search_flights_api(slots_dict, max_results=10)
    state["last_search_results"] = [f if isinstance(f, dict) else getattr(f, "dict", lambda: f)() for f in flights]
    state["last_search_params"] = slots_dict.copy()

    if flights:
        flight_dicts = state["last_search_results"]
        stats = calculate_price_stats(flight_dicts)
        if stats:
            state["price_stats"] = stats

    combined_msg = state["conversational_message"]
    if not combined_msg.endswith("\n"):
        combined_msg += "\n\n"

    if api_error:
        combined_msg += f"⚠️ {api_error}\n\n"
        state["error_context"] = api_error_details
        suggestions = suggest_alternatives(slots_dict)
        if suggestions["suggestion_message"]:
            combined_msg += f"{suggestions['suggestion_message']}\n\nWould you like to try any of these alternatives?"
            state["suggested_alternatives"] = suggestions
    elif not flights:
        combined_msg += "I couldn't find any flights matching your exact criteria.\n\n"
        suggestions = suggest_alternatives(slots_dict)
        if suggestions["suggestion_message"]:
            combined_msg += f"{suggestions['suggestion_message']}\n\nWould you like to try any of these alternatives?"
            state["suggested_alternatives"] = suggestions
    else:
        combined_msg += "**Flight Search Results:**\n\n"
        if state["price_stats"]:
            combined_msg += f"*{format_price_range(state['price_stats'])}*\n\n"
        for i, f in enumerate(flights[:10], 1):
            f_dict = f if isinstance(f, dict) else (getattr(f, "dict", lambda: f)() if callable(getattr(f, "dict", None)) else f)
            if not isinstance(f_dict, dict):
                f_dict = {"airline": "?", "departure_time": "?", "arrival_time": "?", "price": None, "non_stop": True, "source_url": "#", "flight_number": None}
            fn = f_dict.get("flight_number")
            airline_display = f"{f_dict['airline']} ({fn})" if fn else f_dict["airline"]
            route = ""
            if f_dict.get("origin_code") and f_dict.get("destination_code"):
                route = f"   {f_dict['origin_code']} → {f_dict['destination_code']}\n"
            combined_msg += f"**{i}. {airline_display}** — {format_flight_price(f_dict.get('price'))}\n"
            if route:
                combined_msg += route
            combined_msg += f"   Departure: {f_dict['departure_time']} | Arrival: {f_dict['arrival_time']}\n"
            combined_msg += f"   {'Non-stop' if f_dict.get('non_stop') else 'With stops'}\n"
            if f_dict.get("source_url") and f_dict["source_url"] != "#":
                combined_msg += f"   [Book here]({f_dict['source_url']})\n"
            combined_msg += "\n"
        combined_msg += "\n*Data source: Real flight API*"

    state["conversational_message"] = combined_msg
    state["chat_history"].append({"role": "assistant", "content": combined_msg})
    return state


def handle_refining_search(state: FlightState) -> FlightState:
    """Handle refining_search: price filter, nearby airports, or flexible dates."""
    slots_dict = state["slots"] or {}
    preferences = slots_dict.get("preferences") or {}
    refinement_type = None
    if preferences.get("nearby_airports"):
        refinement_type = "nearby_airports"
    elif (preferences.get("flexible_dates") or {}).get("enabled"):
        refinement_type = "flexible_dates"
    elif preferences.get("max_price") or any(w in (state.get("user_message") or "").lower() for w in ["cheaper", "budget", "low price"]):
        refinement_type = "price_filter"

    refined_flights = []
    refinement_msg = ""

    if refinement_type == "price_filter" and state.get("last_search_results"):
        price_stats = state.get("price_stats") or calculate_price_stats(state["last_search_results"])
        if price_stats:
            threshold = price_stats["avg_price"]
            if any(w in (state.get("user_message") or "").lower() for w in ["cheapest", "lowest"]):
                threshold = price_stats["min_price"] + (price_stats["avg_price"] - price_stats["min_price"]) * 0.3
            filtered = [f for f in state["last_search_results"] if f.get("price", 0) <= threshold]
            search_slots = {**slots_dict, "preferences": {**(slots_dict.get("preferences") or {}), "max_price": threshold}}
            new_flights, _, _ = search_flights_api(search_slots, max_results=10)
            all_flights = filtered + new_flights
            seen = set()
            for f in all_flights:
                key = (f.get("airline"), f.get("departure_time"), f.get("price"))
                if key not in seen:
                    refined_flights.append(f)
                    seen.add(key)
            refined_flights.sort(key=lambda x: x.get("price", 0))
            refinement_msg = f"Found {len(refined_flights)} flights within your budget"

    elif refinement_type == "nearby_airports":
        _oc = (slots_dict.get("origin") or {}).get("airport_code")
        _dc = (slots_dict.get("destination") or {}).get("airport_code")
        origin_code = (_oc[0] if isinstance(_oc, list) and _oc else _oc) or ""
        dest_code = (_dc[0] if isinstance(_dc, list) and _dc else _dc) or ""
        nearby_origin = find_nearby_airports(origin_code) if origin_code else []
        nearby_dest = find_nearby_airports(dest_code) if dest_code else []
        all_flights = []
        for airport in (nearby_origin[:2] + nearby_dest[:2]):
            search_slots = dict(slots_dict)
            search_slots.setdefault("origin", {})
            search_slots.setdefault("destination", {})
            if airport in nearby_origin:
                search_slots["origin"] = {**search_slots["origin"], "airport_code": airport["airport_code"]}
            else:
                search_slots["destination"] = {**search_slots["destination"], "airport_code": airport["airport_code"]}
            flights, _, _ = search_flights_api(search_slots, max_results=5)
            all_flights.extend(flights)
        refined_flights = all_flights[:10]
        refinement_msg = f"Found {len(refined_flights)} flights from nearby airports"

    elif refinement_type == "flexible_dates":
        base_date = slots_dict.get("departure_date")
        date_range = generate_flexible_date_range(base_date) if base_date else []
        all_flights = []
        for date in date_range[:5]:
            search_slots = {**slots_dict, "departure_date": date}
            flights, _, _ = search_flights_api(search_slots, max_results=3)
            all_flights.extend(flights)
        refined_flights = all_flights[:10]
        refinement_msg = f"Found {len(refined_flights)} flights with flexible dates"

    combined_msg = state["conversational_message"]
    if not combined_msg.endswith("\n"):
        combined_msg += "\n\n"
    if refined_flights:
        combined_msg += f"**{refinement_msg}:**\n\n"
        stats = calculate_price_stats(refined_flights)
        if stats:
            state["price_stats"] = stats
            combined_msg += f"*{format_price_range(stats)}*\n\n"
        for i, f in enumerate(refined_flights[:10], 1):
            fn = f.get("flight_number")
            airline_display = f"{f['airline']} ({fn})" if fn else f["airline"]
            route = f"   {f['origin_code']} → {f['destination_code']}\n" if f.get("origin_code") and f.get("destination_code") else ""
            combined_msg += f"**{i}. {airline_display}** — {format_flight_price(f.get('price'))}\n"
            if route:
                combined_msg += route
            combined_msg += f"   Departure: {f['departure_time']} | Arrival: {f['arrival_time']}\n"
            combined_msg += f"   {'Non-stop' if f.get('non_stop') else 'With stops'}\n"
            if f.get("source_url") and f["source_url"] != "#":
                combined_msg += f"   [Book here]({f['source_url']})\n"
            combined_msg += "\n"
    else:
        combined_msg += "I couldn't find any refined options. Would you like to try different criteria?"

    state["conversational_message"] = combined_msg
    state["chat_history"].append({"role": "assistant", "content": combined_msg})
    return state


def handle_awaiting_confirmation(state: FlightState) -> FlightState:
    """Handle awaiting_confirmation: airport suggestions and error context."""
    slots_dict = state["slots"] or {}
    origin = slots_dict.get("origin") or {}
    destination = slots_dict.get("destination") or {}
    origin_city = (origin.get("city") or "").lower()
    dest_city = (destination.get("city") or "").lower()
    suggestions = []
    if origin_city and not origin.get("airport_code"):
        suggestions.extend(resolve_airport_code(origin_city))
    if dest_city and not destination.get("airport_code"):
        suggestions.extend(resolve_airport_code(dest_city))

    combined_msg = state["conversational_message"]
    if suggestions:
        combined_msg += "\n\n**Suggested alternatives:**\n"
        for i, sug in enumerate(suggestions[:3], 1):
            combined_msg += f"{i}. {sug['city']} ({sug['airport_code']})"
            if sug.get("distance_km", 0) > 0:
                combined_msg += f" - {sug['distance_km']}km away"
            combined_msg += f"\n   {sug.get('reason', '')}\n"
        state["suggested_alternatives"] = {"airports": suggestions}
    if state.get("error_context"):
        combined_msg += "\n\nI encountered an issue with your request. "
        if isinstance(state["error_context"], dict):
            combined_msg += f"Please check: {', '.join(state['error_context'].keys())}"

    state["conversational_message"] = combined_msg
    state["chat_history"].append({"role": "assistant", "content": combined_msg})
    return state


def route_status(state: FlightState) -> Literal["clarification", "update", "ready", "refining", "awaiting", "error", "end"]:
    status = state.get("status", "error")
    if status == "clarification_needed":
        return "clarification"
    if status == "update":
        return "update"
    if status == "ready_for_search":
        return "ready"
    if status == "refining_search":
        return "refining"
    if status == "awaiting_confirmation":
        return "awaiting"
    if status == "error":
        return "error"
    return "end"


def create_flight_finder_graph():
    """Build and compile the LangGraph workflow."""
    workflow = StateGraph(FlightState)
    workflow.add_node("parse_llm", parse_llm_response)
    workflow.add_node("clarification", handle_clarification)
    workflow.add_node("update", handle_update)
    workflow.add_node("ready", handle_ready_for_search)
    workflow.add_node("refining", handle_refining_search)
    workflow.add_node("awaiting", handle_awaiting_confirmation)
    workflow.set_entry_point("parse_llm")
    workflow.add_conditional_edges(
        "parse_llm",
        route_status,
        {
            "clarification": "clarification",
            "update": "update",
            "ready": "ready",
            "refining": "refining",
            "awaiting": "awaiting",
            "error": END,
            "end": END,
        },
    )
    workflow.add_edge("clarification", END)
    workflow.add_edge("update", END)
    workflow.add_edge("ready", END)
    workflow.add_edge("refining", END)
    workflow.add_edge("awaiting", END)
    return workflow.compile()
