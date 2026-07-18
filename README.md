# Markut — AI Investment Research Advisor

A bull-vs-bear debate swarm: ticker in, evidence-grounded, guardrail-governed
research briefing out. LangGraph state machine (Research → Bull ↔ Bear →
strict-JSON Judge → news-verify → terminal review governor), a three-source
evidence layer (yfinance+FMP, SEC EDGAR RAG, Yahoo RSS) exposed as
discoverable FastMCP tools, and three guardrail layers (input validation,
token budget, deterministic output review). Research, not financial advice.

## Install

    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    cp .env.example .env   # add ANTHROPIC_API_KEY and FMP_API_KEY

## Run

    python run.py NVDA [--max-rounds 2] [--budget 200000]   # one governed debate
    python -m markut.eval NVDA        # full debate + baseline comparison (paid)

## Test

    pytest                 # offline suite (free, no network, no model calls)
    pytest -m live         # MCP output-equivalence + frozen-specimen acceptance

## Evaluation (NVDA case study, byte-identical stored-packet control)

Debate: grounding 94%, risk recall 4/7, ~9.8x token cost. Baseline single
call: 84%, 5/7, 1x. Directional findings replicated across 4 runs (debate
always grounds better; baseline breadth never lower). Details in the course
notebook `markut.ipynb` (Step 6) — the untouched submission artifact.

Website/API/UI: coming — this package is the backend seam for it.
