"""Rupees per point for the cards in the catalogue and on the fare screen.

Each credit_cards row is read from its public listing. A rate is stored only
when that page states a value for one point. The Axis rows already scraped
from axis.bank.in stay, and a catalogue rate does not replace an official
Axis rate. The fare screen then reads this same set.
"""

from __future__ import annotations

import html as html_lib
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

import requests

from src.services.axis_points import _paise
from src.services.points_transfer import wallet_cards
from src.services.scrape_http import ScrapeBlocked, polite_get
from src.services.supabase_persistence import (
    load_card_point_rates,
    load_credit_card_pages,
    replace_card_point_rates,
)

logger = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
)
_STOP = {"credit", "card", "cards", "bank", "the", "and", "for", "with", "small", "finance"}
_UNIT = re.compile(
    r"1\s+(?:[A-Za-z]+\s+){0,4}?"
    r"((?:EDGE\s+Miles?|EDGE\s+REWARD\s+Points?|Reward\s+Points?|Air\s+Miles?|RP|Points?|Miles?))"
    r"\s*(?:=|is equal to)\s*(?:₹|Rs\.?|INR)\s*([0-9]+(?:\.[0-9]+)?)"
    r"([^.]{0,50})",
    re.I,
)
_PER = re.compile(
    r"(?:₹|Rs\.?|INR)\s*([0-9]+(?:\.[0-9]+)?)\s+per\s+"
    r"(reward\s+point|edge\s+mile|edge\s+reward\s+point|point|mile)",
    re.I,
)

# Public listing pages for fare-screen cards that are not in credit_cards.
_UI_PAGES = (
    {
        "bank": "American Express",
        "card_name": "Amex Membership Rewards",
        "url": "https://www.paisabazaar.com/american-express/amex-platinum-travel-credit-card/",
    },
    {
        "bank": "Axis Bank",
        "card_name": "Axis Olympus",
        "url": "https://www.axis.bank.in/cards/credit-card/olympus-credit-card",
    },
    {
        "bank": "HDFC Bank",
        "card_name": "HDFC Diners Black",
        "url": "https://www.paisabazaar.com/hdfc-bank/diners-club-black-credit-card/",
    },
)


