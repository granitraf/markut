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
    def install(replies, stop_reasons=None):
        calls = []   # a fresh log per install, so a second script starts at zero
        replies, stops = list(replies), list(stop_reasons or [])
        def fake(system_prompt, user_content, max_tokens=1000, schema=None, cached_prefix=None):
            calls.append({"system": system_prompt, "user": user_content, "max_tokens": max_tokens, "schema": schema,
                          "cached_prefix": cached_prefix})
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
    assert "EPS (forward, consensus non-GAAP, next fiscal year): $19.39" in text
    assert "P/E (forward, on next-FY consensus non-GAAP EPS): 19.38" in text
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


# ================================================================ P1 — evidence coverage
from markut.evidence import edgar, news as news_mod


def test_exhibit_picker_finds_broadcom_and_nvidia_releases():
    # run #2: Broadcom's release is "...x8kxex99.htm" (no ".1") — never matched before
    assert edgar.pick_exhibit(["0001730168-26-000076-index.html", "avgo-08022026x8kxex99.htm", "avgo-20260902.htm", "R1.htm"]) == "avgo-08022026x8kxex99.htm"
    assert edgar.pick_exhibit(["nvda-20260528.htm", "q1fy27pr.htm", "q1fy27cfocommentary.htm"]) == "q1fy27pr.htm"
    assert edgar.pick_exhibit(["a.htm", "ex99_2.htm", "ex99_1.htm"]) == "ex99_1.htm"       # 99.1 beats 99.2
    assert edgar.pick_exhibit(["x-ex99d1.htm"]) == "x-ex99d1.htm"
    assert edgar.pick_exhibit(["primary.htm", "0001-index.html"]) is None


def test_8k_guidance_chain_outlook_block():
    # the exhibit picker + outlook extractor: guidance appears once the release is indexed
    release = ("<html><body><p>Broadcom Inc. today reported results.</p><p>Outlook</p>"
               "<p>Fourth quarter fiscal year 2026 revenue guidance of approximately $21.0 billion is expected, "
               "an increase of 24 percent from the prior year period.</p>"
               "<p>Adjusted EBITDA guidance of approximately 67 percent of projected revenue is expected.</p>"
               "<p>Conference call details follow.</p></body></html>")
    text = edgar.html_to_text(release)
    outlook = edgar.extract_outlook(text)
    assert outlook.startswith("Outlook") and "$21.0 billion" in outlook and "Conference call" not in outlook


def test_item_1_business_overview_and_10q_commitments_extractors():
    tenk = "<html><body>" + "<p>Item 1. Business</p><p>Item 1A. Risk Factors</p><p>Item 7. MD&A</p>" + \
        "<p>Item 1. Business</p><p>Overview</p>" + \
        "<p>" + " ".join(["Broadcom designs, develops and supplies semiconductor and infrastructure software solutions, "
                          "including custom AI accelerators (XPUs) designed for hyperscale customers."] * 6) + "</p>" + \
        "<p>" + " ".join(["Our products are used in data center networking and custom silicon programs."] * 6) + "</p>" + \
        "<p>Item 1A. Risk Factors</p><p>" + "Risks are many and varied in this business. " * 60 + "</p></body></html>"
    item1 = edgar.extract_10k_business(tenk)
    assert item1.startswith("Item 1. Business") and "Risks are many" not in item1
    overview = edgar.business_overview(item1, char_limit=400)
    assert overview.startswith("Broadcom designs") and len(overview) <= 420 and overview.endswith(".")
    tenq = "<html><body><p>Note 10. Debt</p><p>" + "Debt details here. " * 20 + "</p>" + \
        "<p>Note 11. Commitments and Contingencies</p><p>" + ("We have guaranteed certain obligations of a customer financing arrangement. "
        "Our maximum exposure under the guarantee was $29 billion as of August 2, 2026. ") * 4 + "</p>" + \
        "<p>Note 12. Segment Information</p><p>" + "Segments are described here. " * 20 + "</p></body></html>"
    note = edgar.extract_10q_commitments(tenq)
    assert note.startswith("Note 11. Commitments and Contingencies") and "$29 billion" in note and "Segment Information" not in note
    assert edgar.extract_10q_commitments("<html><body><p>nothing here</p></body></html>").startswith("[section unavailable")


