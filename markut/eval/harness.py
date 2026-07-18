"""Step 6 baseline-comparison harness (notebook cells 36/62, verbatim
bodies): same-ruler grading, LLM-judge recall scorer, stored-packet
control. CLI: python -m markut.eval NVDA (runs a full debate first —
paid calls)."""
import difflib
import json
import re
import textwrap

from markut import config
from markut.agents import llm, prompts
from markut.guardrails.tracer import (extract_numeric_claims, trace_claim,
    find_advice_language, has_disclaimer)


# ---------------- Eval helpers: same-ruler grading + risk worksheet ----------------
# WHY pure functions in their own cell BEFORE the offline suite: the baseline
# comparison (notebook tail) grades BOTH systems' verdicts with the SAME
# deterministic claim layer — these are the graders, and the suite below
# proves them offline for free.


def grade_verdict(verdict: str, evidence: str) -> dict:
    # Same ruler for debate and baseline: every numeric claim traced against
    # the evidence packet, advice language and disclaimer checked. No model
    # involved — the grading itself cannot hallucinate.
    claims = extract_numeric_claims(verdict)
    flagged = [c for c in claims if trace_claim(c, evidence) == "FLAGGED"]
    cited = len(claims) - len(flagged)
    return {
        "claims": len(claims),
        "cited": cited,
        "flagged": flagged,
        # WHY 100 on zero claims: no numbers means no UNGROUNDED numbers —
        # vacuously grounded (a numberless verdict is its own kind of finding,
        # visible in the claims column).
        "grounding_pct": (100.0 * cited / len(claims)) if claims else 100.0,
        "advice": find_advice_language(verdict),
        "disclaimer": has_disclaimer(verdict),
    }


def build_risk_worksheet(titles: list) -> str:
    # WHY the scoring is MANUAL: risk recall means reading both verdicts and
    # judging whether each documented risk is genuinely addressed — semantic
    # matching by code is a second research project. This checklist is the
    # scoring instrument; the human pass is the scoring step.
    lines = ["--- RISK RECALL WORKSHEET (management's own Item 1A captions) ---",
             "Tick each risk an output genuinely covers; fill the summary line below."]
    for i, title in enumerate(titles, 1):
        lines.append(f"{i:3d}. [debate: ] [baseline: ]  {title}")
    return "\n".join(lines)


def select_eval_packet(state) -> str:
    # SINGLE SOURCE OF TRUTH for the whole experiment: the packet the debate
    # actually received, stored in state by research_node. WHY never re-fetch:
    # the market moves intraday — a fresh fetch at eval time produced a packet
    # whose price differed from the debate's ($202.25 vs $202.14), so the
    # debate's own verbatim numbers graded as untraced. Byte-identical
    # evidence is the control condition; this function has no live path.
    return (state or {}).get("evidence") or ""


def parse_risk_titles_from_packet(evidence: str) -> list:
    # Recover management's Item 1A captions from the STORED packet text — the
    # deterministic "-- Risk factor titles --" block travels inside the
    # evidence, so the worksheet checklist is exactly what both systems saw.
    # Format (from format_theme_block): a header line, then ONE quote line
    #   - "Title1 • Title2 • ..."
    # possibly carrying an inline [...truncated] marker; extraction failures
    # render a bracketed placeholder instead of a quote line -> return [].
    lines = (evidence or "").splitlines()
    for i, line in enumerate(lines):
        if line.strip() != "-- Risk factor titles --":
            continue
        for follow in lines[i + 1:]:
            follow = follow.strip()
            if follow.startswith('- "'):
                text = follow[3:].rstrip('"')
                text = text.replace("[...truncated]", "").strip().rstrip('"')
                return [t.strip() for t in text.split(" • ") if t.strip()]
            if follow.startswith("--") or follow.startswith("["):
                break  # next block, or a [not extracted ...] placeholder
        break
    return []


# ---------------- LLM-judge risk-recall scorer (identity withheld) ----------------
# WHY an LLM judge: risk-recall is a READING judgment (does this text engage
# that risk's substance?) that code cannot make — but the judge's claims are
# kept honest mechanically: every tick needs a verbatim quote that is checked
# by substring, and the human audit has the final word.

_RISK_SCORE_SKELETON = (
    '[\n'
    '  {"caption_num": 1, "covered": true,\n'
    '   "quote": "<one sentence copied VERBATIM from the output>", "reason": ""},\n'
    '  {"caption_num": 2, "covered": false, "quote": "",\n'
    '   "reason": "no candidate passage"}\n'
    ']'
)


