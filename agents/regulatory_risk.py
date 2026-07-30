"""Agent 5 - Regulatory/Compliance Risk Agent.

Runs for every ticker in the portfolio (unlike Credit, which is scoped
to financial-sector names). Reuses the same NewsAPI wiring as the
Credit agent but with a regulatory-keyword lexicon and query terms, and
cross-checks SEC EDGAR full-text search for recent filings that may
signal litigation or enforcement activity.

Run: uvicorn agents.regulatory_risk:app --port 8004
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared.a2a_protocol import AgentCard, AgentSkill, build_agent_app
from shared.edgar_data import fetch_filings
from shared.news_data import REGULATORY_KEYWORDS, fetch_headlines, score_headlines
from shared.schemas import RiskArtifact

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("regulatory_risk_agent")

REGULATORY_QUERY_TERMS = "SEC OR CFPB OR OCC OR DOJ OR lawsuit OR investigation OR enforcement"

AGENT_CARD = AgentCard(
    name="Regulatory/Compliance Risk Agent",
    description=(
        "Checks for pending litigation, regulatory investigations, and "
        "enforcement actions across the full portfolio using news search "
        "and SEC EDGAR filings."
    ),
    url="http://localhost:8004",
    skills=[
        AgentSkill(
            id="regulatory_risk.analyze",
            name="Analyze Regulatory/Compliance Risk",
            description="Given a list of tickers, scans regulatory-focused news and SEC EDGAR filings for litigation/enforcement signals.",
            tags=["regulatory-risk", "compliance", "sec-edgar", "news"],
        )
    ],
)


def _score(keyword_hits: int, headlines_scanned: int, litigation_filings: int) -> float:
    hit_ratio = keyword_hits / (headlines_scanned or 1)
    news_component = min(hit_ratio * 70, 70)
    filing_component = min(litigation_filings * 10, 30)
    return min(news_component + filing_component, 100)


async def handle_regulatory_risk(message: Dict[str, Any]) -> Dict[str, Any]:
    tickers = message.get("metadata", {}).get("tickers", [])
    if not tickers:
        raise ValueError("regulatory_risk.analyze requires metadata.tickers (non-empty list)")

    per_ticker = {}
    scores = []
    data_modes = set()
    total_keyword_hits = 0
    total_headlines = 0
    total_active_matters = 0
    total_filings = 0

    for ticker in tickers:
        headlines, news_mode = fetch_headlines(ticker, extra_terms=REGULATORY_QUERY_TERMS)
        data_modes.add(news_mode)
        news_scoring = score_headlines(headlines, REGULATORY_KEYWORDS)

        filings, filing_mode = fetch_filings(ticker)
        data_modes.add(filing_mode)
        litigation_filings = [f for f in filings if f["form"] in ("8-K", "SC 13D/A")]

        keyword_hits = sum(len(h["matched_keywords"]) for h in news_scoring["keyword_hits"])
        score = _score(keyword_hits, news_scoring["headlines_analyzed"], len(litigation_filings))
        scores.append(score)

        total_keyword_hits += keyword_hits
        total_headlines += news_scoring["headlines_analyzed"]
        total_active_matters += len(litigation_filings)
        total_filings += len(filings)

        per_ticker[ticker] = {
            "flagged_headlines": news_scoring["flagged_headlines"],
            "keyword_hits": keyword_hits,
            "sec_filings_90d": len(filings),
            "litigation_relevant_filings": len(litigation_filings),
            "filings": filings,
        }

    overall_score = sum(scores) / len(scores) if scores else 0.0

    summary = (
        f"Regulatory exposure reviewed for {len(tickers)} ticker(s). "
        f"{total_active_matters} litigation-relevant filing(s) and "
        f"{total_keyword_hits} regulatory keyword hit(s) found across "
        f"{total_headlines} headlines scanned. No confirmed active SEC "
        f"enforcement action identified, but keyword density is worth monitoring."
        if total_keyword_hits or total_active_matters
        else f"No material regulatory or litigation signals found for {', '.join(tickers)}."
    )

    artifact = RiskArtifact.make(
        agent="Regulatory/Compliance Risk Agent",
        vector="regulatory_risk",
        tickers=tickers,
        score=overall_score,
        summary=summary,
        metrics={
            "tickers_analyzed": len(tickers),
            "sec_filings_total": total_filings,
            "litigation_relevant_filings_total": total_active_matters,
            "regulatory_keyword_hits_total": total_keyword_hits,
            "headlines_scanned_total": total_headlines,
            "per_ticker": per_ticker,
        },
        sources=["NewsAPI", "SEC EDGAR full-text search"],
        data_mode="live" if data_modes == {"live"} else "offline_fallback",
    )
    return artifact.model_dump()


app = build_agent_app(AGENT_CARD, handle_regulatory_risk)
