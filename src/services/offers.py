"""
Offers that help the fare the user is looking at.

Two feeds:
  1. fare_discounts  — discounts already inside a Booking.com fare. Copied from the
                       same API response the search uses. No extra provider calls.
  2. card_offers     — bank / card deals. Scraped from public bank deal pages that
                       list travel offers, plus curated rows typed in by hand.

Booking.com's and the airlines' booking flows are not scraped. Card-specific prices
only appear at payment, so we show the listed deal and tell the user to confirm it.
"""
from __future__ import annotations

import html as html_lib
import logging
import re
from datetime import date, datetime
from typing import Dict, Iterable, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"

TRAVEL_MERCHANTS = {
    "indigo": "IndiGo",
    "air india": "Air India",
    "akasa": "Akasa Air",
    "spicejet": "SpiceJet",
    "vistara": "Vistara",
    "makemytrip": "MakeMyTrip",
    "goibibo": "Goibibo",
    "cleartrip": "Cleartrip",
    "yatra": "Yatra",
    "easemytrip": "EaseMyTrip",
    "ixigo": "ixigo",
    "paytm travel": "Paytm Travel",
    "musafir": "Musafir",
    "booking.com": "Booking.com",
    "agoda": "Agoda",
    "expedia": "Expedia",
}

AIRLINE_MERCHANTS = {"indigo", "air india", "akasa air", "spicejet", "vistara"}

# Airline named in an offer title -> the offer is only for that airline's fares.
AIRLINE_NAMES = {
    "indigo": "IndiGo", "air india express": "Air India Express", "air india": "Air India", "akasa": "Akasa Air",
    "spicejet": "SpiceJet", "vistara": "Vistara", "fly91": "Fly91", "star air": "Star Air", "alliance air": "Alliance Air",
    "malaysia airlines": "Malaysia Airlines", "thai lion": "Thai Lion Air", "thai airways": "Thai Airways",
    "cathay": "Cathay Pacific", "air arabia": "Air Arabia", "etihad": "Etihad Airways", "emirates": "Emirates",
    "qatar airways": "Qatar Airways", "vietjet": "VietJet", "vietnam airlines": "Vietnam Airlines",
    "singapore airlines": "Singapore Airlines", "scoot": "Scoot", "airasia": "AirAsia", "flydubai": "flydubai",
    "british airways": "British Airways", "lufthansa": "Lufthansa", "klm": "KLM", "air france": "Air France",
    "turkish": "Turkish Airlines", "srilankan": "SriLankan", "oman air": "Oman Air", "gulf air": "Gulf Air",
    "saudia": "Saudia", "batik": "Batik Air", "cebu": "Cebu Pacific", "ethiopian": "Ethiopian", "kenya airways": "Kenya Airways",
}


def _airline_in(text: str) -> Optional[str]:
    low = (text or "").lower()
    for key in sorted(AIRLINE_NAMES, key=len, reverse=True):
        if key in low:
            return AIRLINE_NAMES[key]
    return None


def _airline_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower().replace("airlines", "").replace("airways", "").replace("aviation", ""))

_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}


# --------------------------------------------------------------------------- #
# 1. Discounts already inside a fare
# --------------------------------------------------------------------------- #

def fare_discount_rows(flights: Iterable[Dict]) -> List[Dict]:
    """Rows for fare_discounts from normalised Booking offers. Only fares with a cut."""
    rows: List[Dict] = []
    for f in flights or []:
        discount = float(f.get("discount") or 0)
        labels = [d.get("label") for d in (f.get("applied_discounts") or []) if d.get("label")]
        labels += [b.get("text") for b in (f.get("badges") or []) if b.get("text")]
        genius = bool(f.get("genius_airline") or f.get("genius_self"))
        if discount <= 0 and not labels and not genius and not f.get("show_strikethrough"):
            continue
        if not (f.get("origin_code") and f.get("destination_code") and f.get("departure_date")):
            continue
        rows.append({
            "provider": f.get("provider") or "booking_com",
            "origin": f["origin_code"],
            "destination": f["destination_code"],
            "depart_date": f["departure_date"],
            "cabin_class": (f.get("cabin_class") or "economy").lower(),
            "airline": f.get("airline"),
            "flight_number": f.get("flight_number"),
            "price": float(f.get("price") or 0),
            "list_price": float(f["list_price"]) if f.get("list_price") else None,
            "discount": discount,
            "labels": sorted(set(labels)),
            "genius": genius,
            "fare_name": f.get("fare_name") or None,
            "offer_token": f.get("offer_token") or None,
        })
    return rows


# --------------------------------------------------------------------------- #
# 2. Card offers from public bank deal pages
# --------------------------------------------------------------------------- #

def _strip_html(raw: str) -> str:
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", raw, flags=re.S | re.I)
    text = re.sub(r"<(br|/p|/div|/li|/h\d)[^>]*>", "\n", text, flags=re.I)
    text = html_lib.unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"[ \t]+", " ", text)


def _parse_valid_till(text: str) -> Optional[date]:
    m = re.search(r"valid\s+till:?\s*(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\s+(\d{4})", text, re.I)
    if not m:
        return None
    day, month_name, year = m.groups()
    month = _MONTHS.get(month_name.lower())
    if not month:
        return None
    try:
        return date(int(year), month, int(day))
    except ValueError:
        return None


def _money_in(text: str) -> Optional[float]:
    m = re.search(r"(?:₹|inr|rs\.?)\s*([\d,]+)", text, re.I)
    return float(m.group(1).replace(",", "")) if m else None


def _percent_in(text: str) -> Optional[float]:
    m = re.search(r"(\d{1,2}(?:\.\d)?)\s*%", text)
    return float(m.group(1)) if m else None


def _merchant_in(text: str) -> Optional[str]:
    low = text.lower()
    for key, name in sorted(TRAVEL_MERCHANTS.items(), key=lambda kv: -len(kv[0])):
        if key in low:
            return name
    return None


def _card_kind(text: str) -> str:
    low = text.lower()
    if "emi" in low:
        return "emi"
    if "debit" in low and "credit" not in low:
        return "debit"
    return "credit"


