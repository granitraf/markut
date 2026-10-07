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
    python -m markut.web              # web UI at http://127.0.0.1:8000
    python -m markut.store            # list every logged run (or: show RUN_ID)

## Web UI

`markut.web` is a second entry point beside `run.py`: a FastAPI app that
streams the graph's per-node updates to the browser as Server-Sent Events,
so each agent turn appears as a card the moment it finishes (Research ->
Bull -> Bear -> Judge per round -> claim review -> governed verdict with
UNGROUNDED tags highlighted, review stats and token usage). Two sides,
separate URLs (no login yet; the public page never links to the console):

- **Public `/`** — featured debates (the latest finished run per ticker,
  up to five, with the date it ran; pin an order with `FEATURED_TICKERS=
  NVDA,AVGO,...`), each replayable agent by agent at paced / 2x / instant
  speed, followed by the research article. The article is plain markdown
  in `markut/web/content/article.md`; edit it and reload.
- **Console `/console`** — run a live debate on any ticker (paid, 8-12
  model calls, one at a time) or replay any stored run; `/console?run=ID`
  opens one as a full read.
- **Archive `/console/library`** — every run ever made, by date and
  ticker, with price, rounds, convergence, grounding and token cost.

Shared front-end code lives in `static/debate.js` / `debate.css` (no CDN).
API: `/api/health`, `/api/featured`, `/api/article`, `/api/runs[?ticker=]`,
`/api/runs/{id}`, `/api/replay?run=ID[&speed]`, `/api/debate?ticker=&max_rounds=`.

## Run log

Every debate — CLI or web — is stored in a run log with two tables: `runs`
(one row per debate: ticker, started/finished, mode, price seen, rounds,
converged, review stats, token usage, final verdict, evidence packet) and
`turns` (every streamed event in order with its readable text and full
JSON). One module, `markut/store.py`, is the only reader and writer.

- **Hosted (Supabase Postgres)** when `DATABASE_URL` is set in `.env`: the
  live site and your laptop share one always-on store you can query from
  the Supabase dashboard or any SQL client. Row-level security is enabled
  with no policies, so the project's public REST API cannot read or write
  the tables — only the app's direct connection can.
- **Local (SQLite `markut_runs.db`)** otherwise: zero config, gitignored;
  `MARKUT_DB` overrides the path. This is what the tests run against.

Moving to Supabase: use the **Transaction pooler** connection string (IPv4; the
direct `db.<ref>.supabase.co` host is IPv6-only), replace `[YOUR-PASSWORD]`
*including the brackets* with the database password, then

    python -m markut.store ping                 # schema created, connection verified
    python -m markut.store migrate              # copy the local SQLite runs up (safe to re-run)
    python -m markut.store list                 # now reads from Supabase

Loose `web_records/*.json` files from before the database are imported
once on first start, whichever backend is active.

## Deploy (Railway)

The repo ships a `Dockerfile` (CPU-only torch, both local models baked in at
build time so cold starts are fast) and `railway.json` (health check on
`/api/health`, restart on failure, one replica). Any Docker host works; the
steps for Railway:

1. Push the repo to GitHub. In Railway: New Project -> Deploy from GitHub
   repo -> pick it. Railway detects the Dockerfile.
2. Variables (Service -> Variables). `PORT` is set by Railway automatically.

       ANTHROPIC_API_KEY   required for live debates
       FMP_API_KEY         DCF line of the evidence packet
       DATABASE_URL        Supabase Transaction-pooler URI (see Run log)
       CONSOLE_PASSWORD    required — the console is LOCKED without it
       FEATURED_TICKERS    optional, e.g. NVDA,AVGO

3. Settings -> Networking -> Generate Domain. The public page is `/`, the
   console `/console` (browser asks for the password; any username).

Sizing: measured peak RSS of one research step (torch + both models + a
~400-chunk filing index) is ~700 MB; give the service a **2 GB** memory
limit. On a 512 MB / 1 GB trial allocation the container is OOM-killed
right after `RAG: collection ... ready` with no traceback. Keep one always-on
replica (the live-debate lock and warmed models live in process memory, so
a second replica would double cost and allow two paid runs at once). The
first build downloads torch and the models and takes several minutes;
later builds are cached. Caches (`edgar_cache/`, `news_cache/`) live on the
container's ephemeral disk and simply regenerate; nothing durable is on it —
the run log is in Supabase.

Production behaviour (`MARKUT_PRODUCTION=1` in the image, or Railway's own
`RAILWAY_ENVIRONMENT`): binds 0.0.0.0:$PORT, warms the models on startup,
and the console fails closed without `CONSOLE_PASSWORD`.
