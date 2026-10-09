"""The graph: profiler -> planner -> research (coverage gate, one loop back)
-> bull <-> bear -> judge -> claim review -> governor. should_continue and
the debate loop are the notebook's (cells 44/46); build_graph() compiles on
CALL, not at import — no side effects."""
from langgraph.graph import StateGraph, START, END

from markut import config
from markut.agents import llm
from markut.agents.state import DebateState
from markut.agents.nodes import (research_node, bull_node, bear_node,
    judge_node, news_verify_node)
from markut.agents.profiler import profiler_node, planner_node
from markut.guardrails.review import review_node


def should_continue(state: DebateState) -> str:
    # WHY only two outcomes now (was continue/verify/end): every exit flows
    # done -> news_verify -> review (wired in the builder). news_verify
    # already no-ops cheaply when the judge flagged nothing, and review is
    # the OUTPUT guardrail nothing may skip — so there is no unreviewed exit
    # left to route to.
    #
    # EXECUTION-LAYER GUARDRAIL: the token budget outranks every other signal
    # and is checked FIRST. Convergence and the round cap assume normal
    # behavior; the budget exists for ABNORMAL runs (ballooning prompts, a
    # judge that never converges). Crossing it closes the debate through the
    # normal done path, so even a budget-killed run exits GOVERNED — with a
    # reviewed, disclaimed verdict. (Conditional-edge functions cannot write
    # state in LangGraph, so review_node is what records budget_exceeded.)
    spent = (llm.TOKENS["input"] + llm.TOKENS["output"]
             + llm.TOKENS.get("cache_write", 0) + llm.TOKENS.get("cache_read", 0))
    if spent >= config.TOKEN_BUDGET:
        print(f"TOKEN BUDGET EXCEEDED — closing debate ({spent:,} >= {config.TOKEN_BUDGET:,} tokens)\n")
        return "done"
    if state["converged"]:
        print("Judge ruled the debate CONVERGED — wrapping up.\n")
        return "done"
    if state["round"] >= state["max_rounds"]:
        print(f"Max rounds ({state['max_rounds']}) reached — wrapping up.\n")
        return "done"
    print(f"Round {state['round']} complete — looping back for rebuttal.\n")
    return "continue"


def coverage_decision(state: dict) -> dict:
    """PURE. The coverage gate's ruling, count-only: how many of the planner's
    questions have at least one sourced evidence line. Fewer than
    config.COVERAGE_MIN_QUESTIONS after the first pass routes research back
    once, for the uncovered questions only; after that the debate proceeds
    and the remaining gaps are COVERAGE GAP lines in the packet."""
    coverage = state.get("coverage") or {}
    total = len(coverage)
    covered = sum(1 for n in coverage.values() if n)
    uncovered = [q for q, n in coverage.items() if not n]
    passes = state.get("research_pass") or 1
    again = bool(total) and covered < config.COVERAGE_MIN_QUESTIONS and passes < 2 and bool(uncovered)
    return {"decision": "research" if again else "debate", "covered": covered, "total": total,
            "uncovered": uncovered, "pass": passes}


def coverage_route(state: DebateState) -> str:
    d = coverage_decision(state)
    if d["decision"] == "research":
        print(f"COVERAGE GATE: {d['covered']}/{d['total']} questions covered — second research pass on "
              f"{', '.join(d['uncovered'])}\n")
    else:
        print(f"COVERAGE GATE: {d['covered']}/{d['total']} questions covered after pass {d['pass']} — to the debate\n")
    return d["decision"]


def build_graph():
    builder = StateGraph(DebateState)

    builder.add_node("profiler", profiler_node)
    builder.add_node("planner", planner_node)
    builder.add_node("research", research_node)
    builder.add_node("bull", bull_node)
    builder.add_node("bear", bear_node)
    builder.add_node("judge", judge_node)
    builder.add_node("news_verify", news_verify_node)
    builder.add_node("review", review_node)

    # understand the company first: what it is (profiler), what decides its
    # outlook (planner), then research organized around those questions
    builder.add_edge(START, "profiler")
    builder.add_edge("profiler", "planner")
    builder.add_edge("planner", "research")
    # the coverage gate: research loops back ONCE for the uncovered questions
    builder.add_conditional_edges(
        "research",
        coverage_route,
        {"research": "research", "debate": "bull"},
    )
    builder.add_edge("bull", "bear")
    builder.add_edge("bear", "judge")
    # WHY every exit funnels done -> news_verify -> review -> END: NOTHING leaves
    # the graph unreviewed — convergence, the round cap, and the token budget all
    # close through the same governed tail. review runs LAST so the output layer
    # governs the FINAL artifact (news_verify's claim re-audit can no longer alter
    # the verdict after review). The single bounded revision lives INSIDE
    # review_node, so termination is provable: the research loop is bounded to
    # one retry by research_pass, rounds are capped, the budget is capped, and
    # the tail is a straight line to END.
    builder.add_edge("news_verify", "review")
    builder.add_edge("review", END)

    builder.add_conditional_edges(
        "judge",
        should_continue,
        {"continue": "bull", "done": "news_verify"},
    )

    app = builder.compile()
    print("Graph compiled.")
    return app