def _applies_to(text: str) -> str:
    low = text.lower()
    if "flight" in low and "hotel" in low:
        return "flights+hotels"
    if "hotel" in low or "stay" in low:
        return "hotels"
    if "flight" in low or "air" in low or "domestic" in low:
        return "flights"
    return "travel"


def parse_axis_grab_deals(raw_html: str, base_url: str = "https://www.axisbank.com/grab-deals") -> List[Dict]:
    """Axis Bank's public Grab Deals listing. Each card has a path, title, code, valid-till."""
    rows: List[Dict] = []
    # One card = one <div class="compare-card">. Split there so fields never bleed across cards.
    blocks = re.split(r'(?=<div class="compare-card")', raw_html)
    for block in blocks[1:]:
        def grab(pat: str) -> str:
            m = re.search(pat, block, re.S | re.I)
            return html_lib.unescape(re.sub(r"\s+", " ", m.group(1))).strip() if m else ""

        category = grab(r'id="offer-category-\d+"[^>]*>(.*?)</span>')
        path = grab(r'class="Hide-Brand">(.*?)</p>')
        title = grab(r'id="offer-title-\d+"[^>]*>(.*?)</p>')
        code = grab(r'class="code-cont">(.*?)</p>')
        valid = grab(r"Valid Till:?\s*([^<]{6,40})")
        know_more = grab(r'<a href="([^"]+)"[^>]*class="[^"]*knowmore')
        if not title:
            continue
        if "travel" not in category.lower() and not re.search(r"flight|air", title, re.I):
            continue
        merchant = _merchant_in(title) or _merchant_in(path)
        if not merchant:
            continue
        if code.lower() in ("not available", "na"):
            code = ""
        # axisbank.com now 301s to axis.bank.in; relative Know-More paths only resolve on the new host.
        detail = ("https://www.axis.bank.in" + know_more) if know_more.startswith("/") else (know_more or base_url)
        rows.append({
            "source": "scraped",
            "site": "axis_grab_deals",
            "bank": "Axis Bank",
            "card_name": None,
            "card_kind": _card_kind(category),
            "merchant": merchant,
            "segment": _segment_in(title),
            "eligible_cards": [],
            "title": title[:240],
            "code": code or None,
            "percent_off": _percent_in(title),
            "max_amount": _money_in(title) if "up to" in title.lower() else None,
            "flat_amount": _money_in(title) if "up to" not in title.lower() else None,
            "min_spend": _min_spend_in(title),
            "applies_to": _applies_to(title),
            "terms_url": detail,
            "detail_url": detail,
            "valid_from": None,
            "valid_until": _parse_valid_till("valid till " + valid) if valid else None,
        })
    return rows


def _segment_in(text: str) -> str:
    low = (text or "").lower()
    dom = "domestic" in low
    intl = "international" in low or "intl" in low
    if dom and not intl:
        return "domestic"
    if intl and not dom:
        return "international"
    return "any"


def _min_spend_in(text: str) -> Optional[float]:
    m = re.search(r"(?:min(?:imum)?\.?\s*(?:atv|booking amount|transaction|spend|txn)?[^\d₹]{0,12})(?:₹|rs\.?|inr)?\s*([\d,]{4,})", text or "", re.I)
    return float(m.group(1).replace(",", "")) if m else None


def _cap_in(text: str) -> Optional[float]:
    m = re.search(r"(?:up\s*to|max(?:imum)?\.?\s*discount)\s*[:\-–]?\s*(?:₹|rs\.?|inr)?\s*([\d,]{3,})", text or "", re.I)
    return float(m.group(1).replace(",", "")) if m else None


_EXCLUSION_CUE = re.compile(r"not (?:valid|applicable|eligible)[^.]{0,80}?(?:cards?|following)|excluded?\s+cards?|does not apply", re.I)


def _cards_mentioned(text: str, bank: str, known_cards: Optional[List[str]]) -> Tuple[List[str], List[str]]:
    """(eligible, excluded) catalogue card names the offer text mentions.

    A name inside a 'not valid on the following cards' clause is an exclusion; elsewhere it is
    taken as the card the offer is for. Empty eligible = whole bank.
    """
    low = (text or "").lower()
    eligible, excluded = [], []
    for name in known_cards or []:
        core = re.sub(r"\b(credit|debit|card|cards?|by|bank)\b", " ", name.lower())
        core = re.sub(r"\s+", " ", core).strip()
        if len(core) < 4 or core not in low or bank_key(bank) != bank_key(_bank_from(name) or bank):
            continue
        pos = low.find(core)
        window = low[max(0, pos - 220):pos]
        (excluded if _EXCLUSION_CUE.search(window) else eligible).append(name)
    return sorted(set(eligible)), sorted(set(excluded))


def _eligible_cards(text: str, bank: str, known_cards: Optional[List[str]]) -> List[str]:
    return _cards_mentioned(text, bank, known_cards)[0]


# India's commercial airports, enough to call a leg domestic. Anything else is international.
INDIA_AIRPORTS = {
    "DEL", "BOM", "BLR", "HYD", "MAA", "CCU", "AMD", "PNQ", "GOI", "GOX", "COK", "TRV", "CJB", "IXE", "IXM",
    "IXC", "ATQ", "LKO", "JAI", "VNS", "PAT", "BBI", "RPR", "NAG", "IDR", "BHO", "UDR", "JDH", "DED", "SXR",
    "IXJ", "IXL", "GAU", "IXB", "IMF", "DIB", "IXA", "IXR", "VTZ", "VGA", "TIR", "IXZ", "STV", "BDQ", "RAJ",
    "HBX", "BLR", "IXU", "AGR", "GWL", "JLR", "KNU", "IXD", "TRZ", "MYQ", "CNN", "SHL", "AJL", "DMU", "IXS",
}


def route_segment(origin: str, destination: str) -> str:
    o, d = (origin or "").upper(), (destination or "").upper()
    if o in INDIA_AIRPORTS and d in INDIA_AIRPORTS:
        return "domestic"
    return "international"


