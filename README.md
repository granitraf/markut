---
title: Markut
emoji: ⚖️
colorFrom: green
colorTo: red
sdk: docker
app_port: 7860
pinned: false
short_description: Bull-vs-bear research debate swarm with a governed verdict
---

# Markut — AI Investment Research Advisor

A bull-vs-bear debate swarm: ticker in, evidence-grounded, guardrail-governed
research briefing out. LangGraph state machine (Profiler → Planner →
Research with a coverage gate → Bull ↔ Bear → strict-JSON Judge →
claim review → terminal review governor), a three-source
evidence layer (yfinance+FMP, SEC EDGAR RAG, Yahoo RSS) exposed as
discoverable FastMCP tools, and three guardrail layers (input validation,
token budget, deterministic output review). Research, not financial advice.

## How a run works

1. **Profiler** reads the latest 10-K Item 1, the newest MD&A, the segment
   note and the earnings exhibit (an image-only deck is transcribed once per
   filing) and writes a profile: business, archetype, segments, the KPIs the
   company itself reports, accounting flags. Cached by ticker + filing
   accession numbers.
2. **Planner** turns the profile plus a market summary into the five questions
   that decide the outlook, the ways standard metrics mislead here, and peer
   tickers (stored only).
3. **Research** works through Q1–Q5 in the filings (every excerpt tagged with
   source, date, period, basis, segment; each chunk used once), adds general
   evidence, the generic guidance scan, market data and news, then a
   count-only **coverage gate** loops back once for uncovered questions.
4. **Bull ↔ Bear** argue Q1–Q5 (at least two rounds), the **Judge** rules per
   question and overall, **claim review** re-audits flagged claims (flag
   upheld / overturned / unresolved), and the **governor** traces every number.

CHANGES.md lists what this iteration removed and fixed.

## Install

    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    cp .env.example .env   # add ANTHROPIC_API_KEY and FMP_API_KEY

## Run

    python run.py TICKER [--max-rounds 2] [--budget 300000]   # one governed debate (rounds >= 2)
    python -m markut.eval TICKER      # full debate + baseline comparison (paid)
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
- **PDF report** — any stored run as an equity-style report (title block,
  key metrics, the governed verdict with UNGROUNDED tags as footnotes, the
  debate round by round, judge scoreboards, appendix with the evidence and
  method). Console: `download NVDA` (Tab cycles the run dates); public
  player: "download report"; API: `/api/runs/{id}/report.pdf`. Built from
  the store on request in ~0.1s (`markut/report.py`, ReportLab, IBM Plex
  Serif + JetBrains Mono embedded).

Shared front-end code lives in `static/debate.js` / `debate.css` (no CDN).
API: `/api/health`, `/api/featured`, `/api/article`, `/api/runs[?ticker=]`,
`/api/runs/{id}`, `/api/runs/{id}/report.pdf`, `/api/replay?run=ID[&speed]`,
`/api/debate?ticker=&max_rounds=`.

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

## Deploy

The repo ships a `Dockerfile` (CPU-only torch, both local models baked in at
build time so cold starts are fast, non-root user, port 7860) that runs on
Hugging Face Spaces and on Railway. The block at the very top of this file
is the Spaces metadata (`sdk: docker`, `app_port: 7860`).

### Hugging Face Spaces (Docker/Gradio Spaces are now a paid feature)

The Dockerfile and the metadata block at the top of this README still work
on a Space if you have a paid account: push the repo to the Space's `main`
branch and set the four secrets under Variables and secrets.

### Railway (paid plan needed for the memory)

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

## Output-quality audit (run #2, AVGO, 2026-10-06)

An audit of the first live run drove a three-tier pass. Each item has a
regression test on the stored AVGO run (`tests/test_audit_regressions.py`);
the LangGraph topology is unchanged.

- **Evidence labels.** Every EPS/P-E line names period and basis (GAAP TTM vs
  non-GAAP consensus); `revenueGrowth` is labeled MRQ YoY with a separate
  FY-vs-FY line; consensus EPS by fiscal year; packet-level BASIS/PERIOD notes.
- **Governor.** Label binding (`MISLABELED` when a number traces under a
  different period/basis), derived arithmetic recomputed from the cited
  anchors (`MISCOMPUTED` with the implied value; precision-aware tolerance,
  which also fixed the "DERIVED but footnoted" bug), and a `SCENARIO CHECK`
  for multiple × EPS statements whose direction the arithmetic contradicts.
- **Structured replies.** Judge, claim review and revision use schema-enforced
  output with one repair retry and raw-reply logging. Bull/bear turns that hit
  the token cap are regenerated with a word budget, then trimmed to the last
  complete sentence — a truncated argument never reaches the judge.
- **Evidence coverage.** 8-K exhibit matcher fixed (Broadcom's `...xex99.htm`
  release was never indexed, hence the empty guidance block); 10-K Item 1
  business overview and the 10-Q commitments/contingencies note indexed; an
  obligations/guidance retrieval theme; newest filing wins duplicated text;
  claim review corroborates flagged numbers against the filings before the
  paid re-audit and appends a traceable addendum; news window 30 days ranked
  by materiality; a market-context block (returns, distance from highs,
  earnings reaction).
- **Analytical depth.** A deterministic `[VALUATION]` block (EV/EBITDA, FCF
  yield, PEG, consensus EPS × multiple bear/base/bull table; no peer table —
  peers would need a curated list per ticker, which cannot cover an arbitrary
  symbol) that agents cite instead of computing; a judge
  checklist and a verdict format ending with the debates that move the stock,
  what would change the view, and the next catalyst. Notebook prompts remain
  byte-identical as `*_NOTEBOOK` constants; live prompts extend them.

### Cost after the audit pass (measured, AVGO, two rounds, Sonnet 5.5)

The packet is ~70% larger than before the audit, but the evidence now rides in
a cached system prefix shared by every call of a run, so most input tokens
are cache reads billed at a tenth of the input price. Run #8 (2026-10-08):
8 calls, no retries, 87.6k input tokens of which 56.8k were cache reads and
8.3k a cache write, 9.1k output — about $0.17 at list prices, against about
$0.25 for the first AVGO run on a smaller packet. A cold cache (first run on
a ticker) writes four prefixes (one per schema) and lands near $0.22.
Per-call usage is printed as `CALL: in=... cache_w=... cache_r=... out=...`.

### Run #9 audit (2026-10-08)

- **Data gaps.** The packet opens with a `[DATA GAPS]` block naming every
  source that failed, with a clean reason (FMP's plan limit shows as
  "not covered by the FMP subscription (HTTP 402)", never a parser trace),
  plus the known permanent gap (earnings-call transcripts). The governor
  line, the page and the PDF say "N sources unavailable".
- **Guarantees in full.** Every dollar-bearing sentence of the 10-Q
  commitments note is its own packet line (the $29B Backstop maximum, the
  $42B convertible notes); a commitments quote is never cut before its first
  dollar figure; corroboration accepts the filing's "$ 29 billion" spelling.
- **Scenarios.** P/E on the current-FY and next-FY consensus side by side; a
  trailing P/E band from the company's own history, used as the multiple
  range only when p75/p25 ≤ 2 (otherwise shown as information and the grid
  falls back to today's forward multiple ±20%, saying so); EPS cases −15% /
  consensus / +10%; the "today's multiple × current-FY EPS" rows are gone.
- **All-time high** from daily closes, checked against the 52-week high
  close of the same series. **8-K highlights** (headline figures, segment and
  AI lines, CEO quote) and **dated customer-concentration** sentences from the
  10-K, 10-Q and 8-K are deterministic packet blocks.
