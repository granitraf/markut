"""Central configuration — every constant the notebook scattered across cells.

All modules read from HERE; no other module calls load_dotenv or hardcodes a
path. CLI flags override by MUTATING these attributes (import the module and
read late — `config.TOKEN_BUDGET` — never `from config import TOKEN_BUDGET`),
which preserves the notebook's late-binding globals.
"""
import os
from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
FMP_API_KEY = os.getenv("FMP_API_KEY")

# model used for every agent call (notebook: call_claude's hardcoded model=).
# Sonnet 5.5: same price as Sonnet 5 ($2/$10 per MTok), 1M context.
MODEL_NAME = os.getenv("MARKUT_MODEL", "claude-sonnet-5-5")
# Extended thinking on the Sonnet 5.5 family. Unset -> thinking OFF
# ("between_tools", the model's lowest setting): closest to the notebook's
# no-thinking calls, cheapest, and max_tokens stays the budget for the VISIBLE
# reply. Set to low|medium|high to run adaptive thinking at that effort
# (thinking tokens then count toward max_tokens and are billed as output).
THINKING_EFFORT = os.getenv("THINKING_EFFORT") or None

# SEC EDGAR requires a contactable User-Agent; cache dirs are created lazily
# by the modules that own them (mirrors the notebook cells' makedirs calls).
EDGAR_UA = {"User-Agent": "Granit Rrafshi granitraf@gmail.com"}
EDGAR_CACHE = "edgar_cache"
NEWS_CACHE_DIR = "news_cache"
# run log (markut.store): every debate, every turn. One SQLite file;
# MARKUT_DB overrides the location.
DB_PATH = os.getenv("MARKUT_DB", "markut_runs.db")
# hosted store (Supabase Postgres): set DATABASE_URL to the project's connection
# string and every run is written there instead of the local SQLite file
DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL") or None
# pre-database JSON run records, imported into the store once on first start
LEGACY_RECORDS_DIR = "web_records"
# public page presets: comma-separated tickers to pin (in order). Empty = the
# five most recently debated tickers.
FEATURED_TICKERS = [t for t in os.getenv("FEATURED_TICKERS", "").split(",") if t.strip()]
FEATURED_LIMIT = 5

# deployment: Railway sets RAILWAY_ENVIRONMENT, Hugging Face Spaces sets SPACE_ID,
# the Dockerfile sets MARKUT_PRODUCTION.
# In production the console FAILS CLOSED without a password.
IN_PRODUCTION = bool(os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("SPACE_ID")
                     or os.getenv("MARKUT_PRODUCTION"))
# operator side gate (HTTP Basic, any username). Unset locally = open; unset in
# production = console locked.
CONSOLE_PASSWORD = os.getenv("CONSOLE_PASSWORD") or None
# load the embedding + reranking models at server start (background thread) so
# the first debate does not pay the ~10s model load; default on in production
WARM_MODELS = os.getenv("WARM_MODELS", "1" if IN_PRODUCTION else "0") == "1"

# local models (RAG bi-encoder + cross-encoder reranker)
EMBEDDER_MODEL = "all-MiniLM-L6-v2"
RERANKER_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"

# execution guardrail: soft close at 1x (should_continue), hard stop at 2x
# (call_claude). ~2x a cold run with the profiler, planner and a transcribed
# slide deck — catches runaways, never a normal debate.
TOKEN_BUDGET = 300_000

DEFAULT_MAX_ROUNDS = 2
# every ticker gets at least two rounds: a one-round debate has no rebuttal,
# so the judge never sees either side answer the other
MIN_ROUNDS = 2

# ---- profiler / planner / research (the understand-the-company-first pass) ----
# token caps for the four filing sections the profiler reads (4 chars/token)
PROFILE_SECTION_TOKENS = {"item1": 3000, "mdna": 3000, "segment": 2000, "exhibit": 2000, "slides": 2000}
PROFILER_MAX_TOKENS = 2500
PLANNER_MAX_TOKENS = 3500   # five questions with queries ran ~2k tokens; a truncated plan is invalid JSON
# image-only 8-K exhibits (investor decks filed as JPEG slides) are transcribed
# by the model once per filing and cached by accession number; set to 0 to
# record an extraction failure instead of spending the tokens
TRANSCRIBE_IMAGE_EXHIBITS = os.getenv("MARKUT_TRANSCRIBE_SLIDES", "1") == "1"
TRANSCRIPT_SLIDES_PER_CALL = 8
TRANSCRIPT_MAX_TOKENS = 4000
MAX_SLIDES_PER_EXHIBIT = 60
# research: excerpts per question and words per excerpt (text only — numbers,
# guidance, valuation rows and segment/KPI figures are never trimmed)
EXCERPTS_PER_QUESTION = 5
EXCERPT_WORDS = 50
CHUNKS_PER_QUERY = 4
# coverage gate: fewer covered questions than this routes research back once
COVERAGE_MIN_QUESTIONS = 4

# bull/bear reply budget (AUDIT FIX run #2: 3 of 4 arguments were cut off at the
# cap and reached the judge mid-sentence). First attempt at ARGUMENT_MAX_TOKENS;
# a reply that stops on max_tokens is regenerated ONCE with a word budget and a
# larger cap; a second truncation is trimmed to the last complete sentence.
ARGUMENT_MAX_TOKENS = 1500   # was 1000: with the word budget in the base prompt, a regeneration is now the exception
ARGUMENT_RETRY_MAX_TOKENS = 1600
ARGUMENT_WORD_BUDGET = 450
# judge reply cap (run #6: the checklist + closing sections overran 2000 and the
# JSON was cut mid-string twice). Length limits live in the prompt; the cap is headroom.
JUDGE_MAX_TOKENS = 4000
# claim-review reply cap (run #7: 1400 was cut twice once the review restated the whole verdict)
CLAIM_REVIEW_MAX_TOKENS = 2500

# NOTE: there is deliberately NO per-ticker peer table. A hand-picked list only
# ever covers the tickers someone thought of; any other symbol would get nothing
# or, worse, a guess that looks authoritative. Valuation uses the company's own data.
