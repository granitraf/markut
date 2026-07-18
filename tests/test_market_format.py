"""Migrated verbatim from the notebook suite: format_market_lines block."""
from markut.evidence.market import (format_market_lines, fmt_price,
    fmt_big, fmt_pct, fmt_ratio)


def check(label, condition):
    assert condition, label


def test_format_market_lines_block():
    print("\n--- format_market_lines (offline — fake dict, no network) ---")

    # (a) A fake info dict missing half its keys: only present keys produce lines,
    # nothing crashes, and every data line carries a yfinance source tag. This is
    # the defensive-access contract — one missing key costs one line, never a crash.
    fake_info = {
        "currentPrice": 197.58,
        "marketCap": 4_200_000_000_000,
        "trailingPE": 30.12,
        "profitMargins": 0.6297,
        # deliberately missing: forwardPE, priceToBook, trailingEps, forwardEps,
        # fiftyTwoWeekLow/High, grossMargins, returnOnEquity, debtToEquity,
        # freeCashflow, revenueGrowth
    }
    try:
        lines = format_market_lines(fake_info)
        joined = "\n".join(lines)
        check("present keys produce lines (price, mcap, trailing P/E, margin)",
              "Price (current)" in joined and "Market cap" in joined
              and "P/E (trailing TTM)" in joined and "Profit margin (TTM)" in joined)
        check("missing keys produce NO lines (no forward P/E, 52-week, D/E)",
              "P/E (forward)" not in joined and "52-week" not in joined
              and "Debt/Equity" not in joined)
        data_lines = [l for l in lines if l.startswith("- ")]
        check("every data line carries a [source: yfinance/...] tag",
              len(data_lines) > 0 and all("[source: yfinance/" in l for l in data_lines))
    except Exception as e:
        check(f"format_market_lines with sparse dict (unexpectedly raised: {e})", False)

    # (b) marketCap formatting: trillions and billions must render human-readably.
    check('fmt_big(4_200_000_000_000) -> "$4.20T"', fmt_big(4_200_000_000_000) == "$4.20T")
    check('fmt_big(312_050_000_000) -> "$312.05B"', fmt_big(312_050_000_000) == "$312.05B")
