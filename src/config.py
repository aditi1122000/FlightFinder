"""
Application configuration: environment variables, constants, prompts, and default state.
"""
import os
from dotenv import load_dotenv

load_dotenv(".env")


# API keys and URLs

MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")
FLIGHT_API_KEY = os.getenv("FLIGHT_API_KEY")
FLIGHT_API_BASE_URL = os.getenv("FLIGHT_API_BASE_URL", "")
AIRPORT_API_KEY = os.getenv("AIRPORT_API_KEY")
AIRPORT_API_BASE_URL = os.getenv("AIRPORT_API_BASE_URL", "https://api.aviationstack.com/v1")
RAPIDAPI_KEY = os.getenv("RapidAPI") or os.getenv("RAPIDAPI_KEY")
RAPIDAPI_HOST = os.getenv("RapidAPIHost") or os.getenv("RAPIDAPI_HOST")


# LLM and app constants

MODEL_NAME = "mistral-medium-latest"
MAX_HISTORY = 10
MAX_TOKENS_LLM = 800  # Keep replies compressed (conversational + JSON only)
RETRIES = 4
BASE_DELAY = 1.0
PROTECTIVE_SLEEP = 0.5


# Default booking slots (empty state)

DEFAULT_SLOTS = {
    "origin": {"city": None, "airport_code": None},
    "destination": {"city": None, "airport_code": None},
    "departure_date": None,
    "return_date": None,
    "trip_type": "one_way",
    "passengers": {"adults": 1, "children": 0, "infants": 0},
    "cabin_class": None,
    "preferences": {
        "airlines": None,
        "non_stop_only": None,
        "time_of_day": None,
        "max_price": None,
        "nearby_airports": None,
        "flexible_dates": None,
    },
}


# System prompt for the flight-finder LLM

SYSTEM_PROMPT = """You are a flight-finder assistant. You extract booking details from the user and respond in ONE strict format only.

STRICT OUTPUT FORMAT (no exceptions):
Your reply must contain exactly two blocks in this order:

1) A short conversational message inside <conversational_message>...</conversational_message>
2) Valid JSON inside <json_data>...</json_data>

Example structure (follow exactly):

<conversational_message>
Short reply here.
</conversational_message>

<json_data>
{"status": "...", "slots": {...}, "missing_slots": []}
</json_data>

CONVERSATIONAL MESSAGE RULES:
- Keep it COMPRESSED and ON POINT. 1–3 short sentences max for normal replies.
- No long intros, no repetition of the user's words, no filler. We store these; they must be brief.
- For clarifications: ask only what’s missing (e.g. "Which date?" or "Origin and destination?").
- For confirmations: one line (e.g. "Got it. From HYD to DEL on 2025-12-23. Searching.").
- Do not list full slot recaps in the message; the system shows booking details separately.
- Only when suggesting alternatives (e.g. airport codes) use 2–3 short bullet points if needed; otherwise stay minimal.

JSON RULES:
- Output ONLY valid JSON between <json_data> and </json_data>. No markdown, no extra text, no trailing commas.
- "status" must be exactly one of: clarification_needed | update | ready_for_search | refining_search | awaiting_confirmation | error
- "slots" must be the full object every time (origin, destination, departure_date, return_date, trip_type, passengers, cabin_class, preferences).
- "missing_slots" must be an array of missing slot keys (e.g. ["departure_date"]).

STATUS MEANINGS:
- clarification_needed: origin, destination, or departure_date missing
- update: slots updated but not yet ready to search
- ready_for_search: origin, destination, departure_date present
- refining_search: user wants cheaper / nearby airports / flexible dates
- awaiting_confirmation: ambiguous input (e.g. airport), need user to pick
- error: something went wrong

DATA RULES:
- Use airport codes when known (HYD, DEL, BOM, AUH, DXB, etc.). Dates as YYYY-MM-DD.
- Parse dates flexibly: "12 mar 26" → "2026-03-12", "23 dec 2025" → "2025-12-23".
- Always return the complete "slots" object with all keys; use null for empty values.
"""


# User-facing error messages

ERROR_MESSAGES = {
    "network_error": "I'm having trouble connecting to the flight search service. Please try again in a moment.",
    "api_error_4xx": "I couldn't process your request. Please check your search details and try again.",
    "api_error_5xx": "The flight search service is temporarily unavailable. Please try again later.",
    "timeout": "The request took too long. Please try again.",
    "invalid_response": "I received an unexpected response. Please try again.",
    "no_flights": "I couldn't find any flights matching your criteria.",
    "invalid_airport": "I couldn't find that airport. Please check the airport code or city name.",
    "invalid_date": "Please provide a valid date in the future.",
    "missing_info": "I need more information to search for flights.",
    "format_error": "I couldn't process that. Please try again.",
}
