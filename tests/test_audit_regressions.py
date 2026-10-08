"""Regression tests for the run #2 (AVGO, 2026-10-06) audit. Every case uses
the stored AVGO run as its fixture and runs offline: no network, no model
(the model wrapper is stubbed where a node is exercised)."""
import json
import os

import pytest

from markut import config
from markut.agents import llm, nodes
from markut.evidence import market
from markut.guardrails import tracer
from markut.guardrails.review import review_node, stamp_findings

_FIX = os.path.join(os.path.dirname(__file__), "fixtures", "avgo_run_2026-10-06.json")
AVGO = json.load(open(_FIX, encoding="utf-8"))["events"]
EVIDENCE = next(e for e in AVGO if e["event"] == "research")["data"]["evidence"]
VERDICT = next(e for e in AVGO if e["event"] == "review")["data"]["verdict"]
JUDGE1 = next(e for e in AVGO if e["event"] == "judge")["data"]
# the packet as the NEW market builder labels it (run #2 predates the relabel)
EVIDENCE_V2 = EVIDENCE.replace("Revenue growth (yoy): 85.50%",
                               "Revenue growth (MRQ YoY, most recent quarter vs year-ago quarter): 85.50%")


@pytest.fixture
def stub_llm(monkeypatch):
    """Replace call_claude with a scripted stub; records every call's kwargs."""
    calls = []
    def install(replies, stop_reasons=None):
        replies, stops = list(replies), list(stop_reasons or [])
        def fake(system_prompt, user_content, max_tokens=1000, schema=None):
            calls.append({"system": system_prompt, "user": user_content, "max_tokens": max_tokens, "schema": schema})
            llm.LAST_STOP_REASON = stops.pop(0) if stops else "end_turn"
            return replies.pop(0) if replies else "{}"
        monkeypatch.setattr(llm, "call_claude", fake)
        return calls
    yield install
    llm.LAST_STOP_REASON = "end_turn"


# ---------------------------------------------------------------- 1. labels
def test_market_lines_name_period_and_basis():
    info = {"trailingPE": 46.28, "forwardPE": 19.38, "trailingEps": 8.12, "forwardEps": 19.39,
            "revenueGrowth": 0.855, "earningsGrowth": 1.2, "currentPrice": 375.81}
    text = "\n".join(market.format_market_lines(info))
    assert "EPS (trailing TTM, GAAP): $8.12" in text
    assert "EPS (forward, consensus non-GAAP, next 12 months): $19.39" in text
    assert "P/E (forward, consensus non-GAAP EPS): 19.38" in text
    assert "Revenue growth (MRQ YoY, most recent quarter vs year-ago quarter): 85.50%" in text
    assert "Revenue growth (yoy)" not in text
    notes = "\n".join(market.basis_notes(info))
    assert "BASIS NOTE" in notes and "PERIOD NOTE" in notes


def test_fy_growth_and_consensus_lines():
    fy = market.fy_growth_lines([(2025, 63.89e9, 23.13e9), (2024, 51.57e9, 5.90e9)])
    assert fy[0].startswith("- Revenue growth (FY2025 vs FY2024, annual): +23.8")
    est = market.format_estimate_lines({"0y": {"avg": 11.57, "numberOfAnalysts": 28}, "+1y": {"avg": 19.39, "numberOfAnalysts": 30}}, 2026)
    assert "EPS consensus (FY2026, current FY, non-GAAP, 28 analysts): $11.57" in est[0]
    assert "EPS consensus (FY2027, next FY, non-GAAP, 30 analysts): $19.39" in est[1]
    assert market.format_estimate_lines({"0y": {"avg": float("nan")}}) == []


# ---------------------------------------------------------------- 2. label binding
def test_fy_growth_mislabel_is_caught():
    # the judge's own sentence from run #2: 85.5% is MRQ growth, asserted as FY2025
    sentence = "Broadcom's filings confirm sustained, realized revenue growth of 85.5% YoY to $63.89B in FY2025."
    assert JUDGE1["bull_strongest"].startswith(sentence[:60])
    found = tracer.find_mislabeled(sentence, EVIDENCE_V2)
    assert [f["claim"] for f in found] == ["85.5%"]
    assert found[0]["packet_period"] == "quarter" and found[0]["claim_period"] == "annual"
    # the same number stated as a quarterly rate, and an annual figure that IS annual, pass
    assert tracer.find_mislabeled("Revenue grew 85.5% in the most recent quarter.", EVIDENCE_V2) == []
    assert tracer.find_mislabeled("Revenue was $63.89B in FY2025.", EVIDENCE_V2) == []
    # basis: trailing GAAP EPS called non-GAAP
    assert tracer.find_mislabeled("Non-GAAP EPS of $8.12 was weak.",
                                  "- EPS (trailing TTM, GAAP): $8.12  [source: x]")[0]["reason"].endswith("not non-gaap")


