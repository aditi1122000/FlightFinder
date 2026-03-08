"""
Optional: Amadeus OAuth2 token and flight-destinations API.
Run from project root: python scripts/amadeus_token.py
"""
import os
import json
import requests
from dotenv import load_dotenv

# Load .env from project root (parent of scripts/)
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(_ROOT, ".env"))

DEFAULT_TOKEN_URL = "https://test.api.amadeus.com/v1/security/oauth2/token"
DEFAULT_AMADEUS_BASE = "https://test.api.amadeus.com"
AVIATION_RESPONSE_JSON = os.path.join(_ROOT, "data", "aviation_response.json")


def get_token_url() -> str:
    return os.getenv("AMADEUS_TOKEN_URL", DEFAULT_TOKEN_URL).strip() or DEFAULT_TOKEN_URL


def _save_response(data: dict) -> None:
    os.makedirs(os.path.dirname(AVIATION_RESPONSE_JSON), exist_ok=True)
    with open(AVIATION_RESPONSE_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def get_amadeus_token(
    client_id: str | None = None,
    client_secret: str | None = None,
    use_dummy: bool = False,
    save_response: bool = False,
) -> dict:
    if use_dummy:
        client_id, client_secret = "dummy", "dummy"
    else:
        client_id = client_id or os.getenv("AMADEUS_API_KEY")
        client_secret = client_secret or os.getenv("AMADEUS_API_SECRET")
        if not client_id or not client_secret:
            raise ValueError("Set AMADEUS_API_KEY and AMADEUS_API_SECRET in .env")
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    data = {"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret}
    response = requests.post(get_token_url(), headers=headers, data=data, timeout=30)
    try:
        out = response.json()
    except Exception:
        out = {"error": response.text, "status_code": response.status_code}
    if save_response or use_dummy:
        _save_response(out)
    if not use_dummy and not response.ok:
        response.raise_for_status()
    return out


def get_access_token(force_refresh: bool = False) -> str:
    if not force_refresh:
        existing = (os.getenv("AMADEUS_ACCESS_TOKEN") or "").strip()
        if existing:
            return existing
    return (get_amadeus_token().get("access_token") or "").strip()


def get_flight_destinations(
    origin: str = "PAR",
    max_price: int | str = 200,
    access_token: str | None = None,
    save_response: bool = True,
) -> dict:
    base = os.getenv("AMADEUS_BASE_URL", DEFAULT_AMADEUS_BASE).strip() or DEFAULT_AMADEUS_BASE
    url = f"{base.rstrip('/')}/v1/shopping/flight-destinations"
    token = (access_token or "").strip() or get_access_token()
    response = requests.get(url, headers={"Authorization": f"Bearer {token}"}, params={"origin": origin, "maxPrice": str(max_price)}, timeout=30)
    try:
        out = response.json()
    except Exception:
        out = {"error": response.text, "status_code": response.status_code}
    if save_response:
        _save_response(out)
    if not response.ok:
        response.raise_for_status()
    return out


if __name__ == "__main__":
    token = get_access_token()
    print(f"Using Bearer token: {token[:20]}...")
    result = get_flight_destinations(origin="PAR", max_price=200, save_response=True)
    print(json.dumps(result, indent=2))
    print(f"\nResponse saved to {AVIATION_RESPONSE_JSON}")
    if "data" in result:
        print(f"Found {len(result['data'])} destinations")