class _FakeColl:
    """A Chroma-like collection: .get(where_document={"$contains": s}) on stored docs."""
    def __init__(self, docs):
        self.docs = docs  # [(text, metadata)]
    def get(self, where_document=None, include=None, limit=None, **_):
        needle = (where_document or {}).get("$contains", "")
        hits = [(t, m) for t, m in self.docs if needle in t][: (limit or 10)]
        return {"documents": [t for t, _ in hits], "metadatas": [m for _, m in hits]}


def test_filing_backed_number_from_news_is_promoted_to_cited():
    from markut.evidence import rag
    coll = _FakeColl([
        ("AI semiconductor revenue grew 221 percent year-over-year to $5.2 billion in the quarter.",
         {"form": "8-K", "section": "press release", "filing_date": "2026-09-02", "url": "https://sec.gov/x/ex99.htm"}),
        ("Our maximum exposure under the guarantee was $29 billion as of August 2, 2026.",
         {"form": "10-Q", "section": "Commitments (10-Q note)", "filing_date": "2026-09-10", "url": "https://sec.gov/y/10q.htm"}),
    ])
    claims = ["News article claim that 'Broadcom's AI semiconductor revenue grew 221% year-on-year last quarter'",
              "News body claim of about $29B tied to that first tranche",
              "Bull claim that hyperscalers have multi-year commitments"]
    hits = rag.corroborate_claims("AVGO", claims, coll=coll)
    assert [(h["number"], h["match"]) for h in hits] == [("221%", "221 percent"), ("$29B", "$29 billion")]
    addendum = rag.format_corroboration_addendum(hits)
    assert addendum.startswith("[FILINGS ADDENDUM") and "EDGAR/8-K press release" in addendum and "EDGAR/10-Q Commitments" in addendum
    # the governor now traces the number: CITED, not rejected. Both figures
    # reached run #2 only through untrusted NEWS text; against the market
    # section alone they are FLAGGED, and the addendum gives them a FILING anchor.
    market_only = EVIDENCE.split("[FILINGS]")[0]
    assert tracer.trace_claim("$29B", market_only) == "FLAGGED" and tracer.trace_claim("221%", market_only) == "FLAGGED"
    assert tracer.trace_claim("$29B", market_only + "\n\n" + addendum) == "CITED"
    assert tracer.trace_claim("221%", market_only + "\n\n" + addendum) == "CITED"
    assert addendum.index("EDGAR/8-K press release") < addendum.index("EDGAR/10-Q Commitments")   # one block per hit, in order


def test_claim_review_corroborates_before_the_paid_reaudit(stub_llm, monkeypatch):
    import markut.evidence.rag as rag
    monkeypatch.setattr(nodes, "get_related_leads", lambda claims, ticker: "")
    monkeypatch.setattr(rag, "corroborate_claims", lambda ticker, claims, coll=None, max_hits=2: [
        {"claim": claims[0], "number": "221%", "match": "221 percent",
         "text": "AI semiconductor revenue grew 221 percent.", "metadata": {"form": "8-K", "section": "press release",
                                                                        "filing_date": "2026-09-02", "url": "u"}}])
    good = json.dumps({"claim_reviews": [{"claim": "c2", "status": "unresolved", "evidence_summary": "", "sources": []}],
                       "reasoning": "ok", "verdict_changed": False, "revised_verdict": "v"})
    calls = stub_llm([good])
    out = nodes.news_verify_node({"ticker": "AVGO", "evidence": EVIDENCE, "verdict": "v",
                                  "judge_decision": {"unsupported_claims": ["c1 says 221% growth", "c2"]}})
    reviews = out["claim_verification"]["claim_reviews"]
    assert reviews[0]["status"] == "supported" and reviews[0]["claim"].startswith("c1") and reviews[0]["sources"] == ["u"]
    assert reviews[1]["claim"] == "c2"
    flagged_list = calls[0]["user"].split("EVIDENCE PACKET")[0]
    assert "- c1 says 221% growth" not in flagged_list and "- c2" in flagged_list   # only the rest is re-audited
    assert out["evidence"].rstrip().endswith("u]") and "[FILINGS ADDENDUM" in out["evidence"]
    # all corroborated -> no paid call at all
    calls = stub_llm([])
    out = nodes.news_verify_node({"ticker": "AVGO", "evidence": EVIDENCE, "verdict": "v",
                                  "judge_decision": {"unsupported_claims": ["only 221% here"]}})
    assert calls == [] and out["claim_verification"]["claim_reviews"][0]["status"] == "supported"


