"""Debate agents: research/bull/bear/judge/news_verify (notebook nodes
cell, verbatim; prompts hoisted to markut.agents.prompts, call_claude and
TOKENS module-qualified so tests can patch markut.agents.llm)."""
from markut import config
from markut.agents import llm, prompts
from markut.agents.state import DebateState
from markut.guardrails.parsing import parse_judge_json
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
    user_content = f"Ticker: {state['ticker']}\n\nEvidence:\n{state['evidence']}{rebuttal}"
    case = llm.call_claude(system_prompt, user_content)
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
    user_content = f"Ticker: {state['ticker']}\n\nEvidence:\n{state['evidence']}{rebuttal}"
    case = llm.call_claude(system_prompt, user_content)
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
        # material instead of taking the analysts' word for them — otherwise a
        # confidently-worded hallucination can win the debate. COST: the packet is
        # ~3k tokens and the judge runs EVERY round, so this adds ~3k input tokens
        # x rounds (~9k on a 3-round debate). Accepted deliberately (2026-07-15):
        # grounding the verdict is the point of having a judge at all.
        f"EVIDENCE PACKET (the exact source material both analysts saw):\n"
        f"{state['evidence']}\n\n"
        f"FULL DEBATE HISTORY (all rounds so far):\n{format_history(state)}\n\n"
        f"Latest Bull case:\n{state['bull_case']}\n\n"
        f"Latest Bear case:\n{state['bear_case']}\n\n"
        f"Rounds completed so far (including this one): {new_round}"
    )

    # WHY 2000 (was 1500): unsupported_claims adds a whole list to the JSON, and a
    # truncated response is unparseable — it burns a full corrective retry. Headroom
    # is cheap; max_tokens is a cap, not a target.
    raw = llm.call_claude(system_prompt, user_content, max_tokens=2000)
    try:
        decision = parse_judge_json(raw)
    except Exception as first_error:
        # WHY (corrective retry): one bad JSON response is usually fixable by showing
        # the model its own invalid output and re-demanding pure JSON. We do this ONCE
        # to avoid burning tokens/latency on repeated failures.
        print(f"JUDGE: JSON parse failed ({first_error}); doing one corrective retry.")
        retry_user_content = (
            user_content
            + "\n\nYour previous response was NOT valid JSON:\n"
            + raw
            + "\n\nRespond again with ONLY the JSON object described, and nothing else."
        )
        raw_retry = llm.call_claude(system_prompt, retry_user_content, max_tokens=2000)
        try:
            decision = parse_judge_json(raw_retry)
        except Exception as second_error:
            # WHY (fail closed): if the judge still won't produce parseable JSON, we
            # must NOT keep looping blindly. We force converged=True so the graph
            # terminates, and stash the raw text as the verdict so nothing is lost.
            print(f"JUDGE: retry also failed ({second_error}); failing CLOSED to terminate.")
            decision = {
                "bull_strongest": "",
                "bear_strongest": "",
                "unsupported_claims": [],
                "reasoning": "Judge failed to return valid JSON twice; failing closed.",
                "verdict": raw_retry,
                "converged": True,
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
        + "\n".join(f"- {claim}" for claim in claims[:NEWS_TARGET_MAX_CLAIMS])
        + f"\n\nEVIDENCE PACKET:\n{state['evidence']}"
    )
    raw = llm.call_claude(system_prompt, user_content, max_tokens=1400)
    try:
        verification = parse_news_review_json(raw)
        verification.setdefault("reasoning", "")
        verification.setdefault("verdict_changed", False)
    except Exception as e:
        print(f"CLAIM REVIEW: review JSON invalid ({e}); preserving verdict and failing unresolved.")
        verification = {
            "claim_reviews": [
                {"claim": claim, "status": "unresolved",
                 "evidence_summary": "Claim-review response was invalid JSON.", "sources": []}
                for claim in claims[:NEWS_TARGET_MAX_CLAIMS]
            ],
            "reasoning": f"Claim review could not be parsed: {e}",
            "verdict_changed": False,
            "revised_verdict": state["verdict"],
        }
    revised = (verification.get("revised_verdict")
               if verification.get("verdict_changed") is True
               else state["verdict"])
    return {
        "news_checked": True,
        "targeted_news_evidence": leads,
        "claim_verification": verification,
        "verdict": revised or state["verdict"],
    }
