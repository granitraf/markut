"""should_continue + the graph builder (notebook cells 44/46, verbatim).
build_graph() compiles on CALL, not at import — no side effects."""
from langgraph.graph import StateGraph, START, END

from markut import config
from markut.agents import llm
from markut.agents.state import DebateState
from markut.agents.nodes import (research_node, bull_node, bear_node,
    judge_node, news_verify_node)
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


def build_graph():
    builder = StateGraph(DebateState)

    builder.add_node("research", research_node)
    builder.add_node("bull", bull_node)
    builder.add_node("bear", bear_node)
    builder.add_node("judge", judge_node)
    builder.add_node("news_verify", news_verify_node)
    builder.add_node("review", review_node)

    builder.add_edge(START, "research")
    builder.add_edge("research", "bull")
    builder.add_edge("bull", "bear")
    builder.add_edge("bear", "judge")
    # WHY every exit funnels done -> news_verify -> review -> END: NOTHING leaves
    # the graph unreviewed — convergence, the round cap, and the token budget all
    # close through the same governed tail. review runs LAST so the output layer
    # governs the FINAL artifact (news_verify's claim re-audit can no longer alter
    # the verdict after review). The single bounded revision lives INSIDE
    # review_node, so the graph stays acyclic except the debate loop — termination
    # is provable: rounds capped, budget capped, and the tail is a straight line
    # to END.
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
