"""FastMCP evidence server (notebook cell 30, verbatim). Importing this
module defines and registers the three tools — mirroring notebook cell
execution — and prints the one-line confirmation."""
from markut.evidence.packet import (market_snapshot, get_filings_evidence,
    get_news_evidence)

from fastmcp import FastMCP, Client

# ---------------- MCP evidence server: "markut-evidence" ----------------
# WHY MCP here: the Research step should pull evidence through a STANDARD tool
# interface instead of hardcoded function calls. What that buys:
#   - a protocol boundary: the agent discovers tools at runtime (list_tools)
#     and calls them by name — it does not import or even know the functions;
#   - source-swappability: replacing yfinance/EDGAR/Yahoo with another vendor
#     means changing a tool IMPLEMENTATION behind this server — zero agent code;
#   - discovery metadata: the docstrings below are exactly what a connected
#     agent reads to decide which tool to call. They are written for an agent
#     reader, not a human skimmer.
# What stays BEHIND the boundary (callers never see it): the EDGAR disk cache,
# the news feed throttle + persistent store, the Chroma index and local models,
# and the FMP API key. Every tool keeps the shared evidence contract: it
# returns a source-tagged text section, never raises, and degrades to a
# bracketed "[... unavailable: ...]" marker.
# WHY fastmcp (not the low-level mcp package): FastMCP ships a first-class
# IN-MEMORY transport — Client(server) runs in this same process and event
# loop, the right transport for a notebook capstone: no subprocess server to
# spawn or babysit under Jupyter.
evidence_server = FastMCP("markut-evidence")


def market_snapshot_tool(ticker: str) -> str:
    """Live market evidence for one stock ticker: quote and valuation,
    fundamentals and margins, analyst view, next earnings date, and a DCF fair
    value. Every line carries a source tag ([source: yfinance ...] or
    [source: FMP ...]) and a period label so figures cannot be mis-scaled or
    mis-dated. Missing fields are omitted, never invented; on failure the
    section degrades to a bracketed unavailable marker instead of raising."""
    return market_snapshot(ticker)


def filings_evidence_tool(ticker: str) -> str:
    """SEC filings evidence for one stock ticker: a token-capped [FILINGS]
    section from the company's latest 10-K, 10-Q, and 8-K press release —
    retrieved risk-factor and results/drivers excerpts plus deterministic
    risk-caption and guidance blocks. Every excerpt carries a source tag
    ([source: EDGAR/<form> <section>, filed <date>, <url>]) and the header
    states each document's age in months. Never raises; degrades to bracketed
    "[filings unavailable: ...]" or per-theme markers."""
    return get_filings_evidence(ticker)


def news_evidence_tool(ticker: str) -> str:
    """Recent-news evidence for one stock ticker: a token-capped [NEWS] section
    from Yahoo Finance's per-ticker feed over a rolling 14-day window. Items
    are epistemically labeled — Status: body (fetched article text, an
    untrusted quote) or Status: lead (feed blurb); leads suggest but can never
    establish facts, and bare headlines are excluded entirely. Source tags name
    the feed, publisher, date, and URL. Never raises; degrades to bracketed
    "[news unavailable: ...]" or "[no news items ...]" markers."""
    return get_news_evidence(ticker)


# Register the three thin wrappers. WHY evidence_server.tool(fn) rather than a
# bare @decorator on each def: registration must not REPLACE the plain
# functions — the offline tests call them directly to prove the wrappers add
# no logic of their own.
evidence_server.tool(market_snapshot_tool)
evidence_server.tool(filings_evidence_tool)
evidence_server.tool(news_evidence_tool)
print("MCP: 'markut-evidence' server defined with 3 evidence tools")
