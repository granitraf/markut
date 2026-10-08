"""Output-layer governor: review_node (notebook nodes-cell tail, verbatim;
prompts hoisted, llm module-qualified for test patching)."""
import re

from markut import config
from markut.agents import llm
from markut.agents import prompts
from markut.agents.prompts import REVIEW_JSON_SKELETON
from markut.agents.schemas import REVIEW_SCHEMA
from markut.agents.state import DebateState
from markut.guardrails.parsing import parse_review_json
from markut.guardrails.tracer import (DISCLAIMER_TEXT, _as_float, check_scenarios,
    extract_numeric_claims, find_advice_language, find_mislabeled, has_disclaimer,
    normalize_number, recompute_derived, trace_claim)


def _close(a, b, pct: bool) -> bool:
    # tolerant numeric equality for derived numbers: 0.15 percentage points for
    # percents (41.3 vs the exact 41.38), 0.5% relative otherwise
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        return False  # ranges carry a string implied value — never compared here
    return abs(a - b) <= (0.15 if pct else max(0.005 * abs(b), 0.01))


# our own inline tags; numbers INSIDE them are never claims to re-trace
_TAG_RE = re.compile(r"\s?\[(?:UNGROUNDED|MISLABELED|MISCOMPUTED|SCENARIO CHECK)[^\]]*\]")


def _sub_outside_tags(pattern, repl, text: str, count: int = 0) -> tuple:
    # like pattern.subn but a match that starts inside an existing tag is left alone
    spans = [(m.start(), m.end()) for m in _TAG_RE.finditer(text)]
    done = 0
    def guard(m):
        nonlocal done
        if any(a <= m.start() < b for a, b in spans) or (count and done >= count):
            return m.group(0)
        done += 1
        return repl(m) if callable(repl) else m.expand(repl)
    out = pattern.sub(guard, text)
    return out, done


def stamp_findings(text: str, mislabeled: list, scenarios: list) -> tuple:
    """Deterministic inline tags for what the revision did not fix.
    MISLABELED goes right after the number, inside its own sentence; SCENARIO
    CHECK is appended to the sentence that holds the arithmetic. Returns
    (text, count)."""
    count = 0
    for f in mislabeled:
        sentence = f.get("sentence") or ""
        if sentence and sentence in text:
            tagged = re.sub(r"(?<![\d.\-–])" + re.escape(f["claim"]) + r"(?!\s*\[(?:UNGROUNDED|MISLABELED|MISCOMPUTED))",
                            f["claim"] + " [MISLABELED — " + f["reason"] + "]", sentence, count=1)
            if tagged != sentence:
                text = text.replace(sentence, tagged, 1)
                count += 1
    for f in scenarios:
        sentence = f.get("sentence") or ""
        if sentence and sentence in text and "[SCENARIO CHECK" not in sentence:
            text = text.replace(sentence, sentence + " [SCENARIO CHECK — " + f["reason"] + "]", 1)
            count += 1
    return text, count


def _claim_sentences(text: str) -> list:
    from markut.guardrails.tracer import claim_contexts
    return claim_contexts(text)


