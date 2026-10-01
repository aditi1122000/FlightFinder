"""
Route mood board: airport coords, great-circle path, weather, local time.
Uses Open-Meteo (no key). The route map is Google Maps.
"""
from __future__ import annotations

import html
import json
import math
import logging
import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from src.services.flight_services import (
    GenerationCancelled,
    _city_iata,
    _http_request,
    _iata,
    _to_str,
    destination_options,
)

logger = logging.getLogger(__name__)

# Common India + leisure routes (IATA → lat/lon/city label)
AIRPORT_COORDS: Dict[str, Dict] = {
    "DEL": {"lat": 28.5562, "lon": 77.1000, "city": "New Delhi", "label": "Delhi (DEL)"},
    "BOM": {"lat": 19.0896, "lon": 72.8656, "city": "Mumbai", "label": "Mumbai (BOM)"},
    "BLR": {"lat": 13.1986, "lon": 77.7066, "city": "Bengaluru", "label": "Bengaluru (BLR)"},
    "HYD": {"lat": 17.2403, "lon": 78.4294, "city": "Hyderabad", "label": "Hyderabad (HYD)"},
    "MAA": {"lat": 12.9941, "lon": 80.1709, "city": "Chennai", "label": "Chennai (MAA)"},
    "CCU": {"lat": 22.6547, "lon": 88.4467, "city": "Kolkata", "label": "Kolkata (CCU)"},
    "GOI": {"lat": 15.3808, "lon": 73.8314, "city": "Goa", "label": "Goa (GOI)"},
    "COK": {"lat": 10.1520, "lon": 76.4019, "city": "Kochi", "label": "Kochi (COK)"},
    "DXB": {"lat": 25.2532, "lon": 55.3657, "city": "Dubai", "label": "Dubai (DXB)"},
    "AUH": {"lat": 24.4330, "lon": 54.6511, "city": "Abu Dhabi", "label": "Abu Dhabi (AUH)"},
    "BKK": {"lat": 13.6900, "lon": 100.7501, "city": "Bangkok", "label": "Bangkok (BKK)"},
    "HAN": {"lat": 21.2212, "lon": 105.8072, "city": "Hanoi", "label": "Hanoi (HAN)"},
    "SGN": {"lat": 10.8188, "lon": 106.6520, "city": "Ho Chi Minh City", "label": "Ho Chi Minh (SGN)"},
    "SIN": {"lat": 1.3644, "lon": 103.9915, "city": "Singapore", "label": "Singapore (SIN)"},
    "KUL": {"lat": 2.7456, "lon": 101.7099, "city": "Kuala Lumpur", "label": "Kuala Lumpur (KUL)"},
    "DPS": {"lat": -8.7482, "lon": 115.1675, "city": "Bali", "label": "Bali (DPS)"},
    "CGK": {"lat": -6.1256, "lon": 106.6559, "city": "Jakarta", "label": "Jakarta (CGK)"},
    "HND": {"lat": 35.5494, "lon": 139.7798, "city": "Tokyo", "label": "Tokyo (HND)"},
    "ICN": {"lat": 37.4602, "lon": 126.4407, "city": "Seoul", "label": "Seoul (ICN)"},
    "LHR": {"lat": 51.4700, "lon": -0.4543, "city": "London", "label": "London (LHR)"},
    "CDG": {"lat": 49.0097, "lon": 2.5479, "city": "Paris", "label": "Paris (CDG)"},
    "JFK": {"lat": 40.6413, "lon": -73.7781, "city": "New York", "label": "New York (JFK)"},
    "SYD": {"lat": -33.9399, "lon": 151.1753, "city": "Sydney", "label": "Sydney (SYD)"},
    "DOH": {"lat": 25.2731, "lon": 51.6081, "city": "Doha", "label": "Doha (DOH)"},
    "IST": {"lat": 41.2753, "lon": 28.7519, "city": "Istanbul", "label": "Istanbul (IST)"},
    "CMB": {"lat": 7.1808, "lon": 79.8841, "city": "Colombo", "label": "Colombo (CMB)"},
    "KTM": {"lat": 27.6966, "lon": 85.3591, "city": "Kathmandu", "label": "Kathmandu (KTM)"},
    "MLE": {"lat": 4.1918, "lon": 73.5290, "city": "Malé", "label": "Malé (MLE)"},
}

