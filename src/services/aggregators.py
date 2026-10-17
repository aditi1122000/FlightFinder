"""Top India flight aggregators — offer scrape hubs + live book URL builders.

Left green box (batch): public deal pages → card_offers when scrape_enabled.
Right green box (live): build search URL + coupon — never scrape on Book.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode


@dataclass(frozen=True)
class Aggregator:
    id: str
    name: str
    # Public offers listing (GET). Empty = no batch scrape for this site.
    offer_url: str = ""
    scrape_enabled: bool = False
    # Hard bot walls — keep book links, skip HTML scrape.
    scrape_hard: bool = False
    # Coupon query key when appending offer.code to a book URL.
    coupon_param: str = "coupon"


# India-focused top 20 OTAs / metas used for offers + book links.
AGGREGATORS: List[Aggregator] = [
    Aggregator("booking_com", "Booking.com", scrape_enabled=False),
    Aggregator(
        "goibibo", "Goibibo",
        offer_url="https://www.goibibo.com/offers/",
        scrape_enabled=True, coupon_param="cpnCode",
    ),
    Aggregator(
        "makemytrip", "MakeMyTrip",
        offer_url="https://www.makemytrip.com/offers/",
        scrape_enabled=False, scrape_hard=True, coupon_param="coupon",
    ),
    Aggregator(
        "cleartrip", "Cleartrip",
        offer_url="https://www.cleartrip.com/all-offers/",
        scrape_enabled=True, coupon_param="coupon",
    ),
    Aggregator(
        "ixigo", "ixigo",
        offer_url="https://www.ixigo.com/offers",
        scrape_enabled=True, coupon_param="couponCode",
    ),
    Aggregator(
        "easemytrip", "EaseMyTrip",
        offer_url="https://www.easemytrip.com/offers/flight-offers.html",
        scrape_enabled=True, coupon_param="promo",
    ),
    Aggregator(
        "yatra", "Yatra",
        offer_url="https://www.yatra.com/offer/domestic-flight-offers",
        scrape_enabled=True, coupon_param="couponCode",
    ),
    Aggregator(
        "paytm_travel", "Paytm Travel",
        offer_url="https://paytm.com/flights",
        scrape_enabled=True, coupon_param="promoCode",
    ),
    Aggregator(
        "happyeasygo", "HappyEasyGo",
        offer_url="https://www.happyeasygo.com/offers",
        scrape_enabled=True, coupon_param="coupon",
    ),
    Aggregator(
        "via", "Via",
        offer_url="https://in.via.com/offers",
        scrape_enabled=True, coupon_param="coupon",
    ),
    Aggregator("musafir", "Musafir", scrape_enabled=False, coupon_param="promo"),
    Aggregator(
        "expedia", "Expedia",
        offer_url="https://www.expedia.co.in/lp/b/flight-deals",
        scrape_enabled=True, coupon_param="couponCode",
    ),
    Aggregator(
        "trip_com", "Trip.com",
        offer_url="https://www.trip.com/sale/flights/",
        scrape_enabled=True, coupon_param="coupon",
    ),
    Aggregator("skyscanner", "Skyscanner", scrape_enabled=False),
    Aggregator("kayak", "Kayak", scrape_enabled=False),
    Aggregator("agoda", "Agoda", scrape_enabled=False),
    Aggregator("airasia", "AirAsia Move", scrape_enabled=False),
    Aggregator(
        "sotc", "SOTC",
        offer_url="https://www.sotc.in/offers",
        scrape_enabled=True, coupon_param="coupon",
    ),
    Aggregator(
        "thomascook", "Thomas Cook",
        offer_url="https://www.thomascook.in/offers",
        scrape_enabled=True, coupon_param="coupon",
    ),
    Aggregator(
        "akbartravels", "Akbar Travels",
        offer_url="https://www.akbartravels.com/offers",
        scrape_enabled=True, coupon_param="coupon",
    ),
]

# Bank deal hubs scraped into the same card_offers table (not OTAs, but same batch job).
BANK_OFFER_HUBS: List[Aggregator] = [
    Aggregator(
        "axis_grab_deals", "Axis Grab Deals",
        offer_url="https://www.axisbank.com/grab-deals",
        scrape_enabled=True,
    ),
    Aggregator(
        "hdfc_offers", "HDFC Bank Offers",
        offer_url="https://www.hdfcbank.com/personal/pay/cards/credit-cards/credit-card-offers",
        scrape_enabled=True,
    ),
    Aggregator(
        "sbi_offers", "SBI Card Offers",
        offer_url="https://www.sbicard.com/en/personal/offers.page",
        scrape_enabled=True,
    ),
    Aggregator(
        "icici_offers", "ICICI Bank Offers",
        offer_url="https://www.icicibank.com/personal-banking/offers",
        scrape_enabled=True,
    ),
    Aggregator(
        "amex_offers", "American Express Offers",
        offer_url="https://www.americanexpress.com/in/benefits/offers/",
        scrape_enabled=True,
    ),
]

BY_ID: Dict[str, Aggregator] = {a.id: a for a in AGGREGATORS + BANK_OFFER_HUBS}


def scrape_targets() -> List[Aggregator]:
    """Batch offer scrapers: enabled OTA hubs + bank hubs (skip scrape_hard)."""
    out = []
    for a in AGGREGATORS + BANK_OFFER_HUBS:
        if a.scrape_enabled and a.offer_url and not a.scrape_hard:
            out.append(a)
    return out


def book_targets() -> List[Aggregator]:
    """Every aggregator we can open a flight search URL for."""
    return list(AGGREGATORS)


def match_aggregator(blob: str) -> Optional[Aggregator]:
    text = (blob or "").lower()
    if not text:
        return None
    # Longer / more specific ids first.
    for a in sorted(AGGREGATORS, key=lambda x: -len(x.id)):
        keys = {a.id.replace("_", " "), a.name.lower(), a.id}
        if a.id == "booking_com":
            keys |= {"booking.com", "booking"}
        if a.id == "makemytrip":
            keys |= {"mmt", "make my trip"}
        if a.id == "paytm_travel":
            keys |= {"paytm"}
        if a.id == "trip_com":
            keys |= {"trip.com", "ctrip"}
        if a.id == "airasia":
            keys |= {"airasia", "air asia"}
        if any(k in text for k in keys if k):
            return a
    return None


def _ymd(slots: dict) -> str:
    return str((slots or {}).get("departure_date") or "")[:10]


def _dmy(slots: dict, sep: str = "/") -> str:
    raw = _ymd(slots)
    try:
        return datetime.strptime(raw, "%Y-%m-%d").strftime(f"%d{sep}%m{sep}%Y")
    except ValueError:
        return raw


def _pax(slots: dict) -> Tuple[int, int, int]:
    p = (slots or {}).get("passengers") or {}
    return (
        max(1, int(p.get("adults") or 1)),
        max(0, int(p.get("children") or 0)),
        max(0, int(p.get("infants") or 0)),
    )


def _cabin(slots: dict) -> str:
    c = str((slots or {}).get("cabin_class") or "economy").lower()
    return "B" if "business" in c else "E"


def build_search_url(agg_id: str, origin: str, dest: str, slots: dict) -> str:
    """Flight search URL for one aggregator. No scrape."""
    o, d = (origin or "").upper(), (dest or "").upper()
    ymd = _ymd(slots).replace("-", "")
    dmy = _dmy(slots)
    adults, children, infants = _pax(slots)
    cabin = _cabin(slots)
    intl = _route_intl(o, d)

    builders: Dict[str, Callable[[], str]] = {
        "booking_com": lambda: (
            f"https://www.booking.com/flights/index.html?"
            f"type=ONEWAY&from={o}.AIRPORT&to={d}.AIRPORT&depart={_ymd(slots)}"
            f"&adults={adults}&cabinClass={'BUSINESS' if cabin == 'B' else 'ECONOMY'}"
        ),
        "goibibo": lambda: (
            f"https://www.goibibo.com/flights/air-{o}-{d}-{ymd}"
            f"--{adults}-{children}-{infants}-{cabin}-{'I' if intl else 'D'}/"
        ),
        "makemytrip": lambda: (
            f"https://www.makemytrip.com/flight/search?itinerary={o}-{d}-{_dmy(slots)}"
            f"&tripType=O&paxType=A-{adults}_C-{children}_I-{infants}"
            f"&intl={'true' if intl else 'false'}&cabinClass={cabin}&ccde=IN&lang=eng"
        ),
        "cleartrip": lambda: (
            f"https://www.cleartrip.com/flights/results?from={o}&to={d}"
            f"&depart_date={dmy}&adults={adults}&childs={children}&infants={infants}"
            f"&class={'Business' if cabin == 'B' else 'Economy'}&intl={'y' if intl else 'n'}"
        ),
        "ixigo": lambda: (
            f"https://www.ixigo.com/search/result/flight?from={o}&to={d}"
            f"&date={_dmy(slots, '')}&adults={adults}&children={children}&infants={infants}"
            f"&class={cabin.lower()}"
        ),
        "easemytrip": lambda: (
            f"https://flight.easemytrip.com/Flight/PassengerDetails?"
            f"org={o}&dept={d}&deptDT={_ymd(slots)}&ADT={adults}&CHD={children}&INF={infants}"
            f"&cabin={'B' if cabin == 'B' else 'E'}&ISINT={'true' if intl else 'false'}"
        ),
        "yatra": lambda: (
            f"https://flight.yatra.com/air-search-ui/dom2/trigger?"
            f"type=O&viewName=normal&flexi=0&noOfSegments=1"
            f"&origin={o}&originCountry=IN&destination={d}"
            f"&flight_depart_date={dmy}&ADT={adults}&CHD={children}&INF={infants}"
            f"&class={'Business' if cabin == 'B' else 'Economy'}"
        ),
        "paytm_travel": lambda: (
            f"https://tickets.paytm.com/flights/flightSearch/{o}/{d}/1/0/0/E/{_ymd(slots)}"
        ),
        "happyeasygo": lambda: (
            f"https://www.happyeasygo.com/flights/{o}-{d}?date={_ymd(slots)}"
            f"&adult={adults}&child={children}&infant={infants}"
        ),
        "via": lambda: (
            f"https://in.via.com/flight/search?sector={o}-{d}&date={_ymd(slots)}"
            f"&adults={adults}&children={children}&infants={infants}"
        ),
        "musafir": lambda: (
            f"https://in.musafir.com/Flights/Search.aspx?triptype=oneway"
            f"&origin={o}&destination={d}&flightdate={_ymd(slots)}"
            f"&adults={adults}&children={children}&infants={infants}"
        ),
        "expedia": lambda: (
            f"https://www.expedia.co.in/Flights-Search?trip=oneway"
            f"&leg1=from%3A{o}%2Cto%3A{d}%2Cdeparture%3A{quote(dmy)}"
            f"&passengers=adults%3A{adults}%2Cchildren%3A{children}%2Cinfantinlap%3AN"
            f"&mode=search"
        ),
        "trip_com": lambda: (
            f"https://www.trip.com/flights/{o.lower()}-to-{d.lower()}/tickets-"
            f"{o.lower()}-{d.lower()}/?dcity={o}&acity={d}&ddate={_ymd(slots)}"
            f"&adult={adults}&child={children}&infant={infants}"
        ),
        "skyscanner": lambda: (
            f"https://www.skyscanner.co.in/transport/flights/"
            f"{o.lower()}/{d.lower()}/{ymd[2:]}/"
            f"?adults={adults}&children={children}&adultsv2={adults}&cabinclass="
            f"{'business' if cabin == 'B' else 'economy'}"
        ),
        "kayak": lambda: (
            f"https://www.kayak.co.in/flights/{o}-{d}/{_ymd(slots)}"
            f"/{adults}adults?sort=bestflight_a"
        ),
        "agoda": lambda: (
            f"https://www.agoda.com/flights/results?"
            f"departureFrom={o}&departureTo={d}&departureDate={_ymd(slots)}"
            f"&adults={adults}&children={children}&infants={infants}"
        ),
        "airasia": lambda: (
            f"https://www.airasia.com/flights/search/?origin={o}&destination={d}"
            f"&departDate={_ymd(slots)}&adult={adults}&child={children}&infant={infants}"
            f"&trip=O&locale=en-gb&currency=INR"
        ),
        "sotc": lambda: (
            f"https://www.sotc.in/flights?from={o}&to={d}&date={_ymd(slots)}"
            f"&adults={adults}&children={children}"
        ),
        "thomascook": lambda: (
            f"https://www.thomascook.in/flights?from={o}&to={d}&date={_ymd(slots)}"
            f"&adults={adults}&children={children}"
        ),
        "akbartravels": lambda: (
            f"https://www.akbartravels.com/flights/search?from={o}&to={d}"
            f"&depart={_ymd(slots)}&adult={adults}&child={children}&infant={infants}"
        ),
    }
    fn = builders.get(agg_id)
    if fn:
        return fn()
    return builders["booking_com"]()


def _route_intl(origin: str, dest: str) -> bool:
    try:
        from src.services.offers import route_segment
        return route_segment(origin, dest) == "international"
    except Exception:
        return False


def coupon_query(agg_id: str, code: str) -> Dict[str, str]:
    code = (code or "").strip()
    if not code:
        return {}
    agg = BY_ID.get(agg_id)
    key = (agg.coupon_param if agg else "coupon") or "coupon"
    return {key: code}
