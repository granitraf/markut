"""Run store: a log of EVERY debate — ticker, when it ran, every agent turn,
every conclusion — shared by the CLI (run.py) and the web layer.

TWO BACKENDS, ONE CODE PATH
  - Postgres (Supabase) when DATABASE_URL is set: the hosted, always-on
    store the live site writes to and you can query from anywhere.
  - SQLite (markut_runs.db) otherwise: zero-config local fallback, and what
    the offline tests run against. Same schema, same functions.
  The target is a string: a postgres:// URL or a file path. Every function
  takes `db=None` and resolves it late from config (DATABASE_URL first,
  then DB_PATH), so tests and the migrate command can pass explicit ones.

  Both engines accept the same SQL here on purpose: INSERT ... RETURNING id
  (SQLite >= 3.35), TEXT timestamps (ISO strings sort correctly), and `?`
  placeholders, which _sql() rewrites to %s for psycopg. The only per-engine
  text is the primary-key type and the RLS statements (see _schema()).

TABLES
  runs   one row per debate: ticker, started/finished timestamps, mode
         (cli / web / course import), outcome (rounds, converged, budget),
         the review governor's stats, token usage, the price the packet
         saw, the final verdict and the evidence packet — the fields a
         library page sorts and filters on, without opening the turns.
  turns  one row per streamed event, in order: the agent, the round, the
         text a reader wants (the bull case, the judge's reasoning...) AND
         the full event JSON, so a replay is byte-faithful.

The event list (markut.web.events) is the ONLY input shape — the CLI and
the server both produce it, so there is exactly one writer path.

SUPABASE NOTE: tables in Supabase's `public` schema are also reachable
through its REST API with the project's anon key. The schema enables ROW
LEVEL SECURITY with no policies, so that door is shut: only this app's
direct Postgres connection (table owner) reads and writes.
"""
import json
import os
import re
import sqlite3
import sys
import time

from markut import config

_COLUMNS = """
    ticker          TEXT NOT NULL,
    started_at      TEXT NOT NULL,          -- ISO local time, sortable
    finished_at     TEXT,
    mode            TEXT NOT NULL,          -- cli | web | course | import
    source          TEXT,                   -- import provenance (file path) — dedupe key
    model           TEXT,
    max_rounds      INTEGER,
    rounds          INTEGER,
    converged       INTEGER,                -- 0/1
    budget_exceeded INTEGER,
    status          TEXT NOT NULL,          -- done | error
    error           TEXT,
    price           TEXT,                   -- "Price (current)" line of the packet, e.g. $375.81
    claims INTEGER, cited INTEGER, flagged INTEGER, derived INTEGER, labeled INTEGER, annotated INTEGER,
    review_status   TEXT,
    calls INTEGER, input_tokens INTEGER, output_tokens INTEGER,
    final_verdict   TEXT,
    evidence        TEXT,
    note            TEXT
"""
_TURN_COLUMNS = """
    run_id    {run_ref} NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    seq       INTEGER NOT NULL,             -- order within the run
    at        TEXT,                         -- when this event arrived
    agent     TEXT NOT NULL,                -- event name: research/bull/bear/judge/route/news_verify/review/...
    round     INTEGER,
    content   TEXT,                         -- the human-readable text of the turn
    data_json TEXT NOT NULL                 -- full event data, for faithful replay
"""

_PRICE_RE = re.compile(r"Price \(current\):\s*(\$[\d,.]+)")
_schema_ready = set()   # targets whose schema this process has already ensured


# ---------------------------------------------------------------- backend seam
def target(db: str = None) -> str:
    # read config LATE (attribute access) so CLI flags / tests / .env repoint it
    return db or config.DATABASE_URL or config.DB_PATH


def is_postgres(db: str = None) -> bool:
    return str(target(db)).startswith(("postgres://", "postgresql://"))


def backend_label(db: str = None) -> str:
    # for the health endpoint: never leak the password in a URL
    t = target(db)
    if is_postgres(t):
        host = re.sub(r"^[^@]*@", "", t).split("/")[0]
        return f"postgres @ {host}"
    return f"sqlite @ {os.path.abspath(t)}"


