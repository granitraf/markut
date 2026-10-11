"""Reliable numbers and labels: the context-keeping table parser, core
extraction (income, KPI table, segments, cash flow, KPI values, derived
metrics, sanity checks), worded guidance bands, YTD period classes,
unknown-is-not-absent, the real-answer coverage gate, leverage from the
balance sheet, profile trimming, governor normalization. Offline; the CAKE
and SHOP release tables are real HTML fixtures (examples of general
structures, not targets)."""
import json
import os

import pandas as pd
import pytest

from markut.agents import llm, profiler
from markut.evidence import assembly, core, edgar, market, questions, sections
from markut.guardrails import tracer
from markut.guardrails.review import review_node

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
SHOP_HTML = open(os.path.join(FIX, "shop_release_tables.html"), encoding="utf-8").read()
CAKE_HTML = open(os.path.join(FIX, "cake_release_tables.html"), encoding="utf-8").read()


def _src(html, date="2026-08-05"):
    return [{"form": "8-K", "name": "ex99-1", "filing_date": date, "url": "u", "html": html, "text": "", "headline_period": "quarter"}]


# ---------------------------------------------------------------- table parser
def test_tables_keep_caption_group_and_period_context():
    for html in (SHOP_HTML, CAKE_HTML):
        rows = edgar.parse_tables(html)
        assert rows
        assert all(r["caption"] or r["group"] for r in rows), "a row without its section or group heading"
        with_headings = [r for r in rows if r["has_period_headings"]]
        assert with_headings
        assert all(c["period"] not in ("", "unstated") for r in with_headings for c in r["cells"]), "unstated period under period headings"
    cake = edgar.parse_tables(CAKE_HTML)
    income = [r for r in cake if r["caption"].startswith("Condensed Consolidated Statements of Income")]
    rev = next(r for r in income if r["label"] == "Revenues")
    periods = [c["period"] for c in rev["cells"]]
    assert periods[:4] == ["quarter"] * 4 and periods[4:] == ["YTD (H1)"] * 4      # 13 weeks vs 26 weeks
    assert any("Percent of Revenues" in c["entity"] for c in rev["cells"])
    seg = [r for r in cake if r["caption"] == "Selected Segment Information"]
    brands = {c["entity"] for r in seg for c in r["cells"]}
    assert {"The Cheesecake Factory restaurants", "North Italia", "Other FRC", "Total"} <= brands
    assert seg[0]["units"] == "thousands" and seg[0]["duration"].startswith("For the 13 Weeks Ended")
    shop = edgar.parse_tables(SHOP_HTML)
    kpi = [r for r in shop if r["label"] in ("GMV", "MRR")]
    assert kpi and all(c["period"] == "quarter" for r in kpi for c in r["cells"]) and kpi[0]["units"] == "millions"
    ops = [r for r in shop if r["group"] == "Revenues"]
    assert {r["label"] for r in ops} >= {"Subscription solutions", "Merchant solutions"}


def test_period_from_heading_classes():
    f = edgar.period_from_heading
    assert f("13 Weeks Ended June 30, 2026")["period"] == "quarter"
    assert f("26 Weeks Ended July 1, 2025")["period"] == "YTD (H1)"
    assert f("Nine Months Ended September 30, 2026")["period"] == "YTD (9M)"
    assert f("Year Ended December 31, 2025")["period"] == "annual"
    assert f("June 30, 2026", "quarter")["period"] == "quarter"
    assert f("June 30, 2026")["period"].startswith("period ended")        # never 'unstated' with a date heading