def test_mislabeled_number_is_stamped_by_the_governor(stub_llm):
    # revision keeps the mislabel -> the governor stamps it, status annotated
    verdict = "Revenue grew 85.5% to $63.89B in FY2025. This is research, not investment advice."
    stub_llm(['{"verdict": "%s", "resolutions": []}' % verdict])
    out = review_node({"verdict": verdict, "evidence": EVIDENCE_V2})
    assert "85.5% [MISLABELED — 85.5% is quarter in the evidence" in out["verdict"]
    assert out["review_report"]["mislabeled"][0]["claim"] == "85.5%"
    assert out["review_report"]["final_status"] == "annotated"


# ---------------------------------------------------------------- 3. arithmetic
def test_recompute_derived_percentages():
    assert tracer.recompute_derived("41.4%", ["$531.31", "$375.81"]) == {"ok": True, "implied": 41.38, "formula": "($531.31 / $375.81 - 1)"}
    assert tracer.recompute_derived("41.3%", ["$531.31", "$375.81"])["ok"]       # rounding slack
    assert not tracer.recompute_derived("44%", ["$531.31", "$375.81"])["ok"]     # wrong arithmetic
    assert tracer.recompute_derived("1.96x", ["$12.83", "$6.53"])["ok"]
    assert tracer.recompute_derived("20-50%", ["$244.85", "$195.55", "$301.62"])["ok"]   # range endpoints


def test_41_4_is_not_footnoted_and_derived_bug_is_fixed(stub_llm):
    # BUG from run #2: resolution claim "41%" DERIVED with traced anchors, verdict says
    # "41.3%" -> was still tagged "no anchor". Now: covered within rounding.
    for shown in ("41.3%", "41.4%"):
        verdict = (f"The upside to the analyst mean target is {shown} (anchors: $531.31 vs $375.81). "
                   "This is research, not investment advice.")
        stub_llm([json.dumps({"verdict": verdict, "resolutions": [
            {"claim": "41%", "resolution": "DERIVED", "anchors": ["$531.31", "$375.81"]}]})])
        out = review_node({"verdict": verdict, "evidence": EVIDENCE})
        assert "[UNGROUNDED" not in out["verdict"], shown
        assert out["review_report"]["resolutions"][0]["qualified"] is True
        assert out["review_report"]["final_status"] == "revised"


def test_wrong_derived_arithmetic_is_tagged_miscomputed(stub_llm):
    verdict = "The upside to the mean target is 44% (anchors: $531.31 vs $375.81). This is research, not investment advice."
    stub_llm([json.dumps({"verdict": verdict, "resolutions": [
        {"claim": "44%", "resolution": "DERIVED", "anchors": ["$531.31", "$375.81"]}]})])
    out = review_node({"verdict": verdict, "evidence": EVIDENCE})
    assert "44% [MISCOMPUTED — anchors $531.31, $375.81 imply 41.38%]" in out["verdict"]
    assert out["review_report"]["miscomputed"][0]["implied"] == 41.38
    assert "[UNGROUNDED" not in out["verdict"]


def test_46_28_times_8_12_scenario_is_flagged():
    found = tracer.check_scenarios(VERDICT, EVIDENCE)
    assert len(found) == 1
    f = found[0]
    assert (f["multiple"], f["eps"], f["implied"], f["direction"]) == (46.28, 8.12, 375.79, "downside")
    assert "375.79" in f["reason"] and "claims downside" in f["reason"]
    # a scenario whose direction holds is not flagged
    assert tracer.check_scenarios("At 15x on $25 EPS the implied $375 is material downside from here.",
                                  "- Price (current): $450.00  [source: x]") == []


