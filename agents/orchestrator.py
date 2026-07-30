"""Agent 1 - Intake / Orchestrator Agent.
 
Takes a portfolio query (natural language or an explicit ticker list),
classifies each ticker, and fans out A2A tasks to the specialist
agents. Does no analysis itself.
 
Routing:
  - every ticker            -> Regulatory/Compliance Agent
  - financial-sector ticker -> Credit/News Risk Agent
  - everything else         -> Market Risk Agent
  - Market/Credit/Regulatory run in parallel (asyncio.gather)
  - Concentration Agent runs *after*, once the aggregated portfolio
    (ticker -> sector) is known - it needs the whole picture, not a
    single name.
 
Run: uvicorn agents.orchestrator:app --port 8000
"""
 
from __future__ import annotations
 
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List
 
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
 
import httpx
from fastapi import Body
from fastapi.responses import HTMLResponse
 
from shared.a2a_protocol import AgentCard, AgentSkill, build_agent_app, call_agent
from shared.config import (
    CONCENTRATION_RISK_URL,
    CREDIT_RISK_URL,
    MARKET_RISK_URL,
    REGULATORY_RISK_URL,
)
from shared.schemas import score_to_level
from shared.sector_data import classify_asset
from shared.ticker_resolver import resolve_query
 
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("orchestrator")
 
AGENT_CARD = AgentCard(
    name="Portfolio Risk Triage Orchestrator",
    description=(
        "Intake agent for portfolio risk triage. Classifies each ticker and "
        "fans out A2A tasks to Market Risk, Credit/News Risk, Regulatory/"
        "Compliance, and Concentration Risk specialist agents."
    ),
    url="http://localhost:8000",
    skills=[
        AgentSkill(
            id="portfolio.triage",
            name="Triage Portfolio Risk",
            description="Given a portfolio query or ticker list, orchestrates the full multi-agent risk triage and returns a consolidated report.",
            tags=["orchestrator", "portfolio-risk", "intake"],
        )
    ],
)
 
