"""Understand-the-company-before-researching iteration: graph snapshot,
profiler / planner / coverage gate, question-driven research tagging,
generic guidance and note extraction, the governor identifier fixes, claim
review dedupe and verdicts, FCF from quarters, the analyst-range grid, turn
ids and the per-node token log. Offline; the two stored runs (AVGO #10,
CAKE #11) are the regression fixtures."""
import json
import os

import pytest

from markut import config, store
from markut.agents import graph as graph_mod
from markut.agents import llm, nodes, profiler
from markut.evidence import assembly, edgar, market, questions, sections, valuation
from markut.evidence.rag import passage_around
from markut.guardrails import tracer

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
RUN10 = json.load(open(os.path.join(FIX, "avgo_run10_2026-10-08.json"), encoding="utf-8"))["events"]
RUN11 = json.load(open(os.path.join(FIX, "cake_run11_2026-10-08.json"), encoding="utf-8"))["events"]


def _ev(events, name, last=False):
    hits = [e["data"] for e in events if e["event"] == name]
    return hits[-1] if last else hits[0]


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


# ---------------------------------------------------------------- graph
def test_graph_snapshot():
    app = graph_mod.build_graph()
    g = app.get_graph()
    nodes_ = sorted(n for n in g.nodes if n not in ("__start__", "__end__"))
    edges = sorted({(e.source, e.target, bool(e.conditional)) for e in g.edges})
    snap = {"nodes": nodes_, "edges": [list(e) for e in edges]}
    path = os.path.join(FIX, "graph_snapshot.json")
    if not os.path.exists(path):
        json.dump(snap, open(path, "w"), indent=1)
    assert snap == json.load(open(path)), "graph topology changed — ask before changing it, then refresh the snapshot"
    assert nodes_ == ["bear", "bull", "judge", "news_verify", "planner", "profiler", "research", "review"]
    assert ["research", "research", True] in snap["edges"] and ["research", "bull", True] in snap["edges"]
    assert ["__start__", "profiler", False] in snap["edges"] and ["planner", "research", False] in snap["edges"]


def test_coverage_gate_routes_back_once_then_proceeds():
    full = {"coverage": {"Q1": 2, "Q2": 1, "Q3": 0, "Q4": 3, "Q5": 1}, "research_pass": 1}
    assert graph_mod.coverage_decision(full)["decision"] == "debate"          # 4 of 5 covered is enough
    thin = {"coverage": {"Q1": 2, "Q2": 0, "Q3": 0, "Q4": 3, "Q5": 1}, "research_pass": 1}
    d = graph_mod.coverage_decision(thin)
    assert d["decision"] == "research" and d["uncovered"] == ["Q2", "Q3"] and graph_mod.coverage_route(thin) == "research"
    assert graph_mod.coverage_decision({**thin, "research_pass": 2})["decision"] == "debate"   # only once
    assert graph_mod.coverage_decision({"coverage": {}, "research_pass": 1})["decision"] == "debate"


# ---------------------------------------------------------------- profiler / planner
def test_profile_check_and_cache_hit(monkeypatch, stub_llm):
    secs = [{"kind": "segment", "id": "a:Segment note", "name": "n", "text": "t", "form": "10-Q", "filing_date": "d", "url": "u"}]
    assert profiler.profile_check({"archetype": "software", "segments": [{"name": "x"}], "company_kpis": [1, 2, 3]}, secs) == []
    probs = profiler.profile_check({"archetype": "", "segments": [], "company_kpis": [1]}, secs)
    assert len(probs) == 3 and any("segments empty" in p for p in probs)
    # cache hit: no extraction, no model call
    monkeypatch.setattr(sections, "filing_set", lambda t: {"ticker": t, "fingerprint": "A|B|C", "10-K": None, "10-Q": None, "8-K": None})
    monkeypatch.setattr(sections, "profile_sections", lambda fs: (_ for _ in ()).throw(AssertionError("extraction ran on a cache hit")))
    monkeypatch.setattr(store, "cache_get", lambda key, db=None: {"profile": {"archetype": "bank", "business": "b"}, "gaps": [], "profiled_text": "P", "sources": ["s"]}
                        if key == "profile:XYZ:A|B|C" else None)
    calls = stub_llm([])
    out = profiler.profiler_node({"ticker": "XYZ"})
    assert out["profile"]["archetype"] == "bank" and out["profile_cached"] is True and out["profiled_text"] == "P" and calls == []


