"""Resolve free-typed text ("Tesla, Apple, Wells Fargo") into tickers.

Order of resolution per comma/and-separated chunk:
  1. Already looks like a ticker (1-5 uppercase letters) -> use as-is.
  2. Exact/partial match against the local company-name table.
  3. Fall back to yfinance's search endpoint (live lookup), if reachable.
Unresolved chunks are dropped rather than raising, so a typo in one name
doesn't kill the whole query.
"""

from __future__ import annotations

import re
from typing import List

from shared.config import FORCE_OFFLINE

# Small local table for common names - checked before hitting the network.
# Extend freely; this is just a fast path, not the ceiling of what's resolvable.
COMPANY_NAME_MAP = {
    "apple": "AAPL",
    "tesla": "TSLA",
    "wells fargo": "WFC",
    "microsoft": "MSFT",
    "google": "GOOGL",
    "alphabet": "GOOGL",
    "amazon": "AMZN",
    "nvidia": "NVDA",
    "meta": "META",
    "facebook": "META",
    "jpmorgan": "JPM",
    "jp morgan": "JPM",
    "bank of america": "BAC",
    "citigroup": "C",
    "citi": "C",
    "goldman sachs": "GS",
    "goldman": "GS",
    "exxon": "XOM",
    "exxonmobil": "XOM",
    "chevron": "CVX",
    "johnson & johnson": "JNJ",
    "johnson and johnson": "JNJ",
    "pfizer": "PFE",
    "netflix": "NFLX",
    "disney": "DIS",
    "walmart": "WMT",
    "regional bank": "WFC",  # crude stand-in for "a regional bank stock"-type phrasing
}

_TICKER_RE_FINDALL = re.compile(r"\b[A-Z]{1,5}\b")
_SPLIT_RE = re.compile(r",| and |&|\n")


def _yfinance_search(name: str) -> str | None:
    if FORCE_OFFLINE:
        return None
    try:
        import yfinance as yf

        results = yf.Search(name, max_results=1).quotes
        if results:
            return results[0].get("symbol")
    except Exception:  # noqa: BLE001 - best-effort only
        return None
    return None


_STOPWORD_TOKENS = {
    "I", "A", "MY", "THE", "FOR", "AND", "OR", "OF", "TO", "IN", "IS",
    "CHECK", "RISK", "EXPOSURE", "PORTFOLIO", "STOCK", "STOCKS",
}


def resolve_query(query: str) -> List[str]:
    tickers: List[str] = []
    for chunk in _SPLIT_RE.split(query):
        chunk = chunk.strip().strip(".!?:").strip()
        if not chunk:
            continue

        # A chunk may still carry leading sentence text (e.g. the first
        # item after "check risk exposure for my portfolio: AAPL"), so
        # look for a bare ticker token anywhere in it before falling
        # back to name matching on the whole chunk. Deliberately NOT
        # upper()'d first - "Tesla" is 5 letters and would otherwise be
        # mistaken for a ticker. Only text that was ALREADY all-caps in
        # the user's input (like a real typed ticker) counts here.
        ticker_tokens = [
            t for t in _TICKER_RE_FINDALL.findall(chunk) if t not in _STOPWORD_TOKENS
        ]

        if ticker_tokens:
            ticker = ticker_tokens[-1]
        else:
            lower = chunk.lower()
            ticker = COMPANY_NAME_MAP.get(lower)
            if not ticker:
                # substring match, e.g. "my tesla shares" -> "tesla"
                ticker = next(
                    (t for name, t in COMPANY_NAME_MAP.items() if name in lower), None
                )
            if not ticker:
                ticker = _yfinance_search(chunk)

        if ticker and ticker not in tickers:
            tickers.append(ticker)

    return tickers
