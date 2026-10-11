"""P1 — evidence completeness (run #26 audit): an open question must be truly
open. (4) current-period disclosures reach the debate before older ones —
the latest 10-Q MD&A is eligible for every question, current-period
excerpts outrank prior-year text and say which period they are, percentage
tables keep their comparison columns, off-topic excerpts are rejected;
(5) the planner only asks what the disclosures can answer; (6) coverage
reflects whether a question can be answered for the current period, and
the judge is told. Offline: the local embedder / reranker run (free, no
network), every model call is stubbed. Fixtures: AAPL-shaped (an example,
not a target) and synthetic companies with a different structure."""
import json
import os
import uuid

import pytest

from markut.agents import llm, nodes, profiler
from markut.evidence import core, edgar, questions

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def check(label, condition):
    assert condition, label


def _html(name):
    return open(os.path.join(FIX, name), encoding="utf-8").read()


@pytest.fixture
def stub_llm(monkeypatch):
    def install(replies, stop_reasons=None):
        calls, replies, stops = [], list(replies), list(stop_reasons or [])
        def fake(system_prompt, user_content, max_tokens=1000, schema=None, cached_prefix=None):
            calls.append({"system": system_prompt, "user": user_content, "schema": schema})
            llm.LAST_STOP_REASON = stops.pop(0) if stops else "end_turn"
            return replies.pop(0) if replies else "{}"
        monkeypatch.setattr(llm, "call_claude", fake)
        return calls
    yield install
    llm.LAST_STOP_REASON = "end_turn"


def _coll(docs):
    """A tiny real Chroma collection: docs = [(text, form, section, filing_date)]."""
    import chromadb
    from markut.evidence import rag
    coll = chromadb.Client().create_collection(f"t_{uuid.uuid4().hex[:10]}", metadata={"hnsw:space": "cosine"})
    vecs = rag.get_embedder().encode([d[0] for d in docs], show_progress_bar=False)
    coll.add(ids=[f"c{i}" for i in range(len(docs))], embeddings=[v.tolist() for v in vecs], documents=[d[0] for d in docs],
             metadatas=[{"form": d[1], "section": d[2], "filing_date": d[3], "url": "u"} for d in docs])
    return coll


# two company shapes for the retrieval tests
SHAPES = {
    "multi-segment (AAPL-shaped)": {
        "profile": {"segments": [{"name": "iPhone"}, {"name": "Services"}, {"name": "Greater China"}, {"name": "Japan"}]},
        "question": {"id": "Q1", "question": "Is iPhone growth sustainable or a one-quarter spike?",
                     "kpis": ["iPhone net sales"], "segments": ["iPhone"],
                     "search_queries": ["iPhone net sales increased due primarily to higher net sales"]},
        "current": ("iPhone net sales increased during the third quarter of 2026 compared to the same quarter of 2025 "
                    "due primarily to higher net sales of the Pro models. iPhone demand was strong in every region.",
                    "10-Q", "Item 2 (10-Q MD&A)", "2026-07-31"),
        "prior": ("Over fiscal 2025 the annual report described iPhone growth as sustainable through a full product cycle. "
                  "Is iPhone growth sustainable, or was it a one-quarter spike? Management argued the former for that year.",
                  "10-K", "Item 7", "2025-10-31"),
    },
    "single-segment manufacturer (synthetic)": {
        "profile": {"segments": [{"name": "Devices"}]},
        "question": {"id": "Q1", "question": "Is unit growth sustainable as average selling prices fall?",
                     "kpis": ["Units shipped"], "segments": [],
                     "search_queries": ["units shipped increased while average selling price declined"]},
        "current": ("Units shipped increased 25% in the third quarter of 2026 while the average selling price declined "
                    "to $94.40 as the product mix shifted toward entry models. Demand for entry models rose in every channel.",
                    "10-Q", "Item 2 (10-Q MD&A)", "2026-10-28"),
        "prior": ("Units shipped increased in fiscal 2025 and unit growth was sustainable while the average selling "
                  "price held steady across the year. Unit growth was sustainable as the average selling price held.",
                  "10-K", "Item 7", "2026-02-20"),
    },
}


