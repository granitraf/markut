"""The SQLite run store, offline against a temp DB: summarize, record/list/
get roundtrip (every event becomes a turn), idempotent import of the
bundled course run, and the terminal listing."""
import json
import os
import sys

import pytest

from markut import store
from markut.web import replay

FIXTURE = os.path.join(replay.BUNDLED_DIR, "nvda_2026-07-17_course_run.json")
COURSE = json.load(open(FIXTURE, encoding="utf-8"))


def _events(ticker="TEST"):
    return [
        {"event": "start", "data": {"ticker": ticker, "max_rounds": 1, "model": "m"}, "at": "2026-10-06 10:00:00"},
        {"event": "research", "data": {"evidence": "[QUOTE]\n- Price (current): $12.34  [source: x]"}, "at": "2026-10-06 10:00:05"},
        {"event": "bull", "data": {"round": 1, "text": "# up"}},
        {"event": "bear", "data": {"round": 1, "text": "# down"}},
        {"event": "judge", "data": {"round": 1, "converged": True, "verdict": "v", "bull_strongest": "b",
                                    "bear_strongest": "r", "unsupported_claims": ["c1"], "reasoning": "why"}},
        {"event": "route", "data": {"round": 1, "decision": "done", "reason": "judge ruled the debate converged"}},
        {"event": "news_verify", "data": {"claim_reviews": [{"status": "unresolved", "claim": "c1"}], "reasoning": "", "verdict_changed": False, "leads": ""}},
        {"event": "review", "data": {"verdict": "final v. Not advice.", "report": {"final_status": "clean", "resolutions": []},
                                     "stats": {"claims": 2, "cited": 2, "flagged": 0, "derived": 0, "labeled": 0, "annotated": 0, "status": "clean"},
                                     "budget_exceeded": False}},
        {"event": "done", "data": {"rounds": 1, "converged": True, "budget_exceeded": False,
                                   "usage": {"calls": 4, "input": 100, "output": 50}}, "at": "2026-10-06 10:01:00"},
    ]


def test_summarize_extracts_the_library_fields():
    s = store.summarize(_events("ABC"))
    assert s["ticker"] == "ABC" and s["price"] == "$12.34" and s["status"] == "done"
    assert s["rounds"] == 1 and s["converged"] == 1 and s["review_status"] == "clean"
    assert (s["claims"], s["cited"], s["calls"], s["input_tokens"]) == (2, 2, 4, 100)
    assert s["final_verdict"] == "final v. Not advice."


def test_summarize_errored_run_keeps_what_landed():
    ev = _events()[:4] + [{"event": "error", "data": {"stage": "debate", "message": "boom", "usage": {"calls": 2, "input": 9, "output": 1}}}]
    s = store.summarize(ev)
    assert s["status"] == "error" and s["error"] == "boom" and s["calls"] == 2 and s["final_verdict"] is None


def test_record_list_get_roundtrip(tmp_path):
    db = str(tmp_path / "runs.db")
    rid = store.record_run(_events("ABC"), mode="cli", db=db)
    rid2 = store.record_run(_events("XYZ"), mode="web", db=db)
    rows = store.list_runs(db)
    assert [r["id"] for r in rows] == [rid2, rid] or [r["ticker"] for r in rows] == ["XYZ", "ABC"]
    assert store.list_runs(db, ticker="abc")[0]["ticker"] == "ABC"
    assert "final_verdict" not in rows[0]            # listing stays light
    run = store.get_run(rid, db)
    assert run["started_at"] == "2026-10-06 10:00:00" and run["finished_at"] == "2026-10-06 10:01:00"
    assert [e["event"] for e in run["events"]] == [e["event"] for e in _events()]
    assert run["events"][2]["data"]["text"] == "# up"      # byte-faithful replay source
    judge = [t for t in run["turns"] if t["agent"] == "judge"][0]
    assert "Strongest bull point: b" in judge["content"] and "- c1" in judge["content"]
    with pytest.raises(KeyError):
        store.get_run(999, db)


def test_record_run_refuses_empty(tmp_path):
    with pytest.raises(ValueError):
        store.record_run([], mode="cli", db=str(tmp_path / "x.db"))