def test_profiler_retries_once_then_records_a_gap(monkeypatch, stub_llm):
    monkeypatch.setattr(sections, "filing_set", lambda t: {"ticker": t, "fingerprint": "F", "10-K": None, "10-Q": None, "8-K": None})
    monkeypatch.setattr(sections, "profile_sections", lambda fs: [
        {"kind": "segment", "id": "acc:Segment note", "name": "10-Q segment note", "text": "Segment A | $10 | $8", "form": "10-Q", "filing_date": "2026-08-01", "url": "u"}])
    saved = {}
    monkeypatch.setattr(store, "cache_get", lambda key, db=None: None)
    monkeypatch.setattr(store, "cache_put", lambda key, value, db=None: saved.setdefault(key, value) or True)
    thin = json.dumps({"business": "b", "archetype": "industrial", "segments": [], "company_kpis": [], "accounting_flags": []})
    calls = stub_llm([thin, thin])
    out = profiler.profiler_node({"ticker": "XYZ"})
    assert len(calls) == 2 and "incomplete" in calls[1]["user"] and calls[0]["schema"]["properties"]["archetype"]["enum"]
    assert any("fewer than 3 KPIs" in g for g in out["profile_gaps"]) and any("segments empty" in g for g in out["profile_gaps"])
    assert "profile:XYZ:F" in saved and "acc:Segment note" in calls[0]["user"]


def test_planner_normalizes_to_exactly_five_questions():
    plan = profiler.normalize_plan({"key_questions": [{"id": "Q9", "question": "a"}, {"id": "Q1", "question": "b"}],
                                    "what_would_mislead": ["x", "y", "z", "w"], "peer_tickers": ["abc", "def"]})
    assert [q["id"] for q in plan["key_questions"]] == ["Q1", "Q2", "Q3", "Q4", "Q5"]
    assert plan["key_questions"][0]["question"] == "a" and "fewer than five" in plan["key_questions"][4]["question"]
    assert plan["what_would_mislead"] == ["x", "y", "z"] and plan["peer_tickers"] == ["ABC", "DEF"]
    summary = profiler.market_summary(_ev(RUN10, "research")["evidence"])
    assert "[QUOTE & VALUATION]" in summary and "Price (current): $376.51" in summary and "Implied price" not in summary


def test_planner_uses_cache_and_market_tool(monkeypatch, stub_llm):
    from markut.mcp import client
    monkeypatch.setattr(client, "call_one_tool", lambda name, args: "[QUOTE & VALUATION]\n- Price (current): $10.00  [source: x]")
    monkeypatch.setattr(store, "cache_get", lambda key, db=None: None)
    monkeypatch.setattr(store, "cache_put", lambda key, value, db=None: True)
    plan = {"key_questions": [{"id": f"Q{i}", "question": f"q{i}", "why": "", "kpis": [], "segments": [], "search_queries": ["s"]} for i in range(1, 6)],
            "what_would_mislead": ["m"], "peer_tickers": ["A", "B", "C", "D"]}
    calls = stub_llm([json.dumps(plan)])
    out = profiler.planner_node({"ticker": "XYZ", "profile": {"archetype": "bank"}, "filing_fingerprint": "F"})
    assert len(calls) == 1 and "PROFILE (JSON)" in calls[0]["user"] and "Price (current): $10.00" in calls[0]["user"]
    assert [q["id"] for q in out["plan"]["key_questions"]] == ["Q1", "Q2", "Q3", "Q4", "Q5"] and out["market_evidence"].startswith("[QUOTE")


