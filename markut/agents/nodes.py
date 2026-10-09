"""Debate agents: research/bull/bear/judge/news_verify (notebook nodes
cell, verbatim; prompts hoisted to markut.agents.prompts, call_claude and
TOKENS module-qualified so tests can patch markut.agents.llm)."""
from markut import config
from markut.agents import llm, prompts
from markut.agents.schemas import CLAIM_REVIEW_SCHEMA, JUDGE_SCHEMA
from markut.agents.state import DebateState
from markut.guardrails.parsing import parse_judge_json, salvage_judge_json
from markut.guardrails.tracer import trim_to_sentence

ADDENDUM_MARK = "\n\n[FILINGS ADDENDUM"


def split_addendum(evidence: str) -> tuple:
    """(base packet, addendum-or-"") — the base is byte-identical for every
    call of a run and is what gets cached; the addendum (added at claim
    review) rides in the user turn so it never breaks the cached prefix."""
    text = evidence or ""
    i = text.find(ADDENDUM_MARK)
    return (text, "") if i < 0 else (text[:i], text[i + 2:])


def evidence_prefix(evidence: str) -> str:
    base, _ = split_addendum(evidence)
    return "EVIDENCE PACKET (the exact source material every agent in this debate sees):\n" + base
from markut.evidence.news import (NEWS_TARGET_MAX_CLAIMS, get_related_leads,
    get_targeted_news_evidence, parse_news_review_json)

EVIDENCE = """
[DEMO ONLY]
- This placeholder evidence is not used by research_node while live FMP data is enabled.
"""


def format_history(state: DebateState) -> str:
    # WHY: the judge decides convergence by asking "are both sides just repeating
    # themselves?". It cannot answer that from only the latest round — it needs to
    # SEE every prior round to spot repetition ACROSS rounds. This zips the two
    # accumulating histories into numbered rounds so the judge reads the debate
    # as a chronological transcript, not two disconnected latest-cases.
    bull = state["bull_history"]
    bear = state["bear_history"]
    rounds = max(len(bull), len(bear))
    if rounds == 0:
        return "(no prior rounds)"
    lines = []
    for i in range(rounds):
        lines.append(f"--- Round {i + 1} ---")
        # Guard indexes: bull and bear lists should stay equal length, but if a
        # round is missing on one side we say so rather than raising IndexError.
        lines.append("BULL: " + (bull[i] if i < len(bull) else "(no bull case this round)"))
        lines.append("BEAR: " + (bear[i] if i < len(bear) else "(no bear case this round)"))
    return "\n\n".join(lines)