def review_node(state: DebateState) -> dict:
    """OUTPUT-LAYER GUARDRAIL (layer 3 of 3) — the graph's TERMINAL node.
    Deterministic extract+trace decides WHAT is ungrounded; one bounded model
    revision decides HOW each flagged number is resolved (prove the arithmetic,
    replace, or label as judgment); anything still unresolved is ANNOTATED
    inline. Annotation over censorship: the reader always sees the claim AND
    its epistemic status."""
    print("REVIEW: tracing verdict claims against the evidence packet")
    verdict = state["verdict"] or ""
    evidence = state["evidence"] or ""
    # Execution-layer bookkeeping lands here: conditional-edge functions
    # cannot write state in LangGraph, so the budget flag is RECORDED by the
    # node every exit path is guaranteed to reach.
    budget_exceeded = llm.TOKENS["input"] + llm.TOKENS["output"] >= config.TOKEN_BUDGET

    claims = extract_numeric_claims(verdict)
    flagged = [c for c in claims if trace_claim(c, evidence) == "FLAGGED"]
    advice = find_advice_language(verdict)
    # AUDIT FIX (run #2): presence is not truth — numbers that trace under the
    # WRONG period/basis, and multiple×EPS scenarios whose direction the
    # arithmetic contradicts, are findings too (deterministic, offline).
    mislabeled = find_mislabeled(verdict, evidence)
    scenarios = check_scenarios(verdict, evidence)
    for f in mislabeled:
        # keep the sentence so the stamper can place the tag inside it
        f.setdefault("sentence", next((c["sentence"] for c in _claim_sentences(verdict) if c["claim"] == f["claim"]), ""))
    cited_count = len(claims) - len(flagged)
    report = {"initial_flags": list(flagged), "advice_phrases": list(advice),
              "mislabeled": [dict(f) for f in mislabeled], "scenario_flags": [dict(f) for f in scenarios],
              "miscomputed": [], "resolutions": [], "final_status": "clean", "revision_used": False}

    if not flagged and not advice and not mislabeled and not scenarios and has_disclaimer(verdict):
        print(f"REVIEW STATS: claims={len(claims)} cited={cited_count} flagged=0 | "
              f"derived=0 labeled=0 annotated=0 | mislabeled=0 scenario=0 miscomputed=0 | status=clean"
              + (" | BUDGET EXCEEDED" if budget_exceeded else ""))
        return {"verdict": verdict, "review_report": report,
                "budget_exceeded": budget_exceeded}

    print(f"REVIEW: {len(flagged)} flagged claim(s), {len(advice)} advice phrase(s), "
          f"{len(mislabeled)} mislabeled, {len(scenarios)} scenario contradiction(s), "
          f"disclaimer {'present' if has_disclaimer(verdict) else 'absent'} — one revision call")
    report["revision_used"] = True
    # WHY the judge must PROVE derivations: trace_claim deliberately flags
    # every number without a literal evidence anchor, including derivable
    # ranges. Code cannot referee arithmetic; the model can — but only by
    # SHOWING it, with the anchors restated inline where a reader can check.
    # WHY the prompt ends with a literal skeleton instead of prose: models
    # mirror examples far more reliably than they follow shape descriptions —
    # a live run returned dict-shaped "resolutions" twice under the old
    # prose-only instruction. The skeleton's anchors are BARE evidence
    # numbers because _resolution_excuses traces each anchor string with
    # trace_claim: descriptive anchors like "$244.85 DCF" would fail to
    # trace and void the excuse.
    # The skeleton is a NAMED string because the corrective retry must show
    # it AGAIN — a model that just failed needs the target shape in front of
    # it, not a memory test.
    json_skeleton = prompts.REVIEW_JSON_SKELETON
    system_prompt = prompts.REVIEW_SYSTEM_PROMPT
    user_content = (
        f"YOUR VERDICT:\n{verdict}\n\n"
        "FLAGGED NUMBERS (no direct evidence anchor found):\n"
        + ("\n".join(f"- {c}" for c in flagged) or "- (none)")
        + "\n\nADVICE-LIKE PHRASES TO REWRITE:\n"
        + ("\n".join(f"- {p}" for p in advice) or "- (none)")
        + "\n\nMISLABELED NUMBERS (the number exists in the evidence but under a different period or "
          "accounting basis than your sentence states — restate it with the evidence's own label, or REVISE it):\n"
        + ("\n".join(f"- {f['reason']}" for f in mislabeled) or "- (none)")
        + "\n\nSCENARIO ARITHMETIC CHECK (recomputed from your own numbers — the stated direction does not "
          "follow; fix the direction, use a different multiple from the evidence, or drop the scenario):\n"
        + ("\n".join(f"- {f['reason']}" for f in scenarios) or "- (none)")
        + f"\n\nEVIDENCE PACKET (the only permissible source of numbers):\n{evidence}"
    )

    revision = None
    raw = llm.call_claude(system_prompt, user_content, max_tokens=2000, schema=REVIEW_SCHEMA)
    try:
        revision = parse_review_json(raw)
    except Exception as first_error:
        print(f"REVIEW: revision JSON invalid ({first_error}); one corrective retry.")
        # WHY: diagnose from evidence, not guesses — the raw reply shows the
        # exact shape the model returned.
        print("REVIEW DEBUG raw revision reply:", raw[:400])
        # WHY the retry restates error + reply + skeleton: a generic "not
        # valid JSON" complaint gives the model nothing to fix — it must see
        # WHAT failed, what it SAID, and the exact shape to mirror.
        retry_content = (
            user_content
            + "\n\nYour previous response FAILED validation with this exact error:\n  "
            + str(first_error)
            + "\n\nYour previous response was:\n" + raw
            + "\n\nFix exactly that problem. Respond again with ONLY this JSON "
              'structure — "resolutions" MUST be a JSON array (list), one '
              "object per flagged claim, even if there is only one:\n"
            + json_skeleton
        )
        raw_retry = ""  # pre-set so the except can print it even if call_claude raises
        try:
            raw_retry = llm.call_claude(system_prompt, retry_content, max_tokens=2000, schema=REVIEW_SCHEMA)
            revision = parse_review_json(raw_retry)
        except Exception as second_error:
            print(f"REVIEW: retry also failed ({second_error}); failing CLOSED — "
                  "keeping the original verdict and annotating inline.")
            print("REVIEW DEBUG raw retry reply:", raw_retry[:400])

    annotated_count = 0
    miscomputed = []
    if revision is None:
        # FAIL CLOSED: original verdict, every flagged claim annotated inline.
        revised = verdict
        for claim in flagged:
            revised = revised.replace(claim, f"{claim} [UNGROUNDED — no evidence anchor]")
            annotated_count += 1
        revised, extra = stamp_findings(revised, mislabeled, scenarios)
        annotated_count += extra
        report["final_status"] = "annotated"
    else:
        revised = revision["verdict"]
        report["resolutions"] = revision["resolutions"]

        def _resolution_excuses(res):
            # A LABELED number may stay (it is marked as judgment). A DERIVED
            # number may stay ONLY if every anchor it cites actually traces to
            # the evidence AND the arithmetic reproduces the number (AUDIT FIX
            # run #2: code now referees the arithmetic — one step over the
            # anchors, with rounding slack). Anchors that trace but do not
            # produce the number make it MISCOMPUTED, tagged with the implied
            # value. REVISED never excuses: the replacement must trace on its
            # own, or it gets annotated below.
            if res.get("resolution") == "LABELED":
                return True
            if res.get("resolution") == "DERIVED":
                anchors = res.get("anchors") or []
                if not anchors or not all(trace_claim(str(a), evidence) == "CITED" for a in anchors):
                    return False
                check = recompute_derived(res.get("claim", ""), anchors)
                res["recomputed"] = check
                if not check["ok"] and check["implied"] is not None:
                    miscomputed.append(res)
                return check["ok"]
            return False

        # WHY three coverage tiers instead of a claim-string whitelist: the
        # annotation layer must AGREE with the resolutions record. UNGROUNDED
        # is reserved for numbers with NO anchor story at all — a number the
        # revision itself just DERIVED (the restated range, its per-leg
        # values) sits in a sentence that already shows its anchors and its
        # DERIVED/LABELED marker, so tagging it would contradict the report.
        # Anchor strictness is UNCHANGED: a DERIVED resolution with even one
        # untraceable anchor qualifies nothing (checked in _resolution_excuses).
        # Every resolution is stamped with a recorded "qualified" flag — the
        # report JSON, the exemption pass, and the stats line below all read
        # THIS one judgment, so text and record can never disagree about
        # which resolutions actually excused numbers.
        qualifying = []
        for r in revision["resolutions"]:
            r["qualified"] = _resolution_excuses(r)
            if r["qualified"]:
                qualifying.append(r)

        def _claim_numbers(res):
            # The record's claim may carry extra words ("genuine 20-50% upside
            # potential") while the verdict says just "20-50%" — match on the
            # claim's NUMBERS, pulled by the same extractor used everywhere
            # else, so surface wording can't break the exemption.
            return (extract_numeric_claims(str(res.get("claim", "")))
                    or [str(res.get("claim", "")).strip()])

        def _norm_or_none(value):
            # ranges ("25-54%") don't normalize to one value — return None
            # instead of raising so a failed comparison just means "not covered"
            try:
                return normalize_number(str(value))
            except Exception:
                return None

        covered_values = {_norm_or_none(a) for r in qualifying
                          for a in _claim_numbers(r) + (r.get("anchors") or [])}
        covered_values.discard(None)
        # A sentence containing a qualifying claim IS that claim's disclosure:
        # the inline anchors and the resolution marker live there, so the
        # sentence's other numbers are the claim's own arithmetic.
        covered_sentences = [
            # split only where a NEW sentence starts (capital, digit, $ or
            # parenthesis) so abbreviations like "i.e. " cannot break a
            # disclosure sentence in half
            sentence for sentence in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9$(])", revised)
            if any(num.lower() in sentence.lower()
                   for r in qualifying for num in _claim_numbers(r))]

        def _covered(number):
            # tier 1+2: the number IS a qualifying claim or one of its cited
            # anchors; tier 1b (AUDIT FIX): it is numerically the SAME derived
            # quantity within rounding — the record said "41%" while the verdict
            # says "41.3%" and the anchors imply 41.38%, so all three are one
            # number; tier 3: it sits in a qualifying claim's sentence.
            # Anything ambiguous falls through to tagging — over-tagging is
            # the safe direction.
            if _norm_or_none(number) in covered_values:
                return True
            value, pct = _as_float(number), str(number).strip().endswith("%")
            for r in qualifying:
                if r.get("resolution") != "DERIVED":
                    continue
                implied = (r.get("recomputed") or {}).get("implied")
                if _close(value, implied, pct):
                    return True
                if any(_close(value, _as_float(c), pct) and (str(c).strip().endswith("%") == pct)
                       for c in _claim_numbers(r)):
                    return True
            return any(number in sentence for sentence in covered_sentences)

        # WHY re-trace and stop: ONE bounded revision, never a loop. Whatever
        # is still unanchored and not covered by a qualifying resolution gets
        # the inline annotation — visible, not deleted.
        # 1. deterministic re-checks on the CLEAN revised text (before any stamp
        #    exists, so sentence lookups and number extraction see no tag text)
        still_mis = find_mislabeled(revised, evidence)
        for f in still_mis:
            f["sentence"] = next((c["sentence"] for c in _claim_sentences(revised) if c["claim"] == f["claim"]), "")
        still_sc = check_scenarios(revised, evidence)
        report["mislabeled_after"] = [dict(f) for f in still_mis]
        report["scenario_flags_after"] = [dict(f) for f in still_sc]
        # 2. which numbers still need an UNGROUNDED tag — decided on clean text,
        #    excluding numbers a MISCOMPUTED resolution already explains
        to_tag = [claim for claim in extract_numeric_claims(revised)
                  if trace_claim(claim, evidence) == "FLAGGED" and not _covered(claim)
                  and not any(_close(_as_float(claim), _as_float(c), str(claim).strip().endswith("%"))
                              for r in miscomputed for c in _claim_numbers(r))]
        # 3. stamps, each pass blind to the tags the previous one wrote
        revised, extra = stamp_findings(revised, still_mis, still_sc)
        annotated_count += extra
        for res in miscomputed:
            implied = res["recomputed"]["implied"]
            unit = "%" if str(res.get("claim", "")).strip().endswith("%") else ""
            shown = f"{implied:g}{unit}" if isinstance(implied, (int, float)) else str(implied)
            for c in _claim_numbers(res):
                pat = re.compile(r"(?<![\d.\-–])" + re.escape(c) + r"(?!\s*\[(?:UNGROUNDED|MISLABELED|MISCOMPUTED))")
                revised, n = _sub_outside_tags(pat, lambda m, c=c, shown=shown, res=res:
                                               f"{c} [MISCOMPUTED — anchors {', '.join(res['anchors'])} imply {shown}]", revised, count=1)
                annotated_count += n
        report["miscomputed"] = [{"claim": r.get("claim"), "anchors": r.get("anchors"),
                                  "implied": r["recomputed"]["implied"], "formula": r["recomputed"]["formula"]}
                                 for r in miscomputed]
        if to_tag:
            # ONE regex pass instead of str.replace per claim: replace hit ALL
            # substring occurrences, so tagging "54%" also stamped inside
            # "25-54%" and the range then collected a second, adjacent tag
            # (defect D2). Longest-first alternation stamps a range before its
            # endpoints; the lookbehind stops an endpoint from matching INSIDE
            # a range; the lookahead never re-tags an already tagged spot.
            tag_pattern = re.compile(
                r"(?<![\d.\-–])("
                + "|".join(re.escape(c) for c in sorted(to_tag, key=len, reverse=True))
                + r")(?!\s*\[UNGROUNDED)")
            revised, n = _sub_outside_tags(tag_pattern, lambda m: m.group(1) + " [UNGROUNDED — no evidence anchor]", revised)
            annotated_count += n
        report["final_status"] = "annotated" if annotated_count else "revised"

    if not has_disclaimer(revised):
        # Deterministic append — a mandatory disclaimer is not something to
        # request from a model and hope.
        revised += DISCLAIMER_TEXT

    # Counted from QUALIFIED resolutions only — the same judgment the
    # annotation pass used, so the printed stats describe what actually
    # happened to the text (fail-closed leaves the list empty -> both 0).
    derived_n = sum(1 for r in report["resolutions"]
                    if r.get("qualified") and r.get("resolution") == "DERIVED")
    labeled_n = sum(1 for r in report["resolutions"]
                    if r.get("qualified") and r.get("resolution") == "LABELED")
    mis_n = len(report.get("mislabeled_after", report["mislabeled"]))
    sc_n = len(report.get("scenario_flags_after", report["scenario_flags"]))
    print(f"REVIEW STATS: claims={len(claims)} cited={cited_count} "
          f"flagged={len(flagged)} | derived={derived_n} labeled={labeled_n} "
          f"annotated={annotated_count} | mislabeled={mis_n} scenario={sc_n} "
          f"miscomputed={len(report['miscomputed'])} | status={report['final_status']}"
          + (" | BUDGET EXCEEDED" if budget_exceeded else ""))
    return {"verdict": revised, "review_report": report,
            "budget_exceeded": budget_exceeded}