# ======================================================================
# 4a. the latest 10-Q MD&A is eligible for every question
# ======================================================================
@pytest.mark.parametrize("shape", list(SHAPES))
def test_latest_mdna_is_eligible_even_when_the_profiler_read_it(shape):
    s = SHAPES[shape]
    coll = _coll([s["current"], s["prior"]])
    lines = questions.question_blocks(coll, {"key_questions": [s["question"]]}, s["profile"], profiled_text=s["current"][0])
    text = "\n".join(lines)
    check(f"{shape}: the current-quarter MD&A sentence reaches the packet although the profiler read it",
          s["current"][0][:60] in text)


# ======================================================================
# 4b. current-period excerpts outrank prior-year text and say their period
# ======================================================================
@pytest.mark.parametrize("shape", list(SHAPES))
def test_current_period_excerpt_outranks_prior_year_and_is_labelled(shape):
    s = SHAPES[shape]
    coll = _coll([s["prior"], s["current"]])
    lines = [l for l in questions.question_blocks(coll, {"key_questions": [s["question"]]}, s["profile"], profiled_text="")
             if l.startswith("- [Q1] \"")]
    check(f"{shape}: two excerpts retrieved (got {len(lines)})", len(lines) == 2)
    check(f"{shape}: the current-quarter excerpt ranks first ({lines[0][:80]!r})", s["current"][0][:40] in lines[0])
    check(f"{shape}: the current excerpt says it is current-period", "current period (10-Q" in lines[0])
    check(f"{shape}: the prior-year excerpt says it is prior-year", "prior fiscal year (10-K" in lines[1])


# ======================================================================
# 4c. percentage tables keep their comparison columns, per segment or line
# ======================================================================
def _core(monkeypatch, html_name, profile, form="10-Q"):
    monkeypatch.setattr(core, "core_sources", lambda fs: [{"form": form, "name": "q.htm", "filing_date": "2026-07-31", "url": "u",
                                                           "html": _html(html_name), "text": "", "headline_period": "quarter"}])
    return core.extract_core({}, profile, None)


def test_percentage_tables_yield_per_line_values_with_comparisons(monkeypatch):
    rows = edgar.parse_tables(_html("aapl_10q_margin_tables.html"))
    pct = [r for r in rows if r["group"] == "Gross margin percentage"]
    check(f"the headerless continuation table inherits its period columns ({[c['period'] for c in pct[0]['cells']] if pct else None})",
          pct and all(c["period"] in ("quarter", "YTD (9M)") for r in pct for c in r["cells"]))
    t = _core(monkeypatch, "aapl_10q_margin_tables.html",
              {"segments": [{"name": "Services"}], "company_kpis": [{"name": "Services gross margin percentage"}]})["text"]
    for label, cur, prev in (("Products", "40.1%", "34.5%"), ("Services", "75.6%", "75.6%"), ("Total gross margin percentage", "50.1%", "46.5%")):
        line = next((l for l in t.splitlines() if l.startswith(f"- {label}") and cur in l), "")
        check(f"AAPL-shaped: {label} {cur} vs {prev} year-ago, quarter (got {line[:120]!r})",
              line and f"vs {prev} year-ago" in line and "(quarter" in line)
    kpi = next((l for l in t.splitlines() if l.startswith("- KPI Services gross margin percentage")), "")
    check(f"the segment margin KPI is the segment's 75.6%, not the 50.1% total (got {kpi[:120]!r})", "75.6%" in kpi and "50.1%" not in kpi)
    t2 = _core(monkeypatch, "synthetic_margin_tables.html", {"segments": [{"name": "Hardware"}, {"name": "Software"}], "company_kpis": []})["text"]
    for label, cur, prev in (("Hardware", "31.3%", "29.8%"), ("Software", "83.3%", "78.6%")):
        line = next((l for l in t2.splitlines() if l.startswith(f"- {label}") and cur in l), "")
        check(f"synthetic: {label} {cur} vs {prev} year-ago, quarter (got {line[:120]!r})", line and f"vs {prev} year-ago" in line and "(quarter" in line)
    check("no percentage line is left without a period", "(unstated" not in t and "(unstated" not in t2)


