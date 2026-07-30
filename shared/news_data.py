"""NewsAPI wrapper shared by the Credit/News agent and the Regulatory agent.

Both agents hit the same NewsAPI `/v2/everything` endpoint - the only
difference is the query terms and the keyword lexicon used to score
what comes back (general negative-sentiment words for Credit, legal/
regulatory terms for Regulatory). Falls back to deterministic synthetic
headlines when there's no API key or no network, so both agents stay
testable offline.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Dict, List, Tuple

import requests

from shared.config import FORCE_OFFLINE, NEWSAPI_KEY

logger = logging.getLogger(__name__)

NEGATIVE_KEYWORDS = [
    "downgrade", "lawsuit", "probe", "investigation", "layoffs", "loss",
    "decline", "plunge", "recall", "fraud", "default", "bankruptcy",
    "warning", "cut", "delist", "fine", "penalty", "resign", "scandal",
    "outflow", "selloff", "shortfall", "misses", "slump",
]

REGULATORY_KEYWORDS = [
    "sec", "cfpb", "occ", "doj", "ftc", "enforcement", "subpoena",
    "consent order", "settlement", "litigation", "compliance", "rulemaking",
    "sanction", "antitrust", "investigation", "fine", "penalty", "class action",
]

_SAMPLE_HEADLINES = [
    "{ticker} shares slip after analyst downgrade cites margin pressure",
    "{ticker} announces cost-cutting plan amid softer demand outlook",
    "Regulators seek more information from {ticker} in routine review",
    "{ticker} beats quarterly estimates, raises full-year guidance",
    "Insider selling at {ticker} draws scrutiny from watchdog groups",
    "{ticker} settles minor compliance matter with no admission of wrongdoing",
    "Analysts mixed on {ticker} after choppy trading session",
    "{ticker} faces class action lawsuit over disclosure practices",
]


def _seed_for(key: str) -> int:
    return int(hashlib.sha256(key.encode()).hexdigest(), 16) % (2**32)


def _synthetic_headlines(ticker: str, n: int = 6) -> List[str]:
    import random

    rng = random.Random(_seed_for(ticker))
    pool = _SAMPLE_HEADLINES.copy()
    rng.shuffle(pool)
    return [h.format(ticker=ticker) for h in pool[:n]]


def fetch_headlines(ticker: str, extra_terms: str = "", page_size: int = 20) -> Tuple[List[str], str]:
    """Return (headlines, data_mode)."""
    if not FORCE_OFFLINE and NEWSAPI_KEY:
        try:
            query = f'"{ticker}"' + (f" AND ({extra_terms})" if extra_terms else "")
            resp = requests.get(
                "https://newsapi.org/v2/everything",
                params={
                    "q": query,
                    "language": "en",
                    "sortBy": "publishedAt",
                    "pageSize": page_size,
                    "apiKey": NEWSAPI_KEY,
                },
                timeout=10,
            )
            resp.raise_for_status()
            articles = resp.json().get("articles", [])
            headlines = [a["title"] for a in articles if a.get("title")]
            if headlines:
                return headlines, "live"
        except Exception as exc:  # noqa: BLE001
            logger.warning("NewsAPI fetch failed for %s: %s", ticker, exc)

    return _synthetic_headlines(ticker), "offline_fallback"


def score_headlines(headlines: List[str], keywords: List[str]) -> Dict[str, object]:
    """Crude keyword-density sentiment flag - counts headlines that contain
    at least one keyword from the given lexicon."""
    hits = []
    for h in headlines:
        h_lower = h.lower()
        matched = [kw for kw in keywords if kw in h_lower]
        if matched:
            hits.append({"headline": h, "matched_keywords": matched})

    total = len(headlines) or 1
    hit_ratio = len(hits) / total
    sentiment_score = round(-1 * hit_ratio + 0.15, 2)  # crude: more hits -> more negative

    return {
        "headlines_analyzed": len(headlines),
        "flagged_headlines": len(hits),
        "keyword_hits": hits,
        "sentiment_score": sentiment_score,
        "hit_ratio": round(hit_ratio, 2),
    }
