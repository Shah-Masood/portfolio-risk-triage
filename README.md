# Portfolio Risk Triage

A multi-agent system that triages portfolio risk the way a real desk would split the work: one intake agent reads the portfolio, decides who should look at what, and delegates — it never analyzes anything itself. Given a portfolio like *"Tesla, Apple, Wells Fargo"*, five independent agents cooperate over the **A2A protocol** (Agent2Agent — the agent-interoperability standard from Google/Linux Foundation) to resolve company names to tickers, classify each holding, fan out to the right specialists in parallel, and — critically — hold one agent back until the others have reported, because it needs the whole portfolio at once, not one name at a time.

The interesting architectural claim this project makes isn't about any single agent's analysis (each one runs a deliberately simple, readable heuristic, not a real quant model) — it's about **task decomposition and dependency ordering in a multi-agent system**. Three of the four specialists are embarrassingly parallel: Market Risk only needs its own tickers, Credit/News Risk only needs its own tickers, Regulatory/Compliance only needs its own tickers. The fourth, Concentration Risk, is not parallel with anything — it's a *portfolio-level* question ("what fraction of this is one sector?") that is structurally unanswerable from a single ticker, so the orchestrator withholds it until every other artifact is back. That's a small thing to build and an easy thing to get wrong, and it's the one piece of this system that couldn't be a single agent with more tools bolted on.

Every agent also degrades gracefully: `yfinance`, `NewsAPI`, and `SEC EDGAR` calls all fall back to a *deterministic* synthetic dataset (seeded off the ticker symbol) when the live call fails — no network, no API key, rate-limited, doesn't matter. Every artifact reports which mode it ran in. This was a deliberate choice, not an afterthought — the whole five-agent pipeline was built and verified end-to-end inside a network-isolated sandbox before a single live API call was ever made against it.

---

## Architecture

```
                                     "Tesla, Apple, Wells Fargo"
                                                │
                                                ▼
                              ┌──────────────────────────────────┐
                              │   Agent 1 — Orchestrator/Intake    │
                              │        (FastAPI :8000)             │
                              │                                     │
                              │  shared/ticker_resolver.py          │
                              │   name/ticker → ticker              │
                              │        │                            │
                              │  shared/sector_data.py               │
                              │   ticker → sector (yfinance)         │
                              │        │                            │
                              │  route: financial sector             │
                              │   → Credit/News; else → Market       │
                              │   every ticker → Regulatory          │
                              └──────────────────┬──────────────────┘
                                                  │
                    ┌─────────────────────────────┼─────────────────────────────┐
                    ▼ parallel (asyncio.gather)    ▼ parallel                    ▼ parallel
       ┌─────────────────────┐      ┌─────────────────────┐      ┌───────────────────────────┐
       │ Agent 2               │      │ Agent 3               │      │ Agent 5                     │
       │ Market Risk :8001     │      │ Credit/News Risk :8002│      │ Regulatory/Compliance :8004 │
       │                        │      │                        │      │                             │
       │ yfinance price history │      │ NewsAPI headlines +    │      │ NewsAPI (regulatory terms) + │
       │ → volatility, drawdown,│      │ negative-keyword scoring│      │ SEC EDGAR full-text search   │
       │   beta vs SPY, RSI      │      │                        │      │ → litigation/enforcement     │
       └────────────┬───────────┘      └────────────┬───────────┘      └──────────────┬──────────────┘
                     │                                │                                 │
                     └────────────────────────────────┴─────────────────────────────────┘
                                                  │  all three artifacts returned
                                                  ▼
                              ┌──────────────────────────────────┐
                              │   Agent 4 — Concentration Risk     │
                              │        (FastAPI :8003)             │
                              │                                     │
                              │  needs the AGGREGATED portfolio     │
                              │  (every ticker's sector at once) —  │
                              │  cannot run until the others do,    │
                              │  so the orchestrator calls it last  │
                              │                                     │
                              │  sector weights, HHI, threshold flag│
                              └──────────────────┬──────────────────┘
                                                  │
                                                  ▼
                              ┌──────────────────────────────────┐
                              │   Consolidated risk report          │
                              │   overall score + level             │
                              │   (HIGH if any vector is HIGH)      │
                              └──────────────────────────────────┘
```