def _visible(page: str) -> str:
    text = re.sub(r"(?is)<script[\s\S]*?</script>", " ", page or "")
    text = re.sub(r"(?is)<style[\s\S]*?</style>", " ", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = html_lib.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _points_name(raw: str) -> str:
    low = re.sub(r"\s+", " ", raw or "").strip().lower()
    if low in {"rp", "reward point", "reward points", "point", "points"}:
        return "Reward Point"
    if low in {"edge mile", "edge miles"}:
        return "EDGE Mile"
    if low in {"edge reward point", "edge reward points"}:
        return "EDGE REWARD Point"
    if low in {"air mile", "air miles", "mile", "miles"}:
        return "Air Mile"
    return re.sub(r"\s+", " ", raw or "").strip()


def _kind(text: str) -> str:
    low = (text or "").lower()
    other = any(word in low for word in ("other", "catalogue", "catalog", "voucher", "product", "cashback"))
    travel = any(word in low for word in ("travel", "flight", "airline", "hotel", "air mile"))
    if travel and not other:
        return "travel"
    if other and not travel:
        return "other"
    return "plain"


def published_rate(text: str) -> Tuple[str, Optional[float]]:
    """The rupee value a page states for one point, preferring the travel rate."""
    hits: List[Tuple[str, float, str]] = []
    for match in _UNIT.finditer(text or ""):
        rate = _paise(float(match.group(2)))
        if rate is None:
            continue
        hits.append((_points_name(match.group(1)), rate, _kind(match.group(3))))
    for match in _PER.finditer(text or ""):
        rate = _paise(float(match.group(1)))
        if rate is None:
            continue
        start = max(0, match.start() - 40)
        hits.append((_points_name(match.group(2)), rate, _kind(text[start:match.end() + 40])))
    if not hits:
        return "", None
    for kind in ("travel", "plain", "other"):
        chosen = [hit for hit in hits if hit[2] == kind]
        rates = {hit[1] for hit in chosen}
        names = {hit[0] for hit in chosen}
        if chosen and len(rates) == 1 and len(names) == 1:
            return chosen[0][0], chosen[0][1]
    return "", None


def _bank_key(bank: str) -> str:
    words = [word for word in re.findall(r"[a-z0-9]+", (bank or "").lower()) if word not in _STOP]
    return " ".join(words)


def _tokens(bank: str, name: str) -> frozenset:
    bank_words = set(re.findall(r"[a-z0-9]+", (bank or "").lower()))
    words = re.findall(r"[a-z0-9]+", f"{name or ''}".lower())
    return frozenset(word for word in words if word not in _STOP and word not in bank_words and len(word) >= 3)


def _same_card(left: Dict, right: Dict) -> bool:
    if _bank_key(left.get("bank") or "") != _bank_key(right.get("bank") or ""):
        return False
    left_tokens = _tokens(left.get("bank") or "", left.get("card_name") or left.get("name") or "")
    right_tokens = _tokens(right.get("bank") or "", right.get("card_name") or right.get("name") or "")
    return bool(left_tokens) and left_tokens == right_tokens


def _slug(bank: str, name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", f"{bank}-{name}".lower()).strip("-")[:64]


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept-Language": "en-IN,en;q=0.9"})
    return session


def _read_page(session: requests.Session, url: str) -> str:
    if not url:
        return ""
    try:
        return _visible(polite_get(url, session=session, timeout=25))
    except ScrapeBlocked:
        raise
    except Exception as exc:
        logger.warning("Card page failed: %s (%s)", url, exc)
        return ""


def _scrape_pages(pages: List[Dict]) -> List[Dict]:
    """Serial-ish scrape (2 workers + host pacing). Stops the host on 403/429."""
    session = _session()
    out: List[Dict] = []
    blocked = False

    def one(page: Dict) -> Optional[Dict]:
        nonlocal blocked
        if blocked:
            return None
        url = page.get("url") or page.get("listing_url") or ""
        try:
            text = _read_page(session, url)
        except ScrapeBlocked as exc:
            blocked = True
            logger.warning("Point-rate host blocked (%s); keeping prior pages", exc)
            return None
        points_name, rate = published_rate(text)
        return {
            "bank": page["bank"],
            "card_name": page.get("card_name") or page.get("name"),
            "points_name": points_name,
            "inr_per_point": rate,
            "source_url": url,
            "slug": _slug(page["bank"], page.get("card_name") or page.get("name") or ""),
        }

    with ThreadPoolExecutor(max_workers=2) as pool:
        for row in pool.map(one, pages):
            if row:
                out.append(row)
    return out


def _prefer(official: Dict, listed: Dict) -> Dict:
    """Keep the bank page's rate when both pages state one."""
    row = {
        "bank": listed.get("bank") or official.get("bank"),
        "card_name": listed.get("card_name") or listed.get("name") or official.get("name"),
        "slug": official.get("id") or official.get("slug") or listed.get("slug"),
        "source_url": listed.get("source_url") or listed.get("url") or official.get("url"),
        "points_name": listed.get("points_name") or "",
        "inr_per_point": listed.get("inr_per_point"),
    }
    if official.get("inr_per_point") is not None:
        row["inr_per_point"] = official.get("inr_per_point")
        row["points_name"] = official.get("points_name") or row["points_name"]
        row["source_url"] = official.get("url") or official.get("source_url") or row["source_url"]
        row["slug"] = official.get("id") or official.get("slug") or row["slug"]
    elif not row["points_name"]:
        row["points_name"] = official.get("points_name") or ""
    return row


def sync_card_point_rates() -> List[Dict]:
    """Scrape the catalogue and the fare-screen cards, then replace the point table."""
    catalogue = _scrape_pages(load_credit_card_pages())
    ui_pages = []
    for page in _UI_PAGES:
        if any(_same_card(page, row) for row in catalogue):
            continue
        ui_pages.append(page)
    extra = _scrape_pages(ui_pages) if ui_pages else []

    official = []
    for row in load_card_point_rates():
        if _bank_key(row.get("bank") or "") != "axis":
            continue
        official.append({
            "id": row.get("slug"),
            "bank": row.get("bank"),
            "name": row.get("card_name"),
            "card_name": row.get("card_name"),
            "points_name": row.get("points_name") or "",
            "inr_per_point": float(row["inr_per_point"]) if row.get("inr_per_point") is not None else None,
            "url": row.get("source_url") or "",
        })

    merged: List[Dict] = []
    used_official = set()
    for listed in catalogue + extra:
        match = next((row for row in official if _same_card(listed, row)), None)
        if match:
            used_official.add(id(match))
            merged.append(_prefer(match, listed))
        else:
            merged.append(listed)
    for row in official:
        if id(row) in used_official:
            continue
        if any(_same_card(row, kept) for kept in merged):
            continue
        merged.append({
            "bank": row["bank"],
            "card_name": row["card_name"],
            "slug": row.get("id") or _slug(row["bank"], row["card_name"]),
            "points_name": row.get("points_name") or "",
            "inr_per_point": row.get("inr_per_point"),
            "source_url": row.get("url") or "",
        })

    for card in wallet_cards():
        shaped = {"bank": card["bank"], "card_name": card["name"], "name": card["name"]}
        if any(_same_card(shaped, row) for row in merged):
            continue
        merged.append({
            "bank": card["bank"],
            "card_name": card["name"],
            "slug": card["id"],
            "points_name": "",
            "inr_per_point": None,
            "source_url": "",
        })

    replace_card_point_rates(merged)
    return merged