def test_news_window_and_materiality_ranking():
    assert news_mod.NEWS_BASELINE_DAYS == 30
    identity = {"ticker": "AVGO", "company": "Broadcom Inc."}
    import time as _t
    now = _t.time()
    old_material = {"title": "Broadcom secures $42 billion loan for Anthropic chip financing, Reuters reports",
                    "summary": "The debt deal backs custom accelerators.", "published": _iso_days_ago(now, 20), "publisher": "Reuters"}
    fresh_fluff = {"title": "What Will $5,000 Invested in Broadcom Stock Be Worth in 5 Years?",
                   "summary": "Three futures for this company.", "published": _iso_days_ago(now, 1), "publisher": "Motley"}
    assert news_mod.score_news_item(old_material, identity, now=now) > news_mod.score_news_item(fresh_fluff, identity, now=now)
    assert news_mod.materiality_score(old_material) == news_mod.MATERIALITY_CAP
    assert news_mod.materiality_score(fresh_fluff) < 1.0


def _iso_days_ago(now, days):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(now - days * 86400, tz=timezone.utc).isoformat()


def test_market_context_lines():
    from datetime import date, timedelta
    closes = [((date(2025, 10, 1) + timedelta(days=i)).isoformat(), 300 + i) for i in range(300)]
    text = "\n".join(market.format_context_lines(closes, {"fiftyTwoWeekHigh": 650}, ["2026-06-11"], 700))
    assert "Return, 1 month" in text and "Return, 3 months" in text and "year to date" in text
    assert "Price vs 52-week high: -7.8% (high $650.00)" in text
    assert "Price vs all-time high (daily closes): -14.4%" in text
    assert "Move around last earnings (2026-06-11; close before → first close after): +0.4%" in text
    assert market.format_context_lines([], {}, []) == []


@pytest.mark.live
def test_live_avgo_packet_has_guidance_block():
    # free (EDGAR + local models, no model calls): the fixed exhibit picker indexes
    # Broadcom's 8-K and the Outlook block is no longer "[not extracted ...]"
    from markut.evidence.rag import get_filings_evidence
    text = get_filings_evidence("AVGO")
    assert "8-K press release filed" in text
    assert "-- Guidance & outlook --\n[not extracted" not in text
    assert "-- Business overview --" in text


# ================================================================ P2 — analytical depth
from markut.agents import prompts
from markut.evidence import valuation


def test_valuation_block_is_deterministic_and_labeled():
    import datetime as dt
    info = {"currentPrice": 376.51, "forwardPE": 19.41, "enterpriseToEbitda": 35.07, "marketCap": 1.8e12, "freeCashflow": 30.6e9, "trailingPegRatio": 0.36}
    est = {"0y": {"avg": 11.66}, "+1y": {"avg": 19.39}}
    closes = [((dt.date(2021, 1, 1) + dt.timedelta(days=i)).isoformat(), 200 + (i % 300)) for i in range(2100)]
    eps = [("2022-10-30", 10.0), ("2023-10-29", 12.0), ("2024-11-03", 14.0), ("2025-11-02", 16.0)]
    text = "\n".join(valuation.format_valuation_lines(info, est, 2026, {"closes": closes, "annual_eps": eps}))
    assert text.startswith("[VALUATION] (computed by code")
    # run #9 item 1: P/E on EACH fiscal year's consensus, side by side
    assert "P/E on current-FY FY2026 consensus EPS ($11.66, non-GAAP): 32.29x" in text
    assert "P/E on next-FY FY2027 consensus EPS ($19.39, non-GAAP): 19.42x" in text
    # the multiple band comes from the company's own history, and the grid crosses EPS cases with it
    assert "Trailing P/E band (company's own history" in text and "p25 22.2x | median 28.5x | p75 32.5x" in text
    rows = [l for l in text.splitlines() if l.startswith("- Implied price, EPS")]
    assert [r.split(":")[0] for r in rows] == ["- Implied price, EPS -15% $16.48", "- Implied price, EPS consensus $19.39", "- Implied price, EPS +10% $21.33"]
    # no cell equals today's price by construction, and the old "today's multiple × current-FY EPS" rows are gone
    assert "$376.51 (" not in "\n".join(rows) and "Implied price from current-FY" not in text
    assert "peer" not in text.lower()
    assert tracer.trace_claim("$430.46", text) == "CITED"          # every implied price traces as a packet number
    # fallback without history: today's forward multiple ±20%, and the packet SAYS the center equals the price
    fb = "\n".join(valuation.format_valuation_lines(info, est, 2026, {}))
    assert "equals today's price by construction" in fb and "fwd 19.4x = $376.17" in fb
    assert valuation.format_valuation_lines({}, {}) == []
    assert not hasattr(config, "PEERS")


