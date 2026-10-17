"""Public Axis Bank credit cards and the rupee value Axis prints per point.

The catalogue comes from the public credit-card index. The rupee figure comes
from that card's own benefits, and only when Axis states a value for one point
("1 EDGE Mile is equal to ₹1") or prices a pile of that card's points
("1,500 EDGE Miles worth ₹1,500"). A partner-mile ratio and another card's
promo on the same page are ignored, so a card with no stated rate stays blank.
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

from src.services.scrape_http import ScrapeBlocked, polite_get

logger = logging.getLogger(__name__)

INDEX_URL = "https://www.axis.bank.in/cards/credit-card"
ORIGIN = "https://www.axis.bank.in"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
)
CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "axis_points.json"
CACHE_HOURS = 24

_MONEY = r"(?:₹|Rs\.?|INR)\s*([0-9][0-9,]*(?:\.[0-9]+)?)"
_RATE_PATTERNS = (
    (re.compile(rf"1\s+EDGE\s+Miles?\s+is equal to\s+{_MONEY}", re.I), "EDGE Mile"),
    (re.compile(rf"Each\s+EDGE\s+Miles?\s+worth of\s+{_MONEY}", re.I), "EDGE Mile"),
    (re.compile(rf"Redemption Value of Each\s+EDGE\s+REWARD\s+Points?\s*=\s*{_MONEY}", re.I), "EDGE REWARD Point"),
    (re.compile(rf"1\s+EDGE\s+REWARD\s+Points?\s*(?:=|is equal to|is worth)\s*{_MONEY}", re.I), "EDGE REWARD Point"),
    (re.compile(rf"Each\s+EDGE\s+REWARD\s+Points?\s+(?:is\s+)?worth(?:\s+of)?\s*{_MONEY}", re.I), "EDGE REWARD Point"),
)
_GENERIC_RATE = re.compile(
    rf"(?:1|one|Each)\s+([A-Za-z][A-Za-z0-9& ]{{2,40}}?)\s+(?:is equal to|worth of)\s+{_MONEY}",
    re.I,
)
_LABELS = (
    ("EDGE Mile", re.compile(r"EDGE\s+Miles?", re.I)),
    ("EDGE REWARD Point", re.compile(r"EDGE\s+REWARD\s+Points?", re.I)),
    ("IndiGo BluChip", re.compile(r"BluChips?", re.I)),
    ("Club Vistara Point", re.compile(r"Club\s+Vistara", re.I)),
    ("Miles & More", re.compile(r"Miles\s+(?:and|&)\s+More", re.I)),
)

_lock = threading.Lock()
_memory: Optional[List[Dict]] = None
_memory_at: Optional[datetime] = None


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept-Language": "en-IN,en;q=0.9"})
    return session


def _visible(page: str) -> str:
    text = re.sub(r"(?is)<script[\s\S]*?</script>", " ", page or "")
    text = re.sub(r"(?is)<style[\s\S]*?</style>", " ", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = html_lib.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _clean_name(name: str) -> str:
    name = re.sub(r"\s+", " ", name or "").strip(" .")
    name = re.sub(r"\bMiles\b", "Mile", name, flags=re.I)
    name = re.sub(r"\bPoints\b", "Point", name, flags=re.I)
    name = re.sub(r"\bStars\b", "Star", name, flags=re.I)
    if name.lower() == "star":
        return "Star"
    if re.fullmatch(r"edge reward point", name, re.I):
        return "EDGE REWARD Point"
    if re.fullmatch(r"edge mile", name, re.I):
        return "EDGE Mile"
    return name


def _ok_name(name: str) -> bool:
    low = name.lower()
    if any(word in low for word in ("partner", "voucher", "welcome", "worth", "spend", "fee")):
        return False
    return 2 <= len(name) <= 40


def _points_name(text: str) -> str:
    for name, pattern in _LABELS:
        if pattern.search(text or ""):
            return name
    return ""


def _amount(raw: str) -> float:
    return float((raw or "").replace(",", ""))


_NAME_SKIP = {"axis", "bank", "credit", "card", "cards", "the", "and", "for", "with", "more"}


def _card_tokens(name: str) -> List[str]:
    return [
        word for word in re.findall(r"[a-z0-9]+", (name or "").lower())
        if word not in _NAME_SKIP and len(word) >= 3
    ]


def _same_card(heading: str, card_name: str) -> bool:
    low = heading.lower()
    return any(token in low for token in _card_tokens(card_name))


def _own_copy(page: str, card_name: str) -> str:
    """Text from this card's heading up to the next card, promo row excluded."""
    product = page or ""
    cut = product.lower().find('class="compare-card"')
    if cut > 0:
        product = product[:cut]
    start = 0
    for match in re.finditer(r"<h1[^>]*>(.*?)</h1>", product, re.S | re.I):
        heading = re.sub(r"<[^>]+>", " ", match.group(1))
        if _same_card(heading, card_name):
            start = match.start()
            break
    section = product[start:]
    for match in re.finditer(r"<h[23][^>]*>(.*?)</h[23]>", section, re.S | re.I):
        if match.start() < 80:
            continue
        heading = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", match.group(1))).strip()
        if "credit card" in heading.lower() and not _same_card(heading, card_name):
            section = section[:match.start()]
            break
    return _visible(section)