# ======================================================================
# 4d. an excerpt must address its question's topic
# ======================================================================
def test_off_topic_excerpts_are_rejected():
    china = {"id": "Q3", "question": "Is Greater China's rebound durable, and how exposed is it to competition and regulation?",
             "kpis": ["Greater China net sales"], "segments": ["Greater China"]}
    services = {"id": "Q2", "question": "Can Services keep growing double digits, and how much comes from advertising and cloud?",
                "kpis": ["Services net sales"], "segments": ["Services"]}
    segs = ["iPhone", "Services", "Greater China", "Japan", "Rest of Asia Pacific"]
    fiscal = ("The Company's fiscal years 2025 and 2024 spanned 52 weeks each, whereas fiscal year 2023 spanned 53 weeks. "
              "Greater China results are reported on the same calendar.")
    japan = "Japan net sales increased due to higher net sales of iPhone. Rest of Asia Pacific net sales increased on iPad."
    china_ok = ("Greater China net sales increased 22% during the third quarter of 2026 due to higher net sales of iPhone. "
                "Demand in the region improved across the product line.")
    services_ok = "Services net sales increased due primarily to higher net sales from advertising, the App Store and cloud services."
    check("fiscal-year length is rejected for a regional question", not questions.excerpt_relevant(china, fiscal, segs))
    check("another region's text is rejected for a Services question", not questions.excerpt_relevant(services, japan, segs))
    check("an on-topic regional excerpt passes", questions.excerpt_relevant(china, china_ok, segs))
    check("an on-topic Services excerpt passes", questions.excerpt_relevant(services, services_ok, segs))
    unit_q = {"id": "Q1", "question": "Is unit growth sustainable as average selling prices fall?", "kpis": ["Units shipped"], "segments": []}
    check("synthetic: a units sentence passes", questions.excerpt_relevant(unit_q, "Units shipped rose 25% to 1,250 thousand.", ["Devices"]))
    check("synthetic: a lease-accounting sentence is rejected",
          not questions.excerpt_relevant(unit_q, "Operating lease liabilities were measured at the present value of remaining payments.", ["Devices"]))
    coll = _coll([(fiscal, "10-K", "Item 7", "2025-10-31"), (china_ok, "10-Q", "Item 2 (10-Q MD&A)", "2026-07-31")])
    text = "\n".join(questions.question_blocks(coll, {"key_questions": [dict(china, search_queries=["Greater China net sales increased", "fiscal year weeks"])]},
                                               {"segments": [{"name": n} for n in segs]}, profiled_text=""))
    check("integration: the boilerplate chunk never reaches the packet", "spanned 52 weeks" not in text and china_ok[:40] in text)


# ======================================================================
# 5. the planner only asks what the disclosures can answer
# ======================================================================
def _records(*names):
    out = []
    for n in names:
        for r in edgar.parse_tables(_html(n)):
            out.append(({"form": "8-K", "name": n, "filing_date": "2026-07-31"}, r))
    return out


