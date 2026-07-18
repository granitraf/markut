"""The notebook suite's offline MCP block (assembly ordering, failure
marker via a fake async client, thin-wrapper proof) migrated verbatim.
The evidence-function swap now patches markut.mcp.server attributes —
the namespace the tool wrappers resolve at call time."""
from types import SimpleNamespace

import markut.mcp.server as _server_mod
from markut.mcp.client import _assemble_packet, call_evidence_tools
from markut.mcp.server import (market_snapshot_tool, filings_evidence_tool,
    news_evidence_tool)


def check(label, condition):
    assert condition, label


def test_mcp_offline_block():
    print("\n--- MCP evidence layer: assembly, failure marker, thin wrappers (offline) ---")
    # No network and no real tool execution: (b) tests the PURE assembler, (a) uses
    # a FAKE client exposing the same async surface as the real one, (c) swaps the
    # underlying evidence functions for stubs (swap-and-restore, the usual pattern).
    try:
        # (b) stable ordering regardless of discovery order
        _mcp_results = {"news_evidence_tool": "<NEWS>",
                        "market_snapshot_tool": "<MARKET>",
                        "filings_evidence_tool": "<FILINGS>"}
        _mcp_scrambled = ["news_evidence_tool", "filings_evidence_tool", "market_snapshot_tool"]
        check("MCP assembly imposes market -> filings -> news regardless of discovery order",
              _assemble_packet(_mcp_scrambled, _mcp_results) == "<MARKET><FILINGS><NEWS>")
        check("MCP assembly appends an unknown future tool AFTER the known three",
              _assemble_packet(_mcp_scrambled + ["extra_tool"],
                               {**_mcp_results, "extra_tool": "<EXTRA>"})
              == "<MARKET><FILINGS><NEWS><EXTRA>")

        # (a) fake client where ONE tool raises. WHY a throwaway type instead of
        # SimpleNamespace: `async with` resolves __aenter__/__aexit__ on the TYPE,
        # never on the instance, so the fake needs a real (local, test-only) type.
        def _fake_mcp_client(fail_tool):
            async def _aenter(self):
                return self
            async def _aexit(self, *exc):
                return False
            async def _list_tools(self):
                return [SimpleNamespace(name=n) for n in
                        ["news_evidence_tool", "market_snapshot_tool", "filings_evidence_tool"]]
            async def _call_tool(self, name, args):
                if name == fail_tool:
                    raise RuntimeError("simulated source outage")
                return SimpleNamespace(data=f"<{name}:{args['ticker']}>", content=[])
            return type("FakeMCPClient", (), {"__aenter__": _aenter, "__aexit__": _aexit,
                                              "list_tools": _list_tools,
                                              "call_tool": _call_tool})()

        _mcp_out = call_evidence_tools("NVDA", client=_fake_mcp_client("filings_evidence_tool"))
        check("MCP: one failing tool -> '[evidence source unavailable: ...]' marker",
              "[evidence source unavailable: filings_evidence_tool," in _mcp_out)
        check("MCP: the other two sections survive, in stable order around the marker",
              _mcp_out.index("<market_snapshot_tool:NVDA>")
              < _mcp_out.index("[evidence source unavailable")
              < _mcp_out.index("<news_evidence_tool:NVDA>"))

        # (c) the tool wrappers are THIN: with the underlying functions stubbed,
        # each wrapper returns exactly the stub output — no added logic anywhere.
        def _stub_market(ticker):
            return f"[stub market {ticker}]"
        def _stub_filings(ticker):
            return f"[stub filings {ticker}]"
        def _stub_news(ticker):
            return f"[stub news {ticker}]"
        _real_market, _real_filings, _real_news = _server_mod.market_snapshot, _server_mod.get_filings_evidence, _server_mod.get_news_evidence
        _server_mod.market_snapshot, _server_mod.get_filings_evidence, _server_mod.get_news_evidence = _stub_market, _stub_filings, _stub_news
        try:
            check("market_snapshot_tool is thin", market_snapshot_tool("NVDA") == "[stub market NVDA]")
            check("filings_evidence_tool is thin", filings_evidence_tool("NVDA") == "[stub filings NVDA]")
            check("news_evidence_tool is thin", news_evidence_tool("NVDA") == "[stub news NVDA]")
        finally:
            _server_mod.market_snapshot, _server_mod.get_filings_evidence, _server_mod.get_news_evidence = _real_market, _real_filings, _real_news
    except Exception as e:
        check(f"MCP evidence tests (unexpectedly raised: {e})", False)
