"""P0 — report integrity (run #26 audit). The report must never contradict
itself: (1) status labels come from an explicit mapping and every
structured-JSON call has the same corrective retry; (2) each piece of state
is rendered exactly once; (3) an extracted figure comes from the row it
claims to describe. Offline: no network, no model calls (swap-and-restore
stubs). Fixtures: a real multi-segment release (AAPL tables, an EXAMPLE of
the defect shapes, not a target), the SHOP and CAKE release tables, and a
SYNTHETIC single-segment company that reports units."""
import json
import os
import re

import pytest

from markut.agents import llm, nodes, profiler
from markut.evidence import core, edgar

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def check(label, condition):
    assert condition, label


def _html(name):
    return open(os.path.join(FIX, name), encoding="utf-8").read()


@pytest.fixture
def stub_llm(monkeypatch):
    """Scripted call_claude: returns replies in order, records every call."""
    def install(replies, stop_reasons=None):
        calls, replies, stops = [], list(replies), list(stop_reasons or [])
        def fake(system_prompt, user_content, max_tokens=1000, schema=None, cached_prefix=None):
            calls.append({"system": system_prompt, "user": user_content, "schema": schema, "max_tokens": max_tokens})
            llm.LAST_STOP_REASON = stops.pop(0) if stops else "end_turn"
            return replies.pop(0) if replies else "{}"
        monkeypatch.setattr(llm, "call_claude", fake)
        return calls
    yield install
    llm.LAST_STOP_REASON = "end_turn"


# ======================================================================
# 1. status labels agree with the reasoning; every structured call retries the same way
# ======================================================================
RUN26_REVIEW = {
    # the run #26 shape: the model CONFIRMED the judge's flag ("Causal
    # attribution is not in the evidence") — the old code rendered it
    # "flag overturned" by inferring the label from status by negation
    "claim": "Bull: attributing the 8.7% post-earnings drop to positioning after the YTD run is speculative.",
    "status": "supported", "flag": "upheld",
    "evidence_summary": "The packet records the -8.7% move but no positioning data. Causal attribution is not in the evidence.",
    "sources": []}


def test_claim_flag_labels_are_explicit(stub_llm, monkeypatch):
    check("one explicit label per flag ruling",
          nodes.CLAIM_FLAG_LABELS == {"upheld": "flag upheld", "overturned": "flag overturned", "unresolved": "unresolved"})
    import markut.evidence.rag as rag
    monkeypatch.setattr(nodes, "get_related_leads", lambda claims, t: "")
    monkeypatch.setattr(rag, "corroborate_claims", lambda t, claims, coll=None, max_hits=2: [])
    reviews = [dict(RUN26_REVIEW),
               {"claim": "Bear: the 12% figure is absent from the packet.", "status": "contradicted", "flag": "overturned",
                "evidence_summary": "The 10-Q states 12%.", "sources": []},
               {"claim": "Both: the margin trend is unclear.", "status": "unresolved", "flag": "unresolved",
                "evidence_summary": "", "sources": []}]
    stub_llm([json.dumps({"claim_reviews": reviews, "reasoning": "r", "verdict_changed": False, "revised_verdict": ""})])
    out = nodes.news_verify_node({"ticker": "XYZ", "evidence": "packet", "verdict": "v",
                                  "judge_decision": {"unsupported_claims": [r["claim"] for r in reviews]}})
    got = [(r["flag"], r["verdict"]) for r in out["claim_verification"]["claim_reviews"]]
    check(f"run #26 shape renders 'flag upheld' (got {got[0]})", got[0] == ("upheld", "flag upheld"))
    check("an overturned flag renders 'flag overturned'", got[1] == ("overturned", "flag overturned"))
    check("an unresolved flag renders 'unresolved'", got[2] == ("unresolved", "unresolved"))
    # a legacy record (no flag field) is never relabelled by inference
    from markut import store
    txt = store.turn_content({"event": "news_verify", "data": {"claim_reviews": [{"claim": "c", "status": "supported", "verdict": "flag overturned"}]}})
    check(f"legacy record shows its raw status, not an inferred flag label (got {txt!r})",
          "flag overturned" not in txt and "status supported" in txt)
    js = open(os.path.join(os.path.dirname(__file__), "..", "markut", "web", "static", "debate.js"), encoding="utf-8").read()
    check("page script has no negation mapping from status to a flag label",
          'supported: "flag overturned"' not in js and "FLAG_LABELS" in js)