def test_pe_band_needs_real_history():
    assert valuation.pe_band([], []) == {} and valuation.pe_band([("2026-01-01", 10.0)], [("2025-01-01", 1.0)]) == {}


def test_judge_prompt_has_checklist_and_verdict_format():
    j = prompts.JUDGE_SYSTEM_PROMPT
    assert j.startswith(prompts.JUDGE_SYSTEM_PROMPT_NOTEBOOK)
    for needle in ("Period and basis", "Overlapping categories", "48% + top-five end customers 40%",
                   "Competitor or customer", "Which year's EPS", "[VALUATION] scenario table",
                   "Debates that move the stock:", "What would change this view:", "Next catalyst:"):
        assert needle in j, needle
    assert "do not compute your own multiple" in j
    assert prompts.BULL_SYSTEM_PROMPT.endswith(prompts.ANALYST_ADDENDUM) and "never call a" in prompts.BEAR_SYSTEM_PROMPT
    assert "[FILINGS ADDENDUM" in prompts.NEWS_VERIFY_SYSTEM_PROMPT


# ================================================================ run #6 (AVGO, 2026-10-08): truncated judge, over-firing labels, cost
from markut.guardrails.parsing import salvage_judge_json

_FIX6 = os.path.join(os.path.dirname(__file__), "fixtures", "avgo_run6_2026-10-08.json")
RUN6 = json.load(open(_FIX6, encoding="utf-8"))["events"]
EVIDENCE6 = next(e for e in RUN6 if e["event"] == "research")["data"]["evidence"]
JUDGE6_RAW = next(e for e in RUN6 if e["event"] == "judge")["data"]["verdict"]   # the raw cut-off JSON the old path passed on
VERDICT6 = next(e for e in RUN6 if e["event"] == "review")["data"]["verdict"]


def test_run6_salvage_recovers_the_truncated_judge_reply():
    out = salvage_judge_json(JUDGE6_RAW)
    assert out["truncated"] is True
    assert out["bull_strongest"].startswith("Reported momentum is strong") and out["bear_strongest"].startswith("The 19.4x forward P/E")
    assert out["verdict"].startswith("Research findings:") and out["verdict"].endswith("[truncated]")
    assert len(out["unsupported_claims"]) == 10 and "converged" not in out        # cut before converged was written
    assert salvage_judge_json("no json here") == {} and salvage_judge_json('{"verdict": "v", "converged": true}') == {"verdict": "v", "converged": True}


def test_run6_label_check_no_longer_over_fires():
    # the old classifier flagged 21 numbers in this text (5 survived revision); none are real mislabels
    assert tracer.find_mislabeled(JUDGE6_RAW, EVIDENCE6) == []
    assert tracer.find_mislabeled(VERDICT6, EVIDENCE6) == []
    # the three patterns that caused them, individually
    assert tracer.find_mislabeled("On FY2027 consensus EPS of $19.39 the stock trades at 19.4x.", EVIDENCE6) == []     # forward ⟂ annual
    assert tracer.find_mislabeled("Reported fundamentals (85.5% MRQ revenue growth, 75.52% gross margin, $30.6B TTM FCF, $34.8B Q4 guide) support execution", EVIDENCE6) == []  # nearest marker, clause-scoped
    assert tracer.find_mislabeled("given TTM GAAP profit margin of 42.94% and FY2025 net income growth of +292.3%", EVIDENCE6) == []    # modifier before the number wins
    # and the real mislabel is still caught
    assert [f["claim"] for f in tracer.find_mislabeled("Revenue grew 85.5% in fiscal year 2025.", EVIDENCE6)] == ["85.5%"]
    assert tracer.find_mislabeled("Trailing EPS of $19.39 supports the multiple.", EVIDENCE6)[0]["reason"].endswith("not ttm")


