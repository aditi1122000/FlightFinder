"""
Application configuration: environment variables, constants, prompts, and default state.
"""
import os
from dotenv import load_dotenv

load_dotenv(".env")


# API keys and URLs

MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
FLIGHT_API_KEY = os.getenv("FLIGHT_API_KEY")
FLIGHT_API_BASE_URL = os.getenv("FLIGHT_API_BASE_URL", "")
AIRPORT_API_KEY = os.getenv("AIRPORT_API_KEY")
AIRPORT_API_BASE_URL = os.getenv("AIRPORT_API_BASE_URL", "https://api.aviationstack.com/v1")
RAPIDAPI_KEY = os.getenv("RapidAPI") or os.getenv("RAPIDAPI_KEY")
RAPIDAPI_HOST = os.getenv("RapidAPIHost") or os.getenv("RAPIDAPI_HOST")
# Maps JavaScript API. Separate from GEMINI_API_KEY / GOOGLE_API_KEY.
GOOGLE_MAPS_API_KEY = (os.getenv("GOOGLE_MAPS_API_KEY") or "").strip()


# LLM and app constants

# gemini | mistral — use gemini while Mistral is rate-limited
LLM_PROVIDER = (os.getenv("LLM_PROVIDER") or "gemini").strip().lower()
# Prefer stable flash models available to new free-tier keys.
# gemini-2.5-pro / gemini-flash-latest are often blocked or overloaded.
GEMINI_MODEL = os.getenv("GEMINI_MODEL") or "gemini-3.6-flash"
GEMINI_FALLBACK_MODELS = [
    m.strip()
    for m in (os.getenv("GEMINI_FALLBACK_MODELS") or "gemini-3.5-flash,gemini-3.6-flash").split(",")
    if m.strip()
]
MISTRAL_MODEL = os.getenv("MISTRAL_MODEL") or "mistral-medium-latest"
MODEL_NAME = GEMINI_MODEL if LLM_PROVIDER == "gemini" else MISTRAL_MODEL
MAX_HISTORY = 10
MAX_TOKENS_LLM = 1500
# Mistral Free/experiment: often 1 request/second. Never burst-retry on 429.
RETRIES = 2
BASE_DELAY = 2.0
PROTECTIVE_SLEEP = 0.3 if LLM_PROVIDER == "gemini" else 1.1
RATE_LIMIT_RETRIES = 0  # fail immediately on 429 — retries dig a deeper hole
RATE_LIMIT_WAIT_SECONDS = 2.5
# After a 429, block further LLM calls in this browser session
RATE_LIMIT_COOLDOWN_SECONDS = 90
# Intent profiling (user summary) — safe again on Gemini; was off during Mistral 429s
ENABLE_USER_SUMMARY_UPDATE = True
# Comma-separated name fragments allowed to open Debug (e.g. "aditi")
GARUDAX_DEBUG_USERS = {
    n.strip().lower()
    for n in (os.getenv("GARUDAX_DEBUG_USERS") or "").split(",")
    if n.strip()
}


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

