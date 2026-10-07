"""FastAPI app: the public site, the operator console, the run-log API.

PAGES — public side and operator side are separate URLs. The operator side
(/console*, /api/debate, /api/runs listing) sits behind HTTP Basic auth when
CONSOLE_PASSWORD is set; in production it is LOCKED until it is set.
  GET /                          PUBLIC: featured runs replayed agent by agent + the research article
  GET /console                   OPERATOR: run a live debate / replay any run; ?run=ID reads one in full
  GET /console/library           OPERATOR: the archive — every run ever made, by date and ticker
  GET /static/*                  shared css/js

API
  GET /api/health                liveness (no DB, no models) + feature flags — Railway's health check
  GET /api/featured              latest finished run per ticker (public presets, max 5)
  GET /api/article               the public article (markdown; edit content/article.md)
  GET /api/runs[?ticker=NVDA]    OPERATOR: the run log (summary rows, newest first)
  GET /api/runs/{id}             one run: summary + every turn + every event
  GET /api/replay?run=ID[&speed] SSE replay of a stored run (free)
  GET /api/debate?ticker=NVDA&max_rounds=2
                                 OPERATOR: SSE live debate (paid: 8-12 model calls); stored when done

WHY Server-Sent Events: one plain HTTP response the browser keeps open, one
"event: NAME / data: JSON" block per graph node. No websockets, no polling,
and the browser's built-in EventSource does the parsing.

ASYNC SEAM: the graph is synchronous (and research_node's MCP client runs
its own asyncio.run in whatever thread calls it), so the live generator is
iterated in a worker thread via starlette's iterate_in_threadpool. That
thread has no running loop, which is exactly the branch _run_coro_blocking
takes for the CLI — one code path, three entry points.
"""
import asyncio
import json
import os
import secrets
import sys
import threading
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import iterate_in_threadpool

from markut import config, store
from markut.web import events as ev
from markut.web import replay

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
CONTENT_DIR = os.path.join(os.path.dirname(__file__), "content")
MAX_ROUNDS_CAP = 4  # a web form must not be able to request a 40-round debate

# EXECUTION GUARDRAIL for the web layer: one live debate at a time. Each one
# is 8-12 paid calls; a page refreshed five times must queue, not fan out.
# The debate runs in its OWN thread (not the request's lifetime): if the
# viewer's connection drops mid-run — proxy timeout, closed tab — the run
# still completes and is logged, so the tokens already spent are never wasted.
_live_job = None            # threading.Thread of the run in progress (or finished)
_live_guard = threading.Lock()
HEARTBEAT_SECONDS = 15      # SSE comment cadence during silent phases (research ~1-2 min)


def live_busy() -> bool:
    return _live_job is not None and _live_job.is_alive()


def _run_debate_job(ticker: str, max_rounds: int, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop):
    # PRODUCER (worker thread): iterate the sync graph to COMPLETION, hand each
    # event to the async consumer, then log the run. Nothing here depends on
    # the HTTP connection staying open.
    seen = []
    def push(item):
        loop.call_soon_threadsafe(queue.put_nowait, item)
    try:
        for event in ev.stream_debate(ticker, max_rounds):
            seen.append(stamped(event))
            push(event)
        # EVERY run that got past validation is logged — finished or errored —
        # so the archive is a complete history, not a highlight reel
        if seen and seen[0]["event"] == "start":
            try:
                run_id = store.record_run(seen, mode="web", db=store.target())
                push({"event": "saved", "data": {"run": run_id}})
            except Exception as e:  # a logging failure must not look like a debate failure
                push({"event": "saved", "data": {"run": None, "error": f"{type(e).__name__}: {e}"}})
    except Exception as e:
        push({"event": "error", "data": {"stage": "server", "message": f"{type(e).__name__}: {e}"}})
    finally:
        push(None)  # end of stream

# store.bootstrap() once per DB target per process — lazily, on first use, so
# tests can repoint config.DB_PATH without lifespan plumbing
_bootstrapped = set()


def _ensure_store():
    t = store.target()
    if t not in _bootstrapped:
        store.bootstrap(t, bundled_dir=replay.BUNDLED_DIR, legacy_dir=config.LEGACY_RECORDS_DIR)
        _bootstrapped.add(t)


def _warm_models():
    # background thread: import the RAG module (torch, chromadb) and load both
    # local models so the first live debate does not stall on them. Failure is
    # logged, never fatal — the models load lazily on first use anyway.
    try:
        from markut.evidence import rag
        rag.get_embedder()
        rag.get_reranker()
        print("WARM: embedding + reranking models loaded")
    except Exception as e:
        print(f"WARM: model preload failed ({type(e).__name__}: {e}); will load lazily")


@asynccontextmanager
async def lifespan(_app):
    if config.IN_PRODUCTION and not config.CONSOLE_PASSWORD:
        print("SECURITY: CONSOLE_PASSWORD is not set — the console and live debates are LOCKED")
    if config.WARM_MODELS:
        threading.Thread(target=_warm_models, name="warm-models", daemon=True).start()
    yield