def build_risk_scoring_prompt(captions: list, verdict_text: str, label: str):
    # PURE. One output at a time, identity withheld: the scorer sees the
    # captions and ONE text under a neutral label — never the evidence
    # packet, never the other output, and this scaffold never names which
    # system wrote the text. (The texts can still self-identify by style;
    # the report acknowledges that as a limitation — "identity withheld",
    # not "blinding".)
    system_prompt = (
        "You grade risk coverage. For each numbered risk caption, decide "
        "whether the OUTPUT genuinely engages that risk's SUBSTANCE — its "
        "mechanism or its impact — not mere keyword brushing. When in "
        "doubt, mark it NOT covered.\n"
        "Example, covered: the caption concerns supplier dependency and the "
        "output says 'entirely dependent on third-party foundries' — that "
        "engages the mechanism.\n"
        "Example, NOT covered: the output merely mentions the word 'supply' "
        "in passing.\n"
        'Rules: "covered": true REQUIRES "quote" — ONE sentence copied '
        "VERBATIM from the output (it is checked mechanically; a quote that "
        'is not found voids the tick). "covered": false REQUIRES "reason" — '
        'one short clause ("no candidate passage", or why the closest '
        "passage is insufficient).\n"
        "Respond with ONLY a JSON array, one object per caption, every "
        "caption number present exactly once, mirroring this structure:\n"
        + _RISK_SCORE_SKELETON
    )
    user_content = (
        "RISK CAPTIONS:\n"
        + "\n".join(f"{i}. {c}" for i, c in enumerate(captions, 1))
        + f"\n\n{label}:\n{verdict_text}"
    )
    return system_prompt, user_content


def parse_risk_scores(text: str, n: int) -> list:
    # Fence-strip + strict validation (style-mirror of parse_judge_json /
    # parse_review_json). Every rejection message is SPECIFIC because it
    # feeds the corrective retry — a model can only fix what it is told.
    cleaned = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()
    data = json.loads(cleaned)
    if not isinstance(data, list):
        raise ValueError(f"scores must be a JSON array (got {type(data).__name__})")
    items = {}
    for item in data:
        if not isinstance(item, dict):
            raise ValueError(f"each score must be a JSON object (got {type(item).__name__})")
        num = item.get("caption_num")
        if not isinstance(num, int) or not (1 <= num <= n):
            raise ValueError(f"caption_num must be an integer 1..{n} (got {num!r})")
        if num in items:
            raise ValueError(f"caption_num {num} appears more than once")
        item["covered"] = bool(item.get("covered"))
        item["quote"] = str(item.get("quote") or "").strip()
        item["reason"] = str(item.get("reason") or "").strip()
        if item["covered"] and not item["quote"]:
            raise ValueError(f"caption {num} is covered but has no quote")
        if not item["covered"] and not item["reason"]:
            raise ValueError(f"caption {num} is not covered but has no reason")
        items[num] = item
    missing = [i for i in range(1, n + 1) if i not in items]
    if missing:
        raise ValueError(f"missing caption numbers: {missing}")
    return [items[i] for i in range(1, n + 1)]


def verify_scored_quotes(scores: list, verdict_text: str):
    # Mechanical: whitespace-normalized substring. WHY this only proves the
    # quote EXISTS, not that it SUFFICES — a real sentence can still fail to
    # address the risk; the human audit closes that gap. A failed quote is
    # KEPT in the record so the audit section can show what the judge
    # claimed, but the tick is voided with a [quote-failed] marker.
    haystack = " ".join((verdict_text or "").split())
    n_failed = 0
    for item in scores:
        if item["covered"] and " ".join(item["quote"].split()) not in haystack:
            item["covered"] = False
            item["reason"] = "[quote-failed] quote not found verbatim in the output"
            n_failed += 1
    return scores, n_failed


def score_risk_coverage(captions: list, verdict_text: str, label: str):
    # ONE scoring call + ONE corrective retry (specific error + previous
    # reply + skeleton — the pattern that fixed the revision path), then
    # fail to None: the harness falls back to the blank manual worksheet for
    # this output. NEVER fabricate ticks. max_tokens=4000 because two dozen
    # quote-bearing JSON objects is a long reply.
    system_prompt, user_content = build_risk_scoring_prompt(captions, verdict_text, label)
    raw = llm.call_claude(system_prompt, user_content, max_tokens=4000)
    try:
        scores = parse_risk_scores(raw, len(captions))
    except Exception as first_error:
        print(f"SCORER: {label} reply invalid ({first_error}); one corrective retry.")
        retry_content = (
            user_content
            + "\n\nYour previous response FAILED validation with this exact error:\n  "
            + str(first_error)
            + "\n\nYour previous response was:\n" + raw
            + "\n\nFix exactly that problem. Respond again with ONLY the JSON "
              "array, every caption number present exactly once:\n"
            + _RISK_SCORE_SKELETON
        )
        try:
            scores = parse_risk_scores(
                llm.call_claude(system_prompt, retry_content, max_tokens=4000), len(captions))
        except Exception as second_error:
            print(f"SCORER: {label} retry also failed ({second_error}) — this "
                  "output falls back to the manual worksheet; no ticks are invented.")
            return None
    scores, n_failed = verify_scored_quotes(scores, verdict_text)
    if n_failed:
        print(f"SCORER: {label} — {n_failed} tick(s) auto-downgraded [quote-failed]")
    return scores