BANK_SLUGS = {
    "hdfc": "HDFC Bank", "icici": "ICICI Bank", "axis": "Axis Bank", "sbi": "SBI Card",
    "kotak": "Kotak Mahindra Bank", "indusind": "IndusInd Bank", "yes-bank": "Yes Bank",
    "yes": "Yes Bank", "au-": "AU Small Finance Bank", "au bank": "AU Small Finance Bank",
    "au small": "AU Small Finance Bank", "canara": "Canara Bank",
    "federal": "Federal Bank", "hsbc": "HSBC", "pnb": "Punjab National Bank",
    "idfc": "IDFC First Bank", "rbl": "RBL Bank", "bob": "Bank of Baroda",
    "amex": "American Express", "onecard": "OneCard", "standard-chartered": "Standard Chartered",
}


def _bank_from(*texts: str) -> Optional[str]:
    joined = " ".join(t or "" for t in texts).lower()
    for key, name in BANK_SLUGS.items():
        if key in joined:
            return name
    return None


def _get(url: str, timeout: int = 25) -> str:
    resp = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "en-IN,en;q=0.9"}, timeout=timeout)
    if resp.status_code != 200 or not resp.text:
        raise RuntimeError(f"HTTP {resp.status_code} for {url}")
    return resp.text


def _goibibo_listing(raw_html: str) -> List[Dict]:
    """Bank offer entries from Goibibo's embedded JSON: slug, title, detail URL, expiry."""
    import json as _json
    from datetime import datetime as _dt

    out = []
    for m in re.finditer(r'\{"lobName":"[^"]*","imageUrl".*?"offerDetailsUrl":"[^"]*"\}', raw_html):
        try:
            o = _json.loads(m.group(0))
        except ValueError:
            continue
        verticals = [v.lower() for v in (o.get("vertical") or [])]
        if not any("flight" in v for v in verticals):
            continue
        slug = o.get("slug") or ""
        title = re.sub(r"\s+", " ", o.get("title") or "").strip()
        bank = _bank_from(slug, o.get("imageUrl", ""), title)
        if not bank:
            continue
        valid_until = None
        if o.get("expiryTS"):
            try:
                valid_until = _dt.utcfromtimestamp(int(o["expiryTS"])).date()
            except (ValueError, OverflowError):
                pass
        out.append({"slug": slug, "title": title, "bank": bank, "valid_until": valid_until,
                    "url": o.get("offerDetailsUrl") or o.get("ctaUrl") or ""})
    return out


def parse_goibibo_detail(raw_html: str, entry: Dict, known_cards: Optional[List[str]] = None) -> List[Dict]:
    """The offer table on a Goibibo offer page: one row per leg (domestic / international flights)."""
    # Old rows are left in HTML comments; drop them or we'd resurrect expired offers.
    clean = re.sub(r"<!--.*?-->", " ", raw_html, flags=re.S)
    table = re.search(r"<table.*?</table>", clean, re.S)
    text_all = _strip_html(clean)
    kind = "emi" if re.search(r"\bemi\b", entry["title"] + " " + text_all[:3000], re.I) else (
        "debit" if "debit" in (entry["title"] + " " + entry["slug"]).lower() else "credit")
    eligible, excluded = _cards_mentioned(text_all, entry["bank"], known_cards)
    rows: List[Dict] = []
    if not table:
        return rows
    for tr in re.findall(r"<tr.*?</tr>", table.group(0), re.S):
        cells = [html_lib.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c))).strip()
                 for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]
        cells = [c for c in cells if c]
        if len(cells) < 2 or "flight" not in cells[0].lower():
            continue
        # Column order varies between pages; pick by shape, not position.
        applicable = cells[0]
        codes = [c for c in cells[1:] if re.fullmatch(r"[A-Z0-9]{4,20}(?:\s*[,/&]\s*[A-Z0-9]{4,20})*", c)]
        code = codes[0].split(",")[0].split("/")[0].split("&")[0].strip() if codes else None
        rest = [c for c in cells[1:] if c not in codes and not re.fullmatch(r"[\d\s'’a-zA-Z–-]{6,30}", c)]
        offer_text = max(rest, key=len) if rest else (cells[1] if cells[1] not in codes else "")
        if not offer_text:
            offer_text = entry["title"]
        rows.append({
            "source": "scraped",
            "site": "goibibo",
            "bank": entry["bank"],
            "card_name": eligible[0] if len(eligible) == 1 else None,
            "card_kind": kind,
            "merchant": "Goibibo",
            "segment": _segment_in(applicable),
            "eligible_cards": eligible,
            "excluded_cards": excluded,
            "title": offer_text[:240],
            "code": code,
            "percent_off": _percent_in(offer_text),
            "max_amount": _cap_in(offer_text),
            "flat_amount": _money_in(offer_text) if ("flat" in offer_text.lower() and not _percent_in(offer_text)) else None,
            "min_spend": _min_spend_in(offer_text),
            "applies_to": "flights",
            "terms_url": entry["url"],
            "detail_url": entry["url"],
            "valid_from": None,
            "valid_until": entry.get("valid_until"),
        })
    return rows


def fetch_goibibo(known_cards: Optional[List[str]] = None) -> List[Dict]:
    """Listing page, then each bank offer's detail page for the per-leg table."""
    entries = _goibibo_listing(_get("https://www.goibibo.com/offers/"))
    rows: List[Dict] = []
    for e in entries:
        if not e["url"]:
            continue
        try:
            got = parse_goibibo_detail(_get(e["url"]), e, known_cards)
        except Exception as exc:
            logger.warning("goibibo detail %s failed: %s", e["slug"], exc)
            continue
        if not got:  # detail page had no flight table; keep the headline so the bank still shows
            got = [{
                "source": "scraped", "site": "goibibo", "bank": e["bank"], "card_name": None,
                "card_kind": "emi" if "emi" in e["slug"] else "credit", "merchant": "Goibibo", "segment": "any",
                "eligible_cards": [], "title": e["title"][:240], "code": None,
                "percent_off": _percent_in(e["title"]), "max_amount": _cap_in(e["title"]), "flat_amount": None,
                "min_spend": None, "applies_to": "flights", "terms_url": e["url"], "detail_url": e["url"],
                "valid_from": None, "valid_until": e.get("valid_until"),
            }]
        rows += got
    return rows


