"""Replay: stream a STORED run's events down the same SSE wire as a live one.

WHY: every live debate costs 8-12 paid model calls. A visitor (a grader, a
portfolio reviewer) should still be able to WATCH a debate without spending
anyone's money. Runs live in the SQLite store (markut.store) — every event
of every run, in order — so a replay is a faithful re-showing, never a
reconstruction. The page cannot tell the two apart; only the "mode" label
on the start event says which it is.

The bundled course run (fixtures/*.json) is imported into the store on
first start by store.bootstrap(), so the site demos with no API key at all.
"""
import os

BUNDLED_DIR = os.path.join(os.path.dirname(__file__), "fixtures")

# Replay pacing (seconds before each event) — long enough that a card visibly
# "arrives", short enough that a whole demo plays in ~15s. The judge pauses
# longest because that is where a viewer reads.
DELAYS = {"start": 0.0, "research": 1.0, "bull": 1.6, "bear": 1.6, "judge": 1.4,
          "route": 0.6, "news_verify": 1.0, "review": 1.4, "done": 0.4, "error": 0.0}


def delay_for(event: dict) -> float:
    return DELAYS.get(event.get("event"), 0.8)


def iter_replay(run: dict):
    """Yield a stored run's events (a store.get_run dict), tagging the start
    event with replay meta so the page can label the mode and show when the
    run really happened. Pacing is the CALLER's job (the async server
    sleeps; tests don't)."""
    for event in run.get("events", []):
        if event.get("event") == "start":
            data = dict(event.get("data", {}))
            data.update({"mode": "replay", "run_id": run.get("id"), "run_mode": run.get("mode", ""),
                         "recorded_at": run.get("started_at", ""), "note": run.get("note", "") or ""})
            yield {"event": "start", "data": data}
        else:
            yield event
