"""MCP client (notebook cell 32, verbatim).

ENTRY POINTS / ASYNC SEAM:
- run.py (CLI) and pytest: no event loop is running, so
  _run_coro_blocking uses plain asyncio.run().
- demo.ipynb (Jupyter, ipykernel 7): the kernel owns a running loop, so
  the same function hands the coroutine to a throwaway worker thread.
One code path, both worlds — the branch is the try/except below."""
from fastmcp import Client

from markut.mcp.server import evidence_server

import asyncio
from concurrent.futures import ThreadPoolExecutor

# ---------------- MCP client: how the Research step consumes evidence ----------------
# The packet's stable reading order — the order the debate prompts have always
# assumed. Discovery may return tools in ANY order; assembly re-imposes this.
EVIDENCE_TOOL_ORDER = ["market_snapshot_tool", "filings_evidence_tool", "news_evidence_tool"]


def _run_coro_blocking(coro):
    # WHY a throwaway worker thread instead of plain asyncio.run(): Jupyter's
    # kernel (ipykernel 7) already owns a RUNNING event loop on this thread,
    # and asyncio.run() refuses to nest ("cannot be called from a running
    # event loop"). Handing the coroutine to a fresh thread gives it a fresh
    # loop — no nesting, and no nest_asyncio monkey-patching of the kernel's
    # loop. Outside Jupyter there is no running loop, so asyncio.run() is used
    # directly; both worlds are covered.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _tool_result_text(result) -> str:
    # WHY defensive extraction: the FastMCP result shape has shifted across
    # versions. Prefer .data (the structured return — our tools return str);
    # fall back to joining the text content blocks.
    data = getattr(result, "data", None)
    if isinstance(data, str):
        return data
    parts = [c.text for c in (getattr(result, "content", None) or []) if hasattr(c, "text")]
    return "".join(parts)


def _assemble_packet(discovered: list, results: dict) -> str:
    # PURE (offline-testable): impose the stable market -> filings -> news
    # order no matter what order discovery listed the tools in. Any FUTURE
    # tool the server grows appends AFTER the known three, so adding a source
    # never reshuffles the packet layout the agents were prompted around.
    ordered = [name for name in EVIDENCE_TOOL_ORDER if name in discovered]
    ordered += [name for name in discovered if name not in EVIDENCE_TOOL_ORDER]
    return "".join(results.get(name, f"\n\n[evidence source unavailable: {name}, no result]")
                   for name in ordered)


async def _gather_evidence(ticker: str, client, tool_args: dict = None, skip: list = None) -> tuple:
    # Discovery FIRST (list_tools — the demo moment: nothing below hardcodes
    # the three tool names), then one call per discovered tool. Per-tool
    # try/except: one dead source contributes a marker line and the packet
    # survives — the same degrade philosophy the sections themselves follow.
    # tool_args = {tool name: extra arguments} (the filings tool takes the
    # planner's questions); skip = tools whose result the caller already holds.
    tool_args, skip = tool_args or {}, set(skip or [])
    async with client:
        discovered = [tool.name for tool in await client.list_tools()]
        print(f"MCP: discovered {len(discovered)} evidence tool(s): {discovered}")
        results = {}
        for name in discovered:
            if name in skip:
                continue
            try:
                result = await client.call_tool(name, {"ticker": ticker, **tool_args.get(name, {})})
                results[name] = _tool_result_text(result)
                print(f"MCP: called {name} -> {len(results[name])} chars")
            except Exception as e:
                results[name] = f"\n\n[evidence source unavailable: {name}, {e}]"
                print(f"MCP: {name} failed ({e}) — marker added, packet continues")
        return discovered, results


def gather_evidence(ticker: str, tool_args: dict = None, skip: list = None, client=None) -> dict:
    """{tool name: section text} for every discovered tool not in skip —
    the research node composes the packet in its own order from these."""
    if client is None:
        client = Client(evidence_server)
    _discovered, results = _run_coro_blocking(_gather_evidence(ticker, client, tool_args, skip))
    return results


def call_one_tool(name: str, args: dict, client=None) -> str:
    """One evidence tool by name (the planner fetches the market snapshot
    before research runs). Raises on failure — the caller decides the marker."""
    if client is None:
        client = Client(evidence_server)

    async def _one():
        async with client:
            return _tool_result_text(await client.call_tool(name, args))
    return _run_coro_blocking(_one())


def call_evidence_tools(ticker: str, client=None) -> str:
    # WHY the client parameter: None means the real IN-MEMORY client against
    # evidence_server (production path — same process, same event-loop story,
    # nothing to spawn). Tests inject a fake with the same async surface, so
    # the discovery/failure/assembly logic is provable offline.
    if client is None:
        client = Client(evidence_server)
    discovered, results = _run_coro_blocking(_gather_evidence(ticker, client))
    return _assemble_packet(discovered, results)
