"""Readable governed verdict (2026-10-11, user request after run #27):
open with the debate's lean on the evidence, show it on a scale with the
analyst consensus as a SEPARATE comparison the agents never saw, keep the
figures' traceability out of the prose (a source list beneath instead), and
keep 'what moves the stock', 'what would change this view' and the next
catalyst as plain sentences. Offline: no network, no model calls (swap-and-
restore stubs). Each rule is checked on an AAPL-shaped example (run #27's own
text and consensus) and a synthetic company of a different shape."""
import re

from markut.agents import prompts, schemas
from markut.evidence import consensus, market
from markut.guardrails import tracer


def check(label, condition):
    assert condition, label


# run #27's governed verdict (opening paragraph): the anchors the governor
# wrote into the prose are what made it read "fact after fact"
RUN27_GOVERNED = (
    "AAPL's June quarter was strong on disclosed figures: revenue +16.4%, operating income +26.6%, growth in every geography, "
    "and a reported gross margin of 50.1% that included a favorable impact of approximately 2 percentage points from tariff refunds, "
    "implying an underlying gross margin of roughly 48.1% (anchors: 50.1% company gross margin, approximately 2 percentage points "
    "tariff refund impact). At a 38.6x trailing GAAP P/E, consensus-EPS scenarios at historical multiples span roughly -17% to +6% "
    "(anchors: p25 29.2x on $9.58 consensus EPS = $279.75 (-16.9%); p75 37.3x = $357.35 (+6.2%); cross-basis, for scale only). "
    "The balance is high quality at a full price.")
AAPL_EVIDENCE = "\n".join([
    "[PROFILE] (what kind of business this is)",
    "- Segment: Americas — latest revenue $45,781 million +11.1% YoY",
    "[CORE FIGURES] (extracted by code)",
    "- Revenue: Total net sales: 109,417 million (quarter) vs 94,036 million year-ago; change +16.4%  (millions; GAAP; company-wide)  [source: T1]",
    "- Operating income: 35,695 million (quarter) vs 28,202 million year-ago; change +26.6%  (millions; GAAP; company-wide)  [source: T1]",
    "- Total gross margin percentage: 50.1% (quarter) vs 46.5% year-ago  (percent; GAAP; company-wide)  [source: T2]",
    "- Diluted EPS: Diluted earnings per share: $2.02 (quarter) vs $1.57 year-ago  (per share; GAAP; company-wide)  [source: T1]",
    "- Services [Gross margin percentage]: 75.6% (quarter) vs 75.6% year-ago; change +0.0 pts  (percent; GAAP; Services)  [Q2]  [source: T4]",
    "- Research and development as a percentage of net sales: 11% vs 9% year-ago; change +2.0 pts  (percent; GAAP; company-wide)  [source: T5]",
    "[EVIDENCE Q4] (gross margin)",
    "- [Q4] \"Company gross margin was 50.1 percent, including a favorable impact of approximately 2 percentage points from tariff refunds.\" "
    "[source: EDGAR/8-K ex99.1, filed 2026-07-30; current period]",
    "[QUOTE & VALUATION]",
    "- P/E (trailing TTM, GAAP EPS): 38.61  [source: yfinance/info]",
    "- Implied price, EPS consensus $9.58: p25 29.2x = $279.75 (-16.9%) | median 34.1x = $326.69 (-3.0%) | p75 37.3x = $357.35 (+6.2%)  [source: computed]",
])


# ======================================================================
# 1. the agents never see analyst opinion about the stock
# ======================================================================
class _FakeTicker:
    def __init__(self, symbol):
        self.info = {"currentPrice": 336.64, "trailingPE": 38.61, "recommendationKey": "buy", "recommendationMean": 2.2,
                     "numberOfAnalystOpinions": 39, "targetMeanPrice": 328.09}
        self.analyst_price_targets = {"mean": 328.09, "high": 400.0, "low": 230.0}
    def __getattr__(self, name):
        raise RuntimeError(f"offline stub: {name}")


