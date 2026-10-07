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
# (call_claude). ~3-4x a measured fully-loaded run — catches runaways.
TOKEN_BUDGET = 200_000

DEFAULT_MAX_ROUNDS = 2
