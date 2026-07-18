"""Typed shared state — the graph's working memory (notebook cell 6,
verbatim)."""
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
    # reasoning, verdict, converged) so downstream code can inspect the decision
    # without re-parsing text.
    judge_decision: dict
    # Guardrail bookkeeping — execution layer (budget) + output layer (review):
    budget_exceeded: bool
    review_report: dict
