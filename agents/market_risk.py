"""Agent 2 - Market Risk Agent.

Handles equities/volatility-type analysis. Pulls price history via
yfinance, computes volatility / drawdown / beta / RSI, and returns a
RiskArtifact with a 0-100 risk score + rationale.

Run: uvicorn agents.market_risk:app --port 8001
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared.a2a_protocol import AgentCard, AgentSkill, build_agent_app
from shared.market_data import compute_metrics, get_price_history
from shared.schemas import RiskArtifact

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("market_risk_agent")

AGENT_CARD = AgentCard(
    name="Market Risk Agent",
    description=(
        "Analyzes equity volatility and price-drawdown risk for a list of "
        "tickers using recent price history."
    ),
    url="http://localhost:8001",
    skills=[
        AgentSkill(
            id="market_risk.analyze",
            name="Analyze Market Risk",
            description="Given a list of equity tickers, computes volatility, drawdown, beta, and RSI, and returns a risk score.",
            tags=["market-risk", "volatility", "equities"],
        )
    ],
)


def _score_from_metrics(vol: float, drawdown: float, beta: float, rsi: float) -> float:
    """Simple weighted heuristic -> 0-100 risk score.

    Not a real quant model - deliberately transparent/simple so the
    rationale is easy to explain: higher vol, deeper drawdown, higher
    beta, and RSI far from the 40-60 neutral band all push risk up.
    """
    vol_component = min(vol / 60 * 40, 40)  # 60% annualized vol -> full 40 pts
    drawdown_component = min(abs(drawdown) / 40 * 30, 30)  # -40% drawdown -> full 30 pts
    beta_component = min(max(beta - 1, 0) / 1.5 * 20, 20)  # beta 2.5 -> full 20 pts
    rsi_distance = abs(rsi - 50)
    rsi_component = min(rsi_distance / 50 * 10, 10)
    return vol_component + drawdown_component + beta_component + rsi_component


async def handle_market_risk(message: Dict[str, Any]) -> Dict[str, Any]:
    tickers = message.get("metadata", {}).get("tickers", [])
    if not tickers:
        raise ValueError("market_risk.analyze requires metadata.tickers (non-empty list)")

    benchmark, bench_mode = get_price_history("SPY")

    per_ticker_metrics = {}
    scores = []
    data_modes = {bench_mode}
    for ticker in tickers:
        m = compute_metrics(ticker, benchmark)
        per_ticker_metrics[ticker] = m
        data_modes.add(m["data_mode"])
        scores.append(
            _score_from_metrics(
                m["annualized_volatility_pct"],
                m["max_drawdown_90d_pct"],
                m["beta_vs_spy"],
                m["rsi_14d"],
            )
        )

    overall_score = sum(scores) / len(scores) if scores else 0.0

    worst_ticker = max(per_ticker_metrics, key=lambda t: per_ticker_metrics[t]["annualized_volatility_pct"])
    worst = per_ticker_metrics[worst_ticker]
    summary = (
        f"Elevated volatility detected across {len(tickers)} ticker(s) "
        f"({', '.join(tickers)}). {worst_ticker} shows the widest swings: "
        f"{worst['annualized_volatility_pct']}% annualized volatility, "
        f"{worst['max_drawdown_90d_pct']}% max drawdown (180d), beta "
        f"{worst['beta_vs_spy']} vs SPY. "
        + ("Amplified beta increases systematic risk exposure." if worst["beta_vs_spy"] > 1.3 else "")
    )

    aggregate_metrics = {
        "tickers_analyzed": len(tickers),
        "per_ticker": per_ticker_metrics,
        "avg_annualized_volatility_pct": round(
            sum(m["annualized_volatility_pct"] for m in per_ticker_metrics.values()) / len(per_ticker_metrics), 1
        ),
        "avg_max_drawdown_pct": round(
            sum(m["max_drawdown_90d_pct"] for m in per_ticker_metrics.values()) / len(per_ticker_metrics), 1
        ),
        "avg_beta_vs_spy": round(
            sum(m["beta_vs_spy"] for m in per_ticker_metrics.values()) / len(per_ticker_metrics), 2
        ),
        "avg_rsi_14d": round(
            sum(m["rsi_14d"] for m in per_ticker_metrics.values()) / len(per_ticker_metrics), 1
        ),
    }

    artifact = RiskArtifact.make(
        agent="Market Risk Agent",
        vector="market_risk",
        tickers=tickers,
        score=overall_score,
        summary=summary.strip(),
        metrics=aggregate_metrics,
        sources=["yfinance"],
        data_mode="live" if data_modes == {"live"} else "offline_fallback",
    )
    return artifact.model_dump()


app = build_agent_app(AGENT_CARD, handle_market_risk)