# ---------------------------------------------------------------- core extraction
def test_core_extraction_income_kpis_segments_and_checks(monkeypatch):
    monkeypatch.setattr(core, "core_sources", lambda fs: _src(SHOP_HTML))
    profile = {"segments": [{"name": "Subscription solutions"}, {"name": "Merchant solutions"}],
               "company_kpis": [{"name": "Gross merchandise volume (GMV)"}, {"name": "Monthly Recurring Revenue (MRR)"},
                                {"name": "Free cash flow margin"}, {"name": "Gross payments volume penetration"}]}
    out = core.extract_core({}, profile, {"key_questions": [{"id": "Q2", "kpis": ["GPV penetration"]}]})
    t = out["text"]
    assert "- Revenue: 3,583 million (quarter" in t and "vs 2,680 million year-ago" in t
    assert "- Gross profit: 1,708 million" in t and "- Operating income: 488 million" in t
    assert "- KPI Gross merchandise volume (GMV): GMV: 115,567 million" in t and "vs 87,837 million year-ago" in t and "+31.6%" in t
    assert "- KPI Monthly Recurring Revenue (MRR): MRR: 221 million" in t and "+19.5%" in t
    assert "- KPI value not found: Gross payments volume penetration" in t and "- KPI value not found: GPV penetration" in t
    assert out["kpi_values"]["Gross merchandise volume (GMV)"] is True and out["kpi_values"]["GPV penetration"] is False
    assert "- Subscription solutions [Revenues]: 802 million" in t and "; Subscription solutions)" in t
    assert "DERIVED Gross margin: 47.7% = Gross profit 1,708 / Revenue 3,583" in t
    assert "SANITY CHECK stated growth OK: YoY revenue growth rate 34%" in t
    assert "(percent; company-defined;" in t                                # a % KPI never says 'millions'


def test_core_extraction_brand_rows_and_residual_margin(monkeypatch):
    monkeypatch.setattr(core, "core_sources", lambda fs: _src(CAKE_HTML, "2026-07-28"))
    profile = {"segments": [{"name": "The Cheesecake Factory restaurants"}, {"name": "North Italia"}, {"name": "Other FRC"}],
               "company_kpis": [{"name": "Food and beverage costs % of revenues"}, {"name": "Labor expenses % of revenues"}, {"name": "Comparable restaurant sales"}]}
    t = core.extract_core({}, profile, None)["text"]
    assert "- Revenues: $98,440 thousand (quarter, For the 13 Weeks Ended June 30, 2026 North Italia)" in t and "; North Italia)" in t
    assert "- Revenues: $729,476 thousand" in t and "; The Cheesecake Factory restaurants)" in t
    assert "641.0%" not in t                                                # brand columns never pair as current vs year-ago
    assert "- KPI Food and beverage costs % of revenues: Food and beverage costs: 21.8% (quarter" in t and "vs 21.6% year-ago" in t and "T1 = EDGAR/8-K" in t
    assert "DERIVED residual margin after the listed cost ratios: 17.6% = 100% − Food and beverage costs 21.8% − Labor expenses 34.1% − Other operating costs and expenses 26.5%" in t
    assert "SANITY CHECK segment sum OK" in t
    assert "- KPI value not found: Comparable restaurant sales" in t      # not in these two tables -> explicit, not silent
    assert t.count("DERIVED Operating margin: 7.6%") == 1                  # deduped across tables


def test_num_of_and_label_matching():
    assert core.num_of("(2,057)") == -2057.0 and core.num_of("$1,029,633") == 1029633.0 and core.num_of("21.8%") == 21.8 and core.num_of("—") is None
    assert core.label_matches("GMV", "Gross merchandise volume (GMV)") == 1.0
    assert core.label_matches("Comparable restaurant sales vs. prior year", "Comparable restaurant sales") >= 0.5
    assert core.label_matches("Interest expense", "Free cash flow margin") < 0.3


