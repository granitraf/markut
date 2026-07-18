"""The notebook suite's eval blocks migrated verbatim: same-ruler grading
(ev-a..c), stored-packet drift fix with poisoned live path (df-a..c), and
the LLM-judge scorer contracts (js-a..f). Swaps target module attributes
(markut.agents.llm / markut.mcp.client)."""
import json

import markut.mcp.client as _mcp_client
from markut.agents import llm
from markut.eval.harness import (grade_verdict, build_risk_worksheet,
    select_eval_packet, parse_risk_titles_from_packet,
    build_risk_scoring_prompt, parse_risk_scores, verify_scored_quotes,
    score_risk_coverage)


def check(label, condition):
    assert condition, label


def test_eval_suite_blocks():
    print("\n--- eval helpers: same-ruler grading + risk worksheet (offline) ---")
    try:
        # (a) grading shape + counts: one cited number, one invented, one advice
        # phrase, no disclaimer — the exact fields the grounding table consumes
        _ev_g = grade_verdict("Price is $10 and upside is 12%. Investors should "
                              "buy the stock.", "Price: $10 [source: test].")
        check("ev-a: claims/cited/flagged counted with the same ruler",
              _ev_g["claims"] == 2 and _ev_g["cited"] == 1 and _ev_g["flagged"] == ["12%"])
        check("ev-a: grounding percent from the counts",
              abs(_ev_g["grounding_pct"] - 50.0) < 1e-9)
        check("ev-a: both advice phrases caught, disclaimer absent",
              set(_ev_g["advice"]) == {"investors should", "buy the stock"}
              and _ev_g["disclaimer"] is False)
        # (b) numberless verdict -> vacuously grounded; disclaimer variant True
        _ev_z = grade_verdict("A cautious, qualitative view. Not investment advice.",
                              "Price: $10.")
        check("ev-b: zero claims -> 100% grounded, disclaimer recognized",
              _ev_z["claims"] == 0 and _ev_z["grounding_pct"] == 100.0
              and _ev_z["disclaimer"] is True)
        # (c) worksheet: numbered, both tick-off columns, order preserved
        _ev_w = build_risk_worksheet(["A", "B", "C"]).splitlines()
        _ev_rows = [line for line in _ev_w if "[debate: ] [baseline: ]" in line]
        check("ev-c: three numbered rows, each with both tick-off columns",
              len(_ev_rows) == 3
              and _ev_rows[0].strip().startswith("1.") and _ev_rows[0].endswith("A")
              and _ev_rows[2].strip().startswith("3.") and _ev_rows[2].endswith("C"))
    except Exception as e:
        check(f"eval helper tests (unexpectedly raised: {e})", False)

    print("\n--- drift fix: stored-packet control condition (offline) ---")
    # The live path is POISONED for the whole block: if any helper under test
    # touches it, the stub raises and the checks fail — proof, not promise.
    _df_saved_fetch = _mcp_client.call_evidence_tools
    _df_touched = {"n": 0}
    def _df_poison(*args, **kwargs):
        _df_touched["n"] += 1
        raise AssertionError("live evidence path touched during eval selection")
    _mcp_client.call_evidence_tools = _df_poison
    try:
        # (a) packet selection returns the STORED evidence, never fetches
        check("df-a: select_eval_packet returns the stored packet string",
              select_eval_packet({"evidence": "STORED PACKET TEXT"}) == "STORED PACKET TEXT"
              and select_eval_packet({}) == "" and select_eval_packet(None) == "")
        # (b) the titles parser recovers all 24 captions from a packet fixture
        _df_titles_in = [f"Documented risk number {i}" for i in range(1, 25)]
        _df_fixture = ("Price (current): $202.25 [source: yfinance/info]\n"
                       "-- Risk factor titles --\n"
                       '- "' + " • ".join(_df_titles_in) + ' [...truncated]"\n'
                       "  [source: EDGAR/10-K Item 1A titles, filed 2026-01-01, https://x]\n"
                       "-- Guidance & outlook --\n"
                       '- "Revenue outlook is strong."\n')
        check("df-b: all 24 captions recovered from the stored packet, in order",
              parse_risk_titles_from_packet(_df_fixture) == _df_titles_in)
        # (c) missing/placeholder titles -> [] (the harness then prints its
        # re-run instruction instead of silently fetching)
        check("df-c: no titles block -> empty list",
              parse_risk_titles_from_packet("Price (current): $202.25") == [])
        check("df-c: extraction placeholder instead of a quote line -> empty list",
              parse_risk_titles_from_packet(
                  "-- Risk factor titles --\n[not extracted: no block]\n") == [])
        check("df-c: poisoned live path never touched by any of the above",
              _df_touched["n"] == 0)
    except Exception as e:
        check(f"drift-fix tests (unexpectedly raised: {e})", False)
    finally:
        _mcp_client.call_evidence_tools = _df_saved_fetch

    print("\n--- llm-judge scorer: quotes, fallback, identity withheld (offline) ---")
    try:
        # (a) a tick whose quote is NOT in the verdict -> downgraded, marked
        _js_scores = [{"caption_num": 1, "covered": True,
                       "quote": "this sentence never appears", "reason": ""}]
        _js_v, _js_nf = verify_scored_quotes(_js_scores, "A completely different text.")
        check("js-a: unfindable quote -> tick voided and marked [quote-failed]",
              _js_nf == 1 and _js_v[0]["covered"] is False
              and _js_v[0]["reason"].startswith("[quote-failed]")
              and _js_v[0]["quote"] == "this sentence never appears")  # kept for audit
        # (b) whitespace-normalized matching survives line breaks / double spaces
        _js_scores2 = [{"caption_num": 1, "covered": True,
                        "quote": "dependent on third-party foundries", "reason": ""}]
        _js_v2, _js_nf2 = verify_scored_quotes(
            _js_scores2, "We are heavily\ndependent on   third-party\nfoundries today.")
        check("js-b: quote verifies across line breaks (whitespace-normalized)",
              _js_nf2 == 0 and _js_v2[0]["covered"] is True)
        # (c) double JSON failure -> None; the harness renders blank ticks, and
        # None can fabricate nothing
        _js_saved_claude = llm.call_claude
        _js_calls = {"n": 0}
        def _js_garbage(system_prompt, user_content, max_tokens=1000):
            _js_calls["n"] += 1
            return "not json at all"
        llm.call_claude = _js_garbage
        try:
            _js_out = score_risk_coverage(["Risk A"], "Some verdict text.", "OUTPUT A")
        finally:
            llm.call_claude = _js_saved_claude
        check("js-c: garbage twice -> None after exactly one retry (no ticks invented)",
              _js_out is None and _js_calls["n"] == 2)
        # (d) identity withheld: the scoring scaffold names no system
        _js_sys, _js_user = build_risk_scoring_prompt(
            ["Export controls", "Customer concentration"],
            "A neutral output text about risks.", "OUTPUT B")
        _js_all = (_js_sys + _js_user).lower()
        check("js-d: prompt contains no 'debate'/'baseline' strings",
              "debate" not in _js_all and "baseline" not in _js_all)
        # (e) totals count ONLY verified ticks: two claimed, one bogus quote
        _js_scores3 = [{"caption_num": 1, "covered": True, "quote": "real sentence here",
                        "reason": ""},
                       {"caption_num": 2, "covered": True, "quote": "fabricated quote",
                        "reason": ""}]
        _js_v3, _ = verify_scored_quotes(_js_scores3, "There is a real sentence here.")
        check("js-e: totals count only quote-verified ticks",
              sum(1 for s in _js_v3 if s["covered"]) == 1)
        # (f) stored-packet path end-to-end: captions parsed from a fixture
        # evidence string feed the prompt builder as a numbered list
        _js_fixture = ("Price (current): $202.25 [source: yfinance/info]\n"
                       "-- Risk factor titles --\n"
                       '- "Export controls • Customer concentration • Foundry capacity"\n'
                       "  [source: EDGAR/10-K Item 1A titles, filed 2026-01-01, https://x]\n")
        _js_caps = parse_risk_titles_from_packet(_js_fixture)
        _js_sys2, _js_user2 = build_risk_scoring_prompt(_js_caps, "text", "OUTPUT A")
        check("js-f: captions from the stored packet reach the prompt, numbered",
              _js_caps == ["Export controls", "Customer concentration", "Foundry capacity"]
              and "1. Export controls" in _js_user2
              and "3. Foundry capacity" in _js_user2)
    except Exception as e:
        check(f"llm-judge scorer tests (unexpectedly raised: {e})", False)