def parse_ixigo_offers(raw_html: str, base_url: str = "https://www.ixigo.com/offers") -> List[Dict]:
    """ixigo ships its offers in __NEXT_DATA__: bank, code, leg, end date."""
    import json as _json

    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', raw_html, re.S)
    if not m:
        return []
    try:
        offers = _json.loads(m.group(1))["props"]["pageProps"]["data"]["offers"]
    except (ValueError, KeyError, TypeError):
        return []
    rows: List[Dict] = []
    for o in offers:
        if (o.get("product") or "").upper() != "FLIGHTS" or not o.get("paymentType"):
            continue
        title = re.sub(r"\s+", " ", o.get("offerTitle") or "").strip()
        bank = _bank_from(o["paymentType"], title)
        if not bank:
            continue
        journey = (o.get("journeyType") or "BOTH").upper()
        segment = "domestic" if journey == "DOMESTIC" else "international" if journey == "INTERNATIONAL" else "any"
        valid_until = None
        if o.get("endDate"):
            try:
                valid_until = date.fromisoformat(o["endDate"][:10])
            except ValueError:
                pass
        url = f"https://www.ixigo.com/offers/{o['slug']}" if o.get("slug") else base_url
        rows.append({
            "source": "scraped", "site": "ixigo", "bank": bank, "card_name": None,
            "card_kind": _card_kind(title), "merchant": "ixigo", "segment": segment, "eligible_cards": [],
            "title": title[:240], "code": o.get("offerCode") or None,
            "percent_off": _percent_in(title), "max_amount": _cap_in(title),
            "flat_amount": _money_in(title) if ("flat" in title.lower() and not _percent_in(title)) else None,
            "min_spend": _min_spend_in(title), "applies_to": "flights",
            "terms_url": url, "detail_url": url, "valid_from": None, "valid_until": valid_until,
        })
    return rows


_BANK_WORDS = re.compile(
    r"\b(hdfc|icici|axis|sbi|kotak|indusind|yes bank|hsbc|au bank|au small finance|federal|rbl|idfc|bob|bobcard|"
    r"amex|american express|onecard|standard chartered|canara|pnb|punjab national)\b", re.I)


def _coupon_code(text: str) -> Optional[str]:
    """A Cleartrip-style coupon token such as HDFCCC or CTUPI, when the page actually prints one."""
    skip = {
        "FLIGHT", "FLIGHTS", "CREDIT", "DEBIT", "CARDS", "CARD", "OFFER", "OFFERS",
        "CLEARTRIP", "DOMESTIC", "INTERNATIONAL", "SPICEJET", "INDIGO", "APPLICABLE",
    }
    for token in re.findall(r"\b([A-Z][A-Z0-9]{4,14})\b", text or ""):
        if token in skip:
            continue
        return token
    return None


def parse_cleartrip_offers(raw_html: str, base_url: str = "https://www.cleartrip.com/all-offers/",
                           known_cards: Optional[List[str]] = None) -> List[Dict]:
    """Cleartrip's offer wall. Cards read 'Get up to 20% off' + 'Applicable on SBI & ICICI Credit Cards'."""
    rows: List[Dict] = []
    seen = set()
    for block in re.split(r'(?=<div class="post-wrapper )', raw_html)[1:]:
        def grab(pat: str) -> str:
            m = re.search(pat, block, re.S)
            return html_lib.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1)))).strip() if m else ""
        title = grab(r'class="post-title[^"]*">(.*?)</h4>')
        text = grab(r'class="post-text[^"]*">(.*?)</p>')
        href = (re.search(r'<a href="([^"]+)" class="card_cover_bottom"', block) or [None, ""])[1]
        if not title or href in seen:
            continue
        seen.add(href)
        joined = f"{title} {text}"
        if not _BANK_WORDS.search(joined):
            continue
        if not re.search(r"flight|indigo|air india|akasa|airline|fly|domestic|international|travel", joined, re.I):
            continue
        if re.search(r"\bhotel|\bbus\b|\btrain\b|\bcab", joined, re.I) and not re.search(r"flight", joined, re.I):
            continue
        banks = {_bank_from(w) for w in _BANK_WORDS.findall(joined)} - {None}
        for bank in sorted(banks):
            eligible, excluded = _cards_mentioned(joined, bank, known_cards)
            rows.append({
                "source": "scraped", "site": "cleartrip", "bank": bank,
                "card_name": eligible[0] if len(eligible) == 1 else None,
                "card_kind": _card_kind(text or title), "merchant": "Cleartrip",
                "segment": _segment_in(joined), "eligible_cards": eligible, "excluded_cards": excluded,
                "title": (title + (f" — {text}" if text else ""))[:240],
                "code": _coupon_code(joined),
                "percent_off": _percent_in(title), "max_amount": _cap_in(title),
                "flat_amount": _money_in(title) if ("flat" in title.lower() and not _percent_in(title)) else None,
                "min_spend": _min_spend_in(joined), "applies_to": "flights",
                "terms_url": href or base_url, "detail_url": href or base_url,
                "valid_from": None, "valid_until": None,  # Cleartrip shows validity only on the detail page
            })
    return rows


# name -> callable(known_cards) -> rows. Only public pages that answer a normal GET.
# MakeMyTrip's own domain and the airlines sit behind Akamai and tarpit scripts;
# Goibibo is MakeMyTrip group and publishes the same bank-offer pool openly.
SCRAPE_SOURCES = {
    "axis_grab_deals": lambda known: parse_axis_grab_deals(_get("https://www.axisbank.com/grab-deals")),
    "goibibo": fetch_goibibo,
    "ixigo": lambda known: parse_ixigo_offers(_get("https://www.ixigo.com/offers")),
    "cleartrip": lambda known: parse_cleartrip_offers(_get("https://www.cleartrip.com/all-offers/"), known_cards=known),
}


def fetch_source(name: str, known_cards: Optional[List[str]] = None) -> List[Dict]:
    rows = SCRAPE_SOURCES[name](known_cards)
    now = datetime.utcnow().isoformat()
    for r in rows:
        r["fetched_at"] = now
        r.setdefault("site", name)
        r.setdefault("segment", "any")
        r.setdefault("eligible_cards", [])
        r.setdefault("excluded_cards", [])
        r.setdefault("airline", _airline_in(r.get("title")))
    return rows


# --------------------------------------------------------------------------- #
# 2b. Travel credit card catalogue
# --------------------------------------------------------------------------- #