async def handle_triage(message: Dict[str, Any]) -> Dict[str, Any]:
    metadata = message.get("metadata", {})
    tickers = metadata.get("tickers")
    if not tickers:
        query_text = metadata.get("query") or (message.get("parts") or [{}])[0].get("text", "")
        tickers = resolve_query(query_text)
 
    if not tickers:
        raise ValueError(
            "Could not find any tickers. Pass metadata.tickers explicitly, "
            "or a query like 'check risk exposure for AAPL, TSLA, WFC'."
        )
 
    logger.info("Received portfolio query. Tickers: %s", tickers)
 
    # Step 1: classify each ticker (sector + asset class) and decide routing.
    classifications = []
    market_tickers: List[str] = []
    credit_tickers: List[str] = []
    for ticker in tickers:
        sector, asset_class, _mode = classify_asset(ticker)
        routed_to = ["regulatory_risk"]
        if asset_class == "financial":
            credit_tickers.append(ticker)
            routed_to.append("credit_risk")
        else:
            market_tickers.append(ticker)
            routed_to.append("market_risk")
        classifications.append(
            {"ticker": ticker, "sector": sector, "asset_class": asset_class, "routed_to": routed_to}
        )
    logger.info(
        "Classified: market=%s credit=%s (all -> regulatory)", market_tickers, credit_tickers
    )
 
    # Step 2: fan out to Market / Credit / Regulatory in parallel.
    async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
        tasks = {}
        if market_tickers:
            tasks["market_risk"] = call_agent(
                MARKET_RISK_URL, {"tickers": market_tickers}, client=client
            )
        if credit_tickers:
            tasks["credit_risk"] = call_agent(
                CREDIT_RISK_URL, {"tickers": credit_tickers}, client=client
            )
        tasks["regulatory_risk"] = call_agent(
            REGULATORY_RISK_URL, {"tickers": tickers}, client=client
        )
 
        logger.info("Fanning out parallel tasks: %s", list(tasks.keys()))
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        vector_results: Dict[str, Any] = {}
        for key, result in zip(tasks.keys(), results):
            if isinstance(result, Exception):
                logger.error("%s failed: %s", key, result)
                continue
            vector_results[key] = result
        logger.info("Parallel agents complete.")
 
        # Step 3: Concentration Agent needs the *aggregated* portfolio -
        # runs after the others, not in parallel with them.
        logger.info("Initiating concentration analysis (post-parallel)...")
        concentration_payload = {
            "portfolio": [{"ticker": c["ticker"], "sector": c["sector"]} for c in classifications]
        }
        try:
            vector_results["concentration_risk"] = await call_agent(
                CONCENTRATION_RISK_URL, concentration_payload, client=client
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("concentration_risk failed: %s", exc)
 
    logger.info("All agent artifacts returned. Triage complete.")
 
    # Step 4: compile the consolidated report.
    vectors = list(vector_results.values())
    scores = [v["score"] for v in vectors if "score" in v]
    overall_score = round(sum(scores) / len(scores), 1) if scores else 0.0
    levels = {v["level"] for v in vectors if "level" in v}
    if "HIGH" in levels:
        overall_level = "HIGH"
    elif "MODERATE" in levels:
        overall_level = "MODERATE"
    else:
        overall_level = "LOW"
 
    overall_summary = (
        f"Analyzed {len(tickers)} ticker(s) across {len(vectors)} risk vector(s). "
        f"Average score {overall_score}, max exposure "
        f"{max(scores) if scores else 0}. Overall portfolio risk: {overall_level}."
    )
 
    return {
        "portfolio": tickers,
        "classifications": classifications,
        "vectors": vectors,
        "overall_score": overall_score,
        "overall_level": overall_level,
        "overall_summary": overall_summary,
    }
 
 
app = build_agent_app(AGENT_CARD, handle_triage)
 
 
@app.post("/triage")
async def triage_convenience_endpoint(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Plain REST convenience wrapper around the A2A skill, for easy curl/demo use.
 
    Accepts {"query": "..."} or {"tickers": ["AAPL", "TSLA", "WFC"]}.
    """
    message = {"role": "user", "parts": [], "metadata": body}
    return await handle_triage(message)
 
 
_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Portfolio Risk Triage</title>
<style>
  :root{
    --bg:#0a0a0d; --card:#111318; --card2:#0d0e12; --border:#23262c;
    --text:#e8e8ec; --dim:#8a8f98; --dim2:#5c6068;
    --high:#ff7a45; --high-bg:rgba(255,122,69,.12); --high-bd:rgba(255,122,69,.35);
    --mod:#e8b339; --mod-bg:rgba(232,179,57,.12); --mod-bd:rgba(232,179,57,.35);
    --low:#4ade80; --low-bg:rgba(74,222,128,.12); --low-bd:rgba(74,222,128,.35);
    --accent:#5b7fff;
    --mono: ui-monospace, SFMono-Regular, "JetBrains Mono", Menlo, Consolas, monospace;
  }
  *{box-sizing:border-box}
  body{font-family:var(--mono);max-width:1040px;margin:0 auto;padding:32px 20px 80px;background:var(--bg);color:var(--text)}
  h1{font-size:20px;letter-spacing:.06em;margin:0 0 4px}
  .version{font-size:11px;color:var(--dim2);font-weight:400;letter-spacing:.08em;margin-left:10px}
  .subtitle{color:var(--dim);font-size:13px;margin:0 0 24px}
 
  .bar{display:flex;align-items:center;gap:10px;background:var(--card2);border:1px solid var(--border);
       border-radius:8px;padding:10px 14px;margin-bottom:14px}
  .bar label{font-size:11px;color:var(--dim2);letter-spacing:.08em;white-space:nowrap}
  .bar .dollar{color:var(--dim)}
  .bar input{flex:1;background:transparent;border:0;outline:0;color:var(--text);font-family:var(--mono);font-size:14px}
  .bar button{background:transparent;border:1px solid var(--border);color:var(--dim);border-radius:6px;
              padding:6px 12px;font-size:11px;letter-spacing:.06em;cursor:pointer;font-family:var(--mono)}
  .bar button:hover{border-color:var(--dim)}
  .run{width:100%;background:var(--accent);color:#fff;border:0;border-radius:8px;padding:12px;
       font-size:14px;font-family:var(--mono);cursor:pointer;margin-bottom:22px}
  .run:hover{opacity:.9}
  .run:disabled{opacity:.5;cursor:default}
 
  .log-card{background:var(--card2);border:1px solid var(--border);border-radius:8px;padding:16px 18px;margin-bottom:20px;display:none}
  .log-head{display:flex;align-items:center;gap:8px;font-size:12px;letter-spacing:.06em;color:var(--dim);margin-bottom:10px}
  .log-head .agent-label{color:var(--text);font-weight:600}
  .status{margin-left:auto;font-size:10px;letter-spacing:.08em;padding:2px 8px;border-radius:4px}
  .status.running{color:var(--mod);background:var(--mod-bg)}
  .status.complete{color:var(--low);background:var(--low-bg)}
  .log-line{font-size:12.5px;color:var(--dim);line-height:1.9;white-space:pre-wrap}
 
  .cards{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}
  @media(max-width:680px){.cards{grid-template-columns:1fr}}
  .card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:18px}
  .card-head{display:flex;align-items:center;gap:9px;margin-bottom:12px}
  .card-title{font-size:11px;letter-spacing:.06em;color:var(--dim);flex:1}
  .badge{font-size:10px;letter-spacing:.06em;padding:3px 9px;border-radius:5px;border:1px solid;white-space:nowrap}
  .badge.high{color:var(--high);background:var(--high-bg);border-color:var(--high-bd)}
  .badge.moderate{color:var(--mod);background:var(--mod-bg);border-color:var(--mod-bd)}
  .badge.low{color:var(--low);background:var(--low-bg);border-color:var(--low-bd)}
 
  .chips{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px}
  .chip{font-size:11px;border:1px solid var(--border);color:var(--dim);padding:3px 9px;border-radius:5px}
  .card-summary{font-size:12.5px;color:var(--dim);line-height:1.6;margin:0 0 14px}
 
  .metrics{border-top:1px solid var(--border);padding-top:10px;display:flex;flex-direction:column;gap:7px}
  .metric-row{display:flex;justify-content:space-between;font-size:12px}
  .metric-row .k{color:var(--dim2)}
  .metric-row .v{color:var(--text);font-weight:600}
 
  .dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
  .dot.high{background:var(--high)} .dot.moderate{background:var(--mod)} .dot.low{background:var(--low)}
 
  .summary{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:22px;
           display:flex;align-items:center;gap:20px;flex-wrap:wrap;margin-bottom:16px}
  .summary-label{font-size:11px;color:var(--dim2);letter-spacing:.06em}
  .summary-text{font-size:12px;color:var(--dim)}
  .summary-mid{flex:1;min-width:180px}
 
  .minis{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
  @media(max-width:680px){.minis{grid-template-columns:1fr 1fr}}
  .mini{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px}
  .mini-label{font-size:10px;color:var(--dim2);letter-spacing:.04em;margin-bottom:8px}
  .mini-score{font-size:22px;font-weight:700;margin-bottom:2px}
  .mini-level{font-size:10px;letter-spacing:.06em}
 
  .raw-toggle{font-size:11px;color:var(--dim2);cursor:pointer;text-decoration:underline;margin-top:6px;display:inline-block}
  pre{white-space:pre-wrap;background:var(--card2);border:1px solid var(--border);padding:14px;border-radius:8px;
      font-size:11px;color:var(--dim);margin-top:10px;display:none}
  .error{color:var(--high);font-size:13px}
</style></head>
<body>
  <h1>PORTFOLIO RISK TRIAGE<span class="version">MULTI-AGENT SYSTEM · V1.0</span></h1>
  <p class="subtitle">Orchestrated analysis across Market · Credit · Regulatory · Concentration risk vectors</p>
 
  <div class="bar">
    <label>PORTFOLIO</label>
    <span class="dollar">$</span>
    <input id="q" placeholder="AAPL, TSLA, WFC" value="AAPL, TSLA, WFC" />
    <button id="resetBtn">RESET</button>
  </div>
  <button class="run" id="runBtn">Run Triage</button>
 
  <div class="log-card" id="logCard">
    <div class="log-head">
      <span class="dot low"></span>
      <span class="agent-label">AGENT 1 — ORCHESTRATOR / INTAKE</span>
      <span class="status running" id="logStatus">RUNNING</span>
    </div>
    <div id="logBody"></div>
  </div>
 
  <div class="cards" id="cards"></div>
  <div class="summary" id="summary" style="display:none"></div>
  <div class="minis" id="minis"></div>
 
  <span class="raw-toggle" id="rawToggle" style="display:none">view raw JSON</span>
  <pre id="raw"></pre>
 
<script>
const VECTOR_META = {
  market_risk:        {num:2, label:"MARKET RISK",               metrics:[["avg_annualized_volatility_pct","Annualized Volatility","pct"],["avg_max_drawdown_pct","Max Drawdown (180d)","pct"],["avg_beta_vs_spy","Beta vs SPY",""],["avg_rsi_14d","RSI (14d)",""],["tickers_analyzed","Tickers Analyzed",""]]},
  credit_risk:         {num:3, label:"CREDIT / NEWS RISK",        metrics:[["negative_headlines_total","Negative Headlines","" ],["avg_sentiment_score","Sentiment Score",""],["headlines_scanned_total","Headlines Scanned",""],["tickers_analyzed","Tickers Analyzed",""]]},
  regulatory_risk:     {num:5, label:"REGULATORY / COMPLIANCE",   metrics:[["sec_filings_total","SEC EDGAR Filings (90d)",""],["litigation_relevant_filings_total","Litigation-Relevant Filings",""],["regulatory_keyword_hits_total","Regulatory Keyword Hits",""],["headlines_scanned_total","Headlines Scanned",""]]},
  concentration_risk:  {num:4, label:"CONCENTRATION RISK",        metrics:[]}
};
const ORDER = ["market_risk","credit_risk","regulatory_risk","concentration_risk"];
 
function levelClass(l){ return l.toLowerCase(); }
 
function ring(score, level){
  const r=26, c=2*Math.PI*r;
  const pct = Math.min(Math.max(score,0),100)/100;
  const offset = c - pct*c;
  const color = level==='HIGH' ? 'var(--high)' : level==='MODERATE' ? 'var(--mod)' : 'var(--low)';
  return '<svg width="56" height="56" viewBox="0 0 64 64" style="flex-shrink:0">' +
    '<circle cx="32" cy="32" r="'+r+'" stroke="#23262c" stroke-width="5" fill="none"/>' +
    '<circle cx="32" cy="32" r="'+r+'" stroke="'+color+'" stroke-width="5" fill="none" stroke-linecap="round"' +
    ' stroke-dasharray="'+c+'" stroke-dashoffset="'+offset+'" transform="rotate(-90 32 32)"/>' +
    '<text x="32" y="37" text-anchor="middle" fill="#e8e8ec" font-size="16" font-weight="700" font-family="var(--mono)">'+Math.round(score)+'</text>' +
    '</svg>';
}
 
function metricRow(label, value, suffix){
  if (value === undefined || value === null) return '';
  const v = suffix === 'pct' ? value + '%' : value;
  return '<div class="metric-row"><span class="k">'+label+'</span><span class="v">'+v+'</span></div>';
}
 
function renderCard(vector){
  const meta = VECTOR_META[vector.vector] || {num:'?', label:vector.vector, metrics:[]};
  const lvl = levelClass(vector.level);
  let metricsHtml = meta.metrics.map(([key,label,suffix]) => metricRow(label, vector.metrics[key], suffix)).join('');
 
  if (vector.vector === 'concentration_risk' && vector.metrics.sector_weights_pct){
    const rows = Object.entries(vector.metrics.sector_weights_pct)
      .sort((a,b)=>b[1]-a[1])
      .map(([sector,pct]) => metricRow(sector, pct, 'pct')).join('');
    metricsHtml = rows +
      metricRow('HHI', vector.metrics.hhi, '') +
      metricRow('Position Count', vector.metrics.position_count, '');
  }
 
  return '<div class="card">' +
    '<div class="card-head">' +
      '<span class="dot '+lvl+'"></span>' +
      '<span class="card-title">AGENT '+meta.num+' — '+meta.label+'</span>' +
      '<span class="badge '+lvl+'">'+vector.level+'</span>' +
      ring(vector.score, vector.level) +
    '</div>' +
    '<div class="chips">'+vector.tickers.map(t=>'<span class="chip">'+t+'</span>').join('')+'</div>' +
    '<p class="card-summary">'+vector.summary+'</p>' +
    '<div class="metrics">'+metricsHtml+'</div>' +
  '</div>';
}
 
function buildLogLines(data){
  const lines = [];
  lines.push('Received portfolio query: ' + data.portfolio.join(', '));
  lines.push('Classifying asset types and risk vectors...');
  const equities = data.classifications.filter(c => c.asset_class === 'equity').map(c => c.ticker);
  const financials = data.classifications.filter(c => c.asset_class === 'financial').map(c => c.ticker);
  if (equities.length) lines.push('Equities detected: ' + equities.join(', ') + ' \\u2192 Market Risk Agent');
  if (financials.length) lines.push('Financials detected: ' + financials.join(', ') + ' \\u2192 Credit/News Risk Agent');
  lines.push('All tickers \\u2192 Regulatory/Compliance Agent');
  lines.push('Queuing Concentration Agent (post-parallel, needs full picture)');
  lines.push('Fanning out parallel tasks...');
  lines.push('Parallel agents complete. Initiating concentration analysis...');
  lines.push('All agent artifacts returned. Triage complete.');
  return lines;
}
 
function sleep(ms){ return new Promise(r => setTimeout(r, ms)); }
 
async function animateLog(lines){
  const body = document.getElementById('logBody');
  body.innerHTML = '';
  for (const line of lines){
    const div = document.createElement('div');
    div.className = 'log-line';
    div.textContent = '> ' + line;
    body.appendChild(div);
    await sleep(260);
  }
}
 
async function run(){
  const q = document.getElementById('q').value.trim();
  if (!q) return;
  const runBtn = document.getElementById('runBtn');
  const logCard = document.getElementById('logCard');
  const logStatus = document.getElementById('logStatus');
  const cards = document.getElementById('cards');
  const summary = document.getElementById('summary');
  const minis = document.getElementById('minis');
  const rawToggle = document.getElementById('rawToggle');
  const raw = document.getElementById('raw');
 
  runBtn.disabled = true;
  cards.innerHTML = ''; summary.style.display = 'none'; minis.innerHTML = '';
  rawToggle.style.display = 'none'; raw.style.display = 'none';
  logCard.style.display = 'block';
  logStatus.textContent = 'RUNNING'; logStatus.className = 'status running';
  document.getElementById('logBody').innerHTML = '<div class="log-line">> Contacting orchestrator...</div>';
 
  let data;
  try {
    const res = await fetch('/triage', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({query: q})
    });
    data = await res.json();
    if (!res.ok) throw new Error(data.detail || JSON.stringify(data));
  } catch (err) {
    document.getElementById('logBody').innerHTML = '<div class="log-line error">Error: ' + err.message + '</div>';
    logStatus.textContent = 'ERROR'; logStatus.className = 'status high';
    runBtn.disabled = false;
    return;
  }
 
  await animateLog(buildLogLines(data));
  logStatus.textContent = 'COMPLETE'; logStatus.className = 'status complete';
 
  cards.innerHTML = ORDER
    .map(key => data.vectors.find(v => v.vector === key))
    .filter(Boolean)
    .map(renderCard).join('');
 
  const scores = data.vectors.map(v => v.score);
  const lvl = levelClass(data.overall_level);
  summary.style.display = 'flex';
  summary.innerHTML =
    ring(data.overall_score, data.overall_level) +
    '<div class="summary-mid">' +
      '<div style="display:flex;align-items:center;gap:10px;margin-bottom:4px">' +
        '<span class="badge '+lvl+'">'+data.overall_level+'</span>' +
        '<span class="summary-label">OVERALL PORTFOLIO RISK</span>' +
      '</div>' +
      '<div class="summary-text">avg score '+data.overall_score+' \\u00b7 max exposure '+Math.max(...scores)+' \\u00b7 '+data.vectors.length+' vectors analyzed</div>' +
    '</div>';
 
  minis.innerHTML = ORDER
    .map(key => data.vectors.find(v => v.vector === key))
    .filter(Boolean)
    .map(v => {
      const meta = VECTOR_META[v.vector];
      const c = levelClass(v.level);
      const color = v.level==='HIGH' ? 'var(--high)' : v.level==='MODERATE' ? 'var(--mod)' : 'var(--low)';
      return '<div class="mini"><div class="mini-label">'+meta.label+'</div>' +
        '<div class="mini-score" style="color:'+color+'">'+v.score+'</div>' +
        '<div class="mini-level badge '+c+'" style="display:inline-block">'+v.level+'</div></div>';
    }).join('');
 
  raw.textContent = JSON.stringify(data, null, 2);
  rawToggle.style.display = 'inline-block';
  runBtn.disabled = false;
}
 
document.getElementById('runBtn').addEventListener('click', run);
document.getElementById('q').addEventListener('keydown', e => { if (e.key === 'Enter') run(); });
document.getElementById('resetBtn').addEventListener('click', () => {
  document.getElementById('q').value = '';
  document.getElementById('logCard').style.display = 'none';
  document.getElementById('cards').innerHTML = '';
  document.getElementById('summary').style.display = 'none';
  document.getElementById('minis').innerHTML = '';
  document.getElementById('rawToggle').style.display = 'none';
  document.getElementById('raw').style.display = 'none';
});
document.getElementById('rawToggle').addEventListener('click', () => {
  const raw = document.getElementById('raw');
  raw.style.display = raw.style.display === 'none' ? 'block' : 'none';
});
</script>
</body></html>"""
 
 
@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return _PAGE