def _paise(rate: float) -> Optional[float]:
    paise = round(rate * 100)
    if abs(rate * 100 - paise) > 0.02 or not 0 < paise <= 500:
        return None
    return paise / 100.0


def _published_rate(text: str) -> Tuple[str, Optional[float]]:
    hits: List[Tuple[str, float]] = []
    for pattern, name in _RATE_PATTERNS:
        for match in pattern.finditer(text):
            hits.append((name, _amount(match.group(1))))
    if not hits:
        for match in _GENERIC_RATE.finditer(text):
            name = _clean_name(match.group(1))
            if _ok_name(name):
                hits.append((name, _amount(match.group(2))))
    bundle = re.compile(
        rf"([0-9][0-9,]*)\s+(EDGE\s+Miles?|EDGE\s+REWARD\s+Points?|IndiGo\s+BluChips?|Stars?)"
        rf"\s+[^.]{{0,80}}?worth\s+{_MONEY}",
        re.I,
    )
    bundles: List[Tuple[str, float]] = []
    for match in bundle.finditer(text):
        count = _amount(match.group(1))
        value = _amount(match.group(3))
        if count <= 0 or value <= 0:
            continue
        rate = _paise(value / count)
        if rate is None:
            continue
        bundles.append((_clean_name(match.group(2)), rate))
    if bundles and not hits:
        rates = {rate for _name, rate in bundles}
        names = {name for name, _rate in bundles}
        if len(rates) == 1 and len(names) == 1:
            hits.append((bundles[0][0], bundles[0][1]))
    hits = [(name, rate) for name, rate in hits if _paise(rate) is not None]
    if not hits:
        return "", None
    best = max(set(hits), key=lambda hit: hits.count(hit))
    return best[0], best[1]


def parse_listing(page: str) -> List[Dict]:
    """Card name, product path, and the benefit line from the public index."""
    cards: List[Dict] = []
    seen = set()
    for block in re.split(r'(?=<div class="w-full px-4 card-col)', page or ""):
        name_match = re.search(r'<h3 class="category-title">\s*([^<]+)', block)
        href_match = re.search(r'href="(/cards/credit-card/[^"#?]+)"', block)
        if not name_match or not href_match:
            continue
        path = href_match.group(1).strip()
        if path in seen:
            continue
        seen.add(path)
        benefits = " ".join(re.findall(r'class="benefits-item">(.*?)</li>', block, re.S))
        slug = path.rstrip("/").split("/")[-1]
        slug = re.sub(r"-credit-card$", "", slug)
        slug = re.sub(r"[^a-z0-9-]", "", slug.lower()).strip("-")
        card_id = slug if slug.startswith("axis-") else f"axis-{slug}"
        cards.append({
            "id": card_id[:64],
            "name": re.sub(r"\s+", " ", html_lib.unescape(name_match.group(1))).strip(),
            "path": path,
            "benefits": _visible(benefits),
        })
    return cards


