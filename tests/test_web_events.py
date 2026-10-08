"""Web event stream, offline: (a) the pure helpers, (b) stream_debate over a
scripted fake app, (c) stream_debate over the REAL compiled graph with the
node functions patched — proving the stream_mode="updates" chunk shape and
the event order end to end without network or model calls."""
import json

from markut import config
from markut.agents import graph as graph_mod
from markut.agents import llm
from markut.web import events as ev

EVIDENCE = "Current price: $195.55 [source: yfinance]. DCF implies +25.2% upside."


def test_apply_update_mirrors_reducers():
    s = ev.initial_state("NVDA", 2)
    s = ev.apply_update(s, {"bull_case": "a", "bull_history": ["a"]})
    s = ev.apply_update(s, {"bull_case": "b", "bull_history": ["b"], "round": 1})
    assert s["bull_history"] == ["a", "b"] and s["bull_case"] == "b" and s["round"] == 1


def test_route_reason_priority(monkeypatch):
    saved = dict(llm.TOKENS)
    try:
        llm.TOKENS.update({"input": 0, "output": 0})
        base = {"converged": False, "round": 1, "max_rounds": 2}
        assert ev.route_reason(base) == "looping back for rebuttal"
        assert ev.route_reason({**base, "round": 2}).startswith("max rounds (2)")
        assert ev.route_reason({**base, "converged": True}).startswith("judge ruled")
        llm.TOKENS.update({"input": config.TOKEN_BUDGET, "output": 1})
        assert ev.route_reason({**base, "converged": True}).startswith("token budget exceeded")
    finally:
        llm.TOKENS.update(saved)


def test_review_stats_recomputes_the_stats_line():
    pre = "Bears see 30-40% downside from $195.55."
    final = "Bears see 30-40% [UNGROUNDED — no evidence anchor] downside from $195.55."
    report = {"final_status": "annotated", "resolutions": [
        {"resolution": "DERIVED", "qualified": True}, {"resolution": "LABELED", "qualified": False}]}
    s = ev.review_stats(pre, EVIDENCE, final, report)
    assert s == {"claims": 2, "cited": 1, "flagged": 1, "derived": 1, "labeled": 0,
                 "annotated": 1, "status": "annotated", "sources_unavailable": 0}


class _FakeApp:
    def __init__(self, chunks):
        self.chunks = chunks
    def stream(self, state, stream_mode):
        assert stream_mode == "updates"
        yield from self.chunks


def test_stream_debate_rejects_bad_ticker_before_any_work():
    events = list(ev.stream_debate("'; drop", app=_FakeApp([]), check_sec=False))
    assert [e["event"] for e in events] == ["error"]
    assert events[0]["data"]["stage"] == "validation"


def test_stream_debate_event_order_and_payloads():
    chunks = [
        {"research": {"evidence": EVIDENCE}},
        {"bull": {"bull_case": "up", "bull_history": ["up"]}},
        {"bear": {"bear_case": "down", "bear_history": ["down"]}},
        {"judge": {"verdict": "Bears see 30-40% downside.", "converged": True, "round": 1,
                   "judge_decision": {"bull_strongest": "x", "bear_strongest": "y",
                                      "unsupported_claims": ["30-40%"], "reasoning": "r",
                                      "verdict": "Bears see 30-40% downside.", "converged": True}}},
        {"news_verify": {"news_checked": True, "targeted_news_evidence": "",
                         "claim_verification": {"claim_reviews": [], "reasoning": "none"}}},
        {"review": {"verdict": "Bears see 30-40% [UNGROUNDED — no evidence anchor] downside. "
                               "This is not financial advice.",
                    "review_report": {"final_status": "annotated", "resolutions": []},
                    "budget_exceeded": False}},
    ]
    saved = dict(llm.TOKENS)
    try:
        events = list(ev.stream_debate("nvda", max_rounds=2, app=_FakeApp(chunks), check_sec=False))
    finally:
        llm.TOKENS.update(saved)
    names = [e["event"] for e in events]
    assert names == ["start", "research", "bull", "bear", "judge", "route",
                     "news_verify", "review", "done"]
    by = {e["event"]: e["data"] for e in events}
    assert by["start"]["ticker"] == "NVDA"                  # normalized by validate_ticker
    assert by["bull"] == {"round": 1, "text": "up"}
    assert by["judge"]["round"] == 1 and by["judge"]["unsupported_claims"] == ["30-40%"]
    assert by["route"] == {"round": 1, "decision": "done", "reason": "judge ruled the debate converged"}
    assert by["review"]["stats"]["flagged"] == 1 and by["review"]["stats"]["annotated"] == 1
    assert by["done"]["converged"] is True and by["done"]["usage"]["calls"] == 0
    json.dumps(events)  # every payload must be JSON-serializable (SSE + record file)


def test_stream_debate_over_real_graph_with_patched_nodes(monkeypatch):
    # graph.py binds node functions by NAME at build time, so patching the
    # graph module's attributes swaps the agents while keeping LangGraph's
    # real routing, reducers and update-chunk shape in the loop.
    monkeypatch.setattr(graph_mod, "research_node", lambda s: {"evidence": EVIDENCE})
    monkeypatch.setattr(graph_mod, "bull_node", lambda s: {"bull_case": f"bull{s['round']+1}", "bull_history": [f"bull{s['round']+1}"]})
    monkeypatch.setattr(graph_mod, "bear_node", lambda s: {"bear_case": f"bear{s['round']+1}", "bear_history": [f"bear{s['round']+1}"]})
    def fake_judge(s):
        return {"verdict": "Price is $195.55. Not financial advice.", "converged": False,
                "round": s["round"] + 1, "judge_decision": {"unsupported_claims": []}}
    monkeypatch.setattr(graph_mod, "judge_node", fake_judge)
    monkeypatch.setattr(graph_mod, "news_verify_node",
                        lambda s: {"news_checked": True, "targeted_news_evidence": "", "claim_verification": {}})
    # the REAL review_node runs: clean verdict (cited number + disclaimer) -> no model call
    saved = dict(llm.TOKENS)
    try:
        events = list(ev.stream_debate("NVDA", max_rounds=2, check_sec=False))
    finally:
        llm.TOKENS.update(saved)
    names = [e["event"] for e in events]
    assert names == ["start", "research", "bull", "bear", "judge", "route",
                     "bull", "bear", "judge", "route", "news_verify", "review", "done"]
    routes = [e["data"] for e in events if e["event"] == "route"]
    assert routes[0]["decision"] == "continue" and routes[1]["decision"] == "done"
    assert routes[1]["reason"] == "max rounds (2) reached"
    bulls = [e["data"] for e in events if e["event"] == "bull"]
    assert [b["round"] for b in bulls] == [1, 2] and bulls[1]["text"] == "bull2"
    review = [e for e in events if e["event"] == "review"][0]["data"]
    assert review["stats"]["status"] == "clean" and review["stats"]["cited"] == 1
    assert events[-1]["data"]["rounds"] == 2
