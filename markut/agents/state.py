"""Typed shared state — the graph's working memory (notebook cell 6, extended
for the profiler / planner / coverage-gate iteration)."""
import operator
from typing import Annotated, TypedDict


class DebateState(TypedDict):
    ticker: str
    evidence: str
    bull_case: str
    bear_case: str
    bull_history: Annotated[list, operator.add]
    bear_history: Annotated[list, operator.add]
    verdict: str
    converged: bool
    round: int
    max_rounds: int
    news_evidence: str
    targeted_news_evidence: str
    news_checked: bool
    claim_verification: dict
    # WHY: the judge now emits structured JSON, not just a verdict string. We keep
    # the full parsed dict (bull_strongest, bear_strongest, unsupported_claims,
    # reasoning, verdict, converged, questions, planner_coverage) so downstream
    # code can inspect the decision without re-parsing text.
    judge_decision: dict
    # Guardrail bookkeeping — execution layer (budget) + output layer (review):
    budget_exceeded: bool
    review_report: dict
    # ---- understand the company before researching it ----
    # profiler: what kind of business this is, read from the filings themselves
    profile: dict            # business, archetype, segments, company_kpis, accounting_flags, sources
    profile_gaps: list       # COVERAGE GAP lines the profile check recorded
    profiled_text: str       # the section text the profiler read (research never retrieves it again)
    filing_fingerprint: str  # accession numbers of the latest 10-K / 10-Q / earnings 8-K (cache key)
    profile_sources: list    # the sections the profile was read from (shown in the packet)
    profile_cached: bool     # True when the profile came from the cache (no extraction, no call)
    plan_cached: bool
    # planner: the five questions that decide the outlook, and how metrics could mislead
    plan: dict               # key_questions (Q1-Q5), what_would_mislead, peer_tickers (stored only)
    market_evidence: str     # the market-data section, fetched once for the planner and reused by research
    # research / coverage gate
    research_pass: int       # 1 on the first pass, 2 after the gate routed back
    coverage: dict           # {"Q1": sourced-line count, ...}
    uncovered: list          # question ids with no sourced evidence after the latest pass
