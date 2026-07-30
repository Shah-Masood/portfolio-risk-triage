"""Shared pydantic models used across all five agents."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


def score_to_level(score: float) -> str:
    """Map a 0-100 risk score to a HIGH / MODERATE / LOW bucket.

    Thresholds are shared by every agent so scores are comparable
    across the whole triage report.
    """
    if score >= 65:
        return "HIGH"
    if score >= 40:
        return "MODERATE"
    return "LOW"


class RiskArtifact(BaseModel):
    """The common shape every specialist agent returns.

    This is what gets attached to the A2A Task as an ``artifact`` -
    see shared/a2a_protocol.py for how it's wrapped on the wire.
    """

    agent: str
    vector: str  # e.g. "market_risk", "credit_risk", "regulatory_risk", "concentration_risk"
    tickers: List[str] = Field(default_factory=list)
    score: float
    level: str
    summary: str
    metrics: Dict[str, Any] = Field(default_factory=dict)
    sources: List[str] = Field(default_factory=list)
    data_mode: str = "live"  # "live" or "offline_fallback"

    @classmethod
    def make(
        cls,
        agent: str,
        vector: str,
        tickers: List[str],
        score: float,
        summary: str,
        metrics: Dict[str, Any],
        sources: Optional[List[str]] = None,
        data_mode: str = "live",
    ) -> "RiskArtifact":
        return cls(
            agent=agent,
            vector=vector,
            tickers=tickers,
            score=round(score, 1),
            level=score_to_level(score),
            summary=summary,
            metrics=metrics,
            sources=sources or [],
            data_mode=data_mode,
        )


class TickerClassification(BaseModel):
    ticker: str
    sector: str
    asset_class: str  # "equity" | "financial"
    routed_to: List[str]  # which specialist agent vectors this ticker was fanned out to


class PortfolioTriageReport(BaseModel):
    portfolio: List[str]
    classifications: List[TickerClassification]
    vectors: List[RiskArtifact]
    overall_score: float
    overall_level: str
    overall_summary: str