### A2A transport (`shared/a2a_protocol.py`)

Every agent is a real, independently-runnable FastAPI service exposing:

```
GET  /.well-known/agent-card.json    Agent Card discovery (name, skills, capabilities)
POST /                                JSON-RPC 2.0, method "message/send"
GET  /health
```

`message/send` returns a Task→Artifact→Part envelope shaped like the real A2A wire format — not a bare JSON blob:

```
POST /
{"jsonrpc":"2.0","id":"1","method":"message/send",
 "params":{"message":{"metadata":{"tickers":["AAPL"]}}}}

→ {"jsonrpc":"2.0","id":"1","result":
     {"id":"...", "status":{"state":"completed"},
      "artifacts":[{"parts":[{"kind":"data","data":{ ...RiskArtifact... }}]}]}}
```

`call_agent()` in the same module is the client side the orchestrator uses to talk to all four specialists — same envelope, same protocol, both directions.

### Orchestrator fan-out (`agents/orchestrator/main.py`)

```
handle_triage(message)
  │
  ├─ resolve_query() / metadata.tickers   → ["TSLA","AAPL","WFC"]
  ├─ classify_asset() per ticker           → sector + market_risk|credit_risk routing
  │
  ├─ asyncio.gather(                       ← Market, Credit, Regulatory — no ordering dependency
  │     call_agent(MARKET_RISK_URL,   {"tickers": market_tickers}),
  │     call_agent(CREDIT_RISK_URL,   {"tickers": credit_tickers}),
  │     call_agent(REGULATORY_RISK_URL,{"tickers": all_tickers}),
  │  )
  │
  ├─ call_agent(CONCENTRATION_RISK_URL,     ← awaited only after the block above returns;
  │     {"portfolio": classifications})       needs every ticker's sector at once
  │
  └─ compile: overall_score = avg(vector scores)
              overall_level = HIGH if any vector HIGH, else MODERATE if any MODERATE, else LOW
```

`return_exceptions=True` on the `gather()` call means one specialist timing out or erroring doesn't take down the whole triage — the orchestrator logs it and reports on whatever did come back.

---

## Quickstart

### Requirements