def _read_card(session: requests.Session, card: Dict) -> Dict:
    url = ORIGIN + card["path"]
    own_copy = card.get("benefits") or ""
    points_name = ""
    rate = None
    try:
        page = polite_get(url, session=session, timeout=25)
        own_copy = f"{own_copy} {_own_copy(page, card['name'])}".strip()
    except ScrapeBlocked:
        raise
    except Exception:
        logger.warning("Axis card page failed: %s", url)
    found_name, rate = _published_rate(own_copy)
    points_name = found_name or _points_name(own_copy)
    return {
        "id": card["id"],
        "name": card["name"],
        "bank": "Axis Bank",
        "points_name": points_name,
        "inr_per_point": rate,
        "url": url,
    }


def scrape_axis_cards() -> List[Dict]:
    session = _session()
    index_html = polite_get(INDEX_URL, session=session, timeout=30)
    listing = parse_listing(index_html)
    if not listing:
        raise RuntimeError("Axis credit-card index had no cards.")
    rows: List[Dict] = []
    # Two workers + polite_get pacing; stop Axis for this run on 403/429.
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_read_card, session, card) for card in listing]
        for fut in futures:
            try:
                rows.append(fut.result())
            except ScrapeBlocked as exc:
                logger.warning("Axis blocked (%s); keeping %d cards already read", exc, len(rows))
                for pending in futures:
                    pending.cancel()
                break
    rows.sort(key=lambda row: (row.get("inr_per_point") is None, row["name"].lower()))
    return rows


def _load_cache() -> Optional[Tuple[datetime, List[Dict]]]:
    try:
        payload = json.loads(CACHE_PATH.read_text())
        fetched = datetime.fromisoformat(payload["fetched_at"])
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        cards = payload.get("cards") or []
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    if not isinstance(cards, list) or not cards:
        return None
    return fetched, cards


def _write_cache(cards: List[Dict]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "cards": cards,
    }
    CACHE_PATH.write_text(json.dumps(payload, indent=2))
    try:
        from src.services.supabase_persistence import upsert_card_point_rates
        saved = upsert_card_point_rates(cards)
        if saved:
            logger.info("Saved %s card point rates to Supabase", saved)
    except Exception:
        logger.warning("Could not save card point rates", exc_info=True)


def _as_wallet(row: Dict) -> Dict:
    return {
        "id": row.get("id") or "",
        "name": row.get("name") or "",
        "bank": "Axis Bank",
        "currency": row.get("points_name") or row.get("currency") or "",
        "inr_per_point": row.get("inr_per_point"),
        "source_url": row.get("url") or row.get("source_url") or "",
    }


def axis_cards() -> List[Dict]:
    """Axis cards for the fare screen. A fresh public scrape fills the cache."""
    global _memory, _memory_at
    now = datetime.now(timezone.utc)
    with _lock:
        if _memory is not None and _memory_at and now - _memory_at < timedelta(hours=CACHE_HOURS):
            return [_as_wallet(row) for row in _memory]
        cached = _load_cache()
        if cached and now - cached[0] < timedelta(hours=CACHE_HOURS):
            _memory, _memory_at = cached[1], cached[0]
            return [_as_wallet(row) for row in _memory]
        try:
            rows = scrape_axis_cards()
        except (requests.RequestException, RuntimeError):
            logger.warning("Axis points scrape failed", exc_info=True)
            if cached:
                _memory, _memory_at = cached[1], now
                return [_as_wallet(row) for row in _memory]
            return []
        _write_cache(rows)
        _memory, _memory_at = rows, now
        return [_as_wallet(row) for row in rows]