def research_node(state: DebateState) -> dict:
    """Question-driven research. The evidence still arrives THROUGH the MCP
    protocol (discover tools, call them by name — swapping a vendor touches
    zero agent code), but the filings tool now takes the planner's questions
    and the packet is composed here in the order every agent reads it:
    profile -> key questions -> evidence under Q1-Q5 -> general evidence ->
    guidance -> valuation and market data -> news -> what would mislead ->
    coverage gaps -> data gaps. A second pass (the coverage gate) reruns the
    tool with a wider net on the uncovered questions only."""
    from markut.mcp.client import gather_evidence
    from markut.evidence.assembly import assemble_packet
    from markut.evidence.questions import coverage_from_text
    ticker = state["ticker"]
    pass_no = (state.get("research_pass") or 0) + 1
    plan, profile = state.get("plan") or {}, state.get("profile") or {}
    qids = [q.get("id") for q in plan.get("key_questions", []) if q.get("id")]
    uncovered = list(state.get("uncovered") or []) if pass_no > 1 else []
    if pass_no == 1:
        print(f"RESEARCH: fetching LIVE data for {ticker} — working through {len(qids)} question(s)")
    else:
        print(f"RESEARCH (pass {pass_no}): wider net on {', '.join(uncovered) or 'nothing'}")
    tool_args = {"filings_evidence_tool": {"plan": plan, "profile": profile,
                                           "profiled_text": state.get("profiled_text") or "",
                                           "relaxed_questions": uncovered}}
    market = state.get("market_evidence") or ""
    results = gather_evidence(ticker, tool_args=tool_args, skip=["market_snapshot_tool"] if market else [])
    market = market or results.get("market_snapshot_tool", "")
    filings = results.get("filings_evidence_tool", "")
    news = results.get("news_evidence_tool", "")
    # a tool this layer does not know yet rides after the news section
    extra = "".join(v for k, v in results.items()
                    if k not in ("market_snapshot_tool", "filings_evidence_tool", "news_evidence_tool"))
    coverage = coverage_from_text(filings, qids)
    packet, gaps = assemble_packet(profile, plan, filings, market, (news or "") + extra, coverage,
                                   research_pass=pass_no, profile_gaps=state.get("profile_gaps") or [],
                                   profile_sources=state.get("profile_sources") or [])
    if gaps:
        print("RESEARCH: data gaps — " + "; ".join(f"{g['source']} ({g['reason']})" for g in gaps))
    print("RESEARCH: coverage " + " ".join(f"{q}:{n}" for q, n in coverage.items()) + f"  (packet {len(packet) // 4:,} tok)")
    return {"evidence": packet, "coverage": coverage, "uncovered": [q for q, n in coverage.items() if not n],
            "research_pass": pass_no, "market_evidence": market}


def complete_argument(system_prompt: str, user_content: str, label: str, cached_prefix: str = None) -> str:
    """One analyst turn that NEVER hands the judge a half-sentence.
    AUDIT FIX (run #2): check stop_reason; on max_tokens regenerate once with
    an explicit word budget and a larger cap; if it still overruns, trim to the
    last complete sentence and say so inline."""
    case = llm.call_claude(system_prompt, user_content, max_tokens=config.ARGUMENT_MAX_TOKENS,
                           cached_prefix=cached_prefix)
    if llm.LAST_STOP_REASON != "max_tokens":
        return case
    print(f"{label}: reply hit the {config.ARGUMENT_MAX_TOKENS}-token cap — regenerating with a "
          f"{config.ARGUMENT_WORD_BUDGET}-word budget")
    budgeted = (user_content + f"\n\nHARD LENGTH LIMIT: the entire case must be under "
                f"{config.ARGUMENT_WORD_BUDGET} words. Make at most 5 points, each one short paragraph. "
                "Finish every sentence — an unfinished case is discarded.")
    case = llm.call_claude(system_prompt, budgeted, max_tokens=config.ARGUMENT_RETRY_MAX_TOKENS,
                           cached_prefix=cached_prefix)
    if llm.LAST_STOP_REASON != "max_tokens":
        return case
    print(f"{label}: still truncated after the retry — trimming to the last complete sentence")
    return trim_to_sentence(case) + "\n\n[argument trimmed to its last complete sentence after exceeding the output cap twice]"


def bull_node(state: DebateState) -> dict:
    round_num = state["round"] + 1
    print(f"BULL  (round {round_num}): building the upside case")
    rebuttal = ""
    if state["bear_case"]:
        rebuttal = (
            f"\n\nThe Bear analyst previously argued:\n{state['bear_case']}\n\n"
            "Directly rebut the Bear's strongest points wherever the evidence allows."
        )
    system_prompt = prompts.BULL_SYSTEM_PROMPT
    user_content = (f"Ticker: {state['ticker']}\n\nThe EVIDENCE PACKET is in your system context above; "
                    f"ground every claim in it.{rebuttal}")
    case = complete_argument(system_prompt, user_content, f"BULL  (round {round_num})",
                             cached_prefix=evidence_prefix(state["evidence"]))
    return {"bull_case": case, "bull_history": [case]}