def test_packet_holds_no_analyst_ratings_or_price_targets(monkeypatch):
    monkeypatch.setattr(market.yf, "Ticker", _FakeTicker)
    monkeypatch.setattr(market, "fmp_get_json", lambda *a, **k: {"error": "offline"})
    text = market.market_snapshot("AAPL")
    for needle in ("[ANALYST VIEW]", "Price target", "Recommendation", "Analyst count", "328.09"):
        check(f"AAPL-shaped: the packet carries no analyst opinion ({needle!r} found)", needle not in text)
    check("the packet still carries the market data itself", "336.64" in text)


# ======================================================================
# 2. the analyst consensus, read separately from the provider
# ======================================================================
def test_analyst_consensus_from_provider_data():
    rows = [{"period": "0m", "strongBuy": 6, "buy": 19, "hold": 13, "sell": 3, "strongSell": 3}]
    c = consensus.analyst_consensus({"recommendationMean": 2.20455, "recommendationKey": "buy", "numberOfAnalystOpinions": 39,
                                     "currentPrice": 336.64}, rows, {"mean": 328.09, "high": 400.0, "low": 230.0})
    check(f"AAPL-shaped: mean, label, count and spread ({c})",
          c and abs(c["mean"] - 2.2) < 0.01 and c["label"] == "buy" and c["n"] == 39
          and c["counts"] == {"strong_buy": 6, "buy": 19, "hold": 13, "sell": 3, "strong_sell": 3})
    check("price targets and the price ride along for the comparison", c["target_mean"] == 328.09 and c["price"] == 336.64)
    synth = consensus.analyst_consensus({"recommendationMean": 3.7, "numberOfAnalystOpinions": 6}, None, None)
    check(f"synthetic small-cap: no rating spread table — the mean alone still places it ({synth})",
          synth and synth["label"] == "sell" and synth["counts"] is None and synth["n"] == 6)
    check("nothing from the provider -> None, never an invented consensus", consensus.analyst_consensus({}, None, None) is None)


def test_scale_positions_line_up():
    check("strongly bearish sits where strong sell sits", consensus.lean_position("strongly_bearish") == consensus.analyst_position(5.0) == 0.0)
    check("neutral sits on hold", consensus.lean_position("neutral") == consensus.analyst_position(3.0) == 0.5)
    check("strongly bullish sits on strong buy", consensus.lean_position("strongly_bullish") == consensus.analyst_position(1.0) == 1.0)
    check("an unknown lean has no position", consensus.lean_position("undecided") is None)


def test_lean_is_compared_with_analysts_in_plain_words():
    aapl = {"mean": 2.2, "label": "buy", "n": 39}
    s = consensus.compare_lean("bearish", aapl)
    check(f"AAPL-shaped: a bearish lean against a buy consensus is more cautious ({s})",
          "more cautious than" in s and "2.2" in s and "39 analysts" in s)
    s2 = consensus.compare_lean("neutral", {"mean": 3.1, "label": "hold", "n": 6})
    check(f"synthetic: neutral against hold is in line ({s2})", "in line with" in s2)
    s3 = consensus.compare_lean("strongly_bullish", {"mean": 3.7, "label": "sell", "n": 6})
    check(f"synthetic: strongly bullish against sell is much more constructive ({s3})", "much more constructive than" in s3)
    check("no consensus -> says so", "no analyst consensus" in consensus.compare_lean("bullish", None).lower())
    check("no lean -> nothing to compare", consensus.compare_lean(None, aapl) == "")


# ======================================================================
# 3. the judge must take a stance, and write a short readable verdict
# ======================================================================
def test_judge_must_return_a_lean():
    props, req = schemas.JUDGE_SCHEMA["properties"], schemas.JUDGE_SCHEMA["required"]
    check("lean, confidence and reason are required", all(k in req for k in ("lean", "lean_confidence", "lean_reason")))
    check("the lean is one of the five scale positions", props["lean"]["enum"] == consensus.LEANS)
    p = prompts.JUDGE_SYSTEM_PROMPT
    check("neutral needs offsetting points; missing data lowers confidence, not the lean",
          "genuinely offset" in p and "lowers lean_confidence" in p)
    check("the agents are told the packet holds no analyst ratings on purpose", "no analyst ratings or price targets" in p)
    for part in ("**What moves the stock**", "**What would change this view**", "**Next catalyst**", "More bullish if", "More bearish if"):
        check(f"the verdict format names {part!r}", part in p)


