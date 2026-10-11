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

    start -> profiler -> planner -> research -> coverage [-> research -> coverage]
          -> (bull -> bear -> judge -> route)* -> news_verify -> review -> done

Every event's data carries a turn_id (unique within the run — the page
dedupes on it) and usage (the model tokens that node spent); the done event
adds by_node totals, the per-node token log.
"""
from markut import config
from markut.agents import llm
from markut.agents.graph import build_graph, coverage_decision, should_continue
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
        "profile": {}, "profile_gaps": [], "profiled_text": "", "filing_fingerprint": "",
        "profile_sources": [], "profile_cached": False, "plan_cached": False,
        "plan": {}, "market_evidence": "", "research_pass": 0, "coverage": {}, "uncovered": [],
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
    # run #26 (P2, item 14): the counts describe the FINAL verdict a reader sees
    # (the governor's own inline tags removed first); the pre-review draft is
    # kept as pre_claims — "7 claims" described a draft, not the 16-figure verdict
    from markut.guardrails.review import _TAG_RE
    final_clean = _TAG_RE.sub("", final_verdict or "")
    claims = extract_numeric_claims(final_clean)
    # run #28: traced against DATA only — never the planner's own question text
    from markut.guardrails.tracer import claim_contexts, rounded_source, tracing_evidence
    data = tracing_evidence(evidence or "")
    sentences = {c["claim"]: c["sentence"] for c in claim_contexts(final_clean)}
    # P1: an honest rounding under the same label is traced, not flagged
    flagged = [c for c in claims if trace_claim(c, data) == "FLAGGED"
               and not rounded_source(c, sentences.get(c, final_clean), data)]
    pre = extract_numeric_claims(pre_verdict or "")
    resolutions = report.get("resolutions", []) or []
    return {
        "claims": len(claims),
        "cited": len(claims) - len(flagged),
        "flagged": len(flagged),
        "pre_claims": len(pre),
        "derived": sum(1 for r in resolutions if r.get("qualified") and r.get("resolution") == "DERIVED"),
        "labeled": sum(1 for r in resolutions if r.get("qualified") and r.get("resolution") == "LABELED"),
        "annotated": (final_verdict or "").count("[UNGROUNDED"),
        "status": report.get("final_status", "unknown"),
        "sources_unavailable": report.get("sources_unavailable", 0),
    }


def usage() -> dict:
    # input = uncached; cached reads/writes reported beside it so a cost line
    # can say "N input tokens (M served from cache)"
    return {"calls": llm.TOKENS["calls"], "input": llm.TOKENS["input"], "output": llm.TOKENS["output"],
            "cache_write": llm.TOKENS.get("cache_write", 0), "cache_read": llm.TOKENS.get("cache_read", 0)}


def event_for(node: str, update: dict, before: dict, after: dict) -> dict:
    # One event per graph node. Payloads carry what the page shows, nothing
    # the page would have to re-derive. Round numbers: bull/bear run BEFORE
    # the judge increments state["round"], so their round is before+1; the
    # judge's update carries the new round itself.
    if node == "profiler":
        return {"event": "profiler", "data": {"profile": update.get("profile", {}) or {},
                                              "gaps": update.get("profile_gaps", []) or [],
                                              "cached": bool(update.get("profile_cached", False)),
                                              "fingerprint": update.get("filing_fingerprint", ""),
                                              "sources": update.get("profile_sources", []) or []}}
    if node == "planner":
        return {"event": "planner", "data": {"plan": update.get("plan", {}) or {},
                                             "cached": bool(update.get("plan_cached", False))}}
    if node == "research":
        return {"event": "research", "data": {"evidence": update.get("evidence", ""),
                                              "coverage": update.get("coverage", {}) or {},
                                              "pass": update.get("research_pass", 1),
                                              "uncovered": update.get("uncovered", []) or []}}
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
            "questions": decision.get("questions", []) or [],
            "planner_coverage": decision.get("planner_coverage", ""),
            "truncated": bool(decision.get("truncated", False)),
        }}
    if node == "news_verify":
        verification = update.get("claim_verification", {}) or {}
        return {"event": "news_verify", "data": {
            "claim_reviews": verification.get("claim_reviews", []) or [],
            "reasoning": verification.get("reasoning", ""),
            "verdict_changed": bool(verification.get("verdict_changed", False)),
            "leads": update.get("targeted_news_evidence", "") or "",
            # P1 (run #27): the filing passages the claim review read were never
            # recorded, so the report could not show them
            "addendum": _addendum_of(update.get("evidence") or ""),
        }}
    if node == "review":
        report = update.get("review_report", {}) or {}
        # 2026-10-11: the judge's stance rides with the governed verdict it
        # opens; a decision without one (legacy / salvaged) stays None
        decision = (before or {}).get("judge_decision") or {}
        lean = ({"lean": decision["lean"], "confidence": decision.get("lean_confidence"), "reason": decision.get("lean_reason", "")}
                if decision.get("lean") else None)
        if not lean and decision.get("lean_withheld"):
            # run #28: no lean, said plainly — never a blank
            lean = {"lean": None, "confidence": None, "reason": "", "withheld": decision["lean_withheld"]}
        return {"event": "review", "data": {
            "lean": lean,
            "verdict": update.get("verdict", ""),
            "report": report,
            "stats": review_stats(before["verdict"], before["evidence"],
                                  update.get("verdict", ""), report),
            "budget_exceeded": bool(update.get("budget_exceeded", False)),
        }}
    # a node this layer does not know yet (future graph growth): pass the
    # raw update through rather than drop it on the floor
    return {"event": node, "data": {"update": update}}


def _usage_snapshot() -> dict:
    return {k: llm.TOKENS.get(k, 0) for k in ("calls", "input", "output", "cache_write", "cache_read")}


def _usage_delta(before: dict, after: dict) -> dict:
    return {k: after.get(k, 0) - before.get(k, 0) for k in after}


def _addendum_of(evidence: str) -> str:
    from markut.agents.nodes import split_addendum
    return split_addendum(evidence)[1].strip() if evidence else ""


def filing_history_forms(cik: str) -> list:
    """The forms in the company's recent SEC filing history (one free request)."""
    from markut.evidence.edgar import get_filing_index
    return list(get_filing_index(cik).get("form") or [])


