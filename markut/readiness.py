"""Readiness check: for ANY ticker, is the evidence an accurate equity report
needs present and sound? (2026-10-11)

Runs the free part of the pipeline — the filing set, the core figures, the
market data and the evidence-quality check; no profiler, planner or debate,
so no model calls — and scores the packet by general rules:

  no_filings          no SEC filings to read (the report withholds a lean)
  no_revenue          no revenue line in the income statement summary
  stale_release       the earnings release is a quarter older than the interim report
  old_filings         the newest filing is more than 200 days old
  undated             the revenue figure has no period
  no_year_ago         revenue is not compared with the same period a year before
  no_net_income       no net income line
  no_cash_flow        no operating cash flow line
  no_balance_sheet    no balance-sheet figure
  units_missing       amounts printed with no units ("as printed")
  currency_unlabelled a foreign filer's amounts without their currency
  no_market_data      no price or market cap
  suspect_figure      the evidence-quality check found an out-of-range figure
  disputed_figure     a filing and the data provider disagree for the same period

READY = nothing wrong; PARTIAL = a report can run, the named gaps are in it;
NOT READY = no filings, no revenue, or the check itself failed.

CLI (free): python -m markut.readiness AAPL TSM …  |  --sample 50 [--seed 1]
"""
import json
import re
import sys
from datetime import date

BLOCKING = {"no_filings", "no_revenue", "error"}
STALE_RELEASE_DAYS = 45      # a release this much older than the interim report is the previous quarter's
OLD_FILINGS_DAYS = 200       # longer than two quarters without a filing


def _block(core: str, title: str) -> list:
    out, inside = [], False
    for ln in (core or "").splitlines():
        s = ln.strip()
        if s.startswith("-- ") and s.endswith(" --"):
            inside = s[3:-3].lower().startswith(title.lower())
            continue
        if s.startswith("["):
            inside = False
        if inside and s.startswith("- ") and not re.match(r"(?i)^- (?:none|no )", s):
            out.append(s)
    return out


def _days(a: str, b: str) -> int:
    return (date.fromisoformat(b[:10]) - date.fromisoformat(a[:10])).days


def score(ticker: str, info: dict, core: str, market: str, today: str = None) -> dict:
    """PURE. -> {"ticker", "verdict", "issues": [{"type", "detail"}], "found": {...}}."""
    today = today or date.today().isoformat()
    issues = []
    add = lambda t, d: issues.append({"type": t, "detail": d})
    if not (info or {}).get("filings"):
        add("no_filings", (info or {}).get("reason") or "no SEC filings found")
    else:
        rel, mid = (info.get("earnings") or {}).get("filing_date"), (info.get("interim") or {}).get("filing_date")
        if rel and mid and _days(rel, mid) > STALE_RELEASE_DAYS:
            add("stale_release", f"earnings release filed {rel}, interim report {mid}")
        newest = max((d.get("filing_date") for d in (info.get("annual"), info.get("interim"), info.get("earnings")) if d), default=None)
        if newest and _days(newest, today) > OLD_FILINGS_DAYS:
            add("old_filings", f"newest filing {newest}")
        income = _block(core, "Income statement summary")
        # the extractor's own rules, on the line's kind or its printed label
        # ("Total revenues", "Net Operating Revenues", "Net sales")
        from markut.evidence.core import INCOME_ROWS
        label = lambda l: re.sub(r"\s*\[[^\]]*\]$", "", l[2:].split(":")[0]).strip().lower()
        is_row = lambda l, i, kind: l.startswith(f"- {kind}:") or bool(re.search(INCOME_ROWS[i][1], label(l)))
        rev = next((l for l in income if is_row(l, 0, "Revenue")), None)
        # a bank's revenue is derived (net interest income + non-interest income)
        rev = rev or next((l for l in (core or "").splitlines() if l.startswith("- DERIVED Revenue:")), None)
        if not rev:
            add("no_revenue", "no revenue line in the income statement summary")
        else:
            if re.search(r"\((?:unstated|change)[,)]", rev) or not re.search(r"\((?:quarter|YTD|annual|ttm|period ended)", rev):
                add("undated", rev[:120])
            if "year-ago" not in rev:
                add("no_year_ago", rev[:120])
        if not any(is_row(l, 3, "Net income") or re.match(r"(?i)^- Net (?:income|earnings)\b", l) for l in income):
            add("no_net_income", "no net income line")
        if not any(l.startswith("- Operating cash flow") for l in _block(core, "Cash flow summary")):
            add("no_cash_flow", "no operating cash flow line")
        if not _block(core, "Balance sheet summary"):
            add("no_balance_sheet", "no balance-sheet figure")
        money = [l for t in ("Income statement summary", "Cash flow summary", "Balance sheet summary") for l in _block(core, t)
                 if not re.search(r"\((?:per share|percent|count|[A-Z]{3} per share)", l) and not l.startswith("- DERIVED")]
        # (a derived line states its units inside its formula)
        bare = [l for l in money if "(as printed;" in l]
        if bare:
            add("units_missing", f"{len(bare)} amount line(s) without units, e.g. {bare[0][:90]}")
        ccy = re.search(r"financial statements are in ([A-Z]{3})\b", market or "")
        if ccy and ccy.group(1) != "USD":
            unlabelled = [l for l in money if f"({ccy.group(1)} " not in l]
            if unlabelled:
                add("currency_unlabelled", f"{len(unlabelled)} amount line(s) without {ccy.group(1)}, e.g. {unlabelled[0][:90]}")
    if not re.search(r"(?m)^- Price \(current\):", market or "") or not re.search(r"(?m)^- Market cap:", market or ""):
        add("no_market_data", "no price or market cap")
    from markut.evidence.quality import quality_block
    block, q = quality_block("[FILINGS]\n" + (core or "") + "\n\n" + (market or ""), [], {})
    if q["suspect"]:
        add("suspect_figure", "; ".join(re.findall(r"(?m)^- SUSPECT: ([^—\n]+)", block))[:200])
    if q["disputed"]:
        add("disputed_figure", f"{q['disputed']} filing/provider disagreement(s)")
    kinds = {i["type"] for i in issues}
    verdict = "NOT READY" if kinds & BLOCKING else "PARTIAL" if issues else "READY"
    return {"ticker": ticker, "verdict": verdict, "issues": issues,
            "found": {"filer_type": (info or {}).get("filer_type", ""),
                      "forms": [f"{d['form']} {d['filing_date']}" for d in ((info or {}).get(k) for k in ("annual", "interim", "earnings")) if d]}}