def _corrective(call_user, bad):
    return "FAILED validation" in call_user and bad in call_user and "JSON" in call_user and "{" in call_user.split(bad)[-1]


def test_every_structured_call_recovers_from_invalid_json(stub_llm, monkeypatch):
    bad = "this is not json"
    # judge
    good_judge = json.dumps({"questions": [], "bull_strongest": "b", "bear_strongest": "r", "unsupported_claims": [],
                             "reasoning": "x", "verdict": "v. Not financial advice.", "converged": False, "planner_coverage": "adequate"})
    calls = stub_llm([bad, good_judge])
    out = nodes.judge_node({"ticker": "XYZ", "evidence": "p", "bull_case": "b", "bear_case": "r",
                            "bull_history": ["b"], "bear_history": ["r"], "round": 0, "verdict": ""})
    check("judge: recovers on retry", out["judge_decision"]["bull_strongest"] == "b" and len(calls) == 2)
    check("judge: retry = error + previous reply + skeleton", _corrective(calls[1]["user"], bad))
    # claim review
    import markut.evidence.rag as rag
    monkeypatch.setattr(nodes, "get_related_leads", lambda claims, t: "")
    monkeypatch.setattr(rag, "corroborate_claims", lambda t, claims, coll=None, max_hits=2: [])
    good_cr = json.dumps({"claim_reviews": [{"claim": "c1", "status": "unresolved", "flag": "unresolved", "evidence_summary": "", "sources": []}],
                          "reasoning": "ok", "verdict_changed": False, "revised_verdict": ""})
    calls = stub_llm([bad, good_cr])
    out = nodes.news_verify_node({"ticker": "XYZ", "evidence": "p", "verdict": "v", "judge_decision": {"unsupported_claims": ["c1"]}})
    check("claim review: recovers on retry", out["claim_verification"]["reasoning"] == "ok" and len(calls) == 2)
    check("claim review: retry = error + previous reply + skeleton", _corrective(calls[1]["user"], bad))
    # profiler
    from markut import store
    from markut.evidence import sections
    monkeypatch.setattr(sections, "filing_set", lambda t: {"ticker": t, "fingerprint": "F", "10-K": None, "10-Q": None, "8-K": None})
    monkeypatch.setattr(sections, "profile_sections", lambda fs: [{"kind": "item1", "id": "a:Item 1", "name": "n", "text": "We sell widgets. Revenue $10.",
                                                                   "form": "10-K", "filing_date": "d", "url": "u"}])
    monkeypatch.setattr(store, "cache_get", lambda key, db=None: None)
    monkeypatch.setattr(store, "cache_put", lambda key, value, db=None: True)
    good_prof = json.dumps({"business": "Sells widgets. Two sentences.", "archetype": "industrial", "segments": [],
                            "company_kpis": [{"name": f"k{i}", "definition": "d", "unit": "u", "segment": "company-wide", "source": "a:Item 1"} for i in range(3)],
                            "accounting_flags": []})
    calls = stub_llm([bad, good_prof])
    out = profiler.profiler_node({"ticker": "XYZ"})
    check("profiler: recovers on retry", out["profile"].get("archetype") == "industrial" and len(calls) == 2)
    check("profiler: retry = error + previous reply + skeleton", _corrective(calls[1]["user"], bad))
    # planner
    from markut.mcp import client
    monkeypatch.setattr(client, "call_one_tool", lambda name, args: "[QUOTE & VALUATION]\n- Price (current): $10.00  [source: x]")
    good_plan = json.dumps({"key_questions": [{"id": f"Q{i}", "question": f"q{i}?", "why": "w", "kpis": [], "segments": [], "search_queries": ["s"]} for i in range(1, 6)],
                            "what_would_mislead": ["m"], "peer_tickers": ["A", "B", "C", "D"]})
    calls = stub_llm([bad, good_plan])
    out = profiler.planner_node({"ticker": "XYZ", "profile": {"archetype": "industrial"}, "filing_fingerprint": "F"})
    check("planner: recovers on retry", out["plan"]["key_questions"][0]["question"] == "q1?" and len(calls) == 2)
    check("planner: retry = error + previous reply + skeleton", _corrective(calls[1]["user"], bad))
    # coverage relevance
    plan = {"key_questions": [{"id": "Q1", "question": "q1?"}]}
    good_cov = json.dumps({"questions": [{"id": "Q1", "answering_lines": [0], "note": ""}]})
    calls = stub_llm([bad, good_cov])
    rel = nodes.assess_relevance(plan, {"Q1": ["- [Q1] \"Revenue rose 5%.\"  [source: x]"]})
    check("coverage relevance: recovers on retry", rel == {"Q1": [0]} and len(calls) == 2)
    check("coverage relevance: retry = error + previous reply + skeleton", _corrective(calls[1]["user"], bad))


