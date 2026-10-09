"""CLI: python run.py TICKER [--max-rounds N] [--budget T] [--no-log]
Validate (input guardrail layers 1-2) -> build graph -> stream -> governed
verdict + REVIEW STATS (printed by the review node) + USAGE line. Every run
is logged to the SQLite run store (markut.store) alongside the web UI's."""
import argparse
import sys
import time

from markut import config, store
from markut.web.events import stream_debate


def main():
    parser = argparse.ArgumentParser(description="Markut bull-vs-bear research debate")
    parser.add_argument("ticker", help="stock ticker symbol")
    parser.add_argument("--max-rounds", type=int, default=config.DEFAULT_MAX_ROUNDS,
                        help=f"debate rounds (minimum {config.MIN_ROUNDS}: a rebuttal needs a second round)")
    parser.add_argument("--budget", type=int, default=config.TOKEN_BUDGET,
                        help="token budget (execution guardrail)")
    parser.add_argument("--no-log", action="store_true", help="do not store this run in the run log")
    args = parser.parse_args()

    # CLI override by attribute mutation — call_claude and should_continue read
    # config.TOKEN_BUDGET late, exactly like the notebook's globals
    config.TOKEN_BUDGET = args.budget

    # WHY the same event stream the web UI consumes: one writer path into the
    # run log. The nodes still print their own progress lines; we collect
    # the per-node events (stamped with arrival time) and store them at the end.
    events = []
    for event in stream_debate(args.ticker, args.max_rounds):
        events.append({**event, "at": time.strftime("%Y-%m-%d %H:%M:%S")})
        if event["event"] == "error" and event["data"].get("stage") == "validation":
            print(f"validation failed: {event['data']['message']}", file=sys.stderr)
            sys.exit(1)

    by = {e["event"]: e["data"] for e in events}
    if "error" in by:
        print(f"\nDEBATE FAILED ({by['error'].get('stage')}): {by['error'].get('message')}", file=sys.stderr)
    done = by.get("done", {})
    review = by.get("review", {})
    usage = done.get("usage") or by.get("error", {}).get("usage") or {}

    print("\n" + "=" * 60)
    print(f"DEBATE RAN {done.get('rounds', '?')} ROUND(S)  |  Converged: {done.get('converged', '?')}")
    print("=" * 60)
    print("FINAL VERDICT:\n", review.get("verdict", "(no governed verdict — see error above)"))
    print("=" * 60)
    cached = (usage.get("cache_read") or 0) + (usage.get("cache_write") or 0)
    print(f"USAGE: {usage.get('calls', 0)} API calls  |  "
          f"input tokens: {usage.get('input', 0) + cached} ({usage.get('cache_read') or 0} read from cache, "
          f"{usage.get('cache_write') or 0} written)  |  output tokens: {usage.get('output', 0)}")

    if not args.no_log:
        run_id = store.record_run(events, mode="cli")
        print(f"LOGGED: run {run_id} in {store.backend_label()}  (python -m markut.store show {run_id})")
    if "error" in by:
        sys.exit(2)


if __name__ == "__main__":
    main()