# Mood photography (Unsplash) keyed by lowercase place tokens
DESTINATION_PHOTOS: Dict[str, str] = {
    "vietnam": "https://images.unsplash.com/photo-1528181304800-259b08848526?auto=format&fit=crop&w=1400&q=80",
    "hanoi": "https://images.unsplash.com/photo-1583417319070-4a69db38a482?auto=format&fit=crop&w=1400&q=80",
    "ho chi minh": "https://images.unsplash.com/photo-1528181304800-259b08848526?auto=format&fit=crop&w=1400&q=80",
    "saigon": "https://images.unsplash.com/photo-1528181304800-259b08848526?auto=format&fit=crop&w=1400&q=80",
    "dubai": "https://images.unsplash.com/photo-1512453979798-5ea266f8880c?auto=format&fit=crop&w=1400&q=80",
    "bangkok": "https://images.unsplash.com/photo-1583417319070-4a69db38a482?auto=format&fit=crop&w=1400&q=80",
    "thailand": "https://images.unsplash.com/photo-1583417319070-4a69db38a482?auto=format&fit=crop&w=1400&q=80",
    "goa": "https://images.unsplash.com/photo-1512343879784-a960bf40e7f2?auto=format&fit=crop&w=1400&q=80",
    "bali": "https://images.unsplash.com/photo-1512343879784-a960bf40e7f2?auto=format&fit=crop&w=1400&q=80",
    "indonesia": "https://images.unsplash.com/photo-1512343879784-a960bf40e7f2?auto=format&fit=crop&w=1400&q=80",
    "singapore": "https://images.unsplash.com/photo-1512453979798-5ea266f8880c?auto=format&fit=crop&w=1400&q=80",
    "tokyo": "https://images.unsplash.com/photo-1540959733332-eab4deabeeaf?auto=format&fit=crop&w=1400&q=80",
    "japan": "https://images.unsplash.com/photo-1540959733332-eab4deabeeaf?auto=format&fit=crop&w=1400&q=80",
    "paris": "https://images.unsplash.com/photo-1502602898657-3e91760cbb34?auto=format&fit=crop&w=1400&q=80",
    "london": "https://images.unsplash.com/photo-1513635269975-59663e0ac1ad?auto=format&fit=crop&w=1400&q=80",
    "sydney": "https://images.unsplash.com/photo-1436491865332-7a61a109cc05?auto=format&fit=crop&w=1400&q=80",
    "maldives": "https://images.unsplash.com/photo-1507525428034-b723cf961d3e?auto=format&fit=crop&w=1400&q=80",
    "default": "https://images.unsplash.com/photo-1436491865332-7a61a109cc05?auto=format&fit=crop&w=1400&q=80",
}


@dataclass
class RoutePoint:
    code: str
    city: str
    label: str
    lat: float
    lon: float


@dataclass
class RouteSnapshot:
    origin: RoutePoint
    destination: RoutePoint
    path: List[Tuple[float, float]]
    distance_km: float
    flight_hours: float
    photo_url: str
    origin_weather: Dict
    dest_weather: Dict
    google_maps_url: str


def _lookup_airport(code: str, city_hint: str = "") -> Optional[RoutePoint]:
    code = _iata(code) or _city_iata(city_hint) or (code or "").upper()[:3]
    if code in AIRPORT_COORDS:
        row = AIRPORT_COORDS[code]
        return RoutePoint(
            code=code,
            city=row["city"],
            label=row["label"],
            lat=row["lat"],
            lon=row["lon"],
        )
    return None


def _destination_photo(city: str) -> str:
    key = (city or "").lower()
    for token in sorted(DESTINATION_PHOTOS.keys(), key=len, reverse=True):
        if token != "default" and token in key:
            return DESTINATION_PHOTOS[token]
    return DESTINATION_PHOTOS["default"]


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _great_circle_path(
    lat1: float, lon1: float, lat2: float, lon2: float, steps: int = 72
) -> List[Tuple[float, float]]:
    """Lat/lon points along a great circle (flight-style arc)."""
    phi1, lam1 = math.radians(lat1), math.radians(lon1)
    phi2, lam2 = math.radians(lat2), math.radians(lon2)
    d = 2 * math.asin(
        min(
            1.0,
            math.sqrt(
                math.sin((phi2 - phi1) / 2) ** 2
                + math.cos(phi1) * math.cos(phi2) * math.sin((lam2 - lam1) / 2) ** 2
            ),
        )
    )
    if d == 0:
        return [(lat1, lon1), (lat2, lon2)]
    out: List[Tuple[float, float]] = []
    for i in range(steps + 1):
        f = i / steps
        a = math.sin((1 - f) * d) / math.sin(d)
        b = math.sin(f * d) / math.sin(d)
        x = a * math.cos(phi1) * math.cos(lam1) + b * math.cos(phi2) * math.cos(lam2)
        y = a * math.cos(phi1) * math.sin(lam1) + b * math.cos(phi2) * math.sin(lam2)
        z = a * math.sin(phi1) + b * math.sin(phi2)
        phi = math.atan2(z, math.sqrt(x * x + y * y))
        lam = math.atan2(y, x)
        out.append((math.degrees(phi), math.degrees(lam)))
    return out


def _fetch_weather(lat: float, lon: float) -> Dict:
    try:
        resp = _http_request(
            "GET",
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,relative_humidity_2m,weather_code",
                "timezone": "auto",
            },
            timeout=8,
        )
        if resp.status_code != 200:
            return {}
        data = resp.json()
        cur = data.get("current") or {}
        tz = data.get("timezone") or ""
        local_time = cur.get("time") or ""
        return {
            "temp_c": cur.get("temperature_2m"),
            "humidity": cur.get("relative_humidity_2m"),
            "weather_code": cur.get("weather_code"),
            "timezone": tz,
            "local_time": local_time,
        }
    except GenerationCancelled:
        raise
    except Exception as e:
        logger.debug("weather fetch failed: %s", e)
        return {}