def test_structured_retry_helper_fails_closed_after_one_retry(stub_llm):
    from markut.agents import structured
    calls = stub_llm(["nope", "still nope"])
    res = structured.call_structured("sys", "user", {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]},
                                     parse=lambda raw: json.loads(raw), max_tokens=100, label="TEST")
    check("two invalid replies -> value None, exactly two calls, error recorded",
          res["value"] is None and len(calls) == 2 and res["error"] and res["raws"] == ["nope", "still nope"])
    check("skeleton is derived from the schema", '"a": "<string>"' in structured.schema_skeleton(
        {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}))


# ======================================================================
# 2. each piece of state is rendered exactly once
# ======================================================================
CASES = {("bull", 1): "**[Q1] Growth is mix-driven, not a one-quarter fluke.** Product revenue rose 18% to $118,000 thousand.\n\n**[Q2] Margins expanded.** Gross margin rose to 41.7%.",
         ("bear", 1): "**[Q1] The mix thesis rests on last year's text.** Unit growth slowed.\n\n**[Q2] Margin gains are price-led.** ASP fell to $94.40.",
         ("bull", 2): "**[Q1] The Bear's spike claim lacks data.** Units rose 25% to 1,250 thousand.",
         ("bear", 2): "**[Q1] The Bull's own point is the problem.** No ASP trend beyond one quarter."}


def _stub_debate_events(monkeypatch):
    from markut.agents import graph as graph_mod
    from markut.web import events as ev
    monkeypatch.setattr(graph_mod, "profiler_node", lambda s: {"profile": {"archetype": "industrial"}, "profile_gaps": []})
    monkeypatch.setattr(graph_mod, "planner_node", lambda s: {"plan": {"key_questions": [{"id": "Q1", "question": "q"}]}})
    monkeypatch.setattr(graph_mod, "research_node", lambda s: {"evidence": "- Price (current): $10.00  [source: x]", "coverage": {"Q1": 1},
                                                               "research_pass": 1, "uncovered": []})
    monkeypatch.setattr(graph_mod, "bull_node", lambda s: {"bull_case": CASES[("bull", s["round"] + 1)], "bull_history": [CASES[("bull", s["round"] + 1)]]})
    monkeypatch.setattr(graph_mod, "bear_node", lambda s: {"bear_case": CASES[("bear", s["round"] + 1)], "bear_history": [CASES[("bear", s["round"] + 1)]]})
    monkeypatch.setattr(graph_mod, "judge_node", lambda s: {"verdict": "Price is $10.00. Not financial advice.", "converged": False,
                                                            "round": s["round"] + 1, "judge_decision": {"unsupported_claims": []}})
    monkeypatch.setattr(graph_mod, "news_verify_node", lambda s: {"news_checked": True, "targeted_news_evidence": "", "claim_verification": {}})
    saved = dict(llm.TOKENS)
    try:
        events = list(ev.stream_debate("XYZ", max_rounds=2, check_sec=False))
    finally:
        llm.TOKENS.update(saved)
    return events


def test_n_round_run_keeps_history_n_and_renders_each_case_once(monkeypatch):
    from markut.web import events as ev
    events = _stub_debate_events(monkeypatch)
    state = ev.initial_state("XYZ", 2)
    for e in events:
        pass
    # history length: replay the node deltas through the reducer mirror
    from markut.agents import graph as graph_mod
    app = graph_mod.build_graph()
    final = None
    for chunk in app.stream(ev.initial_state("XYZ", 2), stream_mode="updates"):
        for node, update in chunk.items():
            state = ev.apply_update(state, update or {})
    check(f"bull history has exactly N=2 entries (got {len(state['bull_history'])})", len(state["bull_history"]) == 2)
    check(f"bear history has exactly N=2 entries (got {len(state['bear_history'])})", len(state["bear_history"]) == 2)
    pdfium = pytest.importorskip("pypdfium2")
    from markut import report
    pdf = report.build_report({"id": 1, "ticker": "XYZ", "started_at": "2026-10-11 00:00:00", "mode": "cli", "events": events})
    doc = pdfium.PdfDocument(pdf)
    text = re.sub(r"\s+", " ", " ".join(doc[i].get_textpage().get_text_range() for i in range(len(doc))))
    for (side, rnd), body in CASES.items():
        probe = re.sub(r"\*+|\[Q\d\]\s*", "", body.split("\n")[0]).strip()[:40]
        n = text.count(probe)
        check(f"{side} round {rnd} rendered exactly once in the report (found {n}x: {probe!r})", n == 1)