def _sql(query: str, db: str = None) -> str:
    # one source of SQL: ? placeholders, rewritten for psycopg
    return query.replace("?", "%s") if is_postgres(db) else query


def _schema(db: str = None) -> list:
    pg = is_postgres(db)
    pk = "BIGSERIAL PRIMARY KEY" if pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    stmts = [
        f"CREATE TABLE IF NOT EXISTS runs (id {pk}, {_COLUMNS})",
        f"CREATE TABLE IF NOT EXISTS turns (id {pk}, {_TURN_COLUMNS.format(run_ref='BIGINT' if pg else 'INTEGER')})",
        "CREATE INDEX IF NOT EXISTS idx_turns_run ON turns(run_id, seq)",
        "CREATE INDEX IF NOT EXISTS idx_runs_ticker_date ON runs(ticker, started_at)",
        # small JSON cache: company profiles, planner output and slide transcripts,
        # keyed by ticker + the accession numbers of the filings they were read from
        "CREATE TABLE IF NOT EXISTS kv_cache (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, created_at TEXT NOT NULL)",
    ]
    if pg:
        # shut the Supabase REST door; the owner connection is unaffected
        stmts += ["ALTER TABLE runs ENABLE ROW LEVEL SECURITY",
                  "ALTER TABLE turns ENABLE ROW LEVEL SECURITY",
                  "ALTER TABLE kv_cache ENABLE ROW LEVEL SECURITY"]
    return stmts


def connect(db: str = None):
    """Open a connection to the target (schema ensured once per process).
    Callers close it; writes commit explicitly."""
    t = target(db)
    if is_postgres(t):
        import psycopg  # lazy: the sqlite path must not need the driver
        from psycopg.rows import dict_row
        # prepare_threshold=None: Supabase's transaction pooler (port 6543)
        # does not support server-side prepared statements
        conn = psycopg.connect(t, row_factory=dict_row, prepare_threshold=None, connect_timeout=15)
    else:
        parent = os.path.dirname(t)
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(t)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
    if t not in _schema_ready:
        cur = conn.cursor()
        for stmt in _schema(t):
            cur.execute(stmt)
        conn.commit()
        _schema_ready.add(t)
    return conn


def _row(r) -> dict:
    return dict(r) if r is not None else None


# ---------------------------------------------------------------- JSON cache
def cache_get(key: str, db: str = None):
    """The cached JSON value for key, or None. Never raises: a cache that is
    down just means a cache miss (the caller recomputes)."""
    try:
        conn = connect(db)
    except Exception as e:
        print(f"CACHE: unavailable ({type(e).__name__}: {e}) — treating as a miss")
        return None
    try:
        row = conn.cursor().execute(_sql("SELECT value_json FROM kv_cache WHERE key = ?", db), (key,)).fetchone()
    except Exception as e:
        print(f"CACHE: read failed ({type(e).__name__}: {e}) — treating as a miss")
        return None
    finally:
        conn.close()
    if row is None:
        return None
    try:
        return json.loads(_row(row)["value_json"])
    except Exception:
        return None


def cache_delete(prefix: str, db: str = None) -> int:
    """Remove every cached value whose key starts with prefix (e.g.
    'profile:XYZ:' or 'transcript:'). Returns the row count; never raises."""
    try:
        conn = connect(db)
    except Exception as e:
        print(f"CACHE: unavailable ({type(e).__name__}: {e})")
        return 0
    try:
        cur = conn.cursor()
        cur.execute(_sql("DELETE FROM kv_cache WHERE key LIKE ?", db), (prefix + "%",))
        conn.commit()
        return cur.rowcount or 0
    except Exception as e:
        print(f"CACHE: delete failed ({type(e).__name__}: {e})")
        return 0
    finally:
        conn.close()


def cache_put(key: str, value, db: str = None) -> bool:
    """Upsert a JSON value. Returns False (and says why) instead of raising."""
    try:
        conn = connect(db)
    except Exception as e:
        print(f"CACHE: unavailable ({type(e).__name__}: {e}) — not cached")
        return False
    try:
        conn.cursor().execute(_sql(
            "INSERT INTO kv_cache (key, value_json, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, created_at = excluded.created_at", db),
            (key, json.dumps(value, ensure_ascii=False), _iso()))
        conn.commit()
        return True
    except Exception as e:
        print(f"CACHE: write failed ({type(e).__name__}: {e}) — not cached")
        return False
    finally:
        conn.close()