# ---------------------------------------------------------------- research: tagging, excerpts, coverage, packet order
def test_excerpt_keeps_figures_whole_and_tags_period_basis_segment():
    long = " ".join(["Context sentence about the business and its markets." for _ in range(12)])
    cut = questions.excerpt(long, max_words=30)
    assert len(cut.split()) <= 30 and cut.endswith(".")
    figure = ("Short lead sentence here. " * 8) + "Revenues increased 7.7% to $1,029.6 million for the fiscal quarter ended June 30, 2026 compared to $955.8 million for the comparable prior year period, primarily due to comparable restaurant sales."
    cut = questions.excerpt(figure, max_words=30)
    assert "$1,029.6 million" in cut and "$955.8 million" in cut            # a sentence with figures is never cut
    assert questions.tag_period("Revenue for the fiscal quarter ended June 30") == "quarter"
    assert questions.tag_period("Our strategy is centered on hospitality") == "unstated"
    assert questions.tag_basis("non-GAAP operating margin of 66%") == "non-GAAP" and questions.tag_basis("x") == "unstated"
    assert questions.tag_segment("North Italia comparable sales rose", ["The Cheesecake Factory", "North Italia"]) == "North Italia"
    assert questions.tag_segment("no brand named", ["North Italia"]) == "unspecified"
    names = questions.segment_names({"segments": [{"name": "A"}, {"name": "Total"}], "company_kpis": [{"segment": "B"}, {"segment": "company-wide"}]})
    assert names == ["A", "B", "company-wide"]


def test_coverage_counts_only_sourced_lines_and_packet_order():
    filings = ("[EVIDENCE Q1] q\n- [Q1] \"a figure 5%\"  [source: EDGAR/10-Q Item 2, filed 2026-08-03; period: quarter; basis: unstated; segment: unspecified]\n"
               "- [Q1] Risk: title\n\n[EVIDENCE Q2] q\n- [Q2] could not find: q (best match 0.20 below the 0.35 floor; pass 1)\n")
    cov = questions.coverage_from_text(filings, ["Q1", "Q2", "Q3"])
    assert cov == {"Q1": 1, "Q2": 0, "Q3": 0}
    plan = {"key_questions": [{"id": "Q1", "question": "one", "kpis": ["k"]}, {"id": "Q2", "question": "two"}], "what_would_mislead": ["m1"]}
    profile = {"business": "B.", "archetype": "software", "segments": [{"name": "S", "latest_revenue": "$1B", "latest_yoy": "+5%", "period": "Q2", "source": "id"}],
               "company_kpis": [{"name": "ARR", "definition": "d", "unit": "$", "segment": "company-wide", "source": "id"}], "accounting_flags": ["large_sbc"]}
    packet, gaps = assembly.assemble_packet(profile, plan, filings, "[QUOTE & VALUATION]\n- Price (current): $10.00  [source: yfinance/info]\n[DCF]\n[FMP DCF section unavailable: HTTP 402 plan]",
                                            "[NEWS]\n- item", cov, research_pass=2, profile_gaps=["profile check: x"])
    order = [packet.index(h) for h in ("[PROFILE]", "[KEY QUESTIONS]", "[EVIDENCE Q1]", "[QUOTE & VALUATION]", "[NEWS]", "[WHAT WOULD MISLEAD]", "[COVERAGE GAPS]", "[DATA GAPS]")]
    assert order == sorted(order) and packet.rstrip().endswith("(no licensed free source)")
    assert "- Segment: S — latest revenue $1B +5% YoY (Q2)" in packet and "- KPI: ARR" in packet and "- Archetype: software" in packet
    assert "COVERAGE GAP Q2" in packet and "COVERAGE GAP Q3" in packet and "COVERAGE GAP profile: profile check: x" in packet
    assert gaps and gaps[0]["source"] == "FMP DCF" and "[FMP DCF: unavailable" in packet
    from markut.evidence.gaps import count_gaps
    assert count_gaps(packet) == 1                                   # the block is found at the end too
    assert tracer.packet_labels(packet).get("10") == ["Price (current)"]  # quote lines never bind labels


