"""P0 (run #28, TSM): the governor counts real data as evidence, and sees
every figure.

Run #28's governor counted 2 claims (64%, 50%) and traced both to the
planner's own question text ("Can gross margin of 64% and net margin near
50% be maintained…", repeated in a "could not find" line) — the market data
said 64.23% and 49.92%. It missed "22.05", a bare decimal. Offline; an
AAPL-shaped packet and a TSM-run-#28-shaped one."""
from markut.guardrails import tracer


def check(label, condition):
    assert condition, label


TSM_PACKET = "\n".join([
    "[PROFILE] (what kind of business this is)",
    "- Business: a foundry with about 64% gross margins",
    "[KEY QUESTIONS] (set by the planner)",
    "- Q2: Can gross margin of 64% and net margin near 50% be maintained?",
    "[EVIDENCE Q2] Can gross margin of 64% and net margin near 50% be maintained?",
    "- [Q2] could not find: Can gross margin of 64% and net margin near 50% be maintained? (limitation: …)",
    "[QUOTE & VALUATION]",
    "- EPS (forward, next-FY consensus non-GAAP, 13 analysts; canonical source): $22.05  [source: yfinance/earnings_estimate]",
    "[FUNDAMENTALS]",
    "- Profit margin (TTM): 49.92%  [source: yfinance/info]",
    "- Gross margin (TTM): 64.23%  [source: yfinance/info]",
    "[WHAT WOULD MISLEAD] (planner)",
    "- Check whether the 64% gross margin includes one-offs",
    "[COVERAGE GAPS]",
    "- COVERAGE GAP Q2: gross margin of 64% not found for the current period",
])
AAPL_PACKET = "\n".join([
    "[KEY QUESTIONS]",
    "- Q1: Is the 16% iPhone growth sustainable?",
    "[CORE FIGURES]",
    "- Revenue: Total net sales: 109,417 million (quarter) vs 94,036 million year-ago; change +16.4%  [source: T1]",
    "[QUOTE & VALUATION]",
    "- P/E (trailing TTM, GAAP EPS): 38.61  [source: yfinance/info]",
])


def test_model_written_text_is_never_evidence():
    data = tracer.tracing_evidence(TSM_PACKET)
    check("TSM run #28: the planner's '64%' is not in the data the governor traces against",
          "Can gross margin" not in data and "could not find" not in data and "COVERAGE GAP Q2" not in data and "Business:" not in data)
    check("…while the data lines stay", "64.23%" in data and "$22.05" in data)
    check("TSM run #28: '64%' no longer traces (the data says 64.23%)", tracer.trace_claim("64%", data) == "FLAGGED")
    check("…and '64.23%' does", tracer.trace_claim("64.23%", data) == "CITED")
    a = tracer.tracing_evidence(AAPL_PACKET)
    check("AAPL-shaped: the planner's 16% is not evidence, the core figure's +16.4% is",
          tracer.trace_claim("16%", a) == "FLAGGED" and tracer.trace_claim("16.4%", a) == "CITED")


def test_bare_decimals_are_figures():
    c = tracer.extract_numeric_claims("Next year's earnings are measured against the 22.05 consensus in fiscal 2026 and Q4 2026.")
    check(f"TSM run #28: '22.05' is a figure; years and quarter labels are not ({c})", "22.05" in c and "2026" not in c)
    d = tracer.extract_numeric_claims("Diluted EPS was $2.02, revenue 10.5 billion, a 1.5x multiple and a 38.61 trailing P/E.")
    check(f"no figure is counted twice ({d})", "2.02" not in d and "10.5" not in d and "1.5" not in d
          and "$2.02" in d and "38.61" in d)


def test_review_stats_and_sources_use_data_only():
    from markut.web.events import review_stats
    verdict = "Gross margin of 64% and net margin near 50% must hold; next year's 22.05 consensus is the bar."
    s = review_stats(verdict, TSM_PACKET, verdict, {"final_status": "revised"})
    check(f"TSM run #28: three figures, and none traces to the planner's text ({s})", s["claims"] == 3 and s["cited"] == 1)
    src = {x["figure"]: x for x in tracer.figure_sources(verdict, TSM_PACKET, [])}
    check(f"'64%' is listed untraced, never sourced to a 'could not find' line ({src.get('64%')})",
          src.get("64%", {}).get("kind") == "untraced" and "could not find" not in src.get("64%", {}).get("line", ""))
