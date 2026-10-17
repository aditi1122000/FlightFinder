"""Polite HTTP for public scrape pages — delays, no hammering after 403/429."""
from __future__ import annotations

import logging
import os
import random
import threading
import time
from typing import Optional
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
)

# Seconds between requests to the same host (override with SCRAPE_GAP_S).
GAP_S = float(os.getenv("SCRAPE_GAP_S") or "3.0")
# Extra pause after a blocked response before the next host.
BLOCK_COOLDOWN_S = float(os.getenv("SCRAPE_BLOCK_COOLDOWN_S") or "8.0")

_BLOCKED = {403, 429}
_last_host_at: dict[str, float] = {}
_lock = threading.Lock()


class ScrapeBlocked(RuntimeError):
    """Host refused us (403/429). Do not retry this source in the same run."""

    def __init__(self, url: str, status: int, body: str = ""):
        self.url = url
        self.status = status
        self.body = (body or "")[:200]
        host = urlparse(url).netloc or url
        super().__init__(f"HTTP {status} blocked {host}")


def _host(url: str) -> str:
    return (urlparse(url).netloc or url).lower()


def _pace(host: str) -> None:
    with _lock:
        last = _last_host_at.get(host, 0.0)
        wait = GAP_S + random.uniform(0.2, 1.2) - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        _last_host_at[host] = time.monotonic()


def polite_get(
    url: str,
    *,
    timeout: int = 25,
    session: Optional[requests.Session] = None,
    headers: Optional[dict] = None,
) -> str:
    """GET a public page. Raises ScrapeBlocked on 403/429; RuntimeError otherwise."""
    host = _host(url)
    _pace(host)
    hdrs = {"User-Agent": UA, "Accept-Language": "en-IN,en;q=0.9"}
    if headers:
        hdrs.update(headers)
    try:
        if session is not None:
            resp = session.get(url, headers=hdrs, timeout=timeout)
        else:
            resp = requests.get(url, headers=hdrs, timeout=timeout)
    except requests.RequestException as exc:
        raise RuntimeError(f"request failed for {url}: {exc}") from exc

    with _lock:
        _last_host_at[host] = time.monotonic()

    if resp.status_code in _BLOCKED:
        time.sleep(BLOCK_COOLDOWN_S)
        raise ScrapeBlocked(url, resp.status_code, resp.text)
    if resp.status_code != 200 or not resp.text:
        raise RuntimeError(f"HTTP {resp.status_code} for {url}")
    return resp.text