def test_question_blocks_tag_dedupe_and_read_once():
    # a tiny real collection (local embedder, no network): two questions, one
    # chunk relevant to both -> used once; a chunk the profiler read -> skipped
    import chromadb
    from markut.evidence import rag
    docs = [("Comparable restaurant sales increased 5.8% driven by higher customer traffic of 2.7% and average check growth of 3.1% in the second quarter of fiscal 2026. Pricing contributed most of the check growth.", "10-Q", "Item 2 (10-Q MD&A)"),
            ("Restaurant-level operating margin expanded 120 basis points on labor productivity in the quarter. Commodity inflation was modest.", "10-Q", "Item 2 (10-Q MD&A)"),
            ("Our strategy is centered on menu innovation and hospitality to drive long-term performance. We invest in our people.", "10-Q", "Item 2 (10-Q MD&A)"),
            ("The bakery division produces desserts for third-party customers and international licensees. It operates two facilities.", "10-K", "Item 7")]
    coll = chromadb.Client().create_collection("t_questions", metadata={"hnsw:space": "cosine"})
    vecs = rag.get_embedder().encode([d[0] for d in docs], show_progress_bar=False)
    coll.add(ids=[f"c{i}" for i in range(len(docs))], embeddings=[v.tolist() for v in vecs], documents=[d[0] for d in docs],
             metadatas=[{"form": d[1], "section": d[2], "filing_date": "2026-08-03", "url": "u"} for d in docs])
    plan = {"key_questions": [
        {"id": "Q1", "question": "Is comparable sales growth driven by traffic or pricing?", "search_queries": ["comparable restaurant sales increased driven by customer traffic and average check"]},
        {"id": "Q2", "question": "Are restaurant-level margins expanding?", "search_queries": ["restaurant-level margin expanded on labor productivity", "comparable restaurant sales increased driven by customer traffic"]},
        {"id": "Q3", "question": "What is the dividend policy for shareholders of the holding company?", "search_queries": ["quarterly cash dividend declared per share"]}]}
    lines = questions.question_blocks(coll, plan, {"segments": []}, profiled_text=docs[2][0], relaxed=[])
    text = "\n".join(lines)
    assert text.count("Comparable restaurant sales increased 5.8%") == 1          # one chunk, one use across questions
    assert "menu innovation and hospitality" not in text                            # read-once: the profiler already read it
    assert "; quarter; unstated basis; segment unspecified]" in text
    q3 = text.split("[EVIDENCE Q3]")[1]
    assert "could not find:" in q3 and "[source:" not in q3
    assert questions.coverage_from_text(text, ["Q1", "Q2", "Q3"])["Q3"] == 0


# ---------------------------------------------------------------- sections: tables, guidance, release lines
def test_table_rows_merge_currency_and_stacked_headers():
    from bs4 import BeautifulSoup
    html = ("<table><tr><td></td><td>The</td><td>North</td></tr><tr><td></td><td>Cheesecake Factory</td><td>Italia</td></tr>"
            "<tr><td>Revenues</td><td>$</td><td>729,476</td><td>$</td><td>98,440</td></tr>"
            "<tr><td>Growth</td><td>5.8</td><td>%</td><td>(3)</td><td>%</td></tr></table>")
    rows = edgar.table_rows(BeautifulSoup(html, "html.parser").table)
    assert rows[0] == "The Cheesecake Factory | North Italia" and rows[1] == "Revenues | $729,476 | $98,440" and rows[2] == "Growth | 5.8% | (3)%"
    text = edgar.html_to_text("<p>Intro.</p>" + html + "<p>21</p><p>Table of Contents</p><p>After the table.</p>", keep_tables=True)
    assert "Revenues | $729,476 | $98,440" in text and "Table of Contents" not in text


def test_guidance_from_slides_and_image_only_failure():
    slides = ("Slide 30: Q2 2026 Highlights\nTotal Revenue: $1.0B, up 8% from PY\n\n"
              "Slide 31: 2026 Underlying Key Assumptions\nConsolidated Sales: Approximately $4.0 Billion\nNet Income Margin: Targeting approximately 5% at the stated sales level\n"
              "New Unit Growth: As many as 26 New Restaurant Openings\n\nSlide 33: Market Potential\n$5B revenue growth potential\n")
    out = sections.guidance_lines([{"label": "8-K ex99-2 (slides)", "text": slides, "form": "8-K", "filing_date": "2026-07-28", "url": "u", "kind": "slides", "status": "ok"}])
    texts = [l["text"] for l in out["lines"]]
    assert "Consolidated Sales: Approximately $4.0 Billion" in texts and any("Targeting approximately 5%" in t for t in texts)
    assert any(l["where"].startswith("slide: 2026 Underlying Key Assumptions") for l in out["lines"])
    assert not any("$1.0B, up 8%" in t for t in texts)                            # a results slide is not guidance
    missing = sections.guidance_lines([{"label": "8-K ex99-2 (slides)", "text": "", "form": "8-K", "filing_date": "d", "url": "u",
                                        "kind": "slides", "status": "not transcribed", "slides": 37}])
    assert missing["lines"] == [] and "image-only exhibit with 37 slides" in missing["failures"][0]
    assert sections.trim_tokens("word " * 100, 10).endswith("[...section trimmed to ~10 tokens]")