def build_route_snapshot(slots: Dict, dest_code: Optional[str] = None) -> Optional[RouteSnapshot]:
    origin = slots.get("origin") if isinstance(slots.get("origin"), dict) else {}
    destination = slots.get("destination") if isinstance(slots.get("destination"), dict) else {}
    o_city = _to_str(origin.get("city")) or ""
    d_city = _to_str(destination.get("city")) or ""
    o_code = _iata(origin.get("airport_code")) or _city_iata(o_city)
    d_code = _iata(dest_code) or _iata(destination.get("airport_code")) or _city_iata(d_city)
    if not d_code:
        opts = destination_options(destination)
        if opts:
            d_city, d_code = opts[0]
    o_pt = _lookup_airport(o_code, o_city)
    d_pt = _lookup_airport(d_code, d_city)
    if not o_pt or not d_pt:
        return None
    path = _great_circle_path(o_pt.lat, o_pt.lon, d_pt.lat, d_pt.lon)
    dist = _haversine_km(o_pt.lat, o_pt.lon, d_pt.lat, d_pt.lon)
    hours = dist / 820.0 if dist else 0
    photo = _destination_photo(d_city or d_pt.city)
    gmaps = f"https://www.google.com/maps/search/?api=1&query={d_pt.lat},{d_pt.lon}"
    return RouteSnapshot(
        origin=o_pt,
        destination=d_pt,
        path=path,
        distance_km=round(dist),
        flight_hours=round(hours, 1),
        photo_url=photo,
        origin_weather=_fetch_weather(o_pt.lat, o_pt.lon),
        dest_weather=_fetch_weather(d_pt.lat, d_pt.lon),
        google_maps_url=gmaps,
    )


def _weather_label(code: Optional[int]) -> str:
    # WMO weather code → short label
    if code is None:
        return "—"
    if code == 0:
        return "Clear"
    if code in (1, 2, 3):
        return "Partly cloudy"
    if code in (45, 48):
        return "Fog"
    if code in (51, 53, 55, 61, 63, 65, 80, 81, 82):
        return "Rain"
    if code in (71, 73, 75, 85, 86):
        return "Snow"
    if code >= 95:
        return "Storm"
    return "Mixed"


def _format_local(iso_time: str) -> str:
    if not iso_time or "T" not in iso_time:
        return "—"
    try:
        date_part, time_part = iso_time.split("T", 1)
        hh, mm = time_part[:5].split(":")
        h = int(hh)
        suffix = "am" if h < 12 else "pm"
        h12 = h % 12 or 12
        return f"{date_part[8:10]} {date_part[5:7]} · {h12}:{mm} {suffix}"
    except Exception:
        return iso_time[:16].replace("T", " ")


def leaflet_route_html(route: RouteSnapshot, height: int = 380) -> str:
    """Google Maps for the route. A Maps key draws the flight arc; otherwise the public embed."""
    o, d = route.origin, route.destination
    key = (os.getenv("GOOGLE_MAPS_API_KEY") or "").strip()
    if not key:
        src = html.escape(
            f"https://maps.google.com/maps?hl=en&z=5&output=embed&q={d.lat},{d.lon}",
            quote=True,
        )
        return (
            "<!DOCTYPE html><html><head>"
            f"<style>html,body,iframe{{margin:0;border:0;width:100%;height:{height}px;}}</style>"
            f"</head><body><iframe src=\"{src}\" loading=\"lazy\" referrerpolicy=\"no-referrer-when-downgrade\"></iframe></body></html>"
        )
    path_json = json.dumps([{"lat": lat, "lng": lon} for lat, lon in route.path])
    safe_key = html.escape(key, quote=True)
    return f"""<!DOCTYPE html>
<html><head>
<style>html,body,#map{{margin:0;height:{height}px;width:100%;background:#e8eaed;}}</style>
<script src="https://maps.googleapis.com/maps/api/js?key={safe_key}"></script>
</head><body>
<div id="map"></div>
<script>
const path = {path_json};
const map = new google.maps.Map(document.getElementById('map'), {{
  mapTypeId: 'terrain',
  gestureHandling: 'cooperative',
  streetViewControl: false,
  mapTypeControl: false,
  fullscreenControl: false
}});
const line = new google.maps.Polyline({{
  path, geodesic: true, strokeColor: '#E6A800', strokeOpacity: 1, strokeWeight: 3, map
}});
new google.maps.Marker({{ position: path[0], map, title: {json.dumps(o.label)} }});
new google.maps.Marker({{ position: path[path.length - 1], map, title: {json.dumps(d.label)} }});
const bounds = new google.maps.LatLngBounds();
path.forEach((point) => bounds.extend(point));
map.fitBounds(bounds, 36);
</script></body></html>"""
