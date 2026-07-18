"""CLI: python -m markut.eval TICKER — runs a FULL debate (paid), then the
baseline comparison against the debate's stored packet."""
import argparse
import sys

from markut import config
from markut.agents import llm
from markut.agents.graph import build_graph
from markut.eval.harness import run_baseline_comparison
from markut.evidence.edgar import ticker_to_cik
from markut.guardrails.validate import validate_ticker


def main():
    parser = argparse.ArgumentParser(prog="python -m markut.eval")
    parser.add_argument("ticker")
    parser.add_argument("--max-rounds", type=int, default=config.DEFAULT_MAX_ROUNDS)
    args = parser.parse_args()
    try:
        ticker = validate_ticker(args.ticker)
        ticker_to_cik(ticker)
    except ValueError as e:
        print(f"validation failed: {e}", file=sys.stderr)
        sys.exit(1)
    llm.reset()
    app = build_graph()
    final_state = app.invoke({
        "ticker": ticker, "evidence": "", "bull_case": "", "bear_case": "",
        "bull_history": [], "bear_history": [], "verdict": "", "converged": False,
        "round": 0, "max_rounds": args.max_rounds, "news_evidence": "",
        "targeted_news_evidence": "", "news_checked": False,
        "claim_verification": {}, "judge_decision": {},
        "budget_exceeded": False, "review_report": {},
    })
    run_baseline_comparison(final_state)


if __name__ == "__main__":
    main()