def test_release_lines_figures_and_quotes():
    text = ("Example Corp Announces Results\n\nRevenue of $29.6 billion for the third quarter, up 86 percent from the prior year period.\n\n"
            "“Demand continues to be very strong,” said the President and CEO. “We expect momentum to continue.”\n\n"
            "Segment | Q3 FY26 | Q3 FY25 | Change\n\nSemiconductor solutions | $20,839 | $9,166 | +127%\n\nInfrastructure software | 8,752 | 6,786 | +29%\n\n"
            "Operating expenses were $3.1 billion.")
    rl = sections.release_lines(text, ["Semiconductor solutions", "Infrastructure software"])
    kinds = {f["segment"]: f for f in rl["figures"]}
    assert "Semiconductor solutions" in kinds and "(columns: Segment | Q3 FY26 | Q3 FY25 | Change)" in kinds["Semiconductor solutions"]["text"]
    assert any(f["text"].startswith("Revenue of $29.6 billion") for f in rl["figures"])
    assert not any("Operating expenses" in f["text"] for f in rl["figures"])
    assert len(rl["quotes"]) == 1 and rl["quotes"][0].startswith("“Demand")


def test_exhibits_detect_image_only_decks(monkeypatch):
    listing = json.dumps({"directory": {"item": [{"name": "ex99-1.htm"}, {"name": "ex99-2.htm"}] + [{"name": f"ex99-2img{i:03d}.jpg"} for i in range(1, 6)]}})
    pages = {"b/index.json": listing, "b/ex99-1.htm": "<html><body><p>" + "Revenue was $1 billion in the quarter. " * 20 + "</p></body></html>",
             "b/ex99-2.htm": "<html><body>" + "".join(f'<img src="ex99-2img{i:03d}.jpg">' for i in range(1, 6)) + "</body></html>"}
    monkeypatch.setattr(sections, "fetch_filing_html", lambda url: pages[url])
    exhs = sections.exhibits_of({"base_url": "b", "filing_date": "2026-07-28", "accession": "acc"})
    assert [e["name"] for e in exhs] == ["ex99-1.htm", "ex99-2.htm"]
    assert exhs[0]["image_only"] is False and exhs[1]["image_only"] is True and len(exhs[1]["images"]) == 5
    monkeypatch.setattr(config, "TRANSCRIBE_IMAGE_EXHIBITS", False)
    monkeypatch.setattr(store, "cache_get", lambda key, db=None: None)
    t = sections.transcribe_exhibit(exhs[1])
    assert t["status"] == "disabled" and t["text"] == "" and t["slides"] == 5


# ---------------------------------------------------------------- governor: identifiers and clause scope
def test_identifiers_are_not_claims_and_clauses_scope_labels():
    assert tracer.extract_numeric_claims("Q4 revenue") == []
    assert tracer.extract_numeric_claims("FY2027 revenue; 2026 revenue; p25 32.8x; the 10-K and 10-Q; 2026-10-27") == ["32.8x"]
    assert tracer.extract_numeric_claims("Q4 revenue guidance of approximately $34.8 billion") == ["$34.8 billion"]
    ev = _ev(RUN10, "research")["evidence"]
    assert tracer.find_mislabeled("The shares trade at 46.37x on trailing TTM GAAP EPS.", ev) == []
    sentence = ("Valuation is mixed: 19.41x on next-FY non-GAAP consensus EPS versus 32.30x on current-FY non-GAAP "
                "consensus and 46.37x on trailing TTM GAAP EPS.")
    assert tracer.find_mislabeled(sentence, ev) == []
    # zero false positives on both stored runs' pre-review verdicts; the real mislabel is still caught
    for events in (RUN10, RUN11):
        packet = _ev(events, "research")["evidence"]
        verdict = _ev(events, "judge", last=True)["verdict"]
        assert tracer.find_mislabeled(verdict, packet) == [], events[0]["data"]["ticker"]
    assert [f["claim"] for f in tracer.find_mislabeled("Revenue grew 85.5% in FY2025.", ev)] == ["85.5%"]