def bear_node(state: DebateState) -> dict:
    round_num = state["round"] + 1
    print(f"BEAR  (round {round_num}): building the downside case")
    rebuttal = ""
    if state["bull_case"]:
        rebuttal = (
            f"\n\nThe Bull analyst just argued:\n{state['bull_case']}\n\n"
            "Directly rebut the Bull's strongest points wherever the evidence allows."
        )
    system_prompt = prompts.BEAR_SYSTEM_PROMPT
    user_content = (f"Ticker: {state['ticker']}\n\nThe EVIDENCE PACKET is in your system context above; "
                    f"ground every claim in it.{rebuttal}")
    case = complete_argument(system_prompt, user_content, f"BEAR  (round {round_num})",
                             cached_prefix=evidence_prefix(state["evidence"]))
    return {"bear_case": case, "bear_history": [case]}

def judge_node(state: DebateState) -> dict:
    new_round = state["round"] + 1
    print(f"JUDGE (round {new_round}): weighing both sides")
    # WHY: we ask for STRICT JSON (not a free-text verdict ending in "CONVERGED: YES").
    # Structured output means convergence is a real boolean we can trust, instead of
    # brittle string-matching that breaks if the model rephrases the sentinel line.
    #
    # WHY (across rounds): convergence means the DEBATE has stopped producing new
    # ideas, not just that this one round looks balanced. So we define "converged"
    # in terms of repetition ACROSS rounds and feed the judge the full transcript
    # (below) so it can actually judge that, rather than guessing from one round.
    system_prompt = prompts.JUDGE_SYSTEM_PROMPT
    # We give the judge BOTH the full chronological transcript (to detect repetition
    # across rounds) AND the latest cases labeled separately (so the most recent
    # arguments are easy to weigh without hunting through the transcript).
    user_content = (
        f"Ticker: {state['ticker']}\n\n"
        # WHY (grounded arbitration): the judge gets the SAME evidence packet the
        # debaters saw, so it can fact-check both sides' claims against the source
        # material instead of taking the analysts' word for them. The packet now
        # rides in the CACHED system prefix (one write per run, cheap reads), so
        # giving it to the judge every round no longer costs ~3k fresh tokens.
        "The EVIDENCE PACKET (the exact source material both analysts saw) is in your system context above.\n\n"
        f"FULL DEBATE HISTORY (all rounds so far):\n{format_history(state)}\n\n"
        f"Latest Bull case:\n{state['bull_case']}\n\n"
        f"Latest Bear case:\n{state['bear_case']}\n\n"
        f"Rounds completed so far (including this one): {new_round}"
    )
    prefix = evidence_prefix(state["evidence"])

    # AUDIT FIX (run #2): the schema makes the SHAPE a guarantee; the parser
    # still checks substance. RUN #6 FIX: a reply cut off at the output cap is
    # not malformed JSON to "repair" — it is too long. So: a larger cap
    # (config.JUDGE_MAX_TOKENS), length limits in the prompt, and on
    # max_tokens a SHORTER retry; the JSON-repair retry is kept for genuinely
    # invalid replies. If both fail, salvage the completed fields and SAY the
    # reply was truncated — never pass raw JSON on as the verdict.
    raw = llm.call_claude(system_prompt, user_content, max_tokens=config.JUDGE_MAX_TOKENS,
                          schema=JUDGE_SCHEMA, cached_prefix=prefix)
    truncated_first = llm.LAST_STOP_REASON == "max_tokens"
    decision = None
    try:
        if truncated_first:
            raise ValueError("reply cut off at the output cap")
        decision = parse_judge_json(raw)
    except Exception as first_error:
        print(f"JUDGE: {'reply truncated' if truncated_first else f'JSON parse failed ({first_error})'}; "
              f"doing one {'shorter' if truncated_first else 'corrective'} retry.")
        print("JUDGE DEBUG raw reply:", (raw or "")[:400])
        if truncated_first:
            retry_user_content = (
                user_content
                + "\n\nYour previous reply exceeded the output limit and was cut off before the JSON closed. "
                  "Reply again MUCH SHORTER: each strongest point under 40 words, reasoning under 80 words, "
                  "at most 4 unsupported_claims of one short sentence each, verdict under 250 words including "
                  "its three closing parts. Finish the JSON.")
        else:
            retry_user_content = (
                user_content
                + "\n\nYour previous response was NOT valid JSON:\n"
                + raw
                + "\n\nRespond again with ONLY the JSON object described, and nothing else."
            )
        raw_retry = llm.call_claude(system_prompt, retry_user_content, max_tokens=config.JUDGE_MAX_TOKENS,
                                    schema=JUDGE_SCHEMA, cached_prefix=prefix)
        try:
            if llm.LAST_STOP_REASON == "max_tokens":
                raise ValueError("retry also cut off at the output cap")
            decision = parse_judge_json(raw_retry)
        except Exception as second_error:
            print(f"JUDGE: retry also failed ({second_error}); salvaging the completed fields.")
            print("JUDGE DEBUG raw retry reply:", (raw_retry or "")[:400])
            # WHY salvage instead of the old "fail closed with converged=True":
            # the round cap and the token budget already guarantee termination,
            # so convergence can come only from the judge's actual ruling. What
            # the judge DID write (strongest points, flagged claims, most of the
            # verdict) is kept and labeled truncated; nothing is invented.
            salvaged = salvage_judge_json(raw_retry) or salvage_judge_json(raw)
            decision = {
                "questions": salvaged.get("questions") if isinstance(salvaged.get("questions"), list) else [],
                "planner_coverage": salvaged.get("planner_coverage", ""),
                "bull_strongest": salvaged.get("bull_strongest", ""),
                "bear_strongest": salvaged.get("bear_strongest", ""),
                "unsupported_claims": salvaged.get("unsupported_claims", []),
                "reasoning": (salvaged.get("reasoning") or "")
                             + " [Judge reply was cut off at the output cap twice; fields recovered from the partial reply.]",
                "verdict": salvaged.get("verdict") or "[Judge reply truncated twice — no verdict text was recovered for this round.]",
                "converged": bool(salvaged.get("converged", False)),
                "truncated": True,
            }

    decision.setdefault("questions", [])
    decision.setdefault("planner_coverage", "")
    return {
        "verdict": decision["verdict"],
        "converged": bool(decision["converged"]),
        "round": new_round,
        "judge_decision": decision,
    }


