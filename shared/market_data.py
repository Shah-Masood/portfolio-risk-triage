"""Price history + technical metrics for the Market Risk agent.

Live path: yfinance daily closes, 6 months, for the ticker and SPY
(benchmark for beta). Offline fallback: a deterministic geometric
random walk seeded off the ticker symbol, so metrics are stable across
runs and the agent stays testable without network access.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Dict, Tuple

import numpy as np
import pandas as pd

from shared.config import FORCE_OFFLINE

logger = logging.getLogger(__name__)

LOOKBACK_DAYS = 180


def _seed_for(ticker: str) -> int:
    return int(hashlib.sha256(ticker.encode()).hexdigest(), 16) % (2**32)


def _synthetic_series(ticker: str, days: int = LOOKBACK_DAYS) -> pd.Series:
    rng = np.random.default_rng(_seed_for(ticker))
    # annualized drift/vol vary a bit per-ticker but stay in a plausible range
    daily_vol = rng.uniform(0.015, 0.045)
    daily_drift = rng.uniform(-0.0015, 0.0015)
    shocks = rng.normal(daily_drift, daily_vol, size=days)
    price = 100 * np.exp(np.cumsum(shocks))
    idx = pd.date_range(end=pd.Timestamp.today(), periods=days, freq="B")
    return pd.Series(price, index=idx, name=ticker)


def get_price_history(ticker: str) -> Tuple[pd.Series, str]:
    """Return (close_price_series, data_mode)."""
    if not FORCE_OFFLINE:
        try:
            import yfinance as yf

            hist = yf.Ticker(ticker).history(period="6mo", interval="1d")
            if not hist.empty and len(hist) > 20:
                return hist["Close"], "live"
        except Exception as exc:  # noqa: BLE001
            logger.warning("yfinance price history failed for %s: %s", ticker, exc)

    return _synthetic_series(ticker), "offline_fallback"


def compute_rsi(prices: pd.Series, period: int = 14) -> float:
    delta = prices.diff().dropna()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean().iloc[-1]
    avg_loss = loss.rolling(period).mean().iloc[-1]
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100 - (100 / (1 + rs)))


def compute_metrics(ticker: str, benchmark: pd.Series) -> Dict[str, object]:
    prices, mode = get_price_history(ticker)
    returns = prices.pct_change().dropna()

    annualized_vol = float(returns.std() * np.sqrt(252) * 100)
    running_max = prices.cummax()
    drawdown = (prices - running_max) / running_max
    max_drawdown = float(drawdown.min() * 100)
    rsi = compute_rsi(prices)

    # Beta vs benchmark, aligned by trading-day index length (best-effort;
    # synthetic series won't share a real calendar with SPY).
    bench_returns = benchmark.pct_change().dropna()
    n = min(len(returns), len(bench_returns))
    if n > 10:
        cov = np.cov(returns.values[-n:], bench_returns.values[-n:])
        beta = float(cov[0, 1] / cov[1, 1]) if cov[1, 1] != 0 else 1.0
    else:
        beta = 1.0

    return {
        "annualized_volatility_pct": round(annualized_vol, 1),
        "max_drawdown_90d_pct": round(max_drawdown, 1),
        "beta_vs_spy": round(beta, 2),
        "rsi_14d": round(rsi, 1),
        "data_mode": mode,
    }
