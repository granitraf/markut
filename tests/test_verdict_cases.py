"""The governed verdict states both sides (2026-10-11, after run #29 TSM).

Run #29's verdict told the strong quarter and "much of this is already in
the price"; the concrete downsides (78% top-ten customers, a margin guide-down,
a six-fold inventory build) appeared only as open questions. The verdict now
carries "The case for" and "The case against" (filled from the judge's own
strongest points if the model leaves them out), the analyst number is
explained where it appears, and the five question rulings are tallied.
Offline; TSM-run-#29-shaped, AAPL-shaped and synthetic cases."""
import json

from markut.agents import llm, nodes, prompts
from markut.evidence.consensus import compare_lean, explain_consensus


def check(label, condition):
    assert condition, label


BULL = "2Q26 net sales grew 36.0% and operating income 65.4%; 3Q26 revenue is guided to US$44.6-45.8B."
BEAR = "The top ten customers reached 78% of 2025 revenue, operating margin is guided down to 56-58%, and the H1 inventory outflow is six times last year's."
VERDICT_NO_CASES = ("TSM is executing strongly: sales grew 36.0% last quarter. The stock trades near 20.65x next-year consensus.\n\n"
                    "**What moves the stock**\n- Whether the margin step-down is temporary.\n\n"
                    "**What would change this view**\n- More bullish if margins beat.\n- More bearish if margins miss.\n\n"
                    "**Next catalyst**\n- Earnings on 2026-10-15.\n\nResearch, not investment advice.")


def _reply(verdict):
    return json.dumps({"questions": [{"id": "Q1", "answer": "a", "stronger_side": "bull", "confidence": "medium", "unsupported_claims": []},
                                     {"id": "Q2", "answer": "a", "stronger_side": "neither", "confidence": "medium", "unsupported_claims": []},
                                     {"id": "Q3", "answer": "a", "stronger_side": "neither", "confidence": "low", "unsupported_claims": []},
                                     {"id": "Q4", "answer": "a", "stronger_side": "bear", "confidence": "medium", "unsupported_claims": []},
                                     {"id": "Q5", "answer": "a", "stronger_side": "bear", "confidence": "low", "unsupported_claims": []}],
                       "bull_strongest": BULL, "bear_strongest": BEAR, "unsupported_claims": [], "reasoning": "r", "verdict": verdict,
                       "converged": True, "planner_coverage": "adequate", "lean": "neutral", "lean_confidence": "low", "lean_reason": "offset"})


def _state():
    return {"ticker": "TSM", "round": 1, "max_rounds": 2, "bull_case": "b", "bear_case": "r", "bull_history": ["b"], "bear_history": ["r"],
            "evidence": "[FILINGS]\nSources: 6-K\n- Revenue: 1,270,381 million  [source: T1]", "coverage": {"Q1": 2, "Q2": 1, "Q3": 1, "Q4": 1, "Q5": 1}}


def test_the_verdict_states_the_case_for_and_against(monkeypatch):
    p = prompts.JUDGE_SYSTEM_PROMPT
    check("the judge is asked for both cases", "**The case for**" in p and "**The case against**" in p)
    monkeypatch.setattr(llm, "call_claude", lambda *a, **k: _reply(VERDICT_NO_CASES))
    v = nodes.judge_node(_state())["verdict"]
    check(f"TSM run #29: a verdict without them gets both, from the judge's own strongest points ({v[:400]!r})",
          "**The case for**\n- " + BULL in v and "**The case against**\n- " + BEAR in v)
    check("…placed after the opening paragraph, before what moves the stock",
          v.index("TSM is executing") < v.index("**The case for**") < v.index("**The case against**") < v.index("**What moves the stock**"))
    with_cases = VERDICT_NO_CASES.replace("**What moves the stock**", "**The case for**\n- Growth.\n\n**The case against**\n- Concentration.\n\n**What moves the stock**")
    monkeypatch.setattr(llm, "call_claude", lambda *a, **k: _reply(with_cases))
    v2 = nodes.judge_node(_state())["verdict"]
    check("a verdict that already has them is left alone", v2.count("**The case for**") == 1 and "- Growth." in v2 and BULL not in v2)


def test_the_analyst_number_is_explained():
    for mean, between in ((1.38, "between strong buy and buy"), (2.2, "between buy and hold"), (3.7, "between hold and sell")):
        e = explain_consensus({"mean": mean, "n": 20})
        check(f"{mean}: says where it sits and what the scale is ({e!r})", between in e and "1 = strong buy" in e and "5 = strong sell" in e)
    check("an exact stop names it", "= buy" in explain_consensus({"mean": 2.0, "n": 9}) or "exactly buy" in explain_consensus({"mean": 2.0, "n": 9}))
    s = compare_lean("neutral", {"mean": 1.38, "label": "strong buy", "n": 20})
    check(f"TSM run #29: the comparison sentence says where 1.38 sits ({s!r})", "between strong buy and buy" in s and "20 analysts" in s)


def test_the_five_rulings_are_tallied_on_the_page_and_in_the_pdf(monkeypatch):
    from markut.web import events as ev
    decision = json.loads(_reply(VERDICT_NO_CASES))
    e = ev.event_for("review", {"verdict": "v", "review_report": {}}, {"verdict": "v", "evidence": "", "judge_decision": decision}, {})
    r = e["data"].get("rulings") or {}
    check(f"TSM run #29: bull 1 · bear 2 · neither 2 ({r})", r.get("tally") == {"bull": 1, "bear": 2, "neither": 2}
          and [q["id"] for q in r.get("questions", [])] == ["Q1", "Q2", "Q3", "Q4", "Q5"])
    legacy = ev.event_for("review", {"verdict": "v", "review_report": {}}, {"verdict": "v", "evidence": "", "judge_decision": {}}, {})
    check("an older decision without rulings -> none shown", not legacy["data"].get("rulings"))
    import pypdfium2 as pdfium
    from markut.report import build_report
    run = {"id": 1, "ticker": "TSM", "started_at": "2026-10-11", "events": [
        {"event": "start", "data": {"ticker": "TSM", "max_rounds": 2}},
        {"event": "review", "data": {"verdict": "v. Research, not investment advice.", "stats": {}, "rulings": r,
                                     "lean": {"lean": "neutral", "confidence": "low", "reason": "offset"},
                                     "consensus": {"mean": 1.38, "label": "strong buy", "n": 20}, "comparison": "c"}}]}
    doc = pdfium.PdfDocument(build_report(run))
    import re
    t = re.sub(r"\s+", " ", " ".join(doc[i].get_textpage().get_text_range() for i in range(len(doc))))   # PDF text wraps lines
    check("the PDF shows the tally", "Question rulings: bull 1 · bear 2 · neither 2" in t)
    check("…and explains the analyst number", "1 = strong buy" in t)