def test_scenario_contradiction_is_stamped_when_revision_keeps_it(stub_llm):
    stub_llm(['{"verdict": %s, "resolutions": []}' % json.dumps(VERDICT)])
    out = review_node({"verdict": VERDICT, "evidence": EVIDENCE})
    assert "[SCENARIO CHECK — 46.28x × $8.12 = $375.79" in out["verdict"]
    assert out["review_report"]["scenario_flags"][0]["implied"] == 375.79


def test_scenario_finding_is_handed_to_the_revision_prompt(stub_llm):
    calls = stub_llm(['{"verdict": %s, "resolutions": []}' % json.dumps(VERDICT)])
    review_node({"verdict": VERDICT, "evidence": EVIDENCE})
    assert "SCENARIO ARITHMETIC CHECK" in calls[0]["user"] and "46.28x × $8.12 = $375.79" in calls[0]["user"]
    assert calls[0]["schema"]["required"] == ["verdict", "resolutions"]


def test_stamp_findings_is_idempotent():
    text = "Revenue grew 85.5% in FY2025."
    mis = [{"claim": "85.5%", "reason": "r", "sentence": text}]
    once, n1 = stamp_findings(text, mis, [])
    twice, n2 = stamp_findings(once, mis, [])
    assert n1 == 1 and n2 == 0 and once == twice


# ---------------------------------------------------------------- 4. structured output + truncation
def test_judge_and_claim_review_request_schemas(stub_llm):
    calls = stub_llm([json.dumps({"bull_strongest": "b", "bear_strongest": "r", "unsupported_claims": ["c"],
                                  "reasoning": "x", "verdict": "v. Not financial advice.", "converged": False})])
    state = {"ticker": "AVGO", "evidence": EVIDENCE, "bull_case": "b", "bear_case": "r",
             "bull_history": ["b"], "bear_history": ["r"], "round": 0, "verdict": ""}
    nodes.judge_node(state)
    assert calls[0]["schema"]["required"] == ["bull_strongest", "bear_strongest", "unsupported_claims", "reasoning", "verdict", "converged"]


def test_claim_review_repairs_once_and_logs_raw(stub_llm, monkeypatch, capsys):
    monkeypatch.setattr(nodes, "get_related_leads", lambda claims, ticker: "")
    good = json.dumps({"claim_reviews": [{"claim": "c1", "status": "unresolved", "evidence_summary": "", "sources": []}],
                       "reasoning": "ok", "verdict_changed": False, "revised_verdict": "v"})
    calls = stub_llm(["not json at all", good])
    out = nodes.news_verify_node({"ticker": "AVGO", "evidence": EVIDENCE, "verdict": "v",
                                  "judge_decision": {"unsupported_claims": ["c1"]}})
    assert len(calls) == 2 and calls[1]["schema"]["required"][0] == "claim_reviews"
    assert "FAILED validation" in calls[1]["user"]
    assert out["claim_verification"]["claim_reviews"][0]["status"] == "unresolved"
    assert "CLAIM REVIEW DEBUG raw reply: not json at all" in capsys.readouterr().out


def test_truncated_argument_is_regenerated_then_trimmed(stub_llm, capsys):
    # first reply cut at the cap -> regenerated with a word budget and a larger cap
    calls = stub_llm(["cut mid sen", "A full case. Done."], ["max_tokens", "end_turn"])
    case = nodes.complete_argument("sys", "user", "BULL  (round 1)")
    assert case == "A full case. Done."
    assert calls[1]["max_tokens"] == config.ARGUMENT_RETRY_MAX_TOKENS
    assert f"under {config.ARGUMENT_WORD_BUDGET} words" in calls[1]["user"]
    # both attempts truncated -> trimmed to the last complete sentence, marker appended
    stub_llm(["First point stands. Second poi", "First point stands. Still cut mid"], ["max_tokens", "max_tokens"])
    case = nodes.complete_argument("sys", "user", "BEAR  (round 1)")
    assert case.startswith("First point stands.") and "Still cut mid" not in case
    assert "[argument trimmed to its last complete sentence" in case
    assert "regenerating" in capsys.readouterr().out


def test_no_truncated_turns_reach_the_judge(stub_llm):
    # the bull node itself: a truncated first reply never becomes bull_case
    stub_llm(["half a case that stops", "Whole case. Complete."], ["max_tokens", "end_turn"])
    out = nodes.bull_node({"ticker": "AVGO", "evidence": EVIDENCE, "bear_case": "", "round": 0})
    assert out["bull_case"] == "Whole case. Complete." and out["bull_history"] == ["Whole case. Complete."]
