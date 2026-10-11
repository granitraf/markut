"""P0 (run #28, TSM): no lean when the evidence cannot support one.

With no filings read, the profiler spent a call saying it could not describe
the business and invented three metrics named "not disclosed", the planner
marked revenue and net income "not disclosed", every question was a coverage
gap — and the verdict still said "Lean: bullish". Rules checked here, offline,
on an AAPL-shaped run (full coverage), a TSM-run-#28-shaped run (no filings,
0 of 5 covered) and thin coverage (1 and 2 of 5)."""
import json

from markut.agents import gate, llm, nodes, profiler
from markut.agents.graph import coverage_decision


def check(label, condition):
    assert condition, label


NO_FILINGS = "[PROFILE]\n[KEY QUESTIONS]\n\n[FILINGS]\n[filings unavailable: no 20-F in the filing history searched]\n\n[QUOTE & VALUATION]\n- Price (current): $453.31  [source: yfinance/info]"
WITH_FILINGS = "[FILINGS]\nSources: 10-Q filed 2026-07-31\n- Revenue: 109,417 million  [source: T1]"


def _cov(n_covered, total=5):
    return {f"Q{i + 1}": (2 if i < n_covered else 0) for i in range(total)}


def test_the_gate_withholds_or_caps_the_lean():
    full = gate.lean_gate({"coverage": _cov(5), "evidence": WITH_FILINGS})
    check(f"AAPL-shaped: full coverage -> a lean is allowed, uncapped ({full})", full["allowed"] and not full["cap"])
    none = gate.lean_gate({"coverage": _cov(0), "evidence": NO_FILINGS})
    check(f"TSM run #28: no filings -> no lean, and the reason names the filings ({none})",
          not none["allowed"] and "filings" in none["reason"] and "20-F" in none["reason"])
    one = gate.lean_gate({"coverage": _cov(1), "evidence": WITH_FILINGS})
    check(f"1 of 5 covered -> no lean ({one})", not one["allowed"] and "1 of 5" in one["reason"])
    two = gate.lean_gate({"coverage": _cov(2), "evidence": WITH_FILINGS})
    check(f"2 of 5 covered -> a lean at low confidence at most ({two})", two["allowed"] and two["cap"] == "low")
    legacy = gate.lean_gate({"evidence": WITH_FILINGS})
    check("no coverage record (older state) -> the gate does not block", legacy["allowed"])


def _judge_reply(lean="bullish", conf="high"):
    return json.dumps({"questions": [], "bull_strongest": "b", "bear_strongest": "r", "unsupported_claims": [], "reasoning": "x",
                       "verdict": "v. Research, not investment advice.", "converged": True, "planner_coverage": "adequate",
                       "lean": lean, "lean_confidence": conf, "lean_reason": "because"})


def _judge_state(cov, evidence):
    return {"ticker": "TSM", "round": 1, "max_rounds": 2, "bull_case": "b", "bear_case": "r", "bull_history": ["b"],
            "bear_history": ["r"], "evidence": evidence, "coverage": cov}


def test_judge_applies_the_gate(monkeypatch):
    seen = []
    def fake(system, user, **k):
        seen.append(user)
        return _judge_reply()
    monkeypatch.setattr(llm, "call_claude", fake)
    out = nodes.judge_node(_judge_state(_cov(0), NO_FILINGS))["judge_decision"]
    check(f"TSM run #28: the judge's 'bullish' is withheld ({out.get('lean')}, {out.get('lean_withheld')})",
          out.get("lean") is None and "filings" in (out.get("lean_withheld") or ""))
    check("the judge is told before it writes the verdict", "LEAN GATE" in seen[-1] and "withheld" in seen[-1].lower())
    capped = nodes.judge_node(_judge_state(_cov(2), WITH_FILINGS))["judge_decision"]
    check(f"2 of 5: the lean stays, confidence capped at low ({capped.get('lean')}, {capped.get('lean_confidence')})",
          capped.get("lean") == "bullish" and capped.get("lean_confidence") == "low")
    full = nodes.judge_node(_judge_state(_cov(5), WITH_FILINGS))["judge_decision"]
    check("AAPL-shaped: untouched", full.get("lean") == "bullish" and full.get("lean_confidence") == "high")