CO_BRANDS = {
    # text in card name -> (brand, kind)
    "indigo": ("IndiGo", "airline"), "6e": ("IndiGo", "airline"),
    "vistara": ("Vistara", "airline"), "air india": ("Air India", "airline"),
    "etihad": ("Etihad", "airline"), "emirates": ("Emirates", "airline"),
    "qatar": ("Qatar Airways", "airline"), "british airways": ("British Airways", "airline"),
    "avios": ("Avios", "airline"), "lufthansa": ("Lufthansa", "airline"),
    "singapore airlines": ("Singapore Airlines", "airline"), "krisflyer": ("Singapore Airlines", "airline"),
    "akasa": ("Akasa Air", "airline"), "spicejet": ("SpiceJet", "airline"),
    "makemytrip": ("MakeMyTrip", "ota"), "easemytrip": ("EaseMyTrip", "ota"),
    "ixigo": ("ixigo", "ota"), "goibibo": ("Goibibo", "ota"), "cleartrip": ("Cleartrip", "ota"),
    "yatra": ("Yatra", "ota"), "scapia": ("Scapia", "ota"), "paytm": ("Paytm", "ota"),
    "marriott": ("Marriott Bonvoy", "hotel"), "taj": ("Taj", "hotel"), "accor": ("Accor", "hotel"),
    "itc": ("ITC Hotels", "hotel"), "oberoi": ("Oberoi", "hotel"),
}


def _co_brand(name: str):
    low = name.lower()
    for key, val in CO_BRANDS.items():
        if re.search(rf"\b{re.escape(key)}\b", low):
            return val
    return (None, None)


def _fee(value) -> Optional[float]:
    if value in (None, "", "NA", "N/A"):
        return None
    m = re.search(r"[\d,]+(?:\.\d+)?", str(value))
    return float(m.group(0).replace(",", "")) if m else None


def _tier(annual_fee: Optional[float]) -> Optional[str]:
    if annual_fee is None:
        return None
    if annual_fee == 0:
        return "entry"
    if annual_fee < 1500:
        return "entry"
    if annual_fee < 5000:
        return "mid"
    if annual_fee < 12000:
        return "premium"
    return "super-premium"


def parse_paisabazaar_cards(raw_html: str, base_url: str = "") -> List[Dict]:
    """PaisaBazaar renders its card lists as a Next.js RSC payload. One row per card."""
    import json as _json

    chunks = []
    for m in re.finditer(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>', raw_html, re.S):
        try:
            chunks.append(_json.loads('"' + m.group(1) + '"'))
        except ValueError:
            continue
    text = "".join(chunks)
    cards: Dict[str, Dict] = {}
    pos = 0
    while True:
        i = text.find('"card_name"', pos)
        if i < 0:
            break
        j = text.rfind("{", 0, i)
        depth, k = 0, j
        for k in range(j, len(text)):
            if text[k] == "{":
                depth += 1
            elif text[k] == "}":
                depth -= 1
                if depth == 0:
                    break
        pos = k + 1
        try:
            o = _json.loads(text[j:k + 1])
        except ValueError:
            continue
        name = re.sub(r"\s+", " ", o.get("card_name") or o.get("title") or "").strip()
        bank = (o.get("BankName") or "").strip()
        if not name or not bank:
            continue
        key = (bank + "|" + name).lower()
        if key in cards:
            continue
        cats = [c.get("category_name") for c in (o.get("cardCategory") or []) if c.get("category_name")]
        highlights = [h for h in (o.get("highlighter_detail") or o.get("highlighter") or []) if h]
        annual = _fee(o.get("annualFee"))
        brand, kind = _co_brand(name)
        cards[key] = {
            "slug": o.get("slug") or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-"),
            "card_name": name,
            "bank": bank,
            "co_brand": brand,
            "co_brand_kind": kind,
            "tier": _tier(annual),
            "joining_fee": _fee(o.get("joiningFee")),
            "annual_fee": annual,
            "categories": cats,
            "highlights": highlights[:6],
            "lounge_access": any("lounge" in (c or "").lower() for c in cats)
                             or any("lounge" in h.lower() for h in highlights),
            "listing_url": o.get("knowMore") or base_url,
            "terms_url": o.get("termsAndCondition") or None,
            "image_url": o.get("cardImage") or None,
            "source": "paisabazaar",
        }
    return list(cards.values())


CARD_CATALOG_SOURCES = {
    "paisabazaar_travel": ("https://www.paisabazaar.com/credit-card/travel-credit-cards/", parse_paisabazaar_cards),
    "paisabazaar_lounge": ("https://www.paisabazaar.com/credit-card/top-10-credit-cards-airport-lounge-access/", parse_paisabazaar_cards),
    "paisabazaar_forex": ("https://www.paisabazaar.com/credit-card/zero-forex-markup-credit-cards/", parse_paisabazaar_cards),
    "paisabazaar_top25": ("https://www.paisabazaar.com/credit-card/25-best-credit-cards-india/", parse_paisabazaar_cards),
}

_TRAVEL_HINTS = re.compile(r"travel|lounge|mile|avios|flight|airline|hotel|forex|airport|edge miles|krisflyer|skywards|bonvoy", re.I)


def is_travel_card(card: Dict) -> bool:
    """Travel-specific: tagged Travel/Lounge, co-branded with an airline/OTA/hotel, or sells itself on miles."""
    cats = " ".join(card.get("categories") or [])
    if re.search(r"travel|lounge", cats, re.I):
        return True
    if card.get("co_brand"):
        return True
    return bool(_TRAVEL_HINTS.search(" ".join(card.get("highlights") or [])))


_DETAIL_SECTIONS = {
    # heading text (lowercase, 'in') -> key
    "rewards program": "rewards", "reward": "rewards", "earn": "rewards",
    "travel benefit": "travel_benefits", "lounge": "travel_benefits", "milestone": "travel_benefits",
    "welcome": "welcome", "joining benefit": "welcome",
    "get this card if": "get_if", "who should": "get_if",
    "eligibility": "eligibility",
    "pros": "pros", "what do we like": "pros",
    "cons": "cons", "what we don't": "cons",
    "about": "about",
}


def parse_paisabazaar_card_detail(raw_html: str) -> Dict:
    """Sections of a PaisaBazaar card page as plain bullets, plus fees table and rating."""
    body = re.sub(r"<script.*?</script>|<style.*?</style>", " ", raw_html, flags=re.S | re.I)
    m = re.search(r"<h2[^>]*>\s*About .*?</h2>", body, re.S | re.I)
    if m:
        body = body[m.start():]
    cut = re.search(r"<h2[^>]*>\s*(Frequently Asked|Trending Articles|Explore other)", body, re.I)
    if cut:
        body = body[:cut.start()]
    out: Dict = {"rewards": [], "travel_benefits": [], "welcome": [], "get_if": [], "eligibility": [],
                 "pros": [], "cons": [], "about": [], "fees": {}, "rating": None}
    # Fees table: two-column "Fee Type | Amount" rows.
    for tbl in re.findall(r"<table.*?</table>", body, re.S):
        if not re.search(r"Fee Type|Joining Fee", tbl, re.I):
            continue
        for tr in re.findall(r"<tr.*?</tr>", tbl, re.S):
            cells = [html_lib.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c))).strip()
                     for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]
            if len(cells) == 2 and cells[0] and cells[0].lower() != "fee type":
                out["fees"][cells[0]] = cells[1]
        break
    r = re.search(r"Overall Rating.*?(\d(?:\.\d)?)\s*/\s*5", html_lib.unescape(re.sub(r"<[^>]+>", " ", body)), re.S)
    if r:
        out["rating"] = float(r.group(1))
    # Walk headings; gather <li> under each until the next heading.
    parts = re.split(r"(<h[23][^>]*>.*?</h[23]>)", body, flags=re.S)
    current = None
    for part in parts:
        if re.match(r"<h[23]", part):
            head = html_lib.unescape(re.sub(r"<[^>]+>", " ", part)).strip().lower()
            current = None
            for cue, key in _DETAIL_SECTIONS.items():
                if cue in head:
                    current = key
                    break
            continue
        if not current:
            continue
        items = [html_lib.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", li))).strip()
                 for li in re.findall(r"<li[^>]*>(.*?)</li>", part, re.S)]
        if not items and current in ("about", "eligibility"):
            items = [html_lib.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", p))).strip()
                     for p in re.findall(r"<p[^>]*>(.*?)</p>", part, re.S)]
        for it in items:
            it = it.rstrip(".").strip()
            if 8 <= len(it) <= 260 and it not in out[current] and not re.search(
                    r"t&c apply|pb pass|get your|need more help|^note:|not available on paisabazaar|apply now", it, re.I):
                out[current].append(it)
    for k in ("rewards", "travel_benefits", "welcome", "get_if", "eligibility", "pros", "cons", "about"):
        out[k] = out[k][:8]
    return out


