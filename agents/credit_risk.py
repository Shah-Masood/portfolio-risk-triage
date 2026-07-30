"""Agent 3 - Credit/News Risk Agent.

Handles anything that smells like credit or event risk (financial-sector
tickers routed here by the orchestrator). Hits NewsAPI for recent
headlines, does a crude keyword-based negative-sentiment flag, and
returns a RiskArtifact.

Run: uvicorn agents.credit_risk.main:app --port 8002
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared.a2a_protocol import AgentCard, AgentSkill, build_agent_app
from shared.news_data import NEGATIVE_KEYWORDS, fetch_headlines, score_headlines
from shared.schemas import RiskArtifact

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("credit_risk_agent")

AGENT_CARD = AgentCard(
    name="Credit/News Risk Agent",
    description=(
        "Flags credit and event risk for financial-sector tickers by scanning "
        "recent news for negative-sentiment signals (downgrades, lawsuits, "
        "insider selling, etc)."
    ),
    url="http://localhost:8002",
    skills=[
        AgentSkill(
            id="credit_risk.analyze",
            name="Analyze Credit/News Risk",
            description="Given a list of tickers, searches recent headlines for negative credit/event signals and returns a risk score.",
            tags=["credit-risk", "news", "sentiment"],
        )
    ],
)


def _score_from_hit_ratio(hit_ratio: float, flagged: int) -> float:
    """0-100 score: ratio of flagged headlines is the main driver, with a
    small bump for absolute flagged count so a single ticker with lots of
    bad press outranks one with a single borderline headline."""
    base = min(hit_ratio * 100, 80)
    bump = min(flagged * 3, 20)
    return min(base + bump, 100)


async def handle_credit_risk(message: Dict[str, Any]) -> Dict[str, Any]:
    tickers = message.get("metadata", {}).get("tickers", [])
    if not tickers:
        raise ValueError("credit_risk.analyze requires metadata.tickers (non-empty list)")

    per_ticker = {}
    scores = []
    data_modes = set()
    for ticker in tickers:
        headlines, mode = fetch_headlines(ticker)
        data_modes.add(mode)
        scoring = score_headlines(headlines, NEGATIVE_KEYWORDS)
        per_ticker[ticker] = scoring
        scores.append(_score_from_hit_ratio(scoring["hit_ratio"], scoring["flagged_headlines"]))

    overall_score = sum(scores) / len(scores) if scores else 0.0

    total_flagged = sum(v["flagged_headlines"] for v in per_ticker.values())
    total_analyzed = sum(v["headlines_analyzed"] for v in per_ticker.values())
    avg_sentiment = round(sum(v["sentiment_score"] for v in per_ticker.values()) / len(per_ticker), 2)

    summary = (
        f"Mixed sentiment signal across {len(tickers)} ticker(s). "
        f"{total_flagged} negative headline(s) found in the last search window "
        f"out of {total_analyzed} scanned. Average sentiment score {avg_sentiment}. "
        f"No imminent credit event confirmed."
        if total_flagged > 0
        else f"No material negative-sentiment headlines found for {', '.join(tickers)}."
    )

    artifact = RiskArtifact.make(
        agent="Credit/News Risk Agent",
        vector="credit_risk",
        tickers=tickers,
        score=overall_score,
        summary=summary,
        metrics={
            "tickers_analyzed": len(tickers),
            "negative_headlines_total": total_flagged,
            "headlines_scanned_total": total_analyzed,
            "avg_sentiment_score": avg_sentiment,
            "per_ticker": per_ticker,
        },
        sources=["NewsAPI"],
        data_mode="live" if data_modes == {"live"} else "offline_fallback",
    )
    return artifact.model_dump()


app = build_agent_app(AGENT_CARD, handle_credit_risk)