import re as _re

# the three rulings a reader sees; the model's status vocabulary stays
# supported / contradicted / unresolved (clearer for the re-audit itself)
CLAIM_VERDICTS = {"supported": "flag overturned", "contradicted": "flag upheld", "unresolved": "unresolved"}


def _claim_key(claim) -> str:
    return _re.sub(r"[^a-z0-9 ]", "", str(claim).lower()).strip()[:160]


def _claim_words(claim) -> set:
    return {w for w in _re.findall(r"[a-z0-9%$.]+", str(claim).lower()) if len(w) > 2}


def claims_similar(a, b, threshold: float = 0.5) -> bool:
    wa, wb = _claim_words(a), _claim_words(b)
    if not wa or not wb:
        return _claim_key(a) == _claim_key(b)
    return len(wa & wb) / len(wa | wb) >= threshold


def dedupe_claims(claims: list) -> list:
    """PURE. Distinct claims in order: exact duplicates and near-restatements
    (word-set Jaccard >= 0.8) collapse onto the first occurrence."""
    out = []
    for c in claims:
        c = str(c).strip()
        if not c:
            continue
        if any(_claim_key(c) == _claim_key(k) or claims_similar(c, k, 0.8) for k in out):
            continue
        out.append(c)
    return out


def claims_from_decision(decision: dict) -> list:
    """The judge's unsupported claims: the top-level list plus every
    per-question list, deduped, in order."""
    raw = (decision or {}).get("unsupported_claims", [])
    claims = list(raw) if isinstance(raw, list) else ([raw] if raw else [])
    for q in (decision or {}).get("questions") or []:
        if isinstance(q, dict):
            claims += [c for c in (q.get("unsupported_claims") or []) if c]
    return dedupe_claims([str(c) for c in claims])


