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
    print("RESEARCH: fetching LIVE data for", state["ticker"])
    # One immutable packet per debate — now assembled THROUGH the MCP protocol.
    # WHY: this agent no longer knows the evidence functions exist; it knows a
    # protocol. It discovers whatever tools the "markut-evidence" server
    # offers and calls them by name, so swapping yfinance (or EDGAR, or the
    # news feed) for another vendor touches ZERO agent code — only the
    # server-side tool implementation changes. News keeps its epistemic
    # labels: leads may suggest a question but may not support a fact.
    # lazy import: the MCP client spins its worker-thread event loop —
    # keep module import free of that machinery
    from markut.mcp.client import call_evidence_tools
    return {"evidence": call_evidence_tools(state["ticker"])}

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
                "bull_strongest": salvaged.get("bull_strongest", ""),
                "bear_strongest": salvaged.get("bear_strongest", ""),
                "unsupported_claims": salvaged.get("unsupported_claims", []),
                "reasoning": (salvaged.get("reasoning") or "")
                             + " [Judge reply was cut off at the output cap twice; fields recovered from the partial reply.]",
                "verdict": salvaged.get("verdict") or "[Judge reply truncated twice — no verdict text was recovered for this round.]",
                "converged": bool(salvaged.get("converged", False)),
                "truncated": True,
            }

    return {
        "verdict": decision["verdict"],
        "converged": bool(decision["converged"]),
        "round": new_round,
        "judge_decision": decision,
    }


def news_verify_node(state: DebateState) -> dict:
    """Terminal post-Judge step, two INDEPENDENT jobs:
    1. RESOLUTION - re-audit the judge's unsupported claims STRICTLY against
       the evidence packet (one bounded LLM call; the existing revision loop).
    2. ATTACHMENT - semantically-ranked related news leads, stored for display
       under the schema-stable key targeted_news_evidence. Leads are context
       only; they are never an input to resolution."""
    raw_claims = state.get("judge_decision", {}).get("unsupported_claims", [])
    claims = raw_claims if isinstance(raw_claims, list) else ([raw_claims] if raw_claims else [])
    claims = [str(claim) for claim in claims]
    print(f"CLAIM REVIEW: re-auditing up to {NEWS_TARGET_MAX_CLAIMS} unsupported claim(s) against the evidence packet")

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

    # AUDIT FIX (run #2): CORROBORATE BEFORE DISCARDING. A number the judge
    # flagged as "untrusted news" may sit verbatim in the filings ("221%" in
    # the 8-K release, "$29B" in the 10-Q note). Search the indexed filings
    # first; a hit makes the claim filing-backed, is appended to the evidence
    # as an addendum the governor can trace against, and is removed from the
    # paid re-audit. Lazy import: the RAG module loads the local models.
    evidence = state["evidence"]
    corroborated = []
    try:
        from markut.evidence.rag import corroborate_claims, format_corroboration_addendum
        hits = corroborate_claims(state["ticker"], claims[:NEWS_TARGET_MAX_CLAIMS])
        if hits:
            addendum = format_corroboration_addendum(hits)
            evidence = evidence + "\n\n" + addendum
            for h in hits:
                if h["claim"] not in [c["claim"] for c in corroborated]:
                    corroborated.append({
                        "claim": h["claim"], "status": "supported",
                        "evidence_summary": f"{h['number']} found verbatim in the filing: \"{h['text'][:200]}\"",
                        "sources": [h["metadata"].get("url", "")]})
            print(f"CLAIM REVIEW: {len(corroborated)} claim(s) corroborated in the filings — promoted, not re-audited")
    except Exception as e:
        print(f"CLAIM REVIEW: filing corroboration skipped ({e})")
    corroborated_claims = {c["claim"] for c in corroborated}
    remaining = [c for c in claims[:NEWS_TARGET_MAX_CLAIMS] if c not in corroborated_claims]
    if not remaining:
        verification = {
            "claim_reviews": corroborated,
            "reasoning": "Every flagged claim's numbers were found verbatim in the indexed filings.",
            "verdict_changed": False,
            "revised_verdict": state["verdict"],
        }
        return {"news_checked": True, "targeted_news_evidence": leads,
                "claim_verification": verification, "evidence": evidence}

    # WHY a second look at the same packet: the judge flags claims while doing
    # five other jobs in one response. This re-audit examines ONLY the flagged
    # claims, so over-flagged ones can be rescued (supported), genuinely
    # contradicted ones named, and everything else stays unresolved. Runs ONCE
    # per debate (terminal node), so the ~3k-token packet costs one extra pass.
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
    # AUDIT FIX (run #2): every claim came back "invalid JSON". Shape is now
    # schema-enforced; one repair retry shows the model its own reply and the
    # exact validator error; the raw reply is logged on every failure.
    raw = llm.call_claude(system_prompt, user_content, max_tokens=1400, schema=CLAIM_REVIEW_SCHEMA,
                          cached_prefix=prefix)
    verification = None
    try:
        verification = parse_news_review_json(raw)
    except Exception as first_error:
        print(f"CLAIM REVIEW: review JSON invalid ({first_error}); one repair retry.")
        print("CLAIM REVIEW DEBUG raw reply:", (raw or "")[:400])
        try:
            raw_retry = llm.call_claude(
                system_prompt,
                user_content + "\n\nYour previous response FAILED validation with this exact error:\n  "
                + str(first_error) + "\n\nYour previous response was:\n" + (raw or "")
                + "\n\nFix exactly that problem and respond again with the JSON object only.",
                max_tokens=1400, schema=CLAIM_REVIEW_SCHEMA, cached_prefix=prefix)
            verification = parse_news_review_json(raw_retry)
        except Exception as e:
            print(f"CLAIM REVIEW: repair retry also failed ({e}); preserving verdict and failing unresolved.")
    if verification is not None:
        verification.setdefault("reasoning", "")
        verification.setdefault("verdict_changed", False)
    else:
        verification = {
            "claim_reviews": [
                {"claim": claim, "status": "unresolved",
                 "evidence_summary": "Claim-review response was invalid JSON.", "sources": []}
                for claim in remaining
            ],
            "reasoning": f"Claim review could not be parsed: {e}",
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
