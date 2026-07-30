"""Agent 4 - Concentration Risk Agent.

Architecturally different from the other specialists: it doesn't
analyze any single ticker, it needs the *aggregated* portfolio (sector
+ asset class for every holding). The orchestrator therefore calls this
agent last, after Market/Credit/Regulatory have all reported back and
it can assemble the full sector breakdown - not in parallel with them.

Run: uvicorn agents.concentration_risk.main:app --port 8003
"""

from __future__ import annotations

import logging
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared.a2a_protocol import AgentCard, AgentSkill, build_agent_app
from shared.schemas import RiskArtifact

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("concentration_risk_agent")

AGENT_CARD = AgentCard(
    name="Concentration Risk Agent",
    description=(
        "Analyzes the whole portfolio's sector/asset-class mix and flags "
        "over-concentration. Requires the aggregated portfolio, so it runs "
        "after the single-ticker specialist agents report back."
    ),
    url="http://localhost:8003",
    skills=[
        AgentSkill(
            id="concentration_risk.analyze",
            name="Analyze Concentration Risk",
            description="Given the full portfolio with sector classifications, computes sector weights and HHI, and flags concentration risk.",
            tags=["concentration-risk", "diversification", "portfolio-level"],
        )
    ],
)

CONCENTRATION_THRESHOLD_PCT = 50.0


def _score_from_hhi(max_sector_pct: float, hhi: float, sector_count: int) -> float:
    """0-100 score: HHI (0-1, higher = more concentrated) is the primary
    driver, with a penalty for very few sectors and a bump if any single
    sector clears the concentration threshold."""
    hhi_component = min(hhi * 70, 70)
    threshold_bump = 15 if max_sector_pct > CONCENTRATION_THRESHOLD_PCT else 0
    breadth_penalty = max(0, (3 - sector_count)) * 5
    return min(hhi_component + threshold_bump + breadth_penalty, 100)


async def handle_concentration_risk(message: Dict[str, Any]) -> Dict[str, Any]:
    portfolio = message.get("metadata", {}).get("portfolio", [])
    if not portfolio:
        raise ValueError(
            "concentration_risk.analyze requires metadata.portfolio: "
            "[{ticker, sector}, ...]"
        )

    sector_counts = Counter(p["sector"] for p in portfolio)
    total = len(portfolio)
    sector_weights = {
        sector: round(count / total * 100, 1) for sector, count in sector_counts.items()
    }
    hhi = sum((count / total) ** 2 for count in sector_counts.values())

    max_sector = max(sector_weights, key=sector_weights.get)
    max_pct = sector_weights[max_sector]

    score = _score_from_hhi(max_pct, hhi, len(sector_counts))

    flag = max_pct > CONCENTRATION_THRESHOLD_PCT
    summary = (
        f"Portfolio shows {max_pct}% exposure to {max_sector}"
        + (
            f" — above the {CONCENTRATION_THRESHOLD_PCT:.0f}% concentration threshold. "
            "Sector correlation risk elevated."
            if flag
            else ", within the concentration threshold."
        )
        + f" Limited diversification across only {len(sector_counts)} sector(s)."
        if len(sector_counts) <= 3
        else f" Diversified across {len(sector_counts)} sectors."
    )

    artifact = RiskArtifact.make(
        agent="Concentration Risk Agent",
        vector="concentration_risk",
        tickers=[p["ticker"] for p in portfolio],
        score=score,
        summary=summary,
        metrics={
            "sector_weights_pct": sector_weights,
            "hhi": round(hhi, 2),
            "position_count": total,
            "sector_count": len(sector_counts),
            "max_sector": max_sector,
            "max_sector_pct": max_pct,
            "concentration_threshold_pct": CONCENTRATION_THRESHOLD_PCT,
            "threshold_breached": flag,
        },
        sources=["portfolio aggregation (from orchestrator)"],
        data_mode="live",
    )
    return artifact.model_dump()


app = build_agent_app(AGENT_CARD, handle_concentration_risk)