# ---------------------------------------------------------------- guidance: words, ranges, lead-ins
def test_worded_guidance_bands_and_lead_in_bullets():
    assert dict(sections.interpret_band("low-thirties percentage rate; mid-to-high twenties; high-single-digit; 33% to 34%; flat")) == {
        "low-thirties percentage rate": "30–33%", "mid-to-high twenties": "24–29%", "high-single-digit": "7–9%", "33% to 34%": "33–34%", "flat": "≈0%"}
    text = ("For the third quarter of 2026, we expect: • Revenue to grow at a low-thirties percentage rate on a year-over-year basis; "
            "• Gross profit dollars to grow at a mid-to-high twenties percentage rate; • Operating expenses as a percentage of revenue to be 33% to 34%; "
            "• Stock-based compensation to be $150 million; and • Free cash flow margin to be similar to the second quarter of 2026.")
    out = sections.guidance_lines([{"label": "8-K ex99-1", "text": text, "form": "8-K", "filing_date": "d", "url": "u", "kind": "text"}])
    assert len(out["lines"]) == 5 and out["failures"] == []
    texts = [l["text"] for l in out["lines"]]
    assert all(t.startswith("For the third quarter of 2026, we expect:") for t in texts)   # the lead-in travels with each bullet
    assert any("→ 30–33%" in t for t in texts) and any("→ 24–29%" in t for t in texts) and any("→ 33–34%" in t for t in texts)
    assert any("$150 million" in t for t in texts) and any("second quarter of 2026 value" in t for t in texts)
    fail = sections.guidance_lines([{"label": "x", "text": "We expect strong growth across every market we serve.", "form": "8-K", "filing_date": "d", "url": "u", "kind": "text"}])
    assert fail["lines"] == [] and len(fail["failures"]) == 1


# ---------------------------------------------------------------- periods: partial years are never annual
def test_partial_year_periods_are_ytd_not_annual():
    assert questions.tag_period("Average sales per restaurant operating week increased 4.1% in the first six months of fiscal 2026") == "YTD (H1)"
    assert questions.tag_period("for the three fiscal quarters ended August 2, 2026") == "YTD (9M)"
    assert questions.tag_period("Comparable sales rose 5.8% in the second quarter of fiscal 2026") == "quarter"
    assert questions.tag_period("fiscal 2025 revenue was $3.75B") == "annual"
    ev = "- Average sales per operating week growth (YTD (H1), 26 weeks ended June 30, 2026): 4.1%  [source: x]"
    assert tracer.find_mislabeled("Per-week sales rose 4.1% in the first six months of fiscal 2026.", ev) == []
    bad = tracer.find_mislabeled("Per-week sales rose 4.1% for fiscal 2026.", ev)
    assert [f["claim"] for f in bad] == ["4.1%"] and "YTD (H1)" in bad[0]["reason"]
    assert frozenset(("ytd_h1", "annual")) in tracer._CONFLICTS and frozenset(("ytd_9m", "quarter")) in tracer._CONFLICTS


# ---------------------------------------------------------------- governor normalization
def test_governor_normalizes_percent_labels_and_unit_scales():
    ev = ("- Debt/Equity (mrq, %, data-provider field): 401.00  [source: yfinance/info]\n"
          "- Revenues: $729,476 thousand (quarter)  (units: thousands; basis: GAAP)  [source: x]\n"
          "- Revenue: 3,583 million (quarter)  (units: millions; basis: GAAP)  [source: x]")
    assert tracer.trace_claim("401.00%", ev) == "CITED" and tracer.trace_claim("401%", ev) == "CITED"
    assert tracer.trace_claim("$729.476M", ev) == "CITED" and tracer.trace_claim("$3.583B", ev) == "CITED" and tracer.trace_claim("$3,583 million", ev) == "CITED"
    assert tracer.trace_claim("402%", ev) == "FLAGGED"


# ---------------------------------------------------------------- unknown is not absent
def test_absence_claims_are_flagged_and_stamped(monkeypatch):
    ev = "- Price (current): $10.00  [source: yfinance/info]\n- EXTRACTION FAILURE: 8-K ex99-2: forward-looking language is present but no guided figure could be extracted"
    found = tracer.find_absence_claims("The company does not disclose quarterly guidance. The price is $10.00.", ev)
    assert len(found) == 1 and found[0]["sentence"].startswith("The company does not disclose")
    assert tracer.find_absence_claims("Guidance was not retrieved from the filings.", ev) == []
    assert tracer.find_absence_claims("The company does not disclose guidance.", "- Price (current): $10.00") == []   # no failure recorded -> no flag
    monkeypatch.setattr(llm, "call_claude", lambda *a, **k: "not json")
    out = review_node({"verdict": "The company does not disclose quarterly guidance. The price is $10.00. This is not financial advice.", "evidence": ev})
    assert "[UNKNOWN, NOT ABSENT" in out["verdict"] and out["review_report"]["absence_flags"]
    from markut.agents import prompts
    assert "Unknown is not absent" in prompts.JUDGE_SYSTEM_PROMPT and "UNKNOWN IS NOT ABSENT" in prompts.BULL_SYSTEM_PROMPT