_INFERENCE_RE = _re.compile(r"(?i)\b(assum\w*|infer\w*|impl(?:y|ies|ied)|rests? on|characteri[sz]\w*|interpret\w*|attribut\w*|"
                            r"because|driven by|came from|due to|result of|reflects?|suggests?|proves?|establish\w*|causal|"
                            r"rel(?:y|ies|ied) on|depends? on|requires?|presumes?|extrapolat\w*|reading|treat\w*|view\w*|"
                            r"as evidence|conclu\w*|signals?|favou?rable|"
                            r"leverage effect|operating leverage|hinted|momentum|trend)\b")


_PRESENCE_RE = _re.compile(r"(?i)appears? only|only in (?:a |the )?news|not in the (?:packet|filings|evidence)|"
                           r"does not appear|do not appear|no (?:such )?figure|not (?:stated|disclosed|found|present|provided)|"
                           r"cannot be (?:verified|traced|found)|unsupported figure|no source|not supported by the packet|"
                           r"packet (?:does not|doesn't) (?:contain|include|state|give|provide)|nowhere in")


def inferential(claim) -> bool:
    """PURE. A flagged claim about an INTERPRETATION ("assumes", "driven by",
    "is inference") is not settled by finding its figure in a filing; only a
    claim whose problem is the figure's PRESENCE ("appears only in a news
    item", "not in the packet") can be overturned by code."""
    text = str(claim)
    return bool(_INFERENCE_RE.search(text)) or not _PRESENCE_RE.search(text)


def with_verdict(review: dict) -> dict:
    review = dict(review)
    review["verdict"] = CLAIM_VERDICTS.get(str(review.get("status", "")).lower(), "unresolved")
    return review