SYSTEM_PROMPT = """You are a flight-finder assistant. You extract booking details from the user and respond in a strict format.

REQUIRED OUTPUT FORMAT:
Your reply must contain these two blocks in this order (and an optional third when needed):

1) A short conversational message inside <conversational_message>...</conversational_message>
2) Valid JSON inside <json_data>...</json_data>
3) OPTIONAL: For special/custom requests only, output inside <misc>...</misc> (see below)

Example (standard reply):

<conversational_message>
Short reply here.
</conversational_message>

<json_data>
{"status": "...", "slots": {...}, "missing_slots": []}
</json_data>

Example (when user asks for custom output e.g. table, summary, comparison):

<conversational_message>
Here's the comparison.
</conversational_message>

<json_data>
{"status": "update", "slots": { ... current slots, use null for N/A ... }, "missing_slots": []}
</json_data>

<misc>
| Flight | Airline | Price | Departure |
|--------|---------|-------|-----------|
| 1 | Air India (AI 2520) | ₹24,771 | 20:00 |
...
</misc>

CONVERSATIONAL MESSAGE RULES:
- Sound like a calm person at a travel desk: warm, plain, and specific. Professional, never bossy or robotic.
- 1–2 short sentences. No "as an AI", no "rate-limited", no "please don't", no "you must".
- When you also return <json_data>, keep the conversational message to one or two short lines so the JSON is not truncated.
- No long intros and no recap of every word they already said.
- For clarifications: ask only what’s missing, in a natural way (e.g. "Hanoi or Ho Chi Minh?"). Do not ask for a specific day when they already gave a month/week or said any date — the chart handles that. Always name the missing item; never say only "I need more information."
- For confirmations: one easy line (e.g. "Delhi to Vietnam in November. I’ll look that up.").
- Do not list full slot recaps in the message; the system shows booking details separately.
- Only when suggesting alternatives (e.g. airport codes) use 2–3 short bullet points if needed; otherwise stay minimal.

JSON RULES (always required):
- Output ONLY valid JSON between <json_data> and </json_data>. No markdown, no extra text, no trailing commas.
- "status" must be exactly one of: clarification_needed | update | ready_for_search | refining_search | awaiting_confirmation | error
- "slots" must be the full object every time (origin, destination, departure_date, return_date, trip_type, passengers, cabin_class, preferences). Use null for empty values and ask followup questions if required.
- "missing_slots" must be an array of missing slot keys (e.g. ["departure_date"]).

STATUS MEANINGS:
- clarification_needed: origin or destination missing, or departure_date missing when the user did NOT name a month/week or say "any date"
- When the user names a month or week ("next month", "November", "next week") or says any date / flexible / no preference on the day: leave departure_date null, do NOT put departure_date in missing_slots, use status "update", and say you will show fares across those days
- update: slots updated but not yet ready to search; also use for special requests (table, summary, etc.) so no new search runs
- ready_for_search: origin, destination, departure_date present
- refining_search: user wants cheaper / nearby airports / flexible dates. If the user only asks for cheaper/budget/affordable (no mention of "flexible dates" or "other dates"), set preferences.flexible_dates to false and keep departure_date from Current booking state so the system filters by price on the same date.
- awaiting_confirmation: ambiguous input (e.g. airport), need user to pick
- error: something went wrong

MISC TAG (optional, for special requests only):
- When the user asks for something that is NOT a booking change or new search (e.g. "put this in a table", "summarize these flights", "which is cheapest?", "give me a comparison"), put your formatted answer inside <misc>...</misc>.
- Output the content as-is: markdown tables, lists, plain text are fine. No JSON inside <misc>.
- When the user asks for "csv", "download csv", or "excel": do NOT paste raw CSV text or long data links. Put only this in <misc>: "Use the **Download as CSV** button below the chat to get the full results as a file (opens in Excel or Google Sheets)."
- Still output valid <json_data> with status "update" and current "slots" (unchanged) so the system does not run a new search.
- Use <misc> only for display-only requests; for normal booking flow DO NOT use <misc>.

DATA RULES:
- Use airport codes when known (HYD, DEL, BOM, AUH, DXB, etc.). Dates as YYYY-MM-DD.
- Parse dates flexibly: "12 mar 26" → "2026-03-12", "23 dec 2025" → "2025-12-23".
- Always return the complete "slots" object with all keys; use null for empty values.

SESSION / LAST MESSAGES:
- You are given "Current booking state" (recent slots). When the user does NOT mention a date, origin, or destination (e.g. "cheaper", "same", "show more", "nearby airports", "flexible"), KEEP the values from Current booking state. Do not clear or omit departure_date, return_date, origin, or destination unless the user explicitly changes them. Use the same date and route as in the last messages so the search reuses the user's previous choices.
"""


# Human-readable labels for missing slots (so we can say what's missing)
MISSING_SLOT_LABELS = {
    "origin": "where you're leaving from",
    "destination": "where you're headed",
    "departure_date": "a day to leave (any day that month is fine)",
    "return_date": "a return day, or say one-way",
    "passengers": "how many are travelling",
}


# User-facing error messages

ERROR_MESSAGES = {
    "network_error": "The fare search didn't answer just now. Give it a moment and try again.",
    "api_error_4xx": "The fare search turned that one down. Check the cities and the date, then try again.",
    "api_error_5xx": "The fare search is having a slow moment. Try again in a bit.",
    "timeout": "That search took too long to come back. Once more should do it.",
    "invalid_response": "The fare search sent back something odd. Try that again.",
    "no_flights": "Nothing came back for that exact day.",
    "provider_quota": (
        "Live fares are paused. The fare provider's monthly allowance is used up, "
        "so I won't show guessed prices."
    ),
    "provider_unconfigured": "Live fares aren't connected yet, so there's nothing real to show.",
    "invalid_airport": "I couldn't place that airport. A city name works too.",
    "invalid_date": "That date has passed. Pick a day ahead and I'll look.",
    "missing_info": "A place and a day, and I can look.",
    "format_error": "I lost the thread on that one. Say it once more.",
    "rate_limit": (
        "The search desk is busy for a moment. "
        "Give it a minute or two, then try that once more."
    ),
}