def sec_filer_refusal(ticker: str) -> str:
    """'' when Markut can read this company's filings; otherwise the plain
    reason it will not run (no model is called either way)."""
    from markut.evidence import filers
    try:
        cik = ticker_to_cik(ticker)
    except ValueError:
        return (f"{ticker} is not an SEC filer: it is not in the SEC's company list, so there are no filings to read. "
                "Markut needs a 10-K, 20-F or 40-F filer. Nothing was run and no model was called.")
    try:
        forms = filing_history_forms(cik)
    except Exception as e:
        return f"{ticker}: the SEC filing history could not be read ({type(e).__name__}). Nothing was run and no model was called."
    if filers.filer_type(forms) == "no_periodic_reports":
        return (f"{ticker} {filers.slot_reason('annual', 'no_periodic_reports', forms)[4:]}. Markut needs a 10-K, 20-F or 40-F "
                "filer. Nothing was run and no model was called.")
    return ""


def stream_debate(ticker: str, max_rounds: int = None, app=None, check_sec: bool = True, consensus: dict = None):
    """Generator: validate (input guardrail layers 1-2) -> build graph ->
    stream -> yield one event per node. app= is the test seam (anything
    with .stream(state, stream_mode="updates")); check_sec=False skips the
    network CIK lookup so wiring is provable offline. consensus= supplies the
    analyst consensus (tests); otherwise it is read from the provider after
    the debate, when check_sec allows network — never inside the graph, so no
    agent sees it."""
    if max_rounds is None:
        max_rounds = config.DEFAULT_MAX_ROUNDS
    if max_rounds < config.MIN_ROUNDS:
        print(f"ROUNDS: {max_rounds} requested — every debate runs at least {config.MIN_ROUNDS}")
        max_rounds = config.MIN_ROUNDS
    try:
        ticker = validate_ticker(ticker)      # layer 1: shape
    except ValueError as e:
        yield {"event": "error", "data": {"stage": "validation", "message": str(e)}}
        return
    if check_sec:
        # layer 2: an SEC filer with periodic reports (run #28 — refuse, before
        # any paid call, a company Markut has no filings to read for)
        refusal = sec_filer_refusal(ticker)
        if refusal:
            yield {"event": "error", "data": {"stage": "validation", "message": refusal}}
            return

    llm.reset()  # USAGE reflects only this run, exactly like run.py
    if app is None:
        app = build_graph()
    state = initial_state(ticker, max_rounds)
    seq = 0
    by_node = {}

    def stamp(event: dict, used: dict = None) -> dict:
        # turn_id: unique within the run, stored with the event, the page's dedupe key
        nonlocal seq
        seq += 1
        event["data"]["turn_id"] = f"{seq:02d}-{event['event']}"
        if used is not None:
            event["data"]["usage"] = used
        return event

    yield stamp({"event": "start", "data": {"ticker": ticker, "max_rounds": max_rounds,
                                            "model": config.MODEL_NAME,
                                            "token_budget": config.TOKEN_BUDGET}})
    try:
        before_usage = _usage_snapshot()
        for chunk in app.stream(state, stream_mode="updates"):
            # one chunk per finished node: {node_name: update_dict}
            for node, update in chunk.items():
                now = _usage_snapshot()
                used = _usage_delta(before_usage, now)
                before_usage = now
                tot = by_node.setdefault(node, {k: 0 for k in used})
                for k, v in used.items():
                    tot[k] += v
                before = state
                state = apply_update(state, update or {})
                event = event_for(node, update or {}, before, state)
                if node == "review":
                    if consensus is None and check_sec:
                        from markut.evidence.consensus import fetch_analyst_consensus
                        consensus = fetch_analyst_consensus(ticker)
                    from markut.evidence.consensus import compare_lean
                    lean = event["data"].get("lean") or {}
                    event["data"]["consensus"] = consensus
                    event["data"]["comparison"] = compare_lean(lean.get("lean"), consensus, withheld=bool(lean.get("withheld")))
                yield stamp(event, used)
                if node == "research":
                    # mirror of coverage_route on the same state: the gate's ruling, reported
                    d = coverage_decision(state)
                    yield stamp({"event": "coverage", "data": d}, _usage_delta(now, now))
                if node == "judge":
                    # the graph's own conditional edge decides; we only report
                    # it. route_reason mirrors should_continue's priority order
                    # on the same state/TOKENS/budget — calling should_continue
                    # itself here printed every routing line twice.
                    reason = route_reason(state)
                    yield stamp({"event": "route", "data": {
                        "round": state["round"],
                        "decision": "continue" if reason == "looping back for rebuttal" else "done",
                        "reason": reason}}, _usage_delta(now, now))
    except Exception as e:  # fail loudly to the page, never hang the stream
        yield stamp({"event": "error", "data": {"stage": "debate", "message": f"{type(e).__name__}: {e}",
                                                "usage": usage(), "by_node": by_node}})
        return
    print_token_log(by_node)
    yield stamp({"event": "done", "data": {"rounds": state["round"], "converged": state["converged"],
                                           "budget_exceeded": state.get("budget_exceeded", False),
                                           "usage": usage(), "by_node": by_node}})


def print_token_log(by_node: dict):
    # the per-node token log: where every input token went
    if not by_node:
        return
    print("TOKENS BY NODE:  node          calls    input  cache_w  cache_r   output")
    for node, u in by_node.items():
        print(f"                 {node:<12} {u.get('calls', 0):>6} {u.get('input', 0):>8,} {u.get('cache_write', 0):>8,} "
              f"{u.get('cache_read', 0):>8,} {u.get('output', 0):>8,}")
