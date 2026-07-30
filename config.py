"""Central config: ports, API keys, and the offline-fallback switch.

Every data-source wrapper in shared/ tries the real API first. If it's
unreachable (no network, no key, rate limited) it falls back to a
deterministic synthetic dataset seeded off the ticker symbol, so the
whole 5-agent pipeline stays demoable/testable with zero setup. Set
FORCE_OFFLINE=1 to skip the live attempt entirely (useful for demos
without burning API quota).
"""

import os

from dotenv import load_dotenv

load_dotenv()

NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "")
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "")  # "anthropic" | "openai" | "" (rule-based)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

FORCE_OFFLINE = os.getenv("FORCE_OFFLINE", "0") == "1"

ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://localhost:8000")
MARKET_RISK_URL = os.getenv("MARKET_RISK_URL", "http://localhost:8001")
CREDIT_RISK_URL = os.getenv("CREDIT_RISK_URL", "http://localhost:8002")
CONCENTRATION_RISK_URL = os.getenv("CONCENTRATION_RISK_URL", "http://localhost:8003")
REGULATORY_RISK_URL = os.getenv("REGULATORY_RISK_URL", "http://localhost:8004")

SEC_EDGAR_USER_AGENT = os.getenv(
    "SEC_EDGAR_USER_AGENT", "portfolio-risk-triage-demo contact@example.com"
)
