"""MCP output-equivalence acceptance (notebook cell 48) as a live test.
Run with: pytest -m live tests/test_mcp_equivalence.py
Live free APIs (yfinance/FMP/EDGAR/Yahoo), no Claude calls. The direct
build runs first and warms every cache; a diff confined to quote/price
lines is a live market tick (tolerated), anything structural fails."""
import difflib

import pytest

from markut.evidence.market import market_snapshot
from markut.evidence.rag import get_filings_evidence
from markut.evidence.news import get_news_evidence
from markut.mcp.client import call_evidence_tools


def _non_tick_diff_lines(direct, mcp):
    # changed lines that are NOT explainable as a live quote tick —
    # notebook rule: 'if ONLY quote/price lines differ, this is a market
    # tick, not a wrap bug'
    changed = [l[2:] for l in difflib.ndiff(direct.splitlines(), mcp.splitlines())
               if l[:2] in ('- ', '+ ')]
    return [l for l in changed if '$' not in l]


@pytest.mark.live
def test_mcp_packet_equals_direct_calls():
    ticker = 'NVDA'
    direct = (market_snapshot(ticker)
              + get_filings_evidence(ticker)
              + get_news_evidence(ticker))
    mcp = call_evidence_tools(ticker)
    if mcp == direct:
        return
    leftovers = _non_tick_diff_lines(direct, mcp)
    assert not leftovers, (
        f'structural MCP/direct divergence ({len(leftovers)} non-price lines): '
        + '; '.join(leftovers[:5]))
