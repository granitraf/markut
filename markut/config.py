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

# model used for every agent call (notebook: call_claude's hardcoded model=)
MODEL_NAME = "claude-sonnet-4-6"

# SEC EDGAR requires a contactable User-Agent; cache dirs are created lazily
# by the modules that own them (mirrors the notebook cells' makedirs calls).
EDGAR_UA = {"User-Agent": "Granit Rrafshi granitraf@gmail.com"}
EDGAR_CACHE = "edgar_cache"
NEWS_CACHE_DIR = "news_cache"

# local models (RAG bi-encoder + cross-encoder reranker)
EMBEDDER_MODEL = "all-MiniLM-L6-v2"
RERANKER_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"

# execution guardrail: soft close at 1x (should_continue), hard stop at 2x
# (call_claude). ~3-4x a measured fully-loaded run — catches runaways.
TOKEN_BUDGET = 200_000

DEFAULT_MAX_ROUNDS = 2