def news_verify_node(state: DebateState) -> dict:
    """Terminal post-Judge step, two INDEPENDENT jobs:
    1. RESOLUTION - re-audit the judge's unsupported claims STRICTLY against
       the evidence packet (one bounded LLM call; the existing revision loop).
    2. ATTACHMENT - semantically-ranked related news leads, stored for display
       under the schema-stable key targeted_news_evidence. Leads are context
       only; they are never an input to resolution.
    Every claim is reviewed once (deduped), a 'found verbatim' ruling shows the
    passage that contains the match, and each ruling carries one of three
    verdicts: flag upheld / flag overturned / unresolved."""
    claims = claims_from_decision(state.get("judge_decision", {}))
    print(f"CLAIM REVIEW: re-auditing up to {NEWS_TARGET_MAX_CLAIMS} of {len(claims)} distinct unsupported claim(s) "
          "against the evidence packet")

    # WHY leads never enter resolution — and why there is no "body counts"
    # carve-out either: by the epistemic rule only status "body" or filings-
    # backed evidence could influence a verdict, and bodies clear the redirect
    # wrappers and paywalls maybe 10-25% of the time. A verifier that fires
    # that rarely makes the rule INCONSISTENT across runs, which is worse than
    # no verifier. Resolution therefore anchors to the ONE corpus every agent
    # already shares — the evidence packet — and the leads below are computed
    # locally (zero network, zero API tokens) purely as display context.
    leads = get_related_leads(claims, state["ticker"])

    if not claims:
        verification = {
            "claim_reviews": [],
            "reasoning": "Judge flagged no unsupported claims; nothing to re-audit.",
            "verdict_changed": False,
            "revised_verdict": state["verdict"],
        }
        return {"news_checked": True, "targeted_news_evidence": leads,
                "claim_verification": verification}

    # CORROBORATE BEFORE DISCARDING. A number the judge flagged as "untrusted
    # news" may sit verbatim in the filings. Search the indexed filings first;
    # a claim whose EVERY figure is found verbatim is filing-backed: the flag
    # is overturned, the passage that contains the match is shown, the
    # passage is appended to the evidence as an addendum the governor can
    # trace against, and the claim leaves the paid re-audit. A claim with no
    # figure, or with figures only partly found, still goes to the re-audit.
    evidence = state["evidence"]
    corroborated = []
    candidates = claims[:NEWS_TARGET_MAX_CLAIMS]
    try:
        from markut.evidence.rag import corroborate_claims, format_corroboration_addendum
        from markut.guardrails.tracer import extract_numeric_claims
        hits = corroborate_claims(state["ticker"], candidates)
        if hits:
            by_claim = {}
            for h in hits:
                by_claim.setdefault(h["claim"], []).append(h)
            promoted = []
            for claim in candidates:
                numbers = extract_numeric_claims(claim)
                found = {h["number"] for h in by_claim.get(claim, [])}
                if numbers and set(numbers) <= found and not inferential(claim):
                    promoted += by_claim[claim]
                    first = by_claim[claim][0]
                    m = first["metadata"]
                    passages = "; ".join(f"{h['number']}: \u201c{h.get('passage') or h['text'][:240]}\u201d" for h in by_claim[claim])
                    corroborated.append(with_verdict({
                        "claim": claim, "status": "supported",
                        "evidence_summary": (f"every figure found verbatim in the {m.get('form', 'filing')} "
                                             f"({m.get('section', '?')}, filed {m.get('filing_date', '?')}): {passages}"),
                        "sources": [h["metadata"].get("url", "") for h in by_claim[claim]]}))
            # every passage found rides in the addendum: the re-audit sees the
            # filing text behind an inferential claim's figures too
            all_hits = [h for hs in by_claim.values() for h in hs]
            if all_hits:
                evidence = evidence + "\n\n" + format_corroboration_addendum(all_hits)
            if promoted:
                print(f"CLAIM REVIEW: {len(corroborated)} claim(s) corroborated in the filings — flag overturned, not re-audited")
    except Exception as e:
        print(f"CLAIM REVIEW: filing corroboration skipped ({e})")
    corroborated_claims = {c["claim"] for c in corroborated}
    remaining = [c for c in candidates if c not in corroborated_claims]
    if not remaining:
        verification = {
            "claim_reviews": corroborated,
            "reasoning": "Every flagged claim's figures were found verbatim in the indexed filings.",
            "verdict_changed": False,
            "revised_verdict": state["verdict"],
        }
        return {"news_checked": True, "targeted_news_evidence": leads,
                "claim_verification": verification, "evidence": evidence}

    # WHY a second look at the same packet: the judge flags claims while doing
    # five other jobs in one response. This re-audit examines ONLY the flagged
    # claims, so over-flagged ones can be rescued (supported), genuinely
    # contradicted ones named, and everything else stays unresolved. Runs ONCE
    # per debate (terminal node), so the packet costs one extra cached pass.
    system_prompt = prompts.NEWS_VERIFY_SYSTEM_PROMPT
    user_content = (
        f"Ticker: {state['ticker']}\n\n"
        f"ORIGINAL VERDICT:\n{state['verdict']}\n\n"
        "UNSUPPORTED CLAIMS (as flagged by the judge):\n"
        + "\n".join(f"- {claim}" for claim in remaining)
        + "\n\nThe EVIDENCE PACKET is in your system context above."
        + (("\n\n" + split_addendum(evidence)[1]) if split_addendum(evidence)[1] else "")
    )
    prefix = evidence_prefix(evidence)
    raw = llm.call_claude(system_prompt, user_content, max_tokens=config.CLAIM_REVIEW_MAX_TOKENS,
                          schema=CLAIM_REVIEW_SCHEMA, cached_prefix=prefix)
    verification, last_error = None, None
    truncated_first = llm.LAST_STOP_REASON == "max_tokens"
    try:
        if truncated_first:
            raise ValueError("reply cut off at the output cap")
        verification = parse_news_review_json(raw)
    except Exception as first_error:
        last_error = first_error
        print(f"CLAIM REVIEW: {'reply truncated' if truncated_first else f'review JSON invalid ({first_error})'}; "
              f"one {'shorter' if truncated_first else 'repair'} retry.")
        print("CLAIM REVIEW DEBUG raw reply:", (raw or "")[:400])
        if truncated_first:
            retry_content = (user_content + "\n\nYour previous reply exceeded the output limit and was cut off. "
                             "Reply again MUCH SHORTER: evidence_summary under 30 words each, reasoning under 60 words, "
                             "revised_verdict an empty string unless verdict_changed is true. Finish the JSON.")
        else:
            retry_content = (user_content + "\n\nYour previous response FAILED validation with this exact error:\n  "
                             + str(first_error) + "\n\nYour previous response was:\n" + (raw or "")
                             + "\n\nFix exactly that problem and respond again with the JSON object only.")
        raw_retry = ""
        try:
            raw_retry = llm.call_claude(system_prompt, retry_content, max_tokens=config.CLAIM_REVIEW_MAX_TOKENS,
                                        schema=CLAIM_REVIEW_SCHEMA, cached_prefix=prefix)
            if llm.LAST_STOP_REASON == "max_tokens":
                raise ValueError("retry also cut off at the output cap")
            verification = parse_news_review_json(raw_retry)
        except Exception as second_error:
            last_error = second_error
            print(f"CLAIM REVIEW: retry also failed ({second_error}); preserving verdict and failing unresolved.")
            print("CLAIM REVIEW DEBUG raw retry reply:", (raw_retry or "")[:400])
    if verification is not None:
        verification.setdefault("reasoning", "")
        verification.setdefault("verdict_changed", False)
        # one ruling per submitted claim: the model's entries are matched back
        # to the claims it was given; restatements of other claims are dropped
        # and a claim it skipped is recorded as unresolved
        returned = list(verification.get("claim_reviews") or [])
        matched, used = [], set()
        for claim in remaining:
            best, best_score = None, 0.0
            for i, r in enumerate(returned):
                if i in used:
                    continue
                wa, wb = _claim_words(claim), _claim_words(r.get("claim", ""))
                score = (len(wa & wb) / len(wa | wb)) if (wa and wb) else (1.0 if _claim_key(claim) == _claim_key(r.get("claim", "")) else 0.0)
                if score > best_score:
                    best, best_score = i, score
            if best is not None and best_score >= 0.3:
                used.add(best)
                matched.append(with_verdict({**returned[best], "claim": claim}))
            else:
                matched.append(with_verdict({"claim": claim, "status": "unresolved",
                                             "evidence_summary": "the re-audit returned no ruling for this claim",
                                             "sources": []}))
        dropped = len(returned) - len(used)
        if dropped:
            print(f"CLAIM REVIEW: {dropped} returned entr{'y' if dropped == 1 else 'ies'} did not match a submitted claim — dropped")
        verification["claim_reviews"] = matched
    else:
        verification = {
            "claim_reviews": [
                with_verdict({"claim": claim, "status": "unresolved",
                              "evidence_summary": "Claim-review response was invalid JSON.", "sources": []})
                for claim in remaining
            ],
            "reasoning": f"Claim review could not be parsed: {last_error}",
            "verdict_changed": False,
            "revised_verdict": state["verdict"],
        }
    # corroborated claims lead the record, then whatever the re-audit decided
    verification["claim_reviews"] = corroborated + list(verification.get("claim_reviews") or [])
    revised = (verification.get("revised_verdict")
               if verification.get("verdict_changed") is True
               else state["verdict"])
    return {
        "news_checked": True,
        "targeted_news_evidence": leads,
        "claim_verification": verification,
        "verdict": revised or state["verdict"],
        "evidence": evidence,   # the packet plus any filings addendum — what the governor traces against
    }