- Python 3.10+
- No database, no vector store, no message broker — five stateless FastAPI processes talking HTTP to each other
- Optional: `NEWSAPI_KEY` ([newsapi.org](https://newsapi.org/register) — free tier works). Not required to run; falls back to synthetic headlines without one.
- No key required for `yfinance` or SEC EDGAR — EDGAR just wants a descriptive `User-Agent` string

### Install

```bash
python3 -m venv venv && source venv/bin/activate      # Windows: .\venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env      # optional — fill in NEWSAPI_KEY if you have one
```

### Run

```bash
./run_all.sh
```

*(PowerShell doesn't run `.sh` files — see the "Running on Windows/PowerShell" section below for the `Start-Job` equivalent.)*

This starts all five agents:

```
Orchestrator            http://localhost:8000   ← open this in a browser
Market Risk              http://localhost:8001
Credit/News Risk         http://localhost:8002
Concentration Risk       http://localhost:8003
Regulatory/Compliance    http://localhost:8004
```

Open **http://localhost:8000** — that's a typable dashboard served directly by the orchestrator (no separate frontend build/process). Type company names or tickers ("Tesla, Apple, Wells Fargo") and hit Enter; the page animates the orchestrator's actual classification/fan-out log, then renders each specialist's score, badge, ticker chips, and metrics as it comes back, followed by a consolidated overall-risk card.

Or hit it headless:

```bash
curl -s -X POST http://localhost:8000/triage \
  -H 'Content-Type: application/json' \
  -d '{"query": "Tesla, Apple, Wells Fargo"}' | python3 -m json.tool
```

Or call a specialist's real A2A endpoint directly, bypassing the orchestrator entirely:

```bash
curl -s -X POST http://localhost:8001/ -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":"1","method":"message/send","params":{"message":{"metadata":{"tickers":["AAPL"]}}}}'
```

### Running on Windows / PowerShell

```powershell
Start-Job { Set-Location $using:PWD; .\venv\Scripts\python.exe -m uvicorn agents.market_risk.main:app --port 8001 }
Start-Job { Set-Location $using:PWD; .\venv\Scripts\python.exe -m uvicorn agents.credit_risk.main:app --port 8002 }
Start-Job { Set-Location $using:PWD; .\venv\Scripts\python.exe -m uvicorn agents.concentration_risk.main:app --port 8003 }
Start-Job { Set-Location $using:PWD; .\venv\Scripts\python.exe -m uvicorn agents.regulatory_risk.main:app --port 8004 }
Start-Sleep -Seconds 2
Start-Job { Set-Location $using:PWD; .\venv\Scripts\python.exe -m uvicorn agents.orchestrator.main:app --port 8000 }
```

Check all five came up with `Get-Job`. **If you restart, stop the old jobs first** (`Get-Job | Stop-Job; Get-Job | Remove-Job`) — a stale process still bound to a port will keep serving old code while you edit files, which looks exactly like your changes "didn't take."

---

## Stage-by-stage breakdown

### Stage 1 — Shared transport and data layer (`shared/`)

**`shared/a2a_protocol.py` — hand-rolled A2A transport**
`build_agent_app(agent_card, handler)` constructs a FastAPI app with Agent Card discovery, `/health`, and a `message/send` JSON-RPC endpoint; `handler` is just an `async def(message_dict) -> dict` — every agent's actual logic is a plain function, the protocol plumbing is identical across all five. `call_agent()` is the matching client, used both by the orchestrator (to reach the four specialists) and available to any external JSON-RPC caller. Deliberately **not** built on the official `a2a-sdk` PyPI package — see "Design decisions" below for why.

**`shared/schemas.py` — the common artifact shape**
`RiskArtifact` (pydantic) is what every specialist returns: `agent`, `vector`, `tickers`, `score`, `level`, `summary`, `metrics`, `sources`, `data_mode`. `score_to_level()` is the one shared bucketing function — **HIGH ≥ 65, MODERATE ≥ 40, LOW < 40** — so a score of 61 means the same thing whether it came from Market Risk or Regulatory. `RiskArtifact.make()` is the single constructor every agent calls, guaranteeing the level is always derived from the score rather than set independently and allowed to drift.

**`shared/config.py` — env vars and the offline switch**
`FORCE_OFFLINE` (default `0`) forces every data wrapper to skip its live call and go straight to synthetic data — useful for demos without burning API quota, and what CI/sandbox verification runs with. All five service URLs (`localhost:8000`–`8004`) are also env-overridable, so the same code runs unmodified if you later containerize each agent with a real hostname.

**`shared/sector_data.py` — sector classification + routing rule**
`get_sector(ticker)` tries `yfinance.Ticker(ticker).info["sector"]` first; falls back to a small static table (`_FALLBACK_SECTORS`) only when yfinance is unreachable. `classify_asset()` is what the orchestrator actually calls — it returns `(sector, asset_class, data_mode)` where `asset_class` is `"financial"` if the sector is in `FINANCIAL_SECTORS` (`Financial Services`, `Financials`, `Banks`, `Insurance`), which is the entire routing decision between Market Risk and Credit/News Risk.

**`shared/market_data.py` — price history + technical metrics**
`get_price_history()` pulls 6 months of daily closes via `yfinance`; on failure, `_synthetic_series()` generates a deterministic geometric random walk seeded off `sha256(ticker)`, so the same ticker always produces the same synthetic volatility profile across runs — useful for reproducible demos. `compute_metrics()` derives annualized volatility, max 180-day drawdown, beta vs. a fetched SPY series, and 14-day RSI, all from the same closes.

**`shared/news_data.py` — NewsAPI wrapper + keyword scoring**
Shared by both Credit/News and Regulatory — same `fetch_headlines()` call, different query terms and different keyword lexicon. `NEGATIVE_KEYWORDS` (downgrade, lawsuit, fraud, bankruptcy, ...) drives Credit; `REGULATORY_KEYWORDS` (SEC, CFPB, OCC, subpoena, consent order, ...) drives Regulatory. `score_headlines()` is a crude but transparent keyword-density flag — counts how many headlines contain at least one lexicon term and turns that ratio into a `sentiment_score`. No live key → deterministic synthetic headlines drawn from a small template pool, seeded per ticker.

**`shared/edgar_data.py` — SEC EDGAR full-text search**
Hits `efts.sec.gov/LATEST/search-index` for recent 8-K/10-Q/10-K/SC 13D-A filings; offline fallback generates a small deterministic synthetic filings list per ticker.

**`shared/ticker_resolver.py` — free-text → ticker resolution**
`resolve_query()` splits on commas/`and`/`&`/newlines, then per chunk: (1) if a bare uppercase ticker token is already present in the *original-case* text, use it — deliberately not case-folded first, because `"Tesla".upper()` is `"TESLA"`, a valid 5-letter ticker-shaped string, and would otherwise misfire; (2) check `COMPANY_NAME_MAP`, a small local table (`"tesla" → "TSLA"`, `"wells fargo" → "WFC"`, etc.); (3) fall back to a live `yfinance.Search()` lookup for anything not in the table. Unresolved chunks are dropped rather than raising, so one typo doesn't kill the whole query.

---

### Stage 2 — The four specialist agents (`agents/`)

**`agents/market_risk/main.py` — Agent 2**
Computes volatility, 180-day drawdown, beta vs. SPY, and RSI per ticker via `shared/market_data.py`, then a weighted heuristic (`_score_from_metrics`) turns those four numbers into a 0–100 score: volatility contributes up to 40 points, drawdown up to 30, beta above 1 up to 20, RSI distance from the 40–60 neutral band up to 10. Every weight and cap is a plain constant in the function body — the point isn't quant sophistication, it's that the resulting score's rationale is legible from the metrics alone.

**`agents/credit_risk/main.py` — Agent 3**
Only receives tickers the orchestrator classified as `financial` (banks, insurers). Scores each ticker's headline hit-ratio, with a small bump for absolute flagged-headline count so a name with five negative articles outranks one with a single borderline headline at the same ratio.

**`agents/regulatory_risk/main.py` — Agent 5**
Receives *every* ticker regardless of sector — a tech stock can still have an open SEC matter. Combines a regulatory-keyword news scan with SEC EDGAR filing counts (`8-K` and `SC 13D/A` filings are treated as litigation-relevant); score blends a news component (up to 70 points) with a filings component (up to 30).

**`agents/concentration_risk/main.py` — Agent 4**
The only agent whose input isn't "a list of tickers" — it takes `metadata.portfolio: [{ticker, sector}, ...]`, the full aggregated set the orchestrator only has once the other three have reported. Computes sector weights, the Herfindahl-Hirschman Index (`hhi = Σ(sector_weight²)`), and flags anything over a 50% single-sector threshold. **Note on single-position portfolios:** HHI is mathematically `1.0` for any one-ticker portfolio regardless of which company it is — one position is 100% of one sector by definition — so a single-ticker triage always scores exactly 95 (`70` from HHI + `15` threshold bump + `10` breadth-of-sectors penalty). That's correct behavior, not a bug: concentration risk is a statement about diversification, and a one-position portfolio has none, no matter how blue-chip the position is. The score differentiates meaningfully once a portfolio has two or more tickers spread across sectors.

---

### Stage 3 — The orchestrator (`agents/orchestrator/main.py`) — Agent 1

**Classification and routing.** For each resolved ticker, `classify_asset()` returns a sector and an `asset_class`. Financial-sector tickers get routed to Credit/News; everything else goes to Market Risk. Every ticker, regardless of class, goes to Regulatory. This is the entire "who does what" decision — no LLM call, just a sector lookup and an `if`.

**Parallel fan-out, sequential concentration.** `asyncio.gather()` fires Market, Credit, and Regulatory concurrently (only including a task for a specialist that actually has tickers routed to it — an all-tech portfolio never calls Credit/News at all). Concentration is `await`ed separately, afterward, on its own line — not because of a technical limitation, but because its input literally doesn't exist until the loop above finishes.

**Compiling the report.** `overall_score` is the mean of whatever vector scores came back; `overall_level` is HIGH if *any* vector is HIGH, MODERATE if any is MODERATE, else LOW — a single hot vector is enough to flag the whole portfolio for review, rather than diluting a real risk signal by averaging it against three calmer ones.

**The web UI.** `GET /` serves a single self-contained HTML/CSS/JS page (`_PAGE`, no build step, no separate static file server) styled as a dark terminal dashboard: a `$`-prefixed portfolio input bar, an animated Agent-1 log that replays the actual classification/routing decisions from the response (not canned text — `buildLogLines()` derives each line from `data.classifications`), four agent cards in a 2×2 grid with SVG ring score gauges colored by level, and a consolidated summary card with per-vector mini scores. `POST /triage` is a plain REST convenience wrapper around the same `handle_triage()` the JSON-RPC endpoint calls — one function, two ways in.

---

## Design decisions worth defending

| Decision | Why |
|---|---|
| Hand-rolled A2A transport instead of the official `a2a-sdk` package | The version on PyPI today (1.1.2) is built on a gRPC/protobuf core (`a2a.types.a2a_pb2`), not the lighter pydantic-based interface most A2A tutorials and this project's mental model assume. Wiring five small agents through the protobuf surface would add real complexity without changing what the project demonstrates architecturally. `shared/a2a_protocol.py` implements the parts of the spec that matter directly on FastAPI — Agent Card discovery, JSON-RPC `message/send`, Task→Artifact→Part response shape — and swapping in the official SDK later is a contained change limited to that one file. |
| Concentration Risk awaited separately, not folded into the `gather()` | It's not an optimization choice, it's a correctness one — the aggregated `{ticker, sector}` list this agent needs doesn't exist as a value until the classification loop (which happens before fan-out) has run for every ticker. Modeling that as "runs after, not in parallel with" in the orchestrator code is the one piece of this system's control flow that directly encodes a real dependency, rather than being parallel-by-default. |
| Every data wrapper tries live, then falls back to *deterministic* synthetic data | Not "some placeholder" — `_synthetic_series()`, `_synthetic_headlines()`, and `_synthetic_filings()` are all seeded off `sha256(ticker)`, so a given ticker produces the same synthetic profile every run. That determinism is what made it possible to build and verify the entire five-agent pipeline end-to-end inside a network-isolated sandbox, and it's what makes `FORCE_OFFLINE=1` demos reproducible rather than random. |
| Shared `score_to_level()` thresholds instead of per-agent buckets | If Market Risk and Regulatory used different HIGH/MODERATE/LOW cutoffs, "the same score means different things depending which agent produced it" — which breaks the orchestrator's `overall_level = HIGH if any vector HIGH` rule, since that rule assumes a HIGH from any agent means the same thing. One shared function in `shared/schemas.py`, called from `RiskArtifact.make()`, is what keeps that invariant true. |
| yfinance as the single source of truth for sector, not a hand-maintained table | The static `_FALLBACK_SECTORS` table exists *only* as an offline fallback, not as the primary source — a hardcoded sector map would drift the moment a company changes classification or you add a ticker nobody thought to add to the table. The tradeoff: yfinance's real sector for TSLA is Consumer Cyclical, not Technology, so live results will genuinely differ from an all-Technology mockup built by hand. |
| Ticker-token detection uses original-case text, not `.upper()`'d text | `resolve_query()` looks for already-uppercase tokens in the untouched input. Case-folding first and then pattern-matching `[A-Z]{1,5}` would match `"TESLA"` (5 letters) just as readily as `"AAPL"` — a real bug caught during testing, where "Tesla, Apple, Wells Fargo" resolved to the literal strings `TESLA`/`APPLE`/`FARGO` instead of falling through to the company-name table. |
| `return_exceptions=True` on the parallel `gather()` | One specialist erroring or timing out shouldn't take down the whole triage. The orchestrator logs the failure and reports on whatever artifacts did come back, rather than the entire `/triage` call failing because, say, NewsAPI rate-limited one request. |
| Orchestrator serves its own frontend, no separate static server | `GET /` on the same FastAPI app that handles `/triage` and the JSON-RPC endpoint means "run one command, open one URL" — no build step, no CORS configuration between a frontend origin and an API origin, no second process to keep alive. |
| Web UI's log animation is derived from real response data, not scripted text | `buildLogLines()` reads `data.classifications` from the actual API response to generate lines like "Equities detected: TSLA, AAPL → Market Risk Agent" — if the routing logic changes, the displayed log changes with it automatically, instead of a hardcoded demo script silently going stale relative to what the backend actually does. |

---

## Known limitations

1. **Concentration Risk always scores 95 for single-ticker portfolios.** This is mathematically correct (see Stage 2 above), but it means the score doesn't differentiate at all until a portfolio has 2+ holdings — worth knowing before reading much into a one-stock triage's concentration number specifically.

2. **Scoring heuristics are intentionally simple, not calibrated models.** Every specialist's 0–100 score is a hand-weighted linear combination of a few metrics, chosen for legibility ("higher volatility → higher score") rather than backtested against real default/drawdown outcomes. None of this should inform real position sizing.

3. **Beta calculation against synthetic data is not meaningful.** When `FORCE_OFFLINE=1` (or yfinance is unreachable), each ticker's synthetic price series and the synthetic SPY benchmark series are independently random walks with no actual correlation structure — the resulting "beta" is statistical noise, not a real market beta. It's fine for exercising the pipeline; don't read anything into the number itself in offline mode.

4. **`COMPANY_NAME_MAP` is a small, manually maintained table.** Anything not in it (or not resolvable via the live `yfinance.Search()` fallback) silently drops out of the portfolio rather than erroring loudly — a typo'd or obscure company name just won't appear in the results, with no warning surfaced to the UI.

5. **NewsAPI's free tier query semantics limit headline relevance.** Query construction (`"{ticker}" AND (...)`) depends on the ticker symbol appearing in article text, which under-matches companies more commonly referred to by name than symbol, and the free tier's coverage/recency is more limited than a paid plan.

6. **SEC EDGAR full-text search only covers what's actually in EDGAR.** Foreign private issuers and non-US-registered names won't have meaningful hits, and the current query doesn't disambiguate ticker collisions across exchanges.

7. **No caching or rate-limit handling beyond try/fallback.** A live call either succeeds within its request or falls back to synthetic data — there's no retry-with-backoff, no shared cache across requests, so a portfolio re-run seconds later re-fetches everything from scratch (or re-hits the same rate limit).

8. **Single-tenant, no auth, no persistence.** Every agent is stateless request-in/response-out; there's no session concept, no stored history of past triages, and nothing preventing any caller from hitting any endpoint.

9. **The orchestrator's ticker-vs-name parsing is regex/table-based, not LLM-assisted**, even though `shared/config.py` has `LLM_PROVIDER`/API key placeholders wired up. A query like "a regional bank stock" resolves via a single crude entry in `COMPANY_NAME_MAP` (`"regional bank" → "WFC"`), not genuine fuzzy understanding.

---

## Roadmap

- **LLM-assisted ticker resolution** — swap `resolve_query()` for an LLM call on queries that don't cleanly split into comma-separated names/tickers ("my FAANG stocks plus a regional bank"), using the existing `LLM_PROVIDER` config as the entry point.
- **GraphRAG behind Credit/News and Regulatory** — a graph of ticker ↔ executive ↔ subsidiary ↔ litigation ↔ regulator relationships would surface indirect exposure a keyword search misses (a bank's risk from a subsidiary's pending suit that never mentions the parent ticker).
- **Expose each specialist as an MCP tool** — `market_risk.analyze` / `credit_risk.analyze` / `regulatory_risk.analyze` / `concentration_risk.analyze` as MCP tools any MCP-aware client could call directly for a single-ticker question, without a full portfolio triage.
- **More post-fan-out vectors** — the Concentration pattern (needs the aggregated portfolio, runs after the parallel block) generalizes to a liquidity/correlation-across-holdings agent or a macro-factor exposure agent.
- **Bidirectional beta/correlation against a real live benchmark cache** — fetch SPY once per triage run instead of once per Market Risk call, and share it across tickers in the same request.
- **Persistent run history** — store past triage reports (even just in Postgres/SQLite) so "how has this portfolio's risk profile changed since last week" becomes answerable.
- **Auth + multi-portfolio sessions** — API-key or session-scoped access so the orchestrator can be safely exposed beyond localhost.
- **Real evaluation of the scoring heuristics** — backtest the Market Risk weighting against actual subsequent drawdowns to see whether the hand-picked coefficients track anything real.

---

## Tech stack

| Layer | Tools |
|---|---|
| Web framework | FastAPI (one instance per agent) |
| ASGI server | Uvicorn |
| Inter-agent transport | Hand-rolled A2A-shaped JSON-RPC 2.0 over HTTP (`shared/a2a_protocol.py`), `httpx.AsyncClient` |
| Schema/validation | Pydantic (`RiskArtifact`, `AgentCard`, `AgentSkill`) |
| Market data | `yfinance` (price history, sector `.info`) |
| News data | NewsAPI `/v2/everything`, `requests` |
| Regulatory data | SEC EDGAR full-text search API (`efts.sec.gov`) |
| Numerics | NumPy (volatility, beta, synthetic random walks), pandas (price series, rolling RSI) |
| Config | `python-dotenv` + plain env vars |
| Frontend | Single self-contained HTML/CSS/vanilla JS page served by the orchestrator — no build step, no framework |
| Offline resilience | Deterministic per-ticker synthetic data generation (`hashlib.sha256`-seeded) across every data wrapper |

---

## Project layout

```
.
├── requirements.txt
├── run_all.sh                          # starts all 5 agents (bash)
├── .env.example
├── demo_transcript.md                  # video walkthrough script
├── shared/
│   ├── a2a_protocol.py                 # Agent Card + JSON-RPC transport (server + client)
│   ├── schemas.py                      # RiskArtifact, score_to_level(), report shape
│   ├── config.py                       # env vars, ports, FORCE_OFFLINE switch
│   ├── ticker_resolver.py              # free-text / company-name → ticker resolution
│   ├── market_data.py                  # yfinance price history + vol/drawdown/beta/RSI
│   ├── news_data.py                    # NewsAPI wrapper + keyword-based sentiment scoring
│   ├── edgar_data.py                   # SEC EDGAR full-text search wrapper
│   └── sector_data.py                  # yfinance sector lookup + financial-sector routing rule
└── agents/
    ├── orchestrator/main.py            # Agent 1 — intake, resolution, classification, fan-out, web UI
    ├── market_risk/main.py             # Agent 2
    ├── credit_risk/main.py             # Agent 3
    ├── concentration_risk/main.py      # Agent 4
    └── regulatory_risk/main.py         # Agent 5
```

---

## Acknowledgments

- The A2A protocol working group (Google / Linux Foundation) for publishing a genuinely implementable spec — Agent Cards and JSON-RPC `message/send` are simple enough to hand-roll faithfully in an afternoon, which says something good about the design.
- The `yfinance` maintainers for making real price history and sector metadata a zero-friction, keyless dependency.
- SEC EDGAR for making filings full-text search freely available with nothing more than a polite `User-Agent` header.
- NewsAPI for making headline search accessible without an enterprise contract.
- FastAPI/Starlette for how little code five independent, discoverable, JSON-RPC-speaking services end up taking.

---

## License

MIT.