# ---------------------------------------------------------------- claim review
def test_claim_dedupe_merge_and_verdicts(stub_llm, monkeypatch):
    judge = _ev(RUN11, "judge")
    claims = judge["unsupported_claims"]
    assert len(nodes.dedupe_claims(claims + [claims[0], claims[1].upper()])) == len(claims)
    merged = nodes.claims_from_decision({"unsupported_claims": claims[:2], "questions": [{"id": "Q1", "unsupported_claims": [claims[0], "a brand-new claim about margins"]}]})
    assert merged == claims[:2] + ["a brand-new claim about margins"]
    assert passage_around("Lead words. The remaining principal of $69.0 million was repaid in June. Trailing words.", "$69.0 million").startswith("…The remaining principal")
    # a corroborated claim needs EVERY figure found; the review shows the passage and the three verdict names
    from markut.evidence import rag
    def fake_corroborate(ticker, claims, coll=None, max_hits=2):
        out = []
        for c in claims:
            if "$69.0M" in c:
                out.append({"claim": c, "number": "$69.0M", "match": "$69.0 million", "text": "x",
                            "passage": "the Company repaid the remaining $69.0 million principal amount",
                            "metadata": {"form": "8-K", "section": "press release", "filing_date": "2026-07-28", "url": "u"}})
            if "7.7%" in c:
                out.append({"claim": c, "number": "7.7%", "match": "7.7%", "text": "y", "passage": "Revenues increased 7.7%",
                            "metadata": {"form": "10-Q", "section": "Item 2", "filing_date": "2026-08-03", "url": "u2"}})
        return out
    monkeypatch.setattr(rag, "corroborate_claims", fake_corroborate)
    monkeypatch.setattr(nodes, "get_related_leads", lambda claims, t: "")
    review = json.dumps({"claim_reviews": [
        {"claim": "Bull's claim that operating leverage is already hinted by MRQ earnings growth of 23.7% vs revenue growth of 7.7% is inference", "status": "unresolved", "evidence_summary": "s", "sources": []},
        {"claim": "a restatement of the first claim about $69.0M deleveraging that was not submitted", "status": "supported", "evidence_summary": "s", "sources": []}],
        "reasoning": "r", "verdict_changed": False, "revised_verdict": ""})
    calls = stub_llm([review])
    state = {"ticker": "CAKE", "evidence": "packet", "verdict": "v", "judge_decision": {"unsupported_claims": [
        "The $69.0M repayment figure does not appear anywhere in the evidence packet.",
        "Operating leverage is hinted by MRQ earnings growth of 23.7% vs revenue growth of 7.7%: inference.",
        "Bear's 'a few points of cost slippage' is an unquantified assertion."]}}
    out = nodes.news_verify_node(state)
    reviews = out["claim_verification"]["claim_reviews"]
    verdicts = {r["claim"][:20]: r["verdict"] for r in reviews}
    assert verdicts["The $69.0M repayment"] == "flag overturned" and "remaining $69.0 million principal" in reviews[0]["evidence_summary"]
    assert nodes.inferential("Retiring convertibles shows deleveraging: the $69.0M repayment alone does not establish it.")
    assert not nodes.inferential("The $69.0M repayment figure does not appear anywhere in the evidence packet.")
    assert verdicts["Operating leverage i"] == "unresolved"           # 23.7% was not found, so it went to the re-audit
    assert verdicts["Bear's 'a few points"] == "unresolved" and "no ruling" in reviews[-1]["evidence_summary"]
    assert len(reviews) == 3 and len(calls) == 1 and "FILINGS ADDENDUM" in out["evidence"]
    assert nodes.CLAIM_VERDICTS["contradicted"] == "flag upheld"