def test_bootstrap_imports_course_run_once(tmp_path, capsys):
    db = str(tmp_path / "runs.db")
    first = store.bootstrap(db, bundled_dir=replay.BUNDLED_DIR, legacy_dir=str(tmp_path / "none"))
    second = store.bootstrap(db, bundled_dir=replay.BUNDLED_DIR, legacy_dir=str(tmp_path / "none"))
    assert len(first) == 1 and second == []
    rows = store.list_runs(db)
    assert len(rows) == 1 and rows[0]["mode"] == "course" and rows[0]["ticker"] == "NVDA"
    assert rows[0]["started_at"] == "2026-07-17 00:00:00"
    assert (rows[0]["claims"], rows[0]["cited"], rows[0]["annotated"]) == (14, 12, 3)
    run = store.get_run(rows[0]["id"], db)
    assert len(run["events"]) == len(COURSE["events"])
    assert run["note"].startswith("The submitted course run")


def test_bootstrap_imports_legacy_web_records(tmp_path):
    legacy = tmp_path / "web_records"; legacy.mkdir()
    (legacy / "ABC_2026-10-01_120000.json").write_text(json.dumps(
        {"meta": {"ticker": "ABC", "recorded_at": "2026-10-01 12:00:00"}, "events": _events("ABC")}))
    (legacy / "broken.json").write_text("{not json")
    db = str(tmp_path / "runs.db")
    imported = store.bootstrap(db, bundled_dir=str(tmp_path / "nofix"), legacy_dir=str(legacy))
    assert [name for name, _ in imported] == ["ABC_2026-10-01_120000.json"]
    assert store.list_runs(db)[0]["started_at"] == "2026-10-01 12:00:00"


def test_cli_listing_prints_rows(tmp_path, monkeypatch, capsys):
    from markut import config
    db = str(tmp_path / "runs.db")
    monkeypatch.setattr(config, "DB_PATH", db)
    monkeypatch.setattr(config, "DATABASE_URL", None)
    store.record_run(_events("ABC"), mode="cli")
    store._main(["store", "list"])
    out = capsys.readouterr().out
    assert "ABC" in out and "$12.34" in out and "2/2" in out
    store._main(["store", "show", "1"])
    out = capsys.readouterr().out
    assert "BULL round 1" in out and "# up" in out


# ---------------------------------------------------------------- backend seam
def test_target_resolution_prefers_database_url(monkeypatch):
    from markut import config
    monkeypatch.setattr(config, "DB_PATH", "local.db")
    monkeypatch.setattr(config, "DATABASE_URL", None)
    assert store.target() == "local.db" and not store.is_postgres()
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://postgres.ref:secret@aws-0-eu.pooler.supabase.com:6543/postgres")
    assert store.is_postgres() and store.target("explicit.db") == "explicit.db"
    assert store.backend_label() == "postgres @ aws-0-eu.pooler.supabase.com:6543"   # password never shown
    assert "secret" not in store.backend_label()


def test_sql_and_schema_per_backend():
    pg = "postgresql://u:p@h/db"
    assert store._sql("SELECT ? , ?", pg) == "SELECT %s , %s"
    assert store._sql("SELECT ? , ?", "x.db") == "SELECT ? , ?"
    pg_schema = "\n".join(store._schema(pg))
    assert "BIGSERIAL PRIMARY KEY" in pg_schema and "ENABLE ROW LEVEL SECURITY" in pg_schema
    assert "run_id    BIGINT" in pg_schema
    lite_schema = "\n".join(store._schema("x.db"))
    assert "AUTOINCREMENT" in lite_schema and "ROW LEVEL" not in lite_schema


def test_migrate_copies_runs_and_is_idempotent(tmp_path, capsys):
    src, dst = str(tmp_path / "src.db"), str(tmp_path / "dst.db")
    a = store.record_run(_events("ABC"), mode="cli", db=src)
    b = store.record_run(_events("XYZ"), mode="web", db=src)
    copied = store.migrate(src, dst)
    assert [s for s, _ in copied] == [a, b]                 # oldest first
    rows = store.list_runs(dst)
    assert sorted(r["ticker"] for r in rows) == ["ABC", "XYZ"]
    moved = store.get_run(copied[0][1], dst)
    assert moved["events"] == store.get_run(a, src)["events"]      # byte-faithful
    assert moved["turns"] == store.get_run(a, src)["turns"]
    assert store.migrate(src, dst) == []                     # re-run: everything skipped
    assert "already present" in capsys.readouterr().out
    with pytest.raises(ValueError):
        store.migrate(src, src)