# ---------------------------------------------------------------- shaping
def _iso(t: float = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t if t is not None else time.time()))


def turn_content(event: dict) -> str:
    # the one text a reader wants from each event — what the turns table
    # shows in a plain SELECT, without digging through JSON
    name, d = event.get("event"), event.get("data", {}) or {}
    if name == "profiler":
        p = d.get("profile", {}) or {}
        parts = [f"Business: {p.get('business', '')}", f"Archetype: {p.get('archetype', '')}"]
        parts += [f"Segment: {s.get('name')} — {s.get('latest_revenue', '')} {s.get('latest_yoy', '')} ({s.get('period', '')})"
                  for s in (p.get("segments") or []) if isinstance(s, dict)]
        parts += [f"KPI: {k.get('name')} — {k.get('definition', '')}" for k in (p.get("company_kpis") or []) if isinstance(k, dict)]
        if p.get("accounting_flags"):
            parts.append("Accounting flags: " + ", ".join(p["accounting_flags"]))
        if d.get("cached"):
            parts.append("(profile served from the cache)")
        return "\n".join(parts)
    if name == "planner":
        plan = d.get("plan", {}) or {}
        parts = [f"{q.get('id')}: {q.get('question')}" for q in plan.get("key_questions", [])]
        parts += [f"Would mislead: {x}" for x in plan.get("what_would_mislead", [])]
        if d.get("cached"):
            parts.append("(plan served from the cache)")
        return "\n".join(parts)
    if name == "coverage":
        return (f"coverage gate: {d.get('covered')}/{d.get('total')} questions covered after pass {d.get('pass')} -> "
                f"{d.get('decision')}" + (f" (uncovered: {', '.join(d.get('uncovered') or [])})" if d.get("uncovered") else ""))
    if name == "research":
        return d.get("evidence", "")
    if name in ("bull", "bear"):
        return d.get("text", "")
    if name == "judge":
        parts = []
        for q in d.get("questions") or []:
            if isinstance(q, dict):
                parts.append(f"{q.get('id')}: {q.get('answer', '')} [{q.get('stronger_side', '')}, {q.get('confidence', '')} confidence]")
        if d.get("planner_coverage"):
            parts.append(f"Planner coverage: {d['planner_coverage']}")
        if d.get("bull_strongest"):
            parts.append(f"Strongest bull point: {d['bull_strongest']}")
        if d.get("bear_strongest"):
            parts.append(f"Strongest bear point: {d['bear_strongest']}")
        if d.get("unsupported_claims"):
            parts.append("Unsupported claims:\n" + "\n".join(f"- {c}" for c in d["unsupported_claims"]))
        if d.get("reasoning"):
            parts.append(f"Reasoning: {d['reasoning']}")
        if d.get("verdict"):
            parts.append(f"Interim verdict: {d['verdict']}")
        return "\n\n".join(parts) or d.get("note", "")
    if name == "route":
        return f"{d.get('decision', '')}: {d.get('reason', '')}"
    if name == "news_verify":
        reviews = d.get("claim_reviews") or []
        lines = [f"- [{r.get('verdict') or r.get('status', '?')}] {r.get('claim', '')}" for r in reviews]
        return "\n".join(lines + ([d["reasoning"]] if d.get("reasoning") else []))
    if name == "review":
        return d.get("verdict", "")
    if name == "done":
        u = d.get("usage", {}) or {}
        return (f"{d.get('rounds')} round(s), converged={d.get('converged')}, "
                f"{u.get('calls')} calls, {u.get('input')} in / {u.get('output')} out tokens")
    if name == "error":
        return d.get("message", "")
    if name == "start":
        return f"{d.get('ticker', '')} · up to {d.get('max_rounds')} round(s) · {d.get('model', '')}"
    return json.dumps(d, ensure_ascii=False)[:2000]