app = FastAPI(title="Markut", docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# ---- operator-side gate (HTTP Basic; the browser prompts once and remembers) ----
_basic = HTTPBasic(auto_error=False)


def require_console(credentials: HTTPBasicCredentials = Depends(_basic)):
    password = config.CONSOLE_PASSWORD
    if not password:
        if config.IN_PRODUCTION:
            # FAIL CLOSED: a public deployment must never expose paid runs by accident
            raise HTTPException(503, "CONSOLE_PASSWORD is not set on this server; the console is locked.")
        return  # local development: open
    ok = credentials is not None and secrets.compare_digest(
        credentials.password.encode(), password.encode())
    if not ok:
        raise HTTPException(401, "console password required",
                            headers={"WWW-Authenticate": 'Basic realm="Markut console"'})


OPERATOR = [Depends(require_console)]


def sse(event: dict) -> str:
    # one SSE block; ensure_ascii keeps the wire pure ASCII so em-dashes in
    # verdicts survive any proxy that mangles encodings
    return f"event: {event['event']}\ndata: {json.dumps(event['data'], ensure_ascii=True)}\n\n"


def stamped(event: dict) -> dict:
    # the store keeps WHEN each turn landed; the wire format ignores extra keys
    return {**event, "at": time.strftime("%Y-%m-%d %H:%M:%S")}


SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


# ---------------------------------------------------------------- pages
@app.get("/")
def public():
    return FileResponse(os.path.join(STATIC_DIR, "public.html"))


@app.get("/console", dependencies=OPERATOR)
def console():
    return FileResponse(os.path.join(STATIC_DIR, "console.html"))


@app.get("/console/library", dependencies=OPERATOR)
def library():
    return FileResponse(os.path.join(STATIC_DIR, "library.html"))


# ---------------------------------------------------------------- public api
@app.get("/api/health")
def health():
    # Railway's health check: must answer without touching the database or
    # the models, so a slow Supabase or a loading model never reads as "down"
    rag = sys.modules.get("markut.evidence.rag")
    return {"ok": True, "model": config.MODEL_NAME, "token_budget": config.TOKEN_BUDGET,
            "default_max_rounds": config.DEFAULT_MAX_ROUNDS, "db_path": store.backend_label(),
            "db_backend": "postgres" if store.is_postgres() else "sqlite",
            "live_enabled": bool(config.ANTHROPIC_API_KEY), "live_busy": live_busy(),
            "console_locked": config.IN_PRODUCTION and not config.CONSOLE_PASSWORD,
            "console_protected": bool(config.CONSOLE_PASSWORD),
            "models_warm": bool(rag and rag._EMBEDDER is not None and rag._RERANKER is not None)}


@app.get("/api/featured")
def featured():
    _ensure_store()
    return {"runs": store.featured_runs(store.target(), limit=config.FEATURED_LIMIT,
                                        tickers=config.FEATURED_TICKERS or None)}


@app.get("/api/article")
def article():
    # the operator's own write-up; markdown rendered client-side by debate.js
    path = os.path.join(CONTENT_DIR, "article.md")
    if not os.path.isfile(path):
        return PlainTextResponse("# Article\n\nWrite `markut/web/content/article.md` and reload.",
                                 media_type="text/markdown")
    return FileResponse(path, media_type="text/markdown; charset=utf-8")


@app.get("/api/runs/{run_id}")
def run_detail(run_id: int):
    _ensure_store()
    try:
        return store.get_run(run_id, store.target())
    except KeyError as e:
        return JSONResponse({"error": str(e)}, status_code=404)


@app.get("/api/replay")
async def replay_stream(run: int = Query(...), speed: float = Query(1.0, ge=0.0, le=10.0)):
    # speed=1 is demo pacing, speed=0 dumps instantly (tests, impatient humans)
    _ensure_store()
    try:
        stored = store.get_run(run, store.target())
    except KeyError as e:
        return JSONResponse({"error": str(e)}, status_code=404)

    async def gen():
        for event in replay.iter_replay(stored):
            if speed > 0:
                await asyncio.sleep(replay.delay_for(event) / speed)
            yield sse(event)
    return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)


# ---------------------------------------------------------------- operator api
@app.get("/api/runs", dependencies=OPERATOR)
def runs(ticker: str = Query(None)):
    _ensure_store()
    return {"runs": store.list_runs(store.target(), ticker=ticker)}


@app.get("/api/debate", dependencies=OPERATOR)
async def debate_stream(ticker: str = Query(...),
                        max_rounds: int = Query(config.DEFAULT_MAX_ROUNDS, ge=1, le=MAX_ROUNDS_CAP)):
    global _live_job
    _ensure_store()

    async def gen():
        global _live_job
        if not config.ANTHROPIC_API_KEY:
            yield sse({"event": "error", "data": {"stage": "config",
                       "message": "ANTHROPIC_API_KEY is not set — live debates are disabled on this server. "
                                  "Use a stored run instead."}})
            return
        queue: asyncio.Queue = asyncio.Queue()
        with _live_guard:
            if live_busy():
                yield sse({"event": "error", "data": {"stage": "busy",
                           "message": "A live debate is already running on this server. Try again in a minute, "
                                      "or watch a stored run."}})
                return
            _live_job = threading.Thread(target=_run_debate_job, name="debate",
                                         args=(ticker, max_rounds, queue, asyncio.get_running_loop()),
                                         daemon=True)
            _live_job.start()
        # CONSUMER: forward events; during silent phases send an SSE comment
        # every HEARTBEAT_SECONDS so proxies never see an idle connection.
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                yield ": ping\n\n"
                continue
            if item is None:
                return
            yield sse(item)
    return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)