# ---------------------------------------------------------------- market data
def test_fcf_from_quarters_and_same_eps_field():
    import pandas as pd
    cf = pd.DataFrame({pd.Timestamp(d): {"Operating Cash Flow": o, "Capital Expenditure": -c} for d, o, c in
                       (("2026-07-31", 14.2e9, 0.53e9), ("2026-04-30", 10.49e9, 0.23e9), ("2026-01-31", 8.26e9, 0.25e9), ("2025-10-31", 7.7e9, 0.24e9))})
    f = market.fcf_from_quarterly(cf)
    assert round(f["fcf"] / 1e9, 1) == 39.4 and market.fcf_from_quarterly(cf.iloc[:, :3]) == {}
    info = {"currentPrice": 376.51, "forwardEps": 19.39, "forwardPE": 18.57, "freeCashflow": 30.6e9, "_fcf_quarters": f, "marketCap": 1.8e12}
    lines = "\n".join(market.format_market_lines(info))
    assert "Free cash flow (TTM: last 4 quarters of operating cash flow $40.65B less capex $1.25B" in lines and "$39.40B" in lines
    assert "P/E (forward, price / next-FY consensus non-GAAP EPS): 19.42" in lines     # not the provider's 18.57
    val = "\n".join(valuation.format_valuation_lines(info, {"+1y": {"avg": 19.39, "low": 17.47, "high": 22.44}}, 2026, {}))
    assert "FCF yield (TTM FCF / market cap): 2.19%" in val and "fwd 19.4x" in val
    assert "Implied price, EPS analyst low $17.47" in val and "Implied price, EPS analyst high $22.44" in val
    assert "P/E (forward, on next-FY consensus non-GAAP EPS)" not in lines


# ---------------------------------------------------------------- rounds, turn ids, token log, cache, store text
def test_two_rounds_minimum_and_turn_ids():
    from markut.web import events as ev
    class App:
        def stream(self, state, stream_mode):
            assert state["max_rounds"] == 2
            yield {"research": {"evidence": "e", "coverage": {}, "research_pass": 1}}
    saved = dict(llm.TOKENS)
    try:
        events = list(ev.stream_debate("abc", max_rounds=1, app=App(), check_sec=False))
    finally:
        llm.TOKENS.update(saved)
    assert events[0]["data"]["max_rounds"] == 2 and config.MIN_ROUNDS == 2
    ids = [e["data"]["turn_id"] for e in events]
    assert len(set(ids)) == len(ids) and events[-1]["data"]["by_node"] == {"research": {"calls": 0, "input": 0, "output": 0, "cache_write": 0, "cache_read": 0}}
    assert [e["event"] for e in events] == ["start", "research", "coverage", "done"]


def test_store_cache_roundtrip_and_turn_text(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", None)
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "c.db"))
    assert store.cache_get("k") is None and store.cache_put("k", {"a": [1]}) and store.cache_get("k") == {"a": [1]}
    assert store.cache_put("k", {"a": 2}) and store.cache_get("k") == {"a": 2}
    assert "kv_cache" in "\n".join(store._schema("x.db"))
    txt = store.turn_content({"event": "profiler", "data": {"profile": {"business": "B", "archetype": "reit", "segments": [{"name": "S"}], "company_kpis": [{"name": "FFO"}]}, "cached": True}})
    assert "Archetype: reit" in txt and "KPI: FFO" in txt and "from the cache" in txt
    assert store.turn_content({"event": "coverage", "data": {"covered": 4, "total": 5, "pass": 1, "decision": "debate", "uncovered": ["Q3"]}}).startswith("coverage gate: 4/5")
    judge_txt = store.turn_content({"event": "judge", "data": {"questions": [{"id": "Q1", "answer": "a", "stronger_side": "bull", "confidence": "high"}], "verdict": "v"}})
    assert judge_txt.startswith("Q1: a [bull, high confidence]")


def test_prompts_and_code_carry_no_company_assumptions():
    import glob, re
    from markut.agents import prompts
    banned = re.compile(r"(?i)hyperscaler|\bxpu\b|backstop|business outlook|export control|broadcom|\bavgo\b|\bnvda\b|nvidia|cheesecake|\bcake\b|vmware|customer concentration|google|\btpu\b")
    for name in ("BULL_SYSTEM_PROMPT", "BEAR_SYSTEM_PROMPT", "JUDGE_SYSTEM_PROMPT", "NEWS_VERIFY_SYSTEM_PROMPT", "PROFILER_SYSTEM_PROMPT", "PLANNER_SYSTEM_PROMPT"):
        assert not banned.search(getattr(prompts, name)), name
    code_banned = re.compile(r"(?i)hyperscaler|\bxpu\b|backstop|business outlook|export control|broadcom|\bavgo\b|\bnvda\b|nvidia|cheesecake|\bcake\b|vmware|customer concentration|\btpu\b")
    for path in glob.glob("markut/**/*.py", recursive=True):
        src = open(path, encoding="utf-8").read()
        hit = code_banned.search(src)
        assert not hit, f"{path}: {hit.group(0)}"