def test_profile_marks_each_kpi_disclosed_or_not():
    aapl = {"segments": [{"name": "iPhone"}, {"name": "Services"}],
            "company_kpis": [{"name": "iPhone net sales"}, {"name": "Installed base of active devices"},
                             {"name": "Services gross margin percentage"}, {"name": "Unit sales"}]}
    marked = profiler.mark_disclosure(aapl, _records("aapl_release_tables.html", "aapl_10q_margin_tables.html"),
                                      "The Company no longer reports unit sales. iPhone net sales were $54,252 million.")
    got = {k["name"]: k["disclosed"] for k in marked["company_kpis"]}
    check(f"AAPL-shaped disclosure map ({got})", got == {"iPhone net sales": True, "Installed base of active devices": False,
                                                         "Services gross margin percentage": True, "Unit sales": False})
    check("the undisclosed metrics are listed on the profile", set(marked["undisclosed_metrics"]) == {"Installed base of active devices", "Unit sales"})
    synth = {"segments": [{"name": "Devices"}], "company_kpis": [{"name": "Units shipped"}, {"name": "Average selling price"}]}
    got2 = {k["name"]: k["disclosed"] for k in profiler.mark_disclosure(synth, _records("synthetic_single_segment_release.html"), "")["company_kpis"]}
    check(f"synthetic: a company that reports units has them disclosed ({got2})", got2 == {"Units shipped": True, "Average selling price": True})


def _plan(question, kpis):
    return json.dumps({"key_questions": [{"id": "Q1", "question": question, "why": "w", "kpis": kpis, "segments": [], "search_queries": ["s"]}]
                      + [{"id": f"Q{i}", "question": f"q{i}?", "why": "w", "kpis": [], "segments": [], "search_queries": ["s"]} for i in range(2, 6)],
                      "what_would_mislead": ["m"], "peer_tickers": ["A", "B", "C", "D"]})


def _planner(monkeypatch, stub_llm, profile, reply):
    from markut import store
    from markut.mcp import client
    monkeypatch.setattr(client, "call_one_tool", lambda name, args: "[QUOTE & VALUATION]\n- Price (current): $10.00  [source: x]")
    monkeypatch.setattr(store, "cache_get", lambda key, db=None: None)
    monkeypatch.setattr(store, "cache_put", lambda key, value, db=None: True)
    calls = stub_llm([reply])
    out = profiler.planner_node({"ticker": "XYZ", "profile": profile, "filing_fingerprint": "F"})
    return calls, out["plan"]["key_questions"][0]


def test_planner_never_requires_an_undisclosed_metric(monkeypatch, stub_llm):
    profile = {"segments": [{"name": "iPhone"}], "undisclosed_metrics": ["Unit sales"],
               "company_kpis": [{"name": "iPhone net sales", "disclosed": True}, {"name": "Unit sales", "disclosed": False}],
               "table_index": [{"label": "iPhone", "group": "Net sales by category", "caption": "", "cells": [{"period": "quarter", "value": "54,252", "entity": ""}]}]}
    calls, q1 = _planner(monkeypatch, stub_llm, profile,
                         _plan("Is iPhone growth driven by unit sales or by average selling price?", ["iPhone net sales", "Unit sales", "Average selling price"]))
    check("the planner is told what is not disclosed", "NOT DISCLOSED" in calls[0]["user"] and "Unit sales" in calls[0]["user"])
    check(f"no undisclosed metric survives in the question's KPIs ({q1['kpis']})", q1["kpis"] == ["iPhone net sales"])
    check(f"the question states the limitation ({q1['question']!r})", "not disclosed" in q1["question"] and "Unit sales" in q1["question"]
          and "Average selling price" in q1["question"])


def test_planner_keeps_a_question_about_disclosed_units(monkeypatch, stub_llm):
    profile = {"segments": [{"name": "Devices"}], "undisclosed_metrics": [],
               "company_kpis": [{"name": "Units shipped", "disclosed": True}, {"name": "Average selling price", "disclosed": True}],
               "table_index": [{"label": "Units shipped (in thousands)", "group": "", "caption": "Key Operating Metrics",
                                "cells": [{"period": "quarter", "value": "1,250", "entity": ""}]},
                               {"label": "Average selling price", "group": "", "caption": "Key Operating Metrics",
                                "cells": [{"period": "quarter", "value": "$94.40", "entity": ""}]}]}
    question = "Is unit growth sustainable as the average selling price falls?"
    _, q1 = _planner(monkeypatch, stub_llm, profile, _plan(question, ["Units shipped", "Average selling price"]))
    check(f"a company that reports units keeps its question unchanged ({q1})",
          q1["question"] == question and q1["kpis"] == ["Units shipped", "Average selling price"])