# ======================================================================
# 4. traceability out of the prose, into a source list
# ======================================================================
def test_inline_anchors_move_out_of_the_prose():
    clean, moved = tracer.strip_inline_anchors(RUN27_GOVERNED)
    check(f"AAPL-shaped: no '(anchors: …)' left in the prose ({clean[:200]!r})", "anchors:" not in clean)
    check("nested parentheses inside an anchor block go with it", "(-16.9%)" not in clean and "cross-basis" not in clean)
    check("the sentences still read", "implying an underlying gross margin of roughly 48.1%." in clean and "span roughly -17% to +6%." in clean)
    check(f"what was moved is kept for the source list ({moved})", len(moved) == 2 and "50.1% company gross margin" in moved[0])
    synth, moved2 = tracer.strip_inline_anchors("Restaurant margin of 17.2% (anchors: $412.0 / $2,395.0) held. Same-store sales rose 3%.")
    check(f"synthetic: one block, sentence intact ({synth!r})", synth == "Restaurant margin of 17.2% held. Same-store sales rose 3%." and len(moved2) == 1)


def test_every_figure_lists_its_packet_line():
    verdict = "Revenue grew +16.4% and operating income +26.6%; gross margin was 50.1%, roughly 48.1% without refunds. Trailing P/E is 38.61."
    res = [{"claim": "48.1%", "resolution": "DERIVED", "anchors": ["50.1%", "2"], "qualified": True}]
    src = {s["figure"]: s for s in tracer.figure_sources(verdict, AAPL_EVIDENCE, res)}
    check(f"AAPL-shaped: a cited figure points at its core-figures line, with its source tag ({src.get('16.4%')})",
          src.get("16.4%", {}).get("kind") == "cited" and "[source: T1]" in src["16.4%"]["line"] and "Total net sales" in src["16.4%"]["line"])
    check(f"a bare multiple at the END of a sentence is a figure too ({sorted(src)})", "38.61" in src)
    check("a market figure points at its market line", "yfinance/info" in src.get("38.61", {}).get("line", ""))
    check(f"a derived figure lists what it was computed from ({src.get('48.1%')})",
          src.get("48.1%", {}).get("kind") == "derived" and "50.1%" in src["48.1%"]["line"])
    check("model-written profile text is never a figure's source", "Segment: Americas" not in " ".join(s["line"] for s in src.values()))
    check(f"a derivation cites the one line holding all its anchors, never an unrelated $2.02 or +2.0 pts ({src['48.1%']['line']})",
          "approximately 2 percentage points" in src["48.1%"]["line"] and "$2.02" not in src["48.1%"]["line"]
          and "+2.0 pts" not in src["48.1%"]["line"])
    synth_ev = "[QUOTE & VALUATION]\n- Price (current): $41.20  [source: yfinance/info]"
    s2 = tracer.figure_sources("Shares trade at $41.20 and margins of 9.9% look thin.", synth_ev, [])
    kinds = {s["figure"]: s["kind"] for s in s2}
    check(f"synthetic: a figure with no packet line is listed as untraced, not hidden ({kinds})", kinds == {"$41.20": "cited", "9.9%": "untraced"})