def rescore(rows: list, today: str = None) -> list:
    """Score saved rows again from the text they carry — no network (after a
    fix to the extractor's scoring rules, or to compare two versions)."""
    out = []
    for r in rows:
        t = r.get("text")
        out.append(dict(score(r["ticker"], t["info"], t["core"], t["market"], today), text=t) if t else r)
    return out


def summarize(rows: list) -> str:
    """Verdict counts, then every issue type with how many tickers have it."""
    from collections import Counter
    v = Counter(r["verdict"] for r in rows)
    t = Counter(i["type"] for r in rows for i in {i["type"]: i for i in r["issues"]}.values())
    lines = [f"{len(rows)} tickers: " + " · ".join(f"{k} {v.get(k, 0)}" for k in ("READY", "PARTIAL", "NOT READY"))]
    lines += [f"  {k:20} {n:3}  ({', '.join(r['ticker'] for r in rows if any(i['type'] == k for i in r['issues']))[:150]})"
              for k, n in t.most_common()]
    return "\n".join(lines)


def check(ticker: str) -> dict:
    """Live: the filing set, core figures, market data — no model calls."""
    import contextlib
    import io
    from markut.evidence import core as core_mod, sections as secs
    from markut.mcp.server import market_snapshot_tool
    ticker = ticker.upper().strip()
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                fs = secs.filing_set(ticker)
            except ValueError as e:
                info, market = {"filings": False, "reason": str(e)[:160]}, market_snapshot_tool(ticker)
                return dict(score(ticker, info, "", market), text={"info": info, "core": "", "market": market})
            roles = {"annual": fs.get("10-K"), "interim": fs.get("10-Q"), "earnings": fs.get("8-K")}
            info = {"filings": any(roles.values()), "filer_type": fs.get("filer_type", ""),
                    "reason": "; ".join(f"{k}: {v}" for k, v in (fs.get("reasons") or {}).items()),
                    **{k: ({"form": d["form"], "filing_date": d["filing_date"]} if d else None) for k, d in roles.items()}}
            core_text = core_mod.extract_core(fs, {}, None)["text"] if info["filings"] else ""
            market = market_snapshot_tool(ticker)
        return dict(score(ticker, info, core_text, market), text={"info": info, "core": core_text, "market": market})
    except Exception as e:
        return {"ticker": ticker, "verdict": "NOT READY", "issues": [{"type": "error", "detail": f"{type(e).__name__}: {str(e)[:160]}"}],
                "found": {}}


def sample(n: int, seed: int = 1) -> list:
    """n tickers from the SEC company list, spread over large, mid and small
    companies (the list runs roughly largest first), one per company."""
    import os
    import random
    from markut.evidence.edgar import EDGAR_CACHE, ticker_to_cik
    ticker_to_cik("AAPL")                                        # makes sure the list is cached
    rows = list(json.load(open(os.path.join(EDGAR_CACHE, "company_tickers.json"))).values())
    seen, firms = set(), []
    for r in rows:
        if r["cik_str"] not in seen and re.fullmatch(r"[A-Z]{1,5}", r["ticker"]):
            seen.add(r["cik_str"])
            firms.append(r["ticker"])
    rng = random.Random(seed)
    tiers = [(0, 500), (500, 3000), (3000, len(firms))]
    per = [n * 3 // 10, n * 4 // 10]
    per.append(n - sum(per))
    return [t for (a, b), k in zip(tiers, per) for t in rng.sample(firms[a:b], k)]


def main(argv=None):
    import argparse
    from markut.agents import llm
    def _no_model(*a, **k):
        raise RuntimeError("the readiness check makes no model calls")
    llm.call_claude = llm.call_claude_images = _no_model
    p = argparse.ArgumentParser(prog="python -m markut.readiness")
    p.add_argument("tickers", nargs="*")
    p.add_argument("--sample", type=int, default=0)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--json", default="", help="save the rows (with their text) here")
    p.add_argument("--rescore", default="", help="score a saved --json file again, offline")
    a = p.parse_args(argv)
    show = lambda r: print(f"{r['ticker']:6} {r['verdict']:9} " + ("; ".join(i["type"] for i in r["issues"]) or "-"), flush=True)
    if a.rescore:
        rows = rescore(json.load(open(a.rescore)))
        for r in rows:
            show(r)
    else:
        rows = []
        for t in [t.upper() for t in a.tickers] + (sample(a.sample, a.seed) if a.sample else []):
            rows.append(check(t))
            show(rows[-1])
    print()
    print(summarize(rows))
    if a.json:
        json.dump(rows, open(a.json, "w"), indent=1)


if __name__ == "__main__":
    main(sys.argv[1:])