# ======================================================================
# 6. coverage reflects whether a question can be answered now
# ======================================================================
PRIOR_ONLY = ('- [Q1] "iPhone net sales decreased during 2025 compared to 2024."  [source: EDGAR/10-K Item 7, filed 2025-10-31; '
              'prior fiscal year (10-K filed 2025-10-31); unstated basis; segment iPhone]\n'
              "- KPI value not found: Unit sales (no table row in the latest 8-K exhibits or 10-Q names this metric)\n")
CURRENT_KPI = ("- KPI Services net sales: Services [Net sales]: 30,739 million (quarter, Three Months Ended June 27, 2026) vs 27,423 million "
               "year-ago (June 28, 2025); change +12.1%  (millions; company-defined; Services)  [Q2]  [source: T1]\n")
CURRENT_EXCERPT = ('- [Q3] "Units shipped increased 25% in the third quarter of 2026."  [source: EDGAR/10-Q Item 2 (10-Q MD&A), filed 2026-10-28; '
                   'current period (10-Q filed 2026-10-28); unstated basis; segment unspecified]\n'
                   "- KPI value not found: Installed units in service (no table row in the latest 8-K exhibits or 10-Q names this metric)\n")
PLAN6 = {"key_questions": [{"id": "Q1", "question": "Unit sales vs price?", "kpis": ["Unit sales"]},
                           {"id": "Q2", "question": "Services growth?", "kpis": ["Services net sales"]},
                           {"id": "Q3", "question": "Unit growth?", "kpis": ["Installed units in service"]}]}


def test_prior_year_only_question_is_a_named_gap():
    text = PRIOR_ONLY + CURRENT_KPI + CURRENT_EXCERPT
    kpi_values = {"Unit sales": False, "Services net sales": True, "Installed units in service": False}
    a = questions.coverage_assess(text, PLAN6, kpi_values, None)
    check(f"AAPL-shaped: prior-year excerpts + KPI not found -> not covered ({a['Q1']})", a["Q1"]["covered"] is False and a["Q1"]["missing"] == ["Unit sales"])
    check(f"a current-period KPI value covers its question ({a['Q2']})", a["Q2"]["covered"] is True)
    check(f"synthetic: a current-period excerpt covers its question ({a['Q3']})", a["Q3"]["covered"] is True)
    from markut.evidence import assembly
    block = assembly.coverage_block({q: (1 if v["covered"] else 0) for q, v in a.items()}, PLAN6, 1, [], detail=a)
    check(f"the gap names the missing metric ({block})", "COVERAGE GAP Q1" in block and "Unit sales" in block and "prior-year" in block)
    check("covered questions are not listed as gaps", "COVERAGE GAP Q2" not in block and "COVERAGE GAP Q3" not in block)
    check("no blanket 'every question has sourced evidence' when a gap exists", "none — every question" not in block)


def test_judge_receives_the_coverage_gaps(stub_llm):
    good = json.dumps({"questions": [], "bull_strongest": "b", "bear_strongest": "r", "unsupported_claims": [], "reasoning": "x",
                       "verdict": "v. Not financial advice.", "converged": False, "planner_coverage": "adequate"})
    calls = stub_llm([good])
    evidence = ("[KEY QUESTIONS]\n- Q1: Unit sales vs price?\n\n[COVERAGE GAPS] (questions ...)\n"
                "- COVERAGE GAP Q1: no current-period value for Unit sales; only prior-year excerpts\n\n[DATA GAPS]\n- none")
    nodes.judge_node({"ticker": "XYZ", "evidence": evidence, "bull_case": "b", "bear_case": "r", "bull_history": ["b"],
                      "bear_history": ["r"], "round": 0, "verdict": ""})
    check("the judge's own input lists the coverage gaps", "COVERAGE GAP Q1: no current-period value for Unit sales" in calls[0]["user"])