def test_governor_keeps_sources_out_of_the_verdict(monkeypatch):
    from markut.guardrails import review
    from markut.agents import llm
    import json
    revised = RUN27_GOVERNED + "\n\nResearch, not investment advice."
    reply = json.dumps({"verdict": revised, "resolutions": [
        {"claim": "48.1%", "resolution": "DERIVED", "anchors": ["50.1%", "2"]},
        {"claim": "-17% to +6%", "resolution": "LABELED", "anchors": []}]})
    monkeypatch.setattr(llm, "call_claude", lambda *a, **k: reply)
    state = {"verdict": "Gross margin was 50.1%, roughly 48.1% underlying; scenarios span -17% to +6%. Research, not investment advice.",
             "evidence": AAPL_EVIDENCE}
    out = review.review_node(state)
    check(f"AAPL-shaped: the governed verdict carries no inline anchors ({out['verdict'][:160]!r})", "anchors:" not in out["verdict"])
    srcs = out["review_report"].get("sources") or []
    check(f"the figures' sources are in the report instead ({[s['figure'] for s in srcs]})",
          any(s["figure"] == "48.1%" and s["kind"] == "derived" for s in srcs) and any(s["kind"] == "cited" for s in srcs))
    p = prompts.REVIEW_SYSTEM_PROMPT
    check("the governor is told not to write anchors into the text and not to add figures",
          "do NOT write them into the verdict" in p and "never add a figure" in p)


# ======================================================================
# 5. the lean and the comparison reach the page and the PDF
# ======================================================================
def test_review_event_carries_the_lean():
    from markut.web import events as ev
    before = {"verdict": "v", "evidence": AAPL_EVIDENCE, "judge_decision": {
        "lean": "bearish", "lean_confidence": "medium", "lean_reason": "The quarter was strong but the price already assumes it lasts."}}
    e = ev.event_for("review", {"verdict": "v", "review_report": {"final_status": "clean"}}, before, before)
    check(f"AAPL-shaped: the review event carries the judge's lean ({e['data'].get('lean')})",
          e["data"].get("lean") == {"lean": "bearish", "confidence": "medium",
                                    "reason": "The quarter was strong but the price already assumes it lasts."})
    legacy = ev.event_for("review", {"verdict": "v", "review_report": {}}, {"verdict": "v", "evidence": "", "judge_decision": {}}, {})
    check("synthetic legacy decision without a lean -> None, never a default", legacy["data"].get("lean") is None)


def test_stream_attaches_consensus_and_comparison():
    from markut.web import events as ev
    from markut.agents import llm

    class App:
        def stream(self, state, stream_mode):
            yield {"judge": {"verdict": "v", "converged": True, "round": 1, "judge_decision": {
                "lean": "bearish", "lean_confidence": "medium", "lean_reason": "r", "questions": []}}}
            yield {"review": {"verdict": "v", "review_report": {"final_status": "clean"}, "budget_exceeded": False}}
    saved = dict(llm.TOKENS)
    try:
        events = list(ev.stream_debate("abc", max_rounds=1, app=App(), check_sec=False,
                                       consensus={"mean": 2.2, "label": "buy", "n": 39}))
    finally:
        llm.TOKENS.update(saved)
    review_ev = next(e for e in events if e["event"] == "review")["data"]
    check(f"the consensus rides on the review event ({review_ev.get('consensus')})", review_ev.get("consensus", {}).get("n") == 39)
    check(f"with the plain-words comparison ({review_ev.get('comparison')!r})", "more cautious than" in (review_ev.get("comparison") or ""))


def _pdf_text(run):
    import pypdfium2 as pdfium
    from markut.report import build_report
    doc = pdfium.PdfDocument(build_report(run))
    return " ".join(doc[i].get_textpage().get_text_range() for i in range(len(doc)))


