"""The HTTP layer, offline via TestClient against a temp DB: pages, health,
run log API, replay stream (speed=0), live-stream refusal without an API
key, and the round cap."""
import json

import pytest
from fastapi.testclient import TestClient

from markut import config, store
from markut.web import app as app_mod

client = TestClient(app_mod.app)


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    # every test gets a fresh store; the lazy bootstrap re-runs for the new path
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "runs.db"))
    monkeypatch.setattr(config, "DATABASE_URL", None)   # never let a .env Supabase URL into the tests
    monkeypatch.setattr(config, "CONSOLE_PASSWORD", None)
    monkeypatch.setattr(config, "IN_PRODUCTION", False)
    monkeypatch.setattr(config, "LEGACY_RECORDS_DIR", str(tmp_path / "no_legacy"))
    app_mod._bootstrapped.clear()
    yield


def parse_sse(text: str) -> list:
    out = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.split("\n") if ": " in line)
        out.append({"event": lines["event"], "data": json.loads(lines["data"])})
    return out


def test_pages_serve():
    pub = client.get("/")
    assert pub.status_code == 200 and "<title>Markut Debate</title>" in pub.text and "/api/featured" in pub.text
    assert "/console" not in pub.text                      # the public page never links to the operator side
    con = client.get("/console")
    assert con.status_code == 200 and "<title>Markut Console</title>" in con.text and "/api/debate" in con.text
    lib = client.get("/console/library")
    assert lib.status_code == 200 and "<title>Markut Archive</title>" in lib.text and "/api/runs" in lib.text
    js = client.get("/static/debate.js")
    assert js.status_code == 200 and "window.Markut" in js.text
    assert client.get("/static/debate.css").status_code == 200


def test_featured_and_article():
    runs = client.get("/api/featured").json()["runs"]
    assert [r["ticker"] for r in runs] == ["NVDA"] and runs[0]["status"] == "done"
    art = client.get("/api/article")
    assert art.status_code == 200 and art.headers["content-type"].startswith("text/markdown")
    assert art.text.lstrip().startswith("# ")


def test_health_reports_config_and_db():
    h = client.get("/api/health").json()
    assert h["ok"] is True and h["model"] == config.MODEL_NAME and h["db_path"].endswith("runs.db") and h["db_backend"] == "sqlite"


def test_runs_lists_the_bundled_course_run():
    runs = client.get("/api/runs").json()["runs"]
    assert len(runs) == 1 and runs[0]["ticker"] == "NVDA" and runs[0]["mode"] == "course"
    detail = client.get(f"/api/runs/{runs[0]['id']}").json()
    assert detail["final_verdict"].startswith("NVDA at $202.76") and len(detail["events"]) == 13
    assert [t["agent"] for t in detail["turns"]][:3] == ["start", "research", "bull"]
    assert client.get("/api/runs/999").status_code == 404
    assert client.get("/api/runs", params={"ticker": "zzz"}).json()["runs"] == []


def test_replay_streams_a_stored_run_instantly():
    rid = client.get("/api/runs").json()["runs"][0]["id"]
    r = client.get("/api/replay", params={"run": rid, "speed": 0})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(r.text)
    assert [e["event"] for e in events] == ["start", "research", "bull", "bear", "judge", "route",
                                            "bull", "bear", "judge", "route", "news_verify", "review", "done"]
    start = events[0]["data"]
    assert start["mode"] == "replay" and start["run_id"] == rid and start["run_mode"] == "course"
    assert start["recorded_at"] == "2026-07-17 00:00:00"
    review = events[-2]["data"]
    assert review["stats"] == {"claims": 14, "cited": 12, "flagged": 2, "derived": 1,
                               "labeled": 0, "annotated": 3, "status": "annotated"}
    assert review["verdict"].count("[UNGROUNDED") == 3
    assert events[-1]["data"]["usage"] == {"calls": 8, "input": 50207, "output": 8270}
    assert client.get("/api/replay", params={"run": 999}).status_code == 404


def test_live_stream_refuses_without_api_key(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", None)
    events = parse_sse(client.get("/api/debate", params={"ticker": "NVDA"}).text)
    assert [e["event"] for e in events] == ["error"] and events[0]["data"]["stage"] == "config"
    assert len(client.get("/api/runs").json()["runs"]) == 1   # a refused request is not a run


def test_live_stream_logs_a_fake_debate(monkeypatch):
    # swap the generator for a scripted one: the server must store the run
    # and announce its id — the path a real debate takes, minus the model
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")
    def fake_stream(ticker, max_rounds):
        yield {"event": "start", "data": {"ticker": ticker, "max_rounds": max_rounds, "model": "m"}}
        yield {"event": "research", "data": {"evidence": "- Price (current): $1.00  [source: x]"}}
        yield {"event": "review", "data": {"verdict": "v", "report": {}, "stats": {"claims": 0, "cited": 0, "flagged": 0,
               "derived": 0, "labeled": 0, "annotated": 0, "status": "clean"}, "budget_exceeded": False}}
        yield {"event": "done", "data": {"rounds": 1, "converged": True, "budget_exceeded": False,
               "usage": {"calls": 1, "input": 2, "output": 3}}}
    monkeypatch.setattr(app_mod.ev, "stream_debate", fake_stream)
    events = parse_sse(client.get("/api/debate", params={"ticker": "aapl"}).text)
    assert [e["event"] for e in events] == ["start", "research", "review", "done", "saved"]
    rid = events[-1]["data"]["run"]
    runs = client.get("/api/runs").json()["runs"]
    assert runs[0]["id"] == rid and runs[0]["ticker"] == "aapl" and runs[0]["mode"] == "web" and runs[0]["price"] == "$1.00"
    assert store.get_run(rid, config.DB_PATH)["turns"][0]["at"]      # arrival stamps recorded


def test_live_stream_caps_rounds():
    assert client.get("/api/debate", params={"ticker": "NVDA", "max_rounds": 40}).status_code == 422


# ---------------------------------------------------------------- operator gate
def test_console_gate_with_password(monkeypatch):
    monkeypatch.setattr(config, "CONSOLE_PASSWORD", "s3cret")
    for path in ("/console", "/console/library", "/api/runs", "/api/debate?ticker=NVDA"):
        r = client.get(path)
        assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic"), path
        assert client.get(path, auth=("anyone", "wrong")).status_code == 401, path
    assert client.get("/console", auth=("op", "s3cret")).status_code == 200
    assert client.get("/api/runs", auth=("op", "s3cret")).status_code == 200
    # the public side never asks
    for path in ("/", "/api/health", "/api/featured", "/api/article", "/api/runs/1", "/static/debate.js"):
        assert client.get(path).status_code == 200, path
    assert client.get("/api/replay", params={"run": 1, "speed": 0}).status_code == 200
    assert client.get("/api/health").json()["console_protected"] is True


def test_console_fails_closed_in_production_without_password(monkeypatch):
    monkeypatch.setattr(config, "IN_PRODUCTION", True)
    assert client.get("/console").status_code == 503
    assert client.get("/api/debate", params={"ticker": "NVDA"}).status_code == 503
    assert client.get("/").status_code == 200
    h = client.get("/api/health").json()
    assert h["console_locked"] is True and h["ok"] is True


def test_health_does_not_touch_the_database(monkeypatch):
    # a dead database must not fail Railway's liveness probe
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://u:p@db.invalid:5432/x")
    h = client.get("/api/health").json()
    assert h["ok"] is True and h["db_backend"] == "postgres" and "db.invalid" in h["db_path"]