def test_the_withheld_state_reaches_the_page_and_the_pdf():
    from markut.web import events as ev
    before = {"verdict": "v", "evidence": NO_FILINGS, "judge_decision": {"lean": None, "lean_withheld": "no SEC filings could be read (no 20-F)"}}
    e = ev.event_for("review", {"verdict": "v", "review_report": {}}, before, before)
    check(f"the review event says withheld, with the reason ({e['data'].get('lean')})",
          e["data"]["lean"] and e["data"]["lean"].get("lean") is None and "no SEC filings" in e["data"]["lean"].get("withheld", ""))
    from markut.evidence.consensus import compare_lean
    check("no lean -> the comparison says so instead of a blank",
          "no lean" in compare_lean(None, {"mean": 1.38, "label": "strong buy", "n": 20}, withheld=True).lower())
    import pypdfium2 as pdfium
    from markut.report import build_report
    run = {"id": 1, "ticker": "TSM", "started_at": "2026-10-11", "events": [
        {"event": "start", "data": {"ticker": "TSM", "max_rounds": 2}},
        {"event": "review", "data": {"verdict": "The evidence cannot support a lean.", "stats": {}, "lean": e["data"]["lean"],
                                     "consensus": {"mean": 1.38, "label": "strong buy", "n": 20}, "comparison": "x"}}]}
    doc = pdfium.PdfDocument(build_report(run))
    text = " ".join(doc[i].get_textpage().get_text_range() for i in range(len(doc)))
    check("the PDF shows 'No lean' and why", "No lean" in text and "no SEC filings" in text)


def test_no_filings_means_no_profiler_call(monkeypatch):
    from markut.evidence import sections as secs
    from markut import store
    monkeypatch.setattr(secs, "filing_set", lambda t: {"10-K": None, "10-Q": None, "8-K": None, "fingerprint": "-|-|-",
                                                        "reasons": {"annual": "no 20-F in the filing history searched"}})
    monkeypatch.setattr(secs, "profile_sections", lambda fs: [])
    monkeypatch.setattr(store, "cache_get", lambda k: None)
    def boom(*a, **k):
        raise AssertionError("no model call may be made without filing sections")
    monkeypatch.setattr(llm, "call_claude", boom)
    out = profiler.profiler_node({"ticker": "TSM"})
    check(f"TSM run #28: empty profile, the reason recorded, no paid call ({out.get('profile_gaps')})",
          out["profile"] == {} and any("20-F" in g for g in out["profile_gaps"]))


def test_no_second_research_pass_without_filings():
    d = coverage_decision({"coverage": _cov(0), "research_pass": 1, "evidence": NO_FILINGS})
    check(f"TSM run #28: nothing to find on a second pass -> straight to the debate ({d})", d["decision"] == "debate")
    d2 = coverage_decision({"coverage": _cov(1), "research_pass": 1, "evidence": WITH_FILINGS})
    check("with filings, a thin first pass still gets its second pass", d2["decision"] == "research")


def test_disclosure_is_unknown_when_nothing_was_read():
    prof = {"company_kpis": [{"name": "Revenue"}, {"name": "Net income"}], "segments": []}
    marked = profiler.mark_disclosure(prof, [], "")
    check(f"no filing records or text -> unknown, never 'not disclosed' ({marked['company_kpis']})",
          all(k["disclosed"] is None for k in marked["company_kpis"]) and marked["undisclosed_metrics"] == [])
    plan = {"key_questions": [{"id": "Q1", "question": "Is revenue growth sustainable?", "kpis": ["Revenue"]}]}
    fixed = profiler.enforce_disclosure(plan, marked, "")
    check(f"…and the planner's questions get no 'not disclosed' limitation ({fixed['key_questions'][0]['question']})",
          "not disclosed" not in fixed["key_questions"][0]["question"] and fixed["key_questions"][0]["kpis"] == ["Revenue"])
