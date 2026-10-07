"""Web layer: the debate graph served over HTTP.

Nothing in markut.agents / markut.guardrails / markut.evidence changes for
this — the web layer is a SECOND entry point beside run.py. It streams the
graph's per-node updates to a browser as Server-Sent Events (events.py),
replays stored runs for a zero-cost demo (replay.py), and serves one static
page (static/index.html). Start it with: python -m markut.web
"""