def summarize(events: list) -> dict:
    """Fold an event list into the runs-row fields. Pure; tolerant of a run
    that ended in an error (then rounds/stats are whatever landed)."""
    by = {}
    for e in events:
        by.setdefault(e.get("event"), []).append(e.get("data", {}) or {})
    start = (by.get("start") or [{}])[0]
    research = (by.get("research") or [{}])[0]
    review = (by.get("review") or [{}])[0]
    stats = review.get("stats", {}) or {}
    done = (by.get("done") or [{}])[0]
    error = (by.get("error") or [{}])[0]
    usage = done.get("usage") or error.get("usage") or {}
    judges = by.get("judge") or []
    evidence = research.get("evidence", "") or ""
    price = _PRICE_RE.search(evidence)
    return {
        "ticker": start.get("ticker") or "?",
        "model": start.get("model"),
        "max_rounds": start.get("max_rounds"),
        "rounds": done.get("rounds", judges[-1]["round"] if judges else 0),
        "converged": int(bool(done.get("converged", judges[-1]["converged"] if judges else False))),
        "budget_exceeded": int(bool(done.get("budget_exceeded", review.get("budget_exceeded", False)))),
        "status": "done" if "done" in by else "error",
        "error": error.get("message"),
        "price": price.group(1) if price else None,
        "claims": stats.get("claims"), "cited": stats.get("cited"), "flagged": stats.get("flagged"),
        "derived": stats.get("derived"), "labeled": stats.get("labeled"), "annotated": stats.get("annotated"),
        "review_status": stats.get("status"),
        # input_tokens = every input token the run sent (uncached + cache write + cache read);
        # the done event keeps the split
        "calls": usage.get("calls"),
        "input_tokens": (None if usage.get("input") is None else
                         usage.get("input", 0) + (usage.get("cache_write") or 0) + (usage.get("cache_read") or 0)),
        "output_tokens": usage.get("output"),
        "final_verdict": review.get("verdict") or (judges[-1]["verdict"] if judges else None),
        "evidence": evidence,
        "note": start.get("note") or "",
    }


# ---------------------------------------------------------------- writes
def _insert_run_and_turns(conn, db, row: dict, turns: list) -> int:
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    cur = conn.cursor()
    cur.execute(_sql(f"INSERT INTO runs ({cols}) VALUES ({marks}) RETURNING id", db), list(row.values()))
    run_id = cur.fetchone()[0] if not is_postgres(db) else cur.fetchone()["id"]
    cur.executemany(_sql("INSERT INTO turns (run_id, seq, at, agent, round, content, data_json) "
                         "VALUES (?,?,?,?,?,?,?)", db),
                    [(run_id, *t) for t in turns])
    return run_id


def record_run(events: list, mode: str, db: str = None, source: str = None,
               started_at: str = None, finished_at: str = None, note: str = None) -> int:
    """Store one run (every event becomes a turn) and return its id. Events
    may carry an "at" timestamp (set by the collector as they arrive); the
    run's started/finished default to the first/last of those, else now."""
    if not events:
        raise ValueError("record_run: no events to store")
    stamps = [e.get("at") for e in events if e.get("at")]
    started_at = started_at or (stamps[0] if stamps else _iso())
    finished_at = finished_at or (stamps[-1] if stamps else _iso())
    row = summarize(events)
    row.update({"mode": mode, "source": source, "started_at": started_at, "finished_at": finished_at})
    if note is not None:
        row["note"] = note
    turns = [(i, e.get("at"), e.get("event", "?"), (e.get("data") or {}).get("round"), turn_content(e),
              json.dumps({"event": e.get("event"), "data": e.get("data", {})}, ensure_ascii=False))
             for i, e in enumerate(events)]
    conn = connect(db)
    try:
        run_id = _insert_run_and_turns(conn, db, row, turns)
        conn.commit()
    finally:
        conn.close()
    return run_id


# ---------------------------------------------------------------- reads
_LIST_COLS = ("id, ticker, started_at, finished_at, mode, source, model, max_rounds, rounds, converged, "
              "budget_exceeded, status, error, price, claims, cited, flagged, derived, labeled, annotated, "
              "review_status, calls, input_tokens, output_tokens, note")


def list_runs(db: str = None, ticker: str = None) -> list:
    # newest first; the heavy text columns (verdict, evidence) stay out of
    # the listing so a long log stays a cheap query
    sql = f"SELECT {_LIST_COLS} FROM runs"
    args = []
    if ticker:
        sql += " WHERE ticker = ?"
        args.append(ticker.upper())
    sql += " ORDER BY started_at DESC, id DESC"
    conn = connect(db)
    try:
        return [_row(r) for r in conn.cursor().execute(_sql(sql, db), args).fetchall()]
    finally:
        conn.close()