def test_judge_truncation_gets_a_shorter_retry_then_salvage(stub_llm):
    good = json.dumps({"bull_strongest": "b", "bear_strongest": "r", "unsupported_claims": [], "reasoning": "x",
                       "verdict": "v. Not financial advice.", "converged": False})
    state = {"ticker": "AVGO", "evidence": EVIDENCE6, "bull_case": "b", "bear_case": "r",
             "bull_history": ["b"], "bear_history": ["r"], "round": 0, "verdict": ""}
    # 1. first reply cut off -> the retry asks for a SHORTER reply (not a JSON repair) and succeeds
    calls = stub_llm([JUDGE6_RAW, good], ["max_tokens", "end_turn"])
    out = nodes.judge_node(dict(state))
    assert len(calls) == 2 and "MUCH SHORTER" in calls[1]["user"] and "NOT valid JSON" not in calls[1]["user"]
    assert calls[0]["max_tokens"] == config.JUDGE_MAX_TOKENS == 4000
    assert out["converged"] is False and out["judge_decision"]["bull_strongest"] == "b" and "truncated" not in out["judge_decision"]
    # 2. both replies cut off -> salvage: real strongest points, truncated flag, NO fake convergence, no raw JSON verdict
    stub_llm([JUDGE6_RAW, JUDGE6_RAW], ["max_tokens", "max_tokens"])
    out = nodes.judge_node(dict(state))
    d = out["judge_decision"]
    assert d["truncated"] is True and out["converged"] is False
    assert d["bull_strongest"].startswith("Reported momentum") and len(d["unsupported_claims"]) == 10
    assert out["verdict"].startswith("Research findings:") and not out["verdict"].startswith("{")
    assert "cut off at the output cap twice" in d["reasoning"]


def test_evidence_rides_in_a_cached_system_prefix(stub_llm):
    calls = stub_llm([json.dumps({"bull_strongest": "b", "bear_strongest": "r", "unsupported_claims": [], "reasoning": "x",
                                  "verdict": "v. Not financial advice.", "converged": True})])
    state = {"ticker": "AVGO", "evidence": EVIDENCE6, "bull_case": "b", "bear_case": "r",
             "bull_history": ["b"], "bear_history": ["r"], "round": 0, "verdict": ""}
    nodes.judge_node(state)
    assert calls[0]["cached_prefix"].startswith("EVIDENCE PACKET") and EVIDENCE6 in calls[0]["cached_prefix"]
    assert EVIDENCE6 not in calls[0]["user"]          # not duplicated in the user turn
    # the addendum never enters the cached prefix: base stays byte-identical across the run
    base, add = nodes.split_addendum(EVIDENCE6 + "\n\n[FILINGS ADDENDUM — x]\n- 221%")
    assert base == EVIDENCE6 and add.startswith("[FILINGS ADDENDUM")
    assert nodes.evidence_prefix(EVIDENCE6 + "\n\n[FILINGS ADDENDUM — x]") == nodes.evidence_prefix(EVIDENCE6)


def test_request_kwargs_cache_control_and_accounting(monkeypatch):
    kw = llm._request_kwargs("role prompt", "user", 100, None, cached_prefix="EVIDENCE PACKET ...")
    assert kw["system"][0] == {"type": "text", "text": "EVIDENCE PACKET ...", "cache_control": {"type": "ephemeral"}}
    assert kw["system"][1] == {"type": "text", "text": "role prompt"}
    assert llm._request_kwargs("role", "user", 100)["system"] == "role"       # no prefix -> plain string, unchanged
    # usage accounting keeps cached reads/writes apart from uncached input
    from types import SimpleNamespace
    class Fake:
        def create(self, **k):
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")], stop_reason="end_turn",
                                   usage=SimpleNamespace(input_tokens=100, output_tokens=5, cache_creation_input_tokens=7000, cache_read_input_tokens=0))
    monkeypatch.setattr(llm, "client", SimpleNamespace(messages=Fake()))
    llm.reset(); llm.call_claude("s", "u", cached_prefix="p")
    assert llm.TOKENS == {"input": 100, "output": 5, "calls": 1, "cache_write": 7000, "cache_read": 0}
    from markut import store
    row = store.summarize([{"event": "start", "data": {"ticker": "X"}},
                           {"event": "done", "data": {"rounds": 1, "converged": True, "usage": {"calls": 8, "input": 9000, "output": 900, "cache_write": 7000, "cache_read": 49000}}}])
    assert row["input_tokens"] == 65000 and row["output_tokens"] == 900      # every input token the run sent
    llm.reset()


