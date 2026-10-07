"""Debate -> event stream (the contract the page renders).

WHY events instead of one big invoke(): a full debate takes a minute or
more and costs 8-12 model calls. run.py uses app.invoke(), which only
returns when EVERYTHING is finished — a browser staring at that is a blank
screen. LangGraph's app.stream(stream_mode="updates") hands back each
node's state update the moment that node finishes, so the page can grow a
card per agent turn exactly as the terminal grows a log line.

Every event is a plain dict {"event": <name>, "data": {...}} — JSON-safe,
so the SAME shape is (a) written down the SSE wire, (b) saved as a run
record, and (c) replayed later for free. The event names are the node
names of the graph plus start/route/done/error:

    start -> research -> (bull -> bear -> judge -> route)* -> news_verify
          -> review -> done
"""
from markut import config
from markut.agents import llm
from markut.agents.graph import build_graph, should_continue
from markut.evidence.edgar import ticker_to_cik
from markut.guardrails.tracer import extract_numeric_claims, trace_claim
from markut.guardrails.validate import validate_ticker

# the two accumulating channels of DebateState (Annotated[list, operator.add]);
# apply_update must mirror that reducer so the local mirror of the state
# stays byte-identical to what the graph holds
_APPEND_KEYS = ("bull_history", "bear_history")


def initial_state(ticker: str, max_rounds: int) -> dict:
    # the same seed dict run.py and the notebook run cell build — every key
    # present so the first node has a defined slot to write into
    return {
        "ticker": ticker, "evidence": "", "bull_case": "", "bear_case": "",
        "bull_history": [], "bear_history": [], "verdict": "", "converged": False,
        "round": 0, "max_rounds": max_rounds, "news_evidence": "",
        "targeted_news_evidence": "", "news_checked": False,
        "claim_verification": {}, "judge_decision": {},
        "budget_exceeded": False, "review_report": {},
    }


def apply_update(state: dict, update: dict) -> dict:
    # WHY a local mirror at all: stream_mode="updates" gives us only each
    # node's DELTA. Events like "bull round 2" need the round number, and
    # the review event needs the PRE-review verdict to recompute stats, so
    # we fold every delta into a copy exactly as LangGraph does (lists
    # append, everything else overwrites).
    merged = dict(state)
    for key, value in update.items():
        if key in _APPEND_KEYS:
            merged[key] = list(merged.get(key, [])) + list(value)
        else:
            merged[key] = value
    return merged


def route_reason(state: dict) -> str:
    # Human label for the routing decision, in the SAME priority order
    # should_continue checks (budget first, then convergence, then the round
    # cap). The DECISION itself still comes from should_continue — this only
    # explains it.
    spent = llm.TOKENS["input"] + llm.TOKENS["output"]
    if spent >= config.TOKEN_BUDGET:
        return f"token budget exceeded ({spent:,} >= {config.TOKEN_BUDGET:,} tokens)"
    if state["converged"]:
        return "judge ruled the debate converged"
    if state["round"] >= state["max_rounds"]:
        return f"max rounds ({state['max_rounds']}) reached"
    return "looping back for rebuttal"


def review_stats(pre_verdict: str, evidence: str, final_verdict: str, report: dict) -> dict:
    # review_node PRINTS its REVIEW STATS line but returns only the report,
    # so the page recomputes the same numbers with the same deterministic
    # tools (extract + trace on the verdict the node was handed). derived/
    # labeled count QUALIFIED resolutions only — the one judgment the node
    # recorded in the report — and annotated counts tag OCCURRENCES in the
    # final text, matching the node's D2-era definition.
    claims = extract_numeric_claims(pre_verdict or "")
    flagged = [c for c in claims if trace_claim(c, evidence or "") == "FLAGGED"]
    resolutions = report.get("resolutions", []) or []
    return {
        "claims": len(claims),
        "cited": len(claims) - len(flagged),
        "flagged": len(flagged),
        "derived": sum(1 for r in resolutions if r.get("qualified") and r.get("resolution") == "DERIVED"),
        "labeled": sum(1 for r in resolutions if r.get("qualified") and r.get("resolution") == "LABELED"),
        "annotated": (final_verdict or "").count("[UNGROUNDED"),
        "status": report.get("final_status", "unknown"),
    }