# ---------------------------------------------------------------- coverage counts real answers
def test_coverage_counts_real_answers_and_linked_kpis(monkeypatch):
    monkeypatch.setattr(core, "core_sources", lambda fs: _src(SHOP_HTML))
    plan = {"key_questions": [{"id": "Q1", "question": "Can GMV growth stay above 25%?", "kpis": ["GMV"]},
                              {"id": "Q3", "question": "Is subscription MRR growth holding?", "kpis": ["Monthly Recurring Revenue"]},
                              {"id": "Q5", "question": "How sustainable is the free cash flow margin?", "kpis": ["Free cash flow margin", "Capital expenditures"]}]}
    profile = {"segments": [], "company_kpis": [{"name": "Gross merchandise volume (GMV)"}, {"name": "Monthly Recurring Revenue (MRR)"}, {"name": "Free cash flow margin"}]}
    out = core.extract_core({}, profile, plan)
    tagged = questions.tag_core_lines(out["text"], plan)
    assert "[Q1]" in tagged and "[Q3]" in tagged
    filler = ("[EVIDENCE Q3] q\n- [Q3] Risk: Our subscription business faces competition.\n"
              "- [Q3] \"These statements are forward-looking statements within the meaning of the safe harbor provisions.\"  [source: EDGAR/8-K x, filed d; quarter; unstated basis; segment unspecified]\n"
              "- [Q3] \"MRR grew 19.5% to $221 million.\"  [source: EDGAR/8-K x, filed d; quarter; unstated basis; segment unspecified]\n")
    cands = questions.candidate_lines(tagged + "\n" + filler, ["Q1", "Q3", "Q5"])
    assert all("safe harbor" not in l and "Risk:" not in l for l in cands["Q3"])
    a = questions.coverage_assess(tagged + "\n" + filler, plan, out["kpi_values"], None)
    assert a["Q1"]["covered"] and a["Q3"]["covered"]
    assert a["Q5"]["kpis_ok"] is True and "- KPI value not found: Capital expenditures" in out["text"]   # linked KPIs always get a value or an explicit not-found
    missing = {k: v for k, v in out["kpi_values"].items() if "Capital" not in k}
    a_missing = questions.coverage_assess(tagged + "\n" + filler, plan, missing, None)
    # run #26 (P1): covered = at least one linked KPI has a CURRENT-period value (free cash flow margin does);
    # the KPI with no value is still reported, by name, as missing
    assert a_missing["Q5"]["kpis_ok"] is False and a_missing["Q5"]["covered"] is True and a_missing["Q5"]["missing"] == ["Capital expenditures"]
    a2 = questions.coverage_assess(tagged + "\n" + filler, plan, out["kpi_values"], {"Q1": [], "Q3": [0], "Q5": []})
    # the relevance call decides which excerpts count (Q1 gets none) and the code routes; Q1 stays covered
    # because its linked KPI has a current-period value (run #26, P1 rule)
    assert a2["Q1"]["relevant"] == 0 and a2["Q1"]["covered"] is True and a2["Q3"]["relevant"] == 1


# ---------------------------------------------------------------- leverage and the planner's mislead items
def test_leverage_with_and_without_leases_and_mislead_rule():
    bs = pd.DataFrame({pd.Timestamp("2026-06-30"): {"Total Debt": 2081.0e6, "Capital Lease Obligations": 1471.0e6, "Stockholders Equity": 519.0e6}})
    lines = market.leverage_lines(bs, {"debtToEquity": 400.999})
    assert len(lines) == 2
    assert lines[0].startswith("- Debt/Equity including lease obligations (computed, balance sheet 2026-06-30): 401.0%") and "matches the yfinance" in lines[0]
    assert lines[1].startswith("- Debt/Equity excluding lease obligations (computed, balance sheet 2026-06-30): 117.5%") and "matches" not in lines[1]
    assert "lease obligations $1.47B" in lines[0] and "equity $519.00M" in lines[0]
    fixed = profiler.enforce_mislead_rules(["High Debt/Equity also ignores operating-lease liabilities.",
                                            "ROE near 39% is inflated by a small equity base (per the packet line Return on equity).",
                                            "Check whether FCF margin excludes SBC."])
    assert fixed[0].startswith("Check whether high Debt/Equity also ignores") and fixed[1].startswith("ROE near") and fixed[2].startswith("Check whether")
    from markut.agents import prompts
    assert "check whether" in prompts.PLANNER_SYSTEM_PROMPT