def test_pdf_shows_lean_scale_comparison_and_sources():
    verdict = ("The business is strong but the price already assumes it lasts.\n\n**What moves the stock**\n"
               "- Whether growth comes from lasting pricing or a one-time cycle.\n\n**What would change this view**\n"
               "- More bullish if unit trends are disclosed.\n- More bearish if margins step down.\n\n**Next catalyst**\n"
               "- Earnings on 2026-11-02.\n\nResearch, not investment advice.")
    review_data = {"verdict": verdict, "stats": {"status": "clean"},
                   "report": {"sources": [{"figure": "+16.4%", "kind": "cited", "line": "Revenue: … change +16.4%  [source: T1]"}]},
                   "lean": {"lean": "bearish", "confidence": "medium", "reason": "The price already assumes the strength lasts."},
                   "consensus": {"mean": 2.2, "label": "buy", "n": 39, "counts": {"strong_buy": 6, "buy": 19, "hold": 13, "sell": 3, "strong_sell": 3},
                                 "target_mean": 328.09, "price": 336.64},
                   "comparison": "The debate leans more cautious than the analyst consensus (2.2, buy, 39 analysts)."}
    run = {"id": 1, "ticker": "AAPL", "started_at": "2026-10-11 01:58", "events": [
        {"event": "start", "data": {"ticker": "AAPL", "max_rounds": 2}}, {"event": "review", "data": review_data}]}
    t = _pdf_text(run)
    for needle in ("Lean: bearish", "medium confidence", "more cautious than the analyst consensus", "strong buy",
                   "What moves the stock", "More bearish if", "Sources for the figures", "[source: T1]"):
        check(f"AAPL-shaped PDF shows {needle!r}", needle in t)
    legacy = {"id": 2, "ticker": "XYZ", "started_at": "2026-10-01", "events": [
        {"event": "start", "data": {"ticker": "XYZ", "max_rounds": 2}}, {"event": "review", "data": {"verdict": "Old verdict text.", "stats": {}}}]}
    lt = _pdf_text(legacy)
    check("synthetic legacy run: renders without a lean block", "Old verdict text." in lt and "Lean:" not in lt)


def test_percentage_point_difference_is_a_valid_derivation():
    # run #27: "underlying gross margin of roughly 48%" = 50.1% minus about 2
    # percentage points of tariff refunds. The referee only knew growth-style
    # formulas for a percent, so the derivation never qualified; with anchors
    # out of the prose it would be tagged untraced.
    r = tracer.recompute_derived("48.1%", ["50.1%", "2"])
    check(f"AAPL-shaped: 50.1% - 2 points = 48.1% ({r})", r["ok"] and abs(r["implied"] - 48.1) < 0.01)
    r2 = tracer.recompute_derived("16.3%", ["17.2%", "0.9"])
    check(f"synthetic restaurant margin: 17.2% - 0.9 points = 16.3% ({r2})", r2["ok"])
    r3 = tracer.recompute_derived("44.0%", ["50.1%", "2"])
    check(f"a wrong difference still fails ({r3})", not r3["ok"])
    r4 = tracer.recompute_derived("26.57%", ["35,695", "28,202"])
    check(f"growth from two amounts still works ({r4})", r4["ok"])


def test_multiples_written_in_words_are_figures():
    # plain-language verdicts write "38.61 times trailing earnings"; with no
    # "x" and no metric name before it, the governor never saw the figure
    c = tracer.extract_numeric_claims("At 38.61 times trailing earnings, the stock trades above its own range.")
    check(f"AAPL-shaped: '38.61 times trailing earnings' is a figure ({c})", "38.61" in c)
    c2 = tracer.extract_numeric_claims("The chain trades at 14 times forward EBITDA and 1.8 times sales, three times its peers.")
    check(f"synthetic: 'N times EBITDA / sales' are figures, a worded 'three times' is not ({c2})", "14" in c2 and "1.8" in c2)


def test_percent_written_as_a_word_traces():
    # releases often write "50.1 percent"; a verdict's "50.1%" must trace to it,
    # while "2 percentage points" is not a 2% figure
    aapl = '- [Q4] "Company gross margin was 50.1 percent, including a favorable impact of approximately 2 percentage points." [source: EDGAR/8-K]'
    check("AAPL-shaped: 50.1% traces to '50.1 percent'", tracer.trace_claim("50.1%", aapl) == "CITED")
    check("'2 percentage points' does not make 2% a cited figure", tracer.trace_claim("2%", aapl) == "FLAGGED")
    synth = '- [Q2] "Comparable restaurant sales increased 3.4 percent versus the prior year." [source: EDGAR/8-K]'
    check("synthetic: 3.4% traces to '3.4 percent'", tracer.trace_claim("3.4%", synth) == "CITED")