def enrich_card_details(cards: List[Dict], max_cards: int = 80, timeout: int = 20) -> int:
    """Fetch each card's PaisaBazaar page and attach details. Returns how many succeeded."""
    done = 0
    for c in cards[:max_cards]:
        url = c.get("listing_url") or ""
        if "paisabazaar.com" not in url:
            continue
        try:
            d = parse_paisabazaar_card_detail(_get(url, timeout))
        except Exception as exc:
            logger.warning("card detail %s failed: %s", c.get("card_name"), exc)
            continue
        if not any(d[k] for k in ("rewards", "travel_benefits", "pros", "get_if")):
            continue
        c["details"] = d
        c["rating"] = d.get("rating")
        c["pros"] = d.get("pros") or []
        c["cons"] = d.get("cons") or []
        extra = [h for h in d["travel_benefits"] + d["rewards"] if h not in (c.get("highlights") or [])]
        c["highlights"] = ((c.get("highlights") or []) + extra)[:8]
        if d["fees"].get("Finance Charges"):
            c["finance_charge"] = d["fees"]["Finance Charges"]
        done += 1
    return done


def fetch_catalog_source(name: str, timeout: int = 25) -> List[Dict]:
    url, parser = CARD_CATALOG_SOURCES[name]
    resp = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "en-IN,en;q=0.9"}, timeout=timeout)
    if resp.status_code != 200 or not resp.text:
        raise RuntimeError(f"{name}: HTTP {resp.status_code}")
    rows = parser(resp.text, resp.url)
    now = datetime.utcnow().isoformat()
    for r in rows:
        r["fetched_at"] = now
    return rows


def merge_catalog(rows: Iterable[Dict]) -> List[Dict]:
    """Same card from several listing pages -> one row, union of categories."""
    merged: Dict[str, Dict] = {}
    for r in rows:
        key = (r["bank"] + "|" + r["card_name"]).lower()
        if key in merged:
            m = merged[key]
            m["categories"] = sorted(set(m["categories"]) | set(r["categories"]))
            m["lounge_access"] = m["lounge_access"] or r["lounge_access"]
            for f in ("joining_fee", "annual_fee", "terms_url", "image_url"):
                m[f] = m[f] if m.get(f) is not None else r.get(f)
        else:
            merged[key] = dict(r)
    return list(merged.values())


def card_pitch(card: Dict, bank_offers: Optional[List[Dict]] = None) -> str:
    """Two or three plain lines a traveller can read in five seconds."""
    parts: List[str] = []
    fee = card.get("annual_fee")
    if fee is None:
        pass
    elif float(fee) == 0:
        parts.append("No annual fee")
    else:
        parts.append(f"₹{float(fee):,.0f} a year")
    if card.get("co_brand"):
        kind = {"airline": "airline", "ota": "booking site", "hotel": "hotel"}.get(card.get("co_brand_kind") or "", "")
        parts.append(f"{card['co_brand']} {kind} card".strip())
    if card.get("lounge_access"):
        parts.append("airport lounge access")
    seen = {p.lower() for p in parts}
    for h in card.get("highlights") or []:
        h = re.sub(r"\s+", " ", h).strip().rstrip(".")
        if not h or h.lower() in seen or "lounge" in h.lower() and "lounge access" in seen:
            continue
        parts.append(h[0].lower() + h[1:] if h[:2].isupper() is False else h)
        seen.add(h.lower())
        if len(parts) >= 5:
            break
    pitch = " · ".join(parts)
    if card.get("pros"):
        pitch += "\nGood: " + "; ".join(p[0].lower() + p[1:] if not p[:2].isupper() else p for p in card["pros"][:3])
    if card.get("cons"):
        pitch += "\nWatch: " + "; ".join(p[0].lower() + p[1:] if not p[:2].isupper() else p for p in card["cons"][:2])
    live = [o for o in (bank_offers or []) if o.get("active", True)]
    if live:
        best = max(live, key=lambda o: (o.get("percent_off") or 0, o.get("max_amount") or o.get("flat_amount") or 0))
        pitch += f"\nNow: {best['title']} ({best['merchant']}" + (f", code {best['code']}" if best.get("code") else "") + ")"
    return pitch