# ---------------------------------------------------------------- found while checking run #26's real filings (no model calls)
def test_disclosure_needs_a_figure_in_the_same_sentence():
    quote_then_dividend = ("“Our installed base of active devices also reached a new all-time high across all major product "
                           "categories.” The board declared a cash dividend of $0.27 per share. Results covered the third quarter of 2026.")
    check("a figure in the NEXT sentence (after a closing quote) does not disclose the metric",
          profiler.text_disclosed("Installed base of active devices", quote_then_dividend) is not True)
    check("a year is not a figure", profiler.text_disclosed("Installed base of active devices",
                                                            "Our installed base of active devices grew in fiscal 2026.") is not True)
    check("a real figure in the same sentence discloses it (synthetic)",
          profiler.text_disclosed("Installed units in service", "Installed units in service reached 4.2 million at quarter end.") is True)


def test_metrics_named_only_in_the_question_text_are_checked(monkeypatch, stub_llm):
    profile = {"segments": [{"name": "iPhone"}], "undisclosed_metrics": [],
               "company_kpis": [{"name": "iPhone net sales", "disclosed": True}],
               "table_index": [{"label": "iPhone", "group": "Net sales by category", "caption": "", "cells": [{"period": "quarter", "value": "54,252", "entity": ""}]}]}
    _, q1 = _planner(monkeypatch, stub_llm, profile,
                     _plan("Is iPhone growth sustainable, and what do unit versus average selling price trends imply?", ["iPhone net sales"]))
    check(f"AAPL-shaped: undisclosed metrics named in the question text are stated as a limitation ({q1['question']!r})",
          "not disclosed" in q1["question"] and "unit" in q1["question"].split("limitation:")[-1].lower()
          and "average selling price" in q1["question"].split("limitation:")[-1].lower())
    synth = {"segments": [{"name": "Devices"}], "undisclosed_metrics": [],
             "company_kpis": [{"name": "Units shipped", "disclosed": True}],
             "table_index": [{"label": "Units shipped (in thousands)", "group": "", "caption": "Key Operating Metrics", "cells": [{"period": "quarter", "value": "1,250", "entity": ""}]},
                             {"label": "Average selling price", "group": "", "caption": "Key Operating Metrics", "cells": [{"period": "quarter", "value": "$94.40", "entity": ""}]}]}
    question = "What do unit versus average selling price trends imply for revenue?"
    _, q1b = _planner(monkeypatch, stub_llm, synth, _plan(question, ["Units shipped"]))
    check(f"synthetic: the same wording is kept when units and ASP are disclosed ({q1b['question']!r})", q1b["question"] == question)


def test_percentage_row_names_the_row_it_describes():
    html = ("<p>Operating Expenses</p><p>(in millions)</p><table>"
            "<tr><td></td><td>Three Months Ended June 30, 2026</td><td>Three Months Ended June 30, 2025</td></tr>"
            "<tr><td>Research and development</td><td>$</td><td>11,729</td><td>$</td><td>8,866</td></tr>"
            "<tr><td>Percentage of total net sales</td><td>11</td><td>%</td><td>9</td><td>%</td></tr>"
            "<tr><td>Selling, general and administrative</td><td>$</td><td>7,346</td><td>$</td><td>6,650</td></tr>"
            "<tr><td>Percentage of total net sales</td><td>7</td><td>%</td><td>7</td><td>%</td></tr></table>")
    rows = edgar.parse_tables(html)
    pct = [r["label"] for r in rows if "%" in r["cells"][0]["value"]]
    check(f"each 'percentage of' row carries the subject of the row above ({pct})",
          pct == ["Research and development — Percentage of total net sales", "Selling, general and administrative — Percentage of total net sales"])