# ---------------------------------------------------------------- profile trimming keeps figures and flags
def test_profile_trimming_cuts_descriptions_first():
    prof = {"business": "Sentence one is long. " * 3 + "Second sentence.", "archetype": "software", "accounting_flags": ["lease_heavy", "large_sbc"],
            "segments": [{"name": f"Seg{i}", "latest_revenue": "$1,000 million", "latest_yoy": "+5%", "period": "Q2", "source": "id"} for i in range(8)],
            "company_kpis": [{"name": f"KPI{i}", "definition": "a very long definition of the metric " * 12, "unit": "%", "segment": "company-wide", "source": "id"} for i in range(8)]}
    blk = assembly.profile_block(prof, [], [])
    assert len(blk) <= assembly.PROFILE_TOKEN_CAP * 4 + 120
    assert "Accounting flags: lease_heavy, large_sbc" in blk and blk.count("$1,000 million") == 8 and blk.count("- KPI: KPI") == 8
    assert "a very long definition" not in blk


# ---------------------------------------------------------------- third-party models, page versioning, report
def test_dcf_far_from_price_is_labeled_unverified():
    from markut.agents import prompts
    assert "unverified third-party model" in prompts.JUDGE_SYSTEM_PROMPT
    src = open(os.path.join(os.path.dirname(__file__), "..", "markut", "evidence", "market.py")).read()
    assert "unverified third-party model, assumptions not disclosed; excluded from the verdict" in src


def test_pages_carry_versioned_assets(monkeypatch, tmp_path):
    from markut import config
    from markut.web import app as app_mod
    monkeypatch.setattr(config, "IN_PRODUCTION", False)
    monkeypatch.setattr(config, "CONSOLE_PASSWORD", None)
    from fastapi.testclient import TestClient
    c = TestClient(app_mod.app)
    html = c.get("/console").text
    assert f"/static/debate.js?v={app_mod.ASSET_VERSION}" in html and c.get("/console").headers["cache-control"] == "no-cache"
    assert c.get("/static/debate.js").headers["cache-control"] == "no-cache"


def test_report_renders_per_question_rulings():
    from markut import report
    ev = [{"event": "start", "data": {"ticker": "XYZ", "max_rounds": 2, "model": "m"}},
          {"event": "research", "data": {"evidence": "[QUOTE & VALUATION]\n- Price (current): $10.00  [source: yfinance/info]"}},
          {"event": "judge", "data": {"round": 1, "converged": True, "verdict": "v", "bull_strongest": "b", "bear_strongest": "r",
                                      "questions": [{"id": "Q1", "answer": "Margins held at 21%.", "stronger_side": "bull", "confidence": "high", "unsupported_claims": []}],
                                      "planner_coverage": "adequate"}},
          {"event": "news_verify", "data": {"claim_reviews": [{"claim": "c", "status": "supported", "verdict": "flag overturned", "evidence_summary": "s"}]}},
          {"event": "review", "data": {"verdict": "Price is $10.00. Not financial advice.", "stats": {"claims": 1, "cited": 1}}},
          {"event": "done", "data": {"rounds": 1, "converged": True, "usage": {"calls": 3, "input": 10, "output": 5}}}]
    pdf = report.build_report({"id": 1, "ticker": "XYZ", "started_at": "2026-10-09 00:00:00", "mode": "cli", "events": ev})
    assert pdf[:5] == b"%PDF-" and len(pdf) > 10000