def test_report_renderer_invariant_no_section_twice():
    from markut import report
    events = [{"event": "bull", "data": {"round": 1, "text": CASES[("bull", 1)], "turn_id": "05-bull"}},
              {"event": "bull", "data": {"round": 1, "text": CASES[("bull", 1)], "turn_id": "05-bull"}}]   # the same turn delivered twice
    bodies = report.debate_sections(events)
    check(f"a turn delivered twice yields one section (got {len(bodies)})", len(bodies) == 1)
    flat = [b for _, b in report.debate_sections([{"event": "bull", "data": {"round": 1, "text": CASES[("bull", 1)], "turn_id": "a"}},
                                                    {"event": "bear", "data": {"round": 1, "text": CASES[("bear", 1)], "turn_id": "b"}}])]
    check("no section body appears twice", len(flat) == len(set(flat)))


def test_judge_input_shows_each_case_once(stub_llm):
    good = json.dumps({"questions": [], "bull_strongest": "b", "bear_strongest": "r", "unsupported_claims": [], "reasoning": "x",
                       "verdict": "v. Not financial advice.", "converged": False, "planner_coverage": "adequate"})
    calls = stub_llm([good])
    b1, b2, r1, r2 = CASES[("bull", 1)], CASES[("bull", 2)], CASES[("bear", 1)], CASES[("bear", 2)]
    nodes.judge_node({"ticker": "XYZ", "evidence": "p", "bull_case": b2, "bear_case": r2,
                      "bull_history": [b1, b2], "bear_history": [r1, r2], "round": 1, "verdict": ""})
    u = calls[0]["user"]
    for name, body in (("bull r1", b1), ("bull r2", b2), ("bear r1", r1), ("bear r2", r2)):
        check(f"judge input contains {name} exactly once (got {u.count(body)})", u.count(body) == 1)


# ======================================================================
# 3. an extracted figure comes from the row it describes
# ======================================================================
AAPL_PROFILE = {"segments": [{"name": n} for n in ("Americas", "Europe", "Greater China", "Japan", "Rest of Asia Pacific",
                                                   "iPhone", "Mac", "iPad", "Wearables, Home and Accessories", "Services")],
                "company_kpis": [{"name": n} for n in ("iPhone net sales", "Mac net sales", "iPad net sales", "Services net sales",
                                                      "Greater China net sales", "Diluted earnings per share",
                                                      "Net sales by product/service category", "Net sales by reportable geographic segment",
                                                      "Installed base of active devices", "Services gross margin")]}
SYNTH_PROFILE = {"segments": [{"name": "Devices"}],
                 "company_kpis": [{"name": n} for n in ("Units shipped", "Average selling price", "Gross margin percentage",
                                                        "Diluted EPS", "Devices revenue", "Accessories revenue")]}


def _core(monkeypatch, html_name, profile, date="2026-08-01"):
    monkeypatch.setattr(core, "core_sources", lambda fs: [{"form": "8-K", "name": "ex99-1", "filing_date": date, "url": "u",
                                                           "html": _html(html_name), "text": "", "headline_period": "quarter"}])
    return core.extract_core({}, profile, None)


def _kpi_line(text, name):
    return next((l for l in text.splitlines() if l.startswith(f"- KPI {name}:") or l.startswith(f"- KPI value not found: {name}")), "")