def read_card_intent(text: str) -> Dict:
    """What the traveller actually asked for, pulled out of their own words."""
    low = (text or "").lower()
    airlines = []
    for key, (brand, kind) in CO_BRANDS.items():
        if kind == "airline" and re.search(rf"\b{re.escape(key)}\b", low) and brand not in airlines:
            airlines.append(brand)
    banks = []
    for key, name in BANK_SLUGS.items():
        # 'yes' and 'au' sit inside ordinary sentences, so they never count as a bank mention.
        if key.strip("-") in ("yes", "au") or len(key.strip("-")) < 3:
            continue
        if re.search(rf"\b{re.escape(key.strip('-'))}\b", low) and name not in banks:
            banks.append(name)
    return {
        "low_fee": bool(re.search(r"\b(cheap|budget|free card|no fee|lifetime free|no annual|low fee)\b", low)),
        "lounge": bool(re.search(r"\blounge\b", low)),
        "forex": bool(re.search(r"\b(forex|international|abroad|overseas|foreign currency)\b", low)),
        "miles": bool(re.search(r"\b(miles|points|rewards|cashback)\b", low)),
        "emi": bool(re.search(r"\bemi\b", low)),
        "airlines": airlines,
        "banks": banks,
    }


def intent_line(intent: Dict, segment: str) -> str:
    bits = []
    if intent.get("low_fee"):
        bits.append("keeping the fee low")
    if intent.get("lounge"):
        bits.append("lounge access")
    if intent.get("forex") or segment == "international":
        bits.append("an international trip")
    if intent.get("miles"):
        bits.append("earning miles")
    if intent.get("airlines"):
        bits.append(intent["airlines"][0])
    if intent.get("banks"):
        bits.append(intent["banks"][0])
    if not bits:
        return "Ranked on the live offers for this route and each card's own benefits."
    return "Read from your messages: " + ", ".join(bits) + "."


def suggest_cards(text: str, cards: List[Dict], offers: List[Dict], *, airline: str = "",
                  fare: float = 0.0, segment: str = "any", limit: int = 3) -> Dict:
    """Top cards for this traveller. Reads their words, then the catalogue and live offers.

    No model call: the chat already spent one on understanding the trip, and a second call
    is what stalls the reply when the provider is rate-limited.
    """
    intent = read_card_intent(text)
    target_airlines = list(intent["airlines"])
    if airline and airline not in target_airlines:
        target_airlines.append(airline)
    by_bank = offers_by_bank(offers)
    ranked = []
    for card in cards or []:
        if intent["banks"] and bank_key(card.get("bank")) not in {bank_key(b) for b in intent["banks"]}:
            continue
        score = float(card.get("rating") or 3) * 4
        reasons: List[str] = []
        brand = card.get("co_brand") or ""
        if brand and any(_airline_key(brand) == _airline_key(a) for a in target_airlines):
            score += 50 if brand in intent["airlines"] else 24
            reasons.append(f"co-branded with {brand}, the airline on this trip")
        fee = card.get("annual_fee")
        fee_f = float(fee) if fee is not None else None
        if intent["low_fee"]:
            score += 22 if fee_f == 0 else (10 if fee_f is not None and fee_f <= 1000 else -8)
            if fee_f == 0:
                reasons.append("no annual fee, which is what you asked for")
        if intent["lounge"] and card.get("lounge_access"):
            score += 16
            reasons.append("airport lounge access, which you mentioned")
        elif segment == "international" and card.get("lounge_access"):
            score += 6
        blob = " ".join(card.get("highlights") or []).lower() + " " + (card.get("pitch") or "").lower()
        if (intent["forex"] or segment == "international") and re.search(r"zero forex|0% forex|no forex|forex markup fee of [01]", blob):
            score += 14
            reasons.append("low or zero forex markup, useful abroad")
        if intent["miles"] and re.search(r"mile|reward|point", blob):
            score += 8
        deals = offers_for_fare(
            by_bank.get(bank_key(card.get("bank")), []),
            airline, card_name=card.get("card_name"), fare=fare, segment=segment,
        )
        best = deals[0] if deals else None
        saving = estimate_saving(best, fare) if best and fare else None
        if saving:
            score += min(saving, 4000) / 180
            where = best.get("merchant") or "the booking site"
            code = f", code {best['code']}" if best.get("code") else ""
            reasons.insert(0, f"about ₹{saving:,.0f} off this fare via {where}{code}")
        elif best and best.get("percent_off"):
            score += 4
            reasons.insert(0, f"up to {float(best['percent_off']):g}% off via {best.get('merchant')}")
        if card.get("pros"):
            reasons.append(card["pros"][0][0].lower() + card["pros"][0][1:])
        if not reasons:
            continue
        ranked.append((score, card, best, saving, reasons[:3]))
    ranked.sort(key=lambda t: t[0], reverse=True)
    picks, seen = [], set()
    for score, card, best, saving, reasons in ranked:
        key = bank_key(card.get("bank"))
        if key in seen:
            continue
        seen.add(key)
        picks.append({
            "card_name": card.get("card_name"),
            "bank": card.get("bank"),
            "annual_fee": card.get("annual_fee"),
            "lounge_access": bool(card.get("lounge_access")),
            "rating": card.get("rating"),
            "listing_url": card.get("listing_url"),
            "terms_url": card.get("terms_url"),
            "reason": ". ".join(r[0].upper() + r[1:] if r else r for r in reasons) + ".",
            "saving": saving,
            "code": (best or {}).get("code"),
            "merchant": (best or {}).get("merchant"),
        })
        if len(picks) >= limit:
            break
    return {"line": intent_line(intent, segment), "picks": picks}


