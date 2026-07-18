"""Thin composition surface: the three evidence functions the MCP server
exposes. Keeps mcp/server.py free of deep imports."""
from markut.evidence.market import market_snapshot
from markut.evidence.rag import get_filings_evidence
from markut.evidence.news import get_news_evidence

__all__ = ["market_snapshot", "get_filings_evidence", "get_news_evidence"]
