"""The notebook wiring-test cell (20 checks) migrated verbatim: fail-closed
review, budget routing, corrective retry, D2 consistency, labeling
regressions. Swaps now target module attributes (markut.agents.llm), the
namespace review_node and should_continue actually read."""
import json

from markut import config
from markut.agents import llm
from markut.agents.graph import should_continue
from markut.guardrails.review import review_node
from markut.guardrails.tracer import has_disclaimer


def wiring_check(label, condition):
    assert condition, label


def test_wiring_suite():
    # Offline wiring tests for the guardrail layers — PASS/FAIL, no network, no
    # model calls (llm.call_claude swapped for a stub and restored in the finally).
    # WHY this cell sits HERE, not in the main offline suite: it exercises
    # review_node, should_continue, and config.TOKEN_BUDGET, which are all defined in
    # LATER cells than the suite — on Run All everything a cell uses must already
    # exist, so these checks live after the RUN CONFIGURATION cell instead.

    print("--- guardrail wiring (offline) ---")
    # (f) review_node fail-closed: garbage JSON twice -> original verdict kept,
    # flagged claims annotated inline, disclaimer appended, status "annotated".
    _w_evidence = "Current price: $195.55 [source: yfinance]. DCF implies +25.2% upside."
    _real_call_claude = llm.call_claude
    def _garbage_claude(system_prompt, user_content, max_tokens=1000, **kw):
        return "not json at all"
    llm.call_claude = _garbage_claude
    try:
        _w_out = review_node({"verdict": "Bears see 30-40% downside. Investors should size positions.",
                              "evidence": _w_evidence})
    finally:
        llm.call_claude = _real_call_claude
    wiring_check("f: fail-closed keeps the original verdict and annotates inline",
                 "30-40% [UNGROUNDED — no evidence anchor]" in _w_out["verdict"])
    wiring_check("f: disclaimer appended deterministically", has_disclaimer(_w_out["verdict"]))
    wiring_check("f: review_report.final_status == 'annotated', revision was attempted",
                 _w_out["review_report"]["final_status"] == "annotated"
                 and _w_out["review_report"]["revision_used"] is True)

    # (g) budget routing: llm.TOKENS forced over config.TOKEN_BUDGET -> should_continue closes
    # the debate through the done path (counters swap-and-restored).
    _w_saved = dict(llm.TOKENS)
    llm.TOKENS["input"], llm.TOKENS["output"] = config.TOKEN_BUDGET, 1
    try:
        _w_route = should_continue({"converged": False, "round": 1, "max_rounds": 99})
    finally:
        llm.TOKENS.update(_w_saved)
    wiring_check("g: over-budget routes to 'done' despite rounds remaining", _w_route == "done")
    wiring_check("g: with counters restored, mid-debate routes 'continue' again",
                 should_continue({"converged": False, "round": 1, "max_rounds": 99}) == "continue")


    # (e) the corrective retry must carry the SPECIFIC validator error, the
    # model's previous reply, and the JSON skeleton. Proven by a stub that fails
    # shape-validation once (string resolutions), CAPTURES what the retry sends,
    # then answers correctly — also proving the node recovers on attempt two.
    _e_calls = []
    _e_bad_reply = '{"verdict": "v", "resolutions": "none"}'
    _e_good_reply = ('{"verdict": "Scenario analysis marks the 30-40% downside range as '
                     'unquantified qualitative judgment. This is investment research, '
                     'not financial advice.", "resolutions": '
                     '[{"claim": "30-40%", "resolution": "LABELED", "anchors": []}]}')
    def _retry_probe_claude(system_prompt, user_content, max_tokens=1000, **kw):
        _e_calls.append(user_content)
        return _e_bad_reply if len(_e_calls) == 1 else _e_good_reply
    llm.call_claude = _retry_probe_claude
    try:
        _e_out = review_node({"verdict": "Bears see 30-40% downside.",
                              "evidence": _w_evidence})
    finally:
        llm.call_claude = _real_call_claude
    _e_retry = _e_calls[1] if len(_e_calls) == 2 else ""
    wiring_check("e: retry names the exact validator error",
                 "review resolutions must be a JSON array of objects (got str)" in _e_retry)
    wiring_check("e: retry shows the model its own previous reply", _e_bad_reply in _e_retry)
    wiring_check("e: retry restates the JSON skeleton",
                 '"resolutions": [' in _e_retry
                 and '"anchors": ["$244.85", "$195.55", "$301.62"]' in _e_retry)
    wiring_check("e: node recovers on the corrected second reply (LABELED, no annotation)",
                 _e_out["review_report"]["final_status"] == "revised"
                 and "[UNGROUNDED" not in _e_out["verdict"])


    # --- labeling-fix regressions: coverage tiers, dedupe, single disclaimer ---
    # One stub per scenario; swap-and-restore around each review_node call.
    _lab_evidence = ("Price: $195.55 [source: yfinance]. DCF fair value: $244.85 vs "
                     "price $195.55 (+25.2%) [source: FMP]. Analyst mean target: "
                     "$301.62. Analyst low target: $180.00.")
    def _lab_run(reply_json, verdict):
        llm.call_claude = lambda system_prompt, user_content, max_tokens=1000, **kw: reply_json
        try:
            return review_node({"verdict": verdict, "evidence": _lab_evidence})
        finally:
            llm.call_claude = _real_call_claude

    # (lab-a) DERIVED with anchors that all trace -> the revision's own arithmetic
    # (restated range, per-leg values) gets NO tag; stats say derived, not annotated.
    # The reply also carries the model-style disclaimer for (lab-d) below.
    _lab_a = _lab_run(
        '{"verdict": "Base-case upside is 20-50% (anchors: DCF fair value $244.85 '
        'vs price $195.55 +25.2%, mean analyst target $301.62 implying 54%), i.e. '
        'a 25-54% anchored span (DERIVED). This report is for research purposes '
        'only and does not constitute investment advice.", '
        '"resolutions": [{"claim": "20-50%", "resolution": "DERIVED", '
        '"anchors": ["$244.85", "$195.55", "$301.62"]}]}',
        "20-50% upside at current valuation. Investors should size positions.")
    wiring_check("lab-a: derived numbers (54%, 25-54%) carry no UNGROUNDED tag",
                 "[UNGROUNDED" not in _lab_a["verdict"])
    wiring_check("lab-a: stats agree — status revised, resolution recorded DERIVED",
                 _lab_a["review_report"]["final_status"] == "revised"
                 and _lab_a["review_report"]["resolutions"][0]["resolution"] == "DERIVED")
    # (lab-d) the model wrote its own disclaimer -> the stapler must NOT add ours
    wiring_check("lab-d: exactly one disclaimer — no deterministic double-append",
                 _lab_a["verdict"].lower().count("does not constitute investment advice") == 1
                 and "educational purposes" not in _lab_a["verdict"].lower())

    # (lab-b) STRICTNESS REGRESSION: a DERIVED resolution citing a computed anchor
    # ("54.2%" is not in the evidence) qualifies NOTHING — the claim stays tagged.
    _lab_b = _lab_run(
        '{"verdict": "Upside of 20-50% is derived (anchors: analyst upside '
        '+54.2%). Not financial advice.", '
        '"resolutions": [{"claim": "20-50%", "resolution": "DERIVED", '
        '"anchors": ["54.2%"]}]}',
        "20-50% upside. Investors should size positions.")
    wiring_check("lab-b: untraceable anchor disqualifies — claim tagged UNGROUNDED",
                 "20-50% [UNGROUNDED" in _lab_b["verdict"]
                 and _lab_b["review_report"]["final_status"] == "annotated")

    # (lab-c) DEDUPE: range + standalone endpoint, nothing covered (REVISED never
    # qualifies) -> range stamped ONCE, endpoint inside it untouched, no adjacent tags.
    _lab_c = _lab_run(
        '{"verdict": "The span is 25-54% while one leg alone is 54%. '
        'Not financial advice.", '
        '"resolutions": [{"claim": "20-50%", "resolution": "REVISED", "anchors": []}]}',
        "20-50% upside. Investors should size positions.")
    wiring_check("lab-c: range tagged exactly once",
                 _lab_c["verdict"].count("25-54% [UNGROUNDED — no evidence anchor]") == 1)
    wiring_check("lab-c: no adjacent duplicate tags anywhere",
                 "anchor] [UNGROUNDED" not in _lab_c["verdict"])
    wiring_check("lab-c: standalone endpoint still tagged on its own",
                 "alone is 54% [UNGROUNDED" in _lab_c["verdict"])

    # (lab-e) BASE CASE: a number outside any resolution still gets tagged.
    _lab_e = _lab_run(
        '{"verdict": "Fair value work implies 12% expected drift. '
        'Not financial advice.", "resolutions": []}',
        "Expect 12% drift. Investors should size positions.")
    wiring_check("lab-e: number with no resolution at all is still tagged",
                 "12% [UNGROUNDED" in _lab_e["verdict"]
                 and _lab_e["review_report"]["final_status"] == "annotated")


    # --- D2 regression: exemption, stats and report share ONE coverage judgment ---
    # Surface drift: the record's claim carries extra words the verdict dropped.
    _d2 = _lab_run(
        '{"verdict": "Base-case upside is a genuine 20-50% upside potential '
        '(DERIVED: anchors $244.85 vs $195.55, target $301.62). '
        'Not financial advice.", '
        '"resolutions": [{"claim": "genuine 20-50% upside potential under '
        'base-case scenarios", "resolution": "DERIVED", '
        '"anchors": ["$244.85", "$195.55", "$301.62"]}]}',
        "20-50% upside. Investors should size positions.")
    wiring_check("d2: claim-number matching survives surface drift — no tag",
                 "[UNGROUNDED" not in _d2["verdict"])
    _d2_q = [r for r in _d2["review_report"]["resolutions"] if r.get("qualified")]
    wiring_check("d2: text and stats agree — one qualified DERIVED, status revised",
                 len(_d2_q) == 1 and _d2_q[0]["resolution"] == "DERIVED"
                 and _d2["review_report"]["final_status"] == "revised")
    # Inverse direction: an unqualified DERIVED must be tagged AND count zero.
    _d2b = _lab_run(
        '{"verdict": "Upside of 20-50% (DERIVED: from +54.2%). Not financial advice.", '
        '"resolutions": [{"claim": "20-50%", "resolution": "DERIVED", "anchors": ["54.2%"]}]}',
        "20-50% upside. Investors should size positions.")
    wiring_check("d2: bogus-anchor DERIVED -> tagged AND recorded qualified=False (agree)",
                 "20-50% [UNGROUNDED" in _d2b["verdict"]
                 and _d2b["review_report"]["resolutions"][0]["qualified"] is False
                 and _d2b["review_report"]["final_status"] == "annotated")