def usage() -> dict:
    return {"calls": llm.TOKENS["calls"], "input": llm.TOKENS["input"],
            "output": llm.TOKENS["output"]}


def event_for(node: str, update: dict, before: dict, after: dict) -> dict:
    # One event per graph node. Payloads carry what the page shows, nothing
    # the page would have to re-derive. Round numbers: bull/bear run BEFORE
    # the judge increments state["round"], so their round is before+1; the
    # judge's update carries the new round itself.
    if node == "research":
        return {"event": "research", "data": {"evidence": update.get("evidence", "")}}
    if node in ("bull", "bear"):
        return {"event": node, "data": {"round": before["round"] + 1,
                                        "text": update.get(f"{node}_case", "")}}
    if node == "judge":
        decision = update.get("judge_decision", {}) or {}
        return {"event": "judge", "data": {
            "round": after["round"],
            "converged": bool(update.get("converged", False)),
            "verdict": update.get("verdict", ""),
            "bull_strongest": decision.get("bull_strongest", ""),
            "bear_strongest": decision.get("bear_strongest", ""),
            "unsupported_claims": decision.get("unsupported_claims", []) or [],
            "reasoning": decision.get("reasoning", ""),
        }}
    if node == "news_verify":
        verification = update.get("claim_verification", {}) or {}
        return {"event": "news_verify", "data": {
            "claim_reviews": verification.get("claim_reviews", []) or [],
            "reasoning": verification.get("reasoning", ""),
            "verdict_changed": bool(verification.get("verdict_changed", False)),
            "leads": update.get("targeted_news_evidence", "") or "",
        }}
    if node == "review":
        report = update.get("review_report", {}) or {}
        return {"event": "review", "data": {
            "verdict": update.get("verdict", ""),
            "report": report,
            "stats": review_stats(before["verdict"], before["evidence"],
                                  update.get("verdict", ""), report),
            "budget_exceeded": bool(update.get("budget_exceeded", False)),
        }}
    # a node this layer does not know yet (future graph growth): pass the
    # raw update through rather than drop it on the floor
    return {"event": node, "data": {"update": update}}


def stream_debate(ticker: str, max_rounds: int = None, app=None, check_sec: bool = True):
    """Generator: validate (input guardrail layers 1-2) -> build graph ->
    stream -> yield one event per node. app= is the test seam (anything
    with .stream(state, stream_mode="updates")); check_sec=False skips the
    network CIK lookup so wiring is provable offline."""
    if max_rounds is None:
        max_rounds = config.DEFAULT_MAX_ROUNDS
    try:
        ticker = validate_ticker(ticker)      # layer 1: shape
        if check_sec:
            ticker_to_cik(ticker)             # layer 2: SEC existence (free, cached)
    except ValueError as e:
        yield {"event": "error", "data": {"stage": "validation", "message": str(e)}}
        return

    llm.reset()  # USAGE reflects only this run, exactly like run.py
    if app is None:
        app = build_graph()
    state = initial_state(ticker, max_rounds)
    yield {"event": "start", "data": {"ticker": ticker, "max_rounds": max_rounds,
                                      "model": config.MODEL_NAME,
                                      "token_budget": config.TOKEN_BUDGET}}
    try:
        for chunk in app.stream(state, stream_mode="updates"):
            # one chunk per finished node: {node_name: update_dict}
            for node, update in chunk.items():
                before = state
                state = apply_update(state, update or {})
                yield event_for(node, update or {}, before, state)
                if node == "judge":
                    # the graph's own conditional edge decides; we only report
                    # it. (Reading should_continue here mirrors what the graph
                    # read: same state, same TOKENS, same budget.)
                    yield {"event": "route", "data": {
                        "round": state["round"],
                        "decision": should_continue(state),
                        "reason": route_reason(state)}}
    except Exception as e:  # fail loudly to the page, never hang the stream
        yield {"event": "error", "data": {"stage": "debate", "message": f"{type(e).__name__}: {e}",
                                          "usage": usage()}}
        return
    yield {"event": "done", "data": {"rounds": state["round"], "converged": state["converged"],
                                     "budget_exceeded": state.get("budget_exceeded", False),
                                     "usage": usage()}}