def run_baseline_comparison(final_state):
    """The harness cell's gated logic as a function. Caller supplies a
    COMPLETED debate final_state (verdict + review_report + evidence —
    the stored packet is the control condition)."""
    if (not final_state or not final_state.get("review_report")
            or not final_state.get("evidence")):
        raise ValueError("run_baseline_comparison needs a completed debate "
                         "final_state (verdict, review_report, evidence)")
    # Debate-side tokens: the run cell reset the accumulator right before
    # app.invoke, so input+output now = the debate plus the acceptance
    # cell's one revision if it ran. WHY the upper bound is fair: it can
    # only OVERSTATE the debate's cost, never flatter it.
    _eval_debate_tokens = llm.TOKENS["input"] + llm.TOKENS["output"]

    # CONTROL CONDITION — byte-identical BY CONSTRUCTION: the baseline,
    # the grading ruler, and the worksheet all consume the packet the
    # debate RUN stored in state. WHY never re-fetch here: the first
    # harness design called the MCP path fresh at eval time, the market
    # moved intraday ($202.25 in the debate's packet vs $202.14 in the
    # fresh one), and grading both verdicts against the fresh packet made
    # the debate's own verbatim numbers flag as untraced. Zero fetch also
    # removes this cell's EDGAR/news network dependency.
    _eval_packet = select_eval_packet(final_state)
    _eval_price = re.search(r"Price \(current\):\s*(\$[\d.,]+)", _eval_packet)
    print("CONTROL: both systems evaluated against the stored packet "
          f"(price {_eval_price.group(1) if _eval_price else 'line not found'}, "
          f"{len(_eval_packet):,} chars)")

    # The baseline: one call, a genuine best effort — fair fight, not a
    # strawman. Same evidence, same output budget as the judge (2000).
    _eval_before = llm.TOKENS["input"] + llm.TOKENS["output"]
    print("EVAL: one baseline call (same evidence, judge-sized budget)...")
    _eval_baseline_verdict = llm.call_claude(
        prompts.BASELINE_SYSTEM_PROMPT,
        f"EVIDENCE PACKET for NVDA:\n{_eval_packet}\n\nYour verdict:",
        max_tokens=2000)
    _eval_baseline_tokens = llm.TOKENS["input"] + llm.TOKENS["output"] - _eval_before

    # Grade BOTH final outputs with the SAME deterministic ruler.
    _eval_dg = grade_verdict(final_state["verdict"], _eval_packet)
    _eval_bg = grade_verdict(_eval_baseline_verdict, _eval_packet)

    print("\n--- DEBATE VERDICT (governed) ---\n")
    print(textwrap.fill(final_state["verdict"], 100))
    print("\n--- BASELINE VERDICT (single call, same evidence) ---\n")
    print(textwrap.fill(_eval_baseline_verdict, 100))

    _eval_rr = final_state["review_report"]
    _eval_res = _eval_rr.get("resolutions", [])
    print("\n--- GROUNDING TABLE (same deterministic ruler on both rows) ---")
    _eval_fmt = "{:<10} {:>7} {:>6} {:>12} {:>7} {:>11} {:>10}"
    print(_eval_fmt.format("system", "claims", "cited", "grounding %",
                           "advice", "disclaimer", "tokens"))
    for _eval_name, _eval_g, _eval_tok in (
            ("debate", _eval_dg, _eval_debate_tokens),
            ("baseline", _eval_bg, _eval_baseline_tokens)):
        print(_eval_fmt.format(_eval_name, _eval_g["claims"], _eval_g["cited"],
                               f"{_eval_g['grounding_pct']:.0f}%",
                               len(_eval_g["advice"]),
                               "yes" if _eval_g["disclaimer"] else "NO",
                               f"{_eval_tok:,}"))
    print("debate context: final_status=" + str(_eval_rr.get("final_status"))
          + " | derived="
          + str(sum(1 for r in _eval_res if r.get("resolution") == "DERIVED"
                    and r.get("qualified", True)))
          + " labeled="
          + str(sum(1 for r in _eval_res if r.get("resolution") == "LABELED"
                    and r.get("qualified", True)))
          + " | the raw ruler flags DERIVED restatements on the debate side too")
    print("token caveat: debate row = accumulator since the run cell's reset"
          " (includes the acceptance cell's one revision if it ran) — an upper bound")

    # Risk-recall scoring on the captions from the SAME stored packet.
    # The scorer sees ONE output at a time under a neutral label —
    # identity withheld, unblinded only in the table below. Every tick
    # was quote-verified mechanically; the human audit is still the
    # final word.
    _eval_titles = parse_risk_titles_from_packet(_eval_packet)
    _eval_ds = None
    _eval_bs = None
    if not _eval_titles:
        print("\n[worksheet not built: no 'Risk factor titles' block in the "
              "stored packet — re-run the Step 5 debate to refresh "
              "final_state, then rerun this cell]")
    else:
        _eval_sc_before = llm.TOKENS["input"] + llm.TOKENS["output"]
        print("\nEVAL: scoring risk coverage — 2 scorer calls (identity withheld)...")
        _eval_ds = score_risk_coverage(_eval_titles, final_state["verdict"], "OUTPUT A")
        _eval_bs = score_risk_coverage(_eval_titles, _eval_baseline_verdict, "OUTPUT B")
        print(f"SCORER: both outputs scored, "
              f"{llm.TOKENS['input'] + llm.TOKENS['output'] - _eval_sc_before:,} tokens "
              "(includes any corrective retries)")

        # Unblinding happens HERE, in the rendering — the scorer never
        # saw a system name. A side whose scoring failed twice renders
        # BLANK tick boxes (its column is the manual worksheet again).
        print("\n--- RISK RECALL (LLM-judged, quote-verified, AUDIT REQUIRED) ---")
        _eval_audit = []
        for _eval_i, _eval_cap in enumerate(_eval_titles, 1):
            _eval_ticks = []
            for _eval_side, _eval_scores in (("debate", _eval_ds), ("baseline", _eval_bs)):
                if _eval_scores is None:
                    _eval_ticks.append(f"[{_eval_side}:  ]")
                else:
                    _eval_ticks.append(
                        f"[{_eval_side}: {'Y' if _eval_scores[_eval_i - 1]['covered'] else 'N'}]")
            print(f"{_eval_i:3d}. {' '.join(_eval_ticks)}  {_eval_cap[:60]}")
            for _eval_tag, _eval_scores in (("D", _eval_ds), ("B", _eval_bs)):
                if _eval_scores is None:
                    continue
                _eval_item = _eval_scores[_eval_i - 1]
                if _eval_item["covered"]:
                    print(f'       {_eval_tag}: "{_eval_item["quote"][:120]}"')
                else:
                    print(f"       {_eval_tag}: {_eval_item['reason'][:120]}")
                if _eval_item["reason"].startswith("[quote-failed]"):
                    _eval_audit.append((_eval_i, f"{_eval_tag}: [quote-failed] — judge's "
                                                 f"claimed quote shown above"))
            if (_eval_ds is not None and _eval_bs is not None
                    and _eval_ds[_eval_i - 1]["covered"] != _eval_bs[_eval_i - 1]["covered"]):
                _eval_audit.append((_eval_i, "systems disagree"))

        print("\n--- AUDIT SECTION — review these first ---")
        if _eval_audit:
            for _eval_i, _eval_why in _eval_audit:
                print(f"  caption {_eval_i}: {_eval_why}")
        else:
            print("  (no quote failures, no disagreements — still spot-check the quotes)")
        for _eval_side, _eval_scores in (("debate", _eval_ds), ("baseline", _eval_bs)):
            if _eval_scores is None:
                print(f"  NOTE: {_eval_side} scoring failed twice — its column is "
                      "blank; tick it manually")

    _eval_n = len(_eval_titles)
    _eval_dt = sum(1 for s in _eval_ds if s["covered"]) if _eval_ds is not None else None
    _eval_bt = sum(1 for s in _eval_bs if s["covered"]) if _eval_bs is not None else None
    _eval_mult = (_eval_debate_tokens / _eval_baseline_tokens
                  if _eval_baseline_tokens else 0.0)
    print(f"\nSUMMARY: Debate {'__' if _eval_dt is None else _eval_dt}/{_eval_n} vs "
          f"Baseline {'__' if _eval_bt is None else _eval_bt}/{_eval_n} documented "
          "risks (LLM-judged, author-audited); grounding "
          f"{_eval_dg['grounding_pct']:.0f}% vs {_eval_bg['grounding_pct']:.0f}%; "
          f"~{_eval_mult:.1f}x token cost")
    print("AUDIT REQUIRED: check quotes, veto unsupported ticks, adjust totals, "
          "paste the final scored table into the Step 6 markdown.")
