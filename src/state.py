"""State models for the flight finder: slots, results, and LangGraph state."""
from typing import List, Optional, Dict, Any, TypedDict, Annotated
import operator
from pydantic import BaseModel


class FlightSlots(BaseModel):
    """Flight booking slots extracted from conversation."""
    origin: Dict[str, Optional[str]] = {"city": None, "airport_code": None}
    destination: Dict[str, Optional[str]] = {"city": None, "airport_code": None}
    departure_date: Optional[str] = None
    return_date: Optional[str] = None
    trip_type: str = "one_way"
    passengers: Dict[str, int] = {"adults": 1, "children": 0, "infants": 0}
    cabin_class: Optional[str] = None
    preferences: Dict[str, Any] = {
        "airlines": None,
        "non_stop_only": None,
        "time_of_day": None,
        "max_price": None,
        "nearby_airports": None,
        "flexible_dates": None,
    }


class FlightResult(BaseModel):
    """Individual flight result."""
    airline: str
    departure_time: str
    arrival_time: str
    price: float
    non_stop: bool
    source_url: Optional[str] = None
    flight_number: Optional[str] = None  # e.g. "AA 293" or "6E 1234"


class PriceStats(BaseModel):
    """Price statistics from search results."""
    min_price: float
    max_price: float
    avg_price: float


class FlightState(TypedDict):
    """
    LangGraph state for flight finder workflow.
    Contains all information needed across the conversation.
    """
    status: str
    user_message: str
    chat_history: List[Dict[str, str]]
    slots: Dict[str, Any]
    conversational_message: Optional[str]
    missing_slots: List[str]
    flights: List[Dict[str, Any]]
    last_search_results: Optional[List[Dict[str, Any]]]
    last_search_params: Optional[Dict[str, Any]]
    price_stats: Optional[Dict[str, float]]
    error_context: Optional[Dict[str, Any]]
    error_message: Optional[str]
    suggested_alternatives: Optional[Dict[str, Any]]
    search_history: Annotated[List[Dict[str, Any]], operator.add]