def offers_by_bank(card_rows: List[Dict]) -> Dict[str, List[Dict]]:
    out: Dict[str, List[Dict]] = {}
    for r in card_rows or []:
        if r.get("active", True) and (r.get("applies_to") or "").startswith("flights") or r.get("applies_to") == "travel":
            out.setdefault(bank_key(r.get("bank")), []).append(r)
    return out


# --------------------------------------------------------------------------- #
# 2c. Watched routes
# --------------------------------------------------------------------------- #

# Two per category. Indian metros one side; Hanoi, Bangkok, New York JFK, London Heathrow the other.
WATCH_ROUTES = [
    {"category": "domestic", "origin": "DEL", "destination": "BOM"},
    {"category": "domestic", "origin": "HYD", "destination": "BLR"},
    {"category": "outbound", "origin": "DEL", "destination": "BKK"},
    {"category": "outbound", "origin": "BOM", "destination": "JFK"},
    {"category": "inbound",  "origin": "HAN", "destination": "BLR"},
    {"category": "inbound",  "origin": "LHR", "destination": "HYD"},
]
WATCH_DAYS_AHEAD = 30


def watch_route_row(route: Dict, depart_date: str, flights: List[Dict], error: Optional[str],
                    card_rows: List[Dict]) -> Dict:
    row = {
        "category": route["category"],
        "origin": route["origin"],
        "destination": route["destination"],
        "depart_date": depart_date,
        "provider": None,
        "status": error or ("ok" if flights else "no_fares"),
        "fare_count": len(flights or []),
        "fare_discount": 0,
        "card_offer_ids": [int(o["id"]) for o in offers_for_fare(
            card_rows, "", segment=route_segment(route["origin"], route["destination"])) if o.get("id")],
    }
    if flights:
        cheapest = min(flights, key=lambda f: float(f.get("price") or 1e12))
        row.update({
            "provider": cheapest.get("provider"),
            "cheapest_price": float(cheapest.get("price") or 0),
            "currency": cheapest.get("currency") or "INR",
            "airline": cheapest.get("airline"),
            "flight_number": cheapest.get("flight_number"),
            "duration_minutes": cheapest.get("duration_minutes"),
            "stops": cheapest.get("stops"),
            "fare_discount": float(cheapest.get("discount") or 0),
            "deep_link": cheapest.get("source_url"),
        })
    return row


# Hand-entered deals. Edit here or insert into card_offers with source='curated'.
CURATED_CARD_OFFERS: List[Dict] = [
    # Example shape. Fill with offers you've confirmed on the bank/airline page.
    # {"bank": "HDFC Bank", "card_name": "HDFC Infinia", "card_kind": "credit", "merchant": "IndiGo",
    #  "title": "10% off up to ₹1,500 on IndiGo", "code": None, "percent_off": 10, "max_amount": 1500,
    #  "applies_to": "flights", "terms_url": "https://...", "valid_until": "2026-11-30"},
]


# --------------------------------------------------------------------------- #
# 3. Matching for the UI
# --------------------------------------------------------------------------- #

def bank_key(name: Optional[str]) -> str:
    """'SBI Cards' == 'SBI Card', 'YES BANK' == 'Yes Bank', 'HSBC Bank' == 'HSBC'."""
    low = (name or "").lower()
    low = re.sub(r"\b(bank|cards?|ltd|limited|small finance|mahindra|first)\b", " ", low)
    return re.sub(r"[^a-z0-9]+", "", low)


def offers_for_fare(card_rows: List[Dict], airline: str, bank: Optional[str] = None,
                    card_name: Optional[str] = None, today: Optional[date] = None,
                    fare: float = 0.0, segment: Optional[str] = None) -> List[Dict]:
    """Card offers that could apply to this fare. Airline match or any flights OTA.

    segment: 'domestic' | 'international' drops offers for the other leg. card_name narrows
    to offers that name this card, or whole-bank offers that name none.
    """
    today = today or date.today()
    out = []
    for r in card_rows or []:
        if not r.get("active", True):
            continue
        seg = r.get("segment") or "any"
        if segment and seg != "any" and seg != segment:
            continue
        if fare and r.get("min_spend") and float(r["min_spend"]) > fare:
            continue
        if card_name:
            same = lambda e: e.lower() in card_name.lower() or card_name.lower() in e.lower()
            if any(same(e) for e in r.get("excluded_cards") or []):
                continue
            if r.get("eligible_cards") and not any(same(e) for e in r["eligible_cards"]):
                continue
        vu = r.get("valid_until")
        if isinstance(vu, str):
            try:
                vu = date.fromisoformat(vu[:10])
            except ValueError:
                vu = None
        if vu and vu < today:
            continue
        if bank and bank_key(r.get("bank")) != bank_key(bank):
            continue
        merchant = (r.get("merchant") or "").lower()
        only_airline = r.get("airline")
        if only_airline and airline and _airline_key(only_airline) != _airline_key(airline):
            continue  # 'up to 20% off on Malaysia Airlines' is no use on an IndiGo fare
        if merchant == (airline or "").lower() or (only_airline and airline and _airline_key(only_airline) == _airline_key(airline)):
            r = {**r, "match": "airline"}
        elif merchant in AIRLINE_MERCHANTS:
            continue  # another airline's own offer can't apply to this fare
        elif r.get("applies_to", "").startswith("flights") or r.get("applies_to") == "travel":
            r = {**r, "match": "ota"}
        else:
            continue
        out.append(r)
    out.sort(key=lambda r: (r.get("match") != "airline", -(estimate_saving(r, fare) or 0)))
    return out


def estimate_saving(offer: Dict, fare: float) -> Optional[float]:
    """Best-case cut on this fare. Never more than the fare itself."""
    cut: Optional[float] = None
    headline = "up to" in (offer.get("title") or "").lower()
    if offer.get("flat_amount"):
        cut = float(offer["flat_amount"])
    elif offer.get("percent_off"):
        if headline and not offer.get("max_amount"):
            return None  # 'up to 35%' with no stated cap is a headline, not a number we can promise
        cut = fare * float(offer["percent_off"]) / 100 if fare else None
        if cut is not None and offer.get("max_amount"):
            cut = min(cut, float(offer["max_amount"]))
    # "up to ₹X" with no percent is tiered in the fine print; quote it, don't compute it.
    if cut is None:
        return None
    if fare:
        cut = min(cut, fare)
    return round(cut, 0)