def test_trims_after_run6():
    from markut.evidence import rag
    title, _, _, fence, k, cap = rag.THEMES[2]
    assert title.startswith("Guidance, commitments") and k == 2 and cap == 400 and "Item 7" not in fence
    assert config.ARGUMENT_MAX_TOKENS == 1500 and news_mod.NEWS_BASELINE_TOKEN_CAP == 1000
    assert "under 450 words" in prompts.BULL_SYSTEM_PROMPT and "LENGTH LIMITS" in prompts.JUDGE_SYSTEM_PROMPT
    text = "\n".join(valuation.format_valuation_lines(
        {"currentPrice": 100.0, "forwardPE": 20.0, "marketCap": 1e9, "freeCashflow": 5e7},
        {"0y": {"avg": 4.0}, "+1y": {"avg": 5.0}}, 2026, {}))
    assert "peer" not in text.lower() and "EV/Revenue" not in text


# ================================================================ run #7: claim review truncation + crash
def test_claim_review_truncation_shorter_retry_and_no_crash(stub_llm, monkeypatch, capsys):
    import markut.evidence.rag as rag
    monkeypatch.setattr(nodes, "get_related_leads", lambda claims, ticker: "")
    monkeypatch.setattr(rag, "corroborate_claims", lambda ticker, claims, coll=None, max_hits=2: [])
    state = {"ticker": "AVGO", "evidence": EVIDENCE6, "verdict": "v", "judge_decision": {"unsupported_claims": ["c1", "c2"]}}
    cut = '{"claim_reviews":[{"claim":"c1","status":"unresolved","evidence_summary":"long long'
    good = json.dumps({"claim_reviews": [{"claim": "c1", "status": "unresolved", "evidence_summary": "", "sources": []}],
                       "reasoning": "ok", "verdict_changed": False, "revised_verdict": ""})
    # 1. cut off once -> a SHORTER retry (not a repair), which succeeds; empty revised_verdict keeps the verdict
    calls = stub_llm([cut, good], ["max_tokens", "end_turn"])
    out = nodes.news_verify_node(dict(state))
    assert len(calls) == 2 and "MUCH SHORTER" in calls[1]["user"] and "FAILED validation" not in calls[1]["user"]
    assert calls[0]["max_tokens"] == config.CLAIM_REVIEW_MAX_TOKENS == 2500
    assert out["verdict"] == "v" and out["claim_verification"]["claim_reviews"][0]["claim"] == "c1"
    # 2. cut off twice -> fail unresolved WITHOUT crashing (run #7 raised UnboundLocalError here)
    stub_llm([cut, cut], ["max_tokens", "max_tokens"])
    out = nodes.news_verify_node(dict(state))
    reviews = out["claim_verification"]["claim_reviews"]
    assert [r["status"] for r in reviews] == ["unresolved", "unresolved"] and out["verdict"] == "v"
    assert "could not be parsed: retry also cut off" in out["claim_verification"]["reasoning"]
    assert "CLAIM REVIEW DEBUG raw retry reply" in capsys.readouterr().out
    assert "revised_verdict must be an EMPTY string unless verdict_changed" in prompts.NEWS_VERIFY_SYSTEM_PROMPT


# ================================================================ run #9 audit items 2-6
from markut.evidence import gaps as gaps_mod


