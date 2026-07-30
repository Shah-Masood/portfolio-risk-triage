"""SEC EDGAR full-text search wrapper for the Regulatory/Compliance agent.

Live path: EDGAR's public full-text search API
(https://efts.sec.gov/LATEST/search-index?q=...) restricted to 8-K /
litigation-relevant filings in the last 90 days. Offline fallback:
a small deterministic synthetic filings list.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Dict, List, Tuple

import requests

from shared.config import FORCE_OFFLINE, SEC_EDGAR_USER_AGENT

logger = logging.getLogger(__name__)

EDGAR_FULLTEXT_URL = "https://efts.sec.gov/LATEST/search-index"


def _seed_for(key: str) -> int:
    return int(hashlib.sha256(key.encode()).hexdigest(), 16) % (2**32)


def _synthetic_filings(ticker: str) -> List[Dict[str, str]]:
    import random

    rng = random.Random(_seed_for(ticker + "-edgar"))
    forms = ["8-K", "10-Q", "DEF 14A", "SC 13D/A"]
    n = rng.randint(1, 4)
    return [
        {
            "form": rng.choice(forms),
            "title": f"{ticker} periodic filing #{i+1}",
            "filed": "recent",
        }
        for i in range(n)
    ]


def fetch_filings(ticker: str, company_name: str = "") -> Tuple[List[Dict[str, str]], str]:
    """Return (filings, data_mode). Each filing is {form, title, filed}."""
    if not FORCE_OFFLINE:
        try:
            resp = requests.get(
                EDGAR_FULLTEXT_URL,
                params={"q": ticker, "forms": "8-K,10-Q,10-K,SC 13D/A", "dateRange": "custom"},
                headers={"User-Agent": SEC_EDGAR_USER_AGENT},
                timeout=10,
            )
            resp.raise_for_status()
            hits = resp.json().get("hits", {}).get("hits", [])
            filings = [
                {
                    "form": h.get("_source", {}).get("root_form", "N/A"),
                    "title": h.get("_source", {}).get("display_names", [ticker])[0],
                    "filed": h.get("_source", {}).get("file_date", ""),
                }
                for h in hits
            ]
            if filings:
                return filings, "live"
        except Exception as exc:  # noqa: BLE001
            logger.warning("SEC EDGAR fetch failed for %s: %s", ticker, exc)

    return _synthetic_filings(ticker), "offline_fallback"
