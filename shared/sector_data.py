"""Sector classification for a ticker, via yfinance's `.info`.

Per the project's design choice: yfinance is the source of truth for
sector (used by both the orchestrator's routing decision and the
Concentration Risk agent's sector-weighting). A small static table is
used only as an offline fallback when yfinance can't be reached.
"""

from __future__ import annotations

import logging
from typing import Tuple

from shared.config import FORCE_OFFLINE

logger = logging.getLogger(__name__)

# Offline fallback only - used when yfinance is unreachable (no network,
# rate-limited, etc). Real runs get this from yfinance's own `.info`.
_FALLBACK_SECTORS = {
    "AAPL": "Technology",
    "MSFT": "Technology",
    "GOOGL": "Technology",
    "NVDA": "Technology",
    "META": "Technology",
    "TSLA": "Technology",  # yfinance actually tags this Consumer Cyclical;
    # kept as Technology here only to mirror the reference mockup's grouping
    # when running fully offline. Live yfinance data will differ (correctly).
    "AMZN": "Consumer Cyclical",
    "WFC": "Financial Services",
    "JPM": "Financial Services",
    "BAC": "Financial Services",
    "C": "Financial Services",
    "GS": "Financial Services",
    "XOM": "Energy",
    "CVX": "Energy",
    "JNJ": "Healthcare",
    "PFE": "Healthcare",
}

FINANCIAL_SECTORS = {"Financial Services", "Financials", "Banks", "Insurance"}


def get_sector(ticker: str) -> Tuple[str, str]:
    """Return (sector, data_mode). data_mode is "live" or "offline_fallback"."""
    ticker = ticker.upper().strip()

    if not FORCE_OFFLINE:
        try:
            import yfinance as yf

            info = yf.Ticker(ticker).info
            sector = info.get("sector")
            if sector:
                return sector, "live"
        except Exception as exc:  # noqa: BLE001
            logger.warning("yfinance sector lookup failed for %s: %s", ticker, exc)

    return _FALLBACK_SECTORS.get(ticker, "Diversified/Other"), "offline_fallback"


def classify_asset(ticker: str) -> Tuple[str, str, str]:
    """Return (sector, asset_class, data_mode). asset_class is "financial" or "equity"."""
    sector, mode = get_sector(ticker)
    asset_class = "financial" if sector in FINANCIAL_SECTORS else "equity"
    return sector, asset_class, mode