def featured_runs(db: str = None, limit: int = 5, tickers: list = None) -> list:
    """The public page's presets: the LATEST finished run per ticker, newest
    first, at most `limit`. With `tickers`, only those symbols, in that order
    (the operator's pinned list); without, whatever has been run most
    recently."""
    sql = (f"SELECT {_LIST_COLS} FROM runs r WHERE status = 'done' AND started_at = "
           "(SELECT MAX(started_at) FROM runs r2 WHERE r2.ticker = r.ticker AND r2.status = 'done') "
           "ORDER BY started_at DESC")
    conn = connect(db)
    try:
        rows = [_row(r) for r in conn.cursor().execute(_sql(sql, db)).fetchall()]
    finally:
        conn.close()
    if tickers:
        wanted = [t.strip().upper() for t in tickers if t.strip()]
        by = {r["ticker"]: r for r in rows}
        rows = [by[t] for t in wanted if t in by]
    return rows[:limit]


def get_run(run_id: int, db: str = None) -> dict:
    conn = connect(db)
    try:
        cur = conn.cursor()
        row = cur.execute(_sql("SELECT * FROM runs WHERE id = ?", db), (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"no run with id {run_id}")
        turns = cur.execute(_sql("SELECT seq, at, agent, round, content, data_json FROM turns "
                                 "WHERE run_id = ? ORDER BY seq", db), (run_id,)).fetchall()
    finally:
        conn.close()
    run = _row(row)
    turns = [_row(t) for t in turns]
    run["events"] = [json.loads(t["data_json"]) for t in turns]
    run["turns"] = [{k: t[k] for k in ("seq", "at", "agent", "round", "content")} for t in turns]
    return run


def has_source(source: str, db: str = None) -> bool:
    conn = connect(db)
    try:
        return conn.cursor().execute(_sql("SELECT 1 FROM runs WHERE source = ?", db), (source,)).fetchone() is not None
    finally:
        conn.close()


# ---------------------------------------------------------------- imports / migration
def import_record_file(path: str, mode: str, db: str = None):
    """Import a legacy {"meta","events"} JSON record (web_records/ or a bundled
    fixture). Idempotent: the file path is the dedupe key. Returns the run id,
    or None when already imported."""
    source = os.path.abspath(path)
    if has_source(source, db):
        return None
    with open(path, encoding="utf-8") as f:
        record = json.load(f)
    meta = record.get("meta", {}) or {}
    stamp = meta.get("recorded_at") or _iso(os.path.getmtime(path))
    if len(stamp) == 10:          # date only (the course run) -> midnight, still sortable
        stamp += " 00:00:00"
    return record_run(record.get("events", []), mode=mode, db=db, source=source,
                      started_at=stamp, finished_at=stamp, note=meta.get("note", ""))


def bootstrap(db: str = None, bundled_dir: str = None, legacy_dir: str = "web_records") -> list:
    """Create the schema and pull in anything stored before it existed: the
    bundled course run (mode 'course') and loose web_records/*.json files
    (mode 'web'). Safe to call every start — nothing imports twice."""
    imported = []
    if bundled_dir is None:
        bundled_dir = os.path.join(os.path.dirname(__file__), "web", "fixtures")
    for directory, mode in ((bundled_dir, "course"), (legacy_dir, "web")):
        if not os.path.isdir(directory):
            continue
        for fname in sorted(os.listdir(directory)):
            if fname.endswith(".json"):
                try:
                    run_id = import_record_file(os.path.join(directory, fname), mode, db)
                except (OSError, ValueError, KeyError) as e:
                    print(f"STORE: skipped {fname} ({e})")
                    continue
                if run_id is not None:
                    imported.append((fname, run_id))
                    print(f"STORE: imported {fname} as run {run_id}")
    return imported


def migrate(src: str, dst: str = None) -> list:
    """Copy every run (and its turns) from one store to another — the
    SQLite file -> Supabase move. Dedupe: a run whose (ticker, started_at,
    mode) already exists at the destination is skipped, so re-running is
    safe. Returns [(src_id, dst_id)] for the runs copied."""
    dst = target(dst)
    if os.path.abspath(str(src)) == os.path.abspath(str(dst)):
        raise ValueError("migrate: source and destination are the same store")
    copied = []
    for summary in reversed(list_runs(src)):          # oldest first -> ids keep chronological order
        run = get_run(summary["id"], src)
        conn = connect(dst)
        try:
            exists = conn.cursor().execute(
                _sql("SELECT id FROM runs WHERE ticker = ? AND started_at = ? AND mode = ?", dst),
                (run["ticker"], run["started_at"], run["mode"])).fetchone()
            if exists:
                print(f"MIGRATE: run {run['id']} ({run['ticker']} {run['started_at']}) already present — skipped")
                continue
            row = {k: run[k] for k in run if k not in ("id", "events", "turns")}
            turns = [(t["seq"], t["at"], t["agent"], t["round"], t["content"], json.dumps(e, ensure_ascii=False))
                     for t, e in zip(run["turns"], run["events"])]
            new_id = _insert_run_and_turns(conn, dst, row, turns)
            conn.commit()
        finally:
            conn.close()
        copied.append((run["id"], new_id))
        print(f"MIGRATE: run {run['id']} ({run['ticker']} {run['started_at']}) -> {new_id}")
    return copied


# ---------------------------------------------------------------- terminal
def _main(argv):
    # python -m markut.store [list [TICKER] | show ID | migrate [SRC_SQLITE]]  — a terminal read of the log
    cmd = argv[1] if len(argv) > 1 else "list"
    if cmd == "list":
        bootstrap()
        rows = list_runs(ticker=argv[2] if len(argv) > 2 else None)
        print(f"{'id':>4}  {'started':<19}  {'ticker':<6}  {'mode':<6}  {'price':>9}  rounds  conv  "
              f"{'grounding':>9}  annot  {'tokens':>8}  status")
        for r in rows:
            grounding = (f"{r['cited']}/{r['claims']}" if r["claims"] is not None else "-")
            tokens = (r["input_tokens"] or 0) + (r["output_tokens"] or 0)
            print(f"{r['id']:>4}  {r['started_at']:<19}  {r['ticker']:<6}  {r['mode']:<6}  {r['price'] or '-':>9}  "
                  f"{r['rounds'] if r['rounds'] is not None else '-':>6}  {'yes' if r['converged'] else 'no':<4}  "
                  f"{grounding:>9}  {r['annotated'] if r['annotated'] is not None else '-':>5}  {tokens:>8}  {r['status']}")
        print(f"\n{len(rows)} run(s) in {backend_label()}")
    elif cmd == "show" and len(argv) > 2:
        run = get_run(int(argv[2]))
        print(f"RUN {run['id']}  {run['ticker']}  {run['started_at']}  mode={run['mode']}  status={run['status']}")
        for t in run["turns"]:
            head = f"[{t['seq']}] {t['agent'].upper()}" + (f" round {t['round']}" if t["round"] else "")
            print("\n" + head + "\n" + "-" * len(head) + "\n" + (t["content"] or ""))
    elif cmd == "migrate":
        src = argv[2] if len(argv) > 2 else config.DB_PATH
        if not is_postgres():
            print("migrate: DATABASE_URL is not set — nothing to migrate to. Put the Supabase connection "
                  "string in .env as DATABASE_URL and run again.")
            sys.exit(1)
        print(f"MIGRATE: {os.path.abspath(src)} -> {backend_label()}")
        copied = migrate(src)
        print(f"MIGRATE: {len(copied)} run(s) copied; {len(list_runs())} run(s) now in {backend_label()}")
    elif cmd == "ping":
        conn = connect()
        try:
            n = conn.cursor().execute("SELECT COUNT(*) AS n FROM runs").fetchone()
            n = n["n"] if is_postgres() else n[0]
        finally:
            conn.close()
        print(f"OK: {backend_label()} — {n} run(s)")
    else:
        print("usage: python -m markut.store [list [TICKER] | show RUN_ID | migrate [SRC_SQLITE_PATH] | ping]")


if __name__ == "__main__":
    _main(sys.argv)