def test_row_groups_come_from_the_rows_actual_section():
    for name in ("aapl_release_tables.html", "synthetic_single_segment_release.html", "shop_release_tables.html", "cake_release_tables.html"):
        rows = edgar.parse_tables(_html(name))
        for r in rows:
            low = r["label"].lower()
            if re.match(r"^(?:gross (?:margin|profit)|operating income|income from operations|net income|income before)", low):
                check(f"{name}: {r['label']!r} is not filed under a cost/expense group (got {r['group']!r})",
                      not re.search(r"(?i)cost|expense", r["group"]))
            check(f"{name}: group heading carries no footnote marker ({r['group']!r})", not re.match(r"^\(\d+\)", r["group"]))
    aapl = edgar.parse_tables(_html("aapl_release_tables.html"))
    seg = [r for r in aapl if r["label"] == "Greater China"]
    check(f"segment rows keep their own section ({seg[0]['group'] if seg else None!r})", seg and seg[0]["group"] == "Net sales by reportable segment")


def test_segment_kpis_resolve_to_their_own_row_or_not_found(monkeypatch):
    t = _core(monkeypatch, "aapl_release_tables.html", AAPL_PROFILE)["text"]
    expect = {"iPhone net sales": "54,252", "Mac net sales": "10,352", "iPad net sales": "6,191",
              "Services net sales": "30,739", "Greater China net sales": "18,816"}
    for kpi, value in expect.items():
        line = _kpi_line(t, kpi)
        check(f"{kpi} resolves to its own row {value} (got {line[:110]!r})", value in line)
        check(f"{kpi} never resolves to the company total", "109,417" not in line)
    for kpi in ("Net sales by product/service category", "Net sales by reportable geographic segment"):
        line = _kpi_line(t, kpi)
        check(f"grouping KPI {kpi!r} is satisfied by the parsed segment lines (got {line[:110]!r})",
              line.startswith(f"- KPI {kpi}:") and "segment lines" in line)
    check("a metric with no row is 'not found'", _kpi_line(t, "Installed base of active devices").startswith("- KPI value not found"))
    sgm = _kpi_line(t, "Services gross margin")
    check(f"a segment KPI with no segment row is 'not found', never the total (got {sgm[:110]!r})",
          sgm.startswith("- KPI value not found") and "50.1" not in sgm)
    eps = _kpi_line(t, "Diluted earnings per share")
    check(f"diluted EPS is per share, never 'million' (got {eps[:140]!r})", "$2.02" in eps and "million" not in eps and "per share" in eps)
    for line in t.splitlines():
        if re.search(r"(?i)\bdiluted\b|per share", line.split(":")[0]):
            check(f"no per-share line carries a scale unit: {line[:100]!r}", not re.search(r"\$\d+\.\d\d (?:million|thousand|billion)", line))
        check(f"no income line is bracketed under an expense group: {line[:80]!r}",
              not re.search(r"(?i)^- (?:Operating income|Net income|Gross (?:margin|profit))[^:]*\[(?:Operating expenses|Cost of)", line))


def test_synthetic_company_units_and_single_segment(monkeypatch):
    out = _core(monkeypatch, "synthetic_single_segment_release.html", SYNTH_PROFILE, "2026-10-28")
    t = out["text"]
    units = _kpi_line(t, "Units shipped")
    check(f"units are units, not dollars (got {units[:140]!r})", "1,250" in units and "thousand units" in units and "$" not in units.split("(")[0])
    asp = _kpi_line(t, "Average selling price")
    check(f"a per-unit price carries no scale word (got {asp[:140]!r})", "$94.40" in asp and "thousand" not in asp.split("vs")[0])
    gm = _kpi_line(t, "Gross margin percentage")
    check(f"a percentage is percent (got {gm[:140]!r})", "41.7%" in gm and "(percent;" in gm)
    eps = _kpi_line(t, "Diluted EPS")
    check(f"EPS from the per-share group is per share (got {eps[:140]!r})", "$1.45" in eps and "per share" in eps and "thousand" not in eps)
    dev = _kpi_line(t, "Devices revenue")
    check(f"single reportable segment: the consolidated row is the segment and says so (got {dev[:160]!r})",
          "120,000" in dev and "single reportable segment" in dev)
    acc = _kpi_line(t, "Accessories revenue")
    check(f"a product line the tables do not show is 'not found' (got {acc[:120]!r})", acc.startswith("- KPI value not found"))
    inc = [l for l in t.splitlines() if l.startswith("- Operating income") or "Income from operations" in l.split(":")[0]]
    check(f"operating income line is not under 'Costs and expenses' ({inc[:1]})", inc and "[Costs and expenses]" not in inc[0])