def test_data_gaps_block_names_sources_and_hides_raw_errors():
    clean, found = gaps_mod.summarize_gaps(EVIDENCE6)                 # run #6 packet: four FMP sections failed
    assert clean.startswith("[DATA GAPS]\n- quote: upstream returned a non-JSON response")
    assert [g["source"] for g in found] == ["quote", "income-statement", "ratios", "dcf"]
    assert "Expecting value" not in clean and "char 0" not in clean
    assert "- known gap (always): earnings-call transcript" in clean
    assert gaps_mod.count_gaps(clean) == 4
    # a clean packet says so; summarizing twice is idempotent
    ok, none = gaps_mod.summarize_gaps("[QUOTE & VALUATION]\n- Price (current): $1.00  [source: x]")
    assert none == [] and "- none — every evidence source responded" in ok and gaps_mod.count_gaps(ok) == 0
    assert gaps_mod.summarize_gaps(ok)[0] == ok
    # new FMP marker names + the 402 reason
    assert gaps_mod.clean_reason("FMP HTTP 402: endpoint 'quote' is not covered by the current FMP subscription") == "not covered by the FMP subscription (HTTP 402)"
    c2, g2 = gaps_mod.summarize_gaps("[FMP DCF section unavailable: FMP HTTP 402: endpoint 'discounted-cash-flow' is not covered]\n[QUOTE & VALUATION]\n- Price (current): $1.00  [source: x]")
    assert g2 == [{"source": "FMP DCF", "reason": "not covered by the FMP subscription (HTTP 402)"}]


def test_governor_reports_unavailable_sources(stub_llm):
    verdict = "Price is $376.51. This is research, not investment advice."
    clean, _ = gaps_mod.summarize_gaps(EVIDENCE6)
    out = review_node({"verdict": verdict, "evidence": clean})
    assert out["review_report"]["sources_unavailable"] == 4 and out["review_report"]["final_status"] == "clean"
    from markut.web.events import review_stats
    assert review_stats(verdict, clean, verdict, out["review_report"])["sources_unavailable"] == 4


def test_commitments_dollar_lines_and_no_truncation_before_dollars():
    note = ("Note 11. Commitments and Contingencies. We make purchase commitments in the ordinary course. " * 3 +
            "During the quarter we entered into a backstop agreement with a financial partner for a customer's lease obligations. "
            "Our maximum potential liability under the Backstop upon the deployment of all AI racks, on an undiscounted basis, was approximately $ 29 billion. "
            "Therefore, $1,755 million of unrecognized tax benefits have been excluded from the table above. "
            "Litigation is described below.")
    lines = edgar.dollar_sentences(note)
    assert lines[0].startswith("Our maximum potential liability under the Backstop") and "$29 billion" in lines[0]   # "$ 29" normalized, ranked first
    assert any("$1,755 million" in l for l in lines) and len(lines) == 2
    assert edgar.dollar_sentences("[section unavailable: x]") == []
    # the theme formatter never cuts a commitments quote before its first dollar figure
    from markut.evidence import rag
    long_quote = ("We make significant decisions about purchase commitments and contractual obligations in the ordinary course of business. " * 4
                  + "Our maximum potential liability under the Backstop was approximately $ 29 billion.")
    block = rag.format_theme_block("t", [{"text": long_quote, "metadata": {"form": "10-Q", "section": "Commitments (10-Q note)", "filing_date": "2026-09-10", "url": "u"}}], 120)
    assert "$29 billion" in block[1]
    assert "$ 29 billion" in rag.number_variants("$29B")               # corroboration accepts the filing's spelling


def test_8k_highlights_extraction():
    release = ("EX-99.1 Document Exhibit 99.1 Broadcom Inc. Announces Third Quarter Fiscal Year 2026 Financial Results\n\n"
               "• Revenue of $ 29.6 billion for the third quarter, up 86 percent from the prior year period\n\n"
               "• AI semiconductor revenue grew 221 percent year-on-year to $5.2 billion\n\n"
               "\u201cWe delivered record results,\u201d said Hock Tan, President and CEO.\n\n"
               "Fourth Quarter Fiscal Year 2026 Business Outlook\n\nFourth quarter revenue guidance of approximately $34.8 billion is expected.\n\n"
               "About Broadcom\n\nBroadcom Inc. is a global technology leader.")
    h = edgar.extract_highlights(release)
    assert h.startswith("Broadcom Inc. Announces") and "EX-99.1" not in h
    assert "$29.6 billion" in h and "221 percent" in h and "said Hock Tan" in h
    assert "Outlook" not in h and "$34.8 billion" not in h and "About Broadcom" not in h   # outlook has its own block; boilerplate stops it


