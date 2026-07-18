"""Input-layer guardrail: ticker shape validation (moved verbatim from the
notebook helpers cell). The CIK-existence check (layer 2) stays a separate
call to evidence.edgar.ticker_to_cik, exactly as the notebook run cell does
it."""
import re


def validate_ticker(raw: str) -> str:
    # WHY: this is the INPUT layer of a three-layer guardrail design (input
    # validation -> cost/round caps -> output hallucination check). Catching a
    # malformed ticker HERE means garbage or injection-style input ("'; drop
    # everything") never reaches an API call, so we don't spend tokens on nonsense
    # and don't hand attacker-controlled text to a model or a downstream query.
    #
    # NOTE: this is only a SHAPE check. It confirms the string LOOKS like a ticker;
    # it does NOT confirm the company exists. Later this will be backed by a real
    # existence check against SEC EDGAR's ticker-to-CIK file (company_tickers.json).
    ticker = raw.strip().upper()
    # 1-5 letters, plus an optional single-letter share-class suffix like BRK.B.
    # fullmatch (not search) so the ENTIRE string must conform — no leading/trailing junk.
    if not re.fullmatch(r"[A-Z]{1,5}(\.[A-Z])?", ticker):
        raise ValueError(
            f"Invalid ticker {raw!r}: expected 1-5 letters with an optional "
            f".X share-class suffix (e.g. NVDA, AAPL, BRK.B)."
        )
    return ticker
