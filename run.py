"""CLI: python run.py TICKER [--max-rounds N] [--budget T]
Validate (input guardrail layers 1-2) -> build graph -> invoke -> governed
verdict + REVIEW STATS (printed by the review node) + USAGE line."""
import argparse
import sys

from markut import config
from markut.agents import llm
from markut.agents.graph import build_graph
from markut.evidence.edgar import ticker_to_cik
from markut.guardrails.validate import validate_ticker


def main():
    parser = argparse.ArgumentParser(description="Markut bull-vs-bear research debate")
    parser.add_argument("ticker", help="stock ticker, e.g. NVDA")
    parser.add_argument("--max-rounds", type=int, default=config.DEFAULT_MAX_ROUNDS)
    parser.add_argument("--budget", type=int, default=config.TOKEN_BUDGET,
                        help="token budget (execution guardrail)")
    args = parser.parse_args()

    # CLI override by attribute mutation — call_claude and should_continue read
    # config.TOKEN_BUDGET late, exactly like the notebook's globals
    config.TOKEN_BUDGET = args.budget

    try:
        ticker = validate_ticker(args.ticker)   # layer 1: shape
        ticker_to_cik(ticker)                   # layer 2: SEC existence
    except ValueError as e:
        print(f"validation failed: {e}", file=sys.stderr)
        sys.exit(1)

    llm.reset()  # mirrors the notebook run cell: USAGE reflects only this run
    app = build_graph()
    final_state = app.invoke({
        "ticker": ticker, "evidence": "", "bull_case": "", "bear_case": "",
        "bull_history": [], "bear_history": [], "verdict": "", "converged": False,
        "round": 0, "max_rounds": args.max_rounds, "news_evidence": "",
        "targeted_news_evidence": "", "news_checked": False,
        "claim_verification": {}, "judge_decision": {},
        "budget_exceeded": False, "review_report": {},
    })

    print("\n" + "=" * 60)
    print(f"DEBATE RAN {final_state['round']} ROUND(S)  |  Converged: {final_state['converged']}")
    print("=" * 60)
    print("FINAL VERDICT:\n", final_state["verdict"])
    print("=" * 60)
    print(f"USAGE: {llm.TOKENS['calls']} API calls  |  "
          f"input tokens: {llm.TOKENS['input']}  |  output tokens: {llm.TOKENS['output']}")


if __name__ == "__main__":
    main()