def test_concentration_sentences_are_dated_facts():
    tenq = ("Financial Guarantee During the fiscal quarter ended August 2, 2026, we arranged for a financial partner to take on certain agreements to purchase AI racks for a customer. "
            "In connection with this arrangement, we entered into a backstop agreement with the financial partner for the customer's lease obligations over the 5-year lease terms. "
            "Gross margin was approximately flat sequentially. "
            "We believe aggregate sales to our top five end customers accounted for approximately 40% of our net revenue for fiscal year 2025.")
    got = edgar.concentration_sentences(tenq)
    assert len(got) == 3 and got[0].startswith("Financial Guarantee") and got[-1].endswith("fiscal year 2025.")
    assert all("customer" in s.lower() for s in got) and not any("Gross margin" in s for s in got)
    assert edgar.concentration_sentences("Nothing about buyers here. Sales rose 10%.") == []


def test_all_time_high_from_daily_closes_with_sanity_check(capsys):
    closes = [(f"2026-01-{d:02d}", 100.0 + d) for d in range(1, 29)]
    assert market.all_time_high_close([("2020-05-05", 300.0)] + closes) == 300.0
    assert market.all_time_high_close(closes) == 128.0                 # ATH is the 52-week high close itself: consistent
    assert market.all_time_high_close([]) is None
    text = "\n".join(market.format_context_lines(closes, {}, [], 300.0))
    assert "Price vs all-time high (daily closes): -57.3% (high $300.00)" in text
    # the series can never contradict itself through this function; simulate a bad input by monkeying the window
    bad = [("2026-01-01", 50.0)] * 300 + [("2026-12-31", 60.0)]
    assert market.all_time_high_close(bad) == 60.0 and "FAILED" not in capsys.readouterr().out


def test_fmp_plan_limit_is_a_named_failure(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(market.requests, "get", lambda *a, **k: SimpleNamespace(status_code=402, text="Premium Query Parameter", json=lambda: (_ for _ in ()).throw(ValueError("x"))))
    with pytest.raises(RuntimeError, match="not covered by the current FMP subscription"):
        market.fmp_get_json("quote", "AVGO")


def test_dispersed_pe_history_is_information_not_the_grid():
    import datetime as dt
    info = {"currentPrice": 376.51, "forwardPE": 19.41}; est = {"0y": {"avg": 11.66}, "+1y": {"avg": 19.39}}
    closes = [((dt.date(2021, 1, 1) + dt.timedelta(days=i)).isoformat(), 200 + (i % 300)) for i in range(2100)]
    trough = [("2022-10-30", 10.0), ("2023-10-29", 1.5), ("2024-11-03", 2.0), ("2025-11-02", 16.0)]   # acquisition/trough years -> huge P/Es
    text = "\n".join(valuation.format_valuation_lines(info, est, 2026, {"closes": closes, "annual_eps": trough}))
    assert "Trailing P/E band" in text and "Band note: p75/p25" in text and "too dispersed" in text
    assert "fwd 19.4x = $376.17" in text and "p75" not in text.split("Scenario grid")[1]   # grid fell back to today's multiple
    assert valuation.BAND_MAX_DISPERSION == 2.0


def test_list_blocks_print_as_lines_and_concentration_skips_design_win_sentences():
    from markut.evidence import rag
    block = rag.format_theme_block("t", [{"text": "- (10-K filed 2025-12-18) top five end customers 40%.\n- (10-Q filed 2026-09-10) backstop for the customer's lease obligations.",
                                          "metadata": {"form": "10-Q", "section": "Concentration (dated)", "filing_date": "2026-09-10", "url": "u"}}], 400)
    assert block[1].startswith("- (10-K filed") and block[2].startswith("- (10-Q filed") and block[3].startswith("  [source:")
    assert not any(l.startswith('- "') for l in block)
    got = edgar.concentration_sentences("Winning a product design does not guarantee sales to a customer. "
                                        "We believe sales to our top five end customers accounted for approximately 40% of our net revenue for fiscal year 2025.")
    assert len(got) == 1 and got[0].startswith("We believe")