def test_postgres_branch_with_fake_driver(monkeypatch):
    # No Postgres in CI. Drive the psycopg branch against a fake connection
    # that records the SQL it was handed and answers like dict_row would —
    # proving placeholders, RETURNING id handling and schema/RLS statements
    # before the real credentials ever arrive.
    import types
    executed = []

    class FakeCursor:
        def __init__(self):
            self._next = None
        def execute(self, sql, params=None):
            executed.append((sql, params))
            if "RETURNING id" in sql:
                self._next = {"id": 42}
            elif sql.startswith("SELECT COUNT"):
                self._next = {"n": 1}
            elif sql.startswith("SELECT 1 FROM runs WHERE source"):
                self._next = None
            else:
                self._next = None
            return self
        def executemany(self, sql, rows):
            executed.append((sql, list(rows)))
        def fetchone(self):
            return self._next
        def fetchall(self):
            return []

    class FakeConn:
        def cursor(self): return FakeCursor()
        def commit(self): executed.append(("COMMIT", None))
        def close(self): executed.append(("CLOSE", None))

    connect_kwargs = {}
    def fake_connect(url, **kw):
        connect_kwargs.update(kw); connect_kwargs["url"] = url
        return FakeConn()
    fake_psycopg = types.ModuleType("psycopg"); fake_psycopg.connect = fake_connect
    fake_rows = types.ModuleType("psycopg.rows"); fake_rows.dict_row = object()
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)
    monkeypatch.setitem(sys.modules, "psycopg.rows", fake_rows)

    url = "postgresql://postgres.ref:pw@aws-0-eu.pooler.supabase.com:6543/postgres"
    store._schema_ready.discard(url)
    rid = store.record_run(_events("PGT"), mode="web", db=url)
    assert rid == 42
    assert connect_kwargs["prepare_threshold"] is None and connect_kwargs["url"] == url
    sqls = [s for s, _ in executed]
    assert any("BIGSERIAL PRIMARY KEY" in s for s in sqls)
    assert "ALTER TABLE runs ENABLE ROW LEVEL SECURITY" in sqls
    insert = [s for s in sqls if s.startswith("INSERT INTO runs")][0]
    assert "%s" in insert and "?" not in insert and insert.endswith("RETURNING id")
    turns_sql, turns_rows = [(s, p) for s, p in executed if s.startswith("INSERT INTO turns")][0]
    assert "%s" in turns_sql and len(turns_rows) == len(_events()) and turns_rows[0][0] == 42
    assert sqls[-2:] == ["COMMIT", "CLOSE"]


def test_featured_runs_latest_per_ticker(tmp_path):
    db = str(tmp_path / "runs.db")
    e = _events("ABC"); e[0]["at"] = "2026-10-01 10:00:00"; e[-1]["at"] = "2026-10-01 10:01:00"
    old_abc = store.record_run(e, mode="cli", db=db)
    new_abc = store.record_run(_events("ABC"), mode="web", db=db)              # 2026-10-06
    xyz = store.record_run(_events("XYZ"), mode="web", db=db)
    bad = _events("ERR")[:3] + [{"event": "error", "data": {"stage": "debate", "message": "boom"}}]
    store.record_run(bad, mode="web", db=db)                                    # errored: never featured
    rows = store.featured_runs(db)
    assert [(r["ticker"], r["id"]) for r in rows] == [("XYZ", xyz), ("ABC", new_abc)] or \
           [(r["ticker"], r["id"]) for r in rows] == [("ABC", new_abc), ("XYZ", xyz)]
    assert old_abc not in [r["id"] for r in rows]
    assert [r["ticker"] for r in store.featured_runs(db, tickers=["xyz", "ABC", "NOPE"])] == ["XYZ", "ABC"]
    assert len(store.featured_runs(db, limit=1)) == 1
