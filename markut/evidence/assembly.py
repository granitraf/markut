"""Packet assembly in the order every agent reads it:
profile -> key questions -> evidence under Q1-Q5 -> general evidence ->
guidance -> valuation and market data -> news -> what would mislead ->
coverage gaps -> data gaps. PURE: strings in, one packet out."""
import re

from markut.evidence.gaps import summarize_gaps

PROFILE_TOKEN_CAP = 1000
SEGMENT_CAP = 24          # a list longer than this says "N of M shown" — never silently stops


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


_MONEY_RE = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)\s*(thousand|million|billion|trillion|[KMBT])?\b", re.IGNORECASE)
_SCALE = {"thousand": 1e3, "k": 1e3, "million": 1e6, "m": 1e6, "billion": 1e9, "b": 1e9, "trillion": 1e12, "t": 1e12}


def _money(text: str):
    m = _MONEY_RE.search(text or "")
    if not m:
        return None
    return float(m.group(1).replace(",", "")) * _SCALE.get((m.group(2) or "").lower(), 1.0)


def _segment_change(seg: dict) -> str:
    """Run #26 (P2, item 8): 'latest revenue $45,781 million not stated (prior
    year $41,198 million) YoY' — both values were there; compute the change."""
    yoy = _one_line(seg.get("latest_yoy", ""))
    if re.search(r"[-+]?\d+(?:\.\d+)?\s?%", yoy):
        return f"{yoy} YoY"
    cur = _money(seg.get("latest_revenue", ""))
    prior_m = re.search(r"(?i)prior[- ]year[^$]*(\$\s?[\d,]+(?:\.\d+)?\s*(?:thousand|million|billion|trillion|[KMBT])?)", yoy + " " + seg.get("latest_revenue", ""))
    prev = _money(prior_m.group(1)) if prior_m else None
    if cur and prev:
        return f"{(cur / prev - 1) * 100:+.1f}% YoY (DERIVED: latest / prior-year {prior_m.group(1).strip()} − 1)"
    return f"{yoy} YoY" if yoy and not re.match(r"(?i)^not stated$", yoy) else "YoY change not in the sections read"


def leverage_with_filed_leases(market_text: str, filings_text: str):
    """Run #26 (P2, item 7): when the provider's debt figure includes no lease
    obligations, source them from the filing (a lease-liability row in the
    core figures) and compute the lease-inclusive ratio — with every input
    and its source on the line. None when either side is missing."""
    m = re.search(r"Debt/Equity on the data provider's total debt[^:]*: [\d.]+% = total debt (\$[\d.,]+[KMBT]?) / stockholders' equity (\$[\d.,]+[KMBT]?)",
                  market_text or "")
    if not m:
        return None
    debt, equity = _money(m.group(1)), _money(m.group(2))
    # the filed total when there is one; else current + non-current from the
    # same table, both named; one unqualified row is ambiguous (it may be only
    # the current portion) and is never used as the total
    rows = [l.strip() for l in (filings_text or "").splitlines() if re.match(r"(?i)^- [^:]*lease liabilit", l.strip())]
    def amount(line):
        v = re.search(r":\s*\$?([\d,]+(?:\.\d+)?)\s*(thousand|million|billion)?\s*(?:\(|as of)", line)
        return float(v.group(1).replace(",", "")) * _SCALE.get((v.group(2) or "").lower(), 1.0) if v else None
    def label(line):
        return line[2:line.index("]:") + 1] if "]:" in line else line[2:].split(":")[0]
    def table(line):
        t = re.search(r"\[source: (T\d+)\]", line)
        return t.group(1) if t else ""
    noncur = [l for l in rows if re.search(r"(?i)non-?current|noncurrent|long-term", label(l))]
    cur = [l for l in rows if re.search(r"(?i)\bcurrent\b", label(l)) and l not in noncur]
    total = next((l for l in rows if re.match(r"(?i)^- total (?:operating )?lease liabilit", l)), None)
    parts = ""
    if total and amount(total):
        lease, src = amount(total), table(total)
    else:
        pair = next(((c, n) for c in cur for n in noncur if table(c) == table(n) and amount(c) and amount(n)), None)
        if not pair:
            return None
        lease, src = amount(pair[0]) + amount(pair[1]), table(pair[0])
        parts = (f" [current {{}} + non-current {{}}, summed — the filing prints no total]", amount(pair[0]), amount(pair[1]))
    if not debt or not equity:
        return None
    legend = re.search(r"^\s*" + re.escape(src) + r" = (.+)$", filings_text, re.M) if src else None
    where = legend.group(1) if legend else "the filing's lease liability lines"
    from markut.evidence.market import fmt_big
    detail = parts[0].format(fmt_big(parts[1]), fmt_big(parts[2])) if parts else ""
    return (f"- Debt/Equity including the filing's lease liabilities (computed): {(debt + lease) / equity * 100:.1f}% = (provider total debt "
            f"{fmt_big(debt)} + lease liabilities {fmt_big(lease)}{detail}) / stockholders' equity {fmt_big(equity)}  "
            f"[source: computed from yfinance/quarterly_balance_sheet and {where}]")


def profile_block(profile: dict, gaps: list = None, sources: list = None) -> str:
    p = profile or {}
    lines = ["[PROFILE] (what kind of business this is — read by the profiler from the filings; the debate is organized around it)"]
    if p.get("business"):
        lines.append(f"- Business: {_one_line(p['business'])}")
    if p.get("archetype"):
        lines.append(f"- Archetype: {p['archetype']}")
    all_segs = [x for x in (p.get("segments") or []) if isinstance(x, dict) and x.get("name")]
    for s in all_segs[:SEGMENT_CAP]:
        parts = []
        if s.get("latest_revenue"):
            parts.append(f"latest revenue {_one_line(s['latest_revenue'])}")
            parts.append(_segment_change(s))
        elif s.get("latest_yoy"):
            parts.append(f"{_one_line(s['latest_yoy'])} YoY")
        if s.get("period"):
            parts.append(f"({_one_line(s['period'])})")
        src = f"  [source: {s['source']}]" if s.get("source") else ""
        lines.append(f"- Segment: {_one_line(s['name'])} — {' '.join(parts) if parts else 'figures not stated'}{src}")
    if len(all_segs) > SEGMENT_CAP:
        lines.append(f"- [{SEGMENT_CAP} of {len(all_segs)} shown — segment list capped]")
    for k in (p.get("company_kpis") or []):
        if not isinstance(k, dict) or not k.get("name"):
            continue
        detail = _one_line(k.get("definition", ""))
        unit = f"; unit: {_one_line(k['unit'])}" if k.get("unit") else ""
        seg = f"; segment: {_one_line(k['segment'])}" if k.get("segment") else ""
        src = f"  [source: {k['source']}]" if k.get("source") else ""
        nd = "  [not found in the latest filings]" if k.get("disclosed") is False else ""
        lines.append(f"- KPI: {_one_line(k['name'])} — {detail}{unit}{seg}{src}{nd}")
    if p.get("accounting_flags"):
        lines.append("- Accounting flags: " + ", ".join(str(f) for f in p["accounting_flags"]))
    if sources:
        lines.append("- Profile sources: " + " | ".join(sources))
    for g in gaps or []:
        lines.append(f"- Profile gap: {g}")
    text = "\n".join(lines)
    limit = PROFILE_TOKEN_CAP * 4
    if len(text) > limit:
        # cut descriptions first (KPI definitions, then the business text);
        # never drop accounting flags, segment figures or KPI names/values
        lines = [_re_sub_kpi_def(ln) if ln.startswith("- KPI:") else ln for ln in lines]
        text = "\n".join(lines)
        if len(text) > limit:
            lines = [(_one_line(ln.split(".")[0]) + ".") if ln.startswith("- Business:") else ln for ln in lines]
            text = "\n".join(lines)
        if len(text) > limit:
            # last resort: drop KPI lines from the end and SAY how many are shown;
            # segment figures and accounting flags are never dropped
            kpi_idx = [i for i, ln in enumerate(lines) if ln.startswith("- KPI:")]
            total = len(kpi_idx)
            while len("\n".join(lines)) > limit and kpi_idx:
                lines.pop(kpi_idx.pop())
            if total and len(kpi_idx) < total:
                lines.append(f"- [{len(kpi_idx)} of {total} KPIs shown — profile token cap]")
            text = "\n".join(lines)
    return text


def _re_sub_kpi_def(line: str) -> str:
    # "- KPI: name — definition; unit: u; segment: s  [source: id]" -> keep name, unit, segment, source
    m = re.match(r"^- KPI: ([^—]+) — .*?((?:; unit: [^;\]]+)?(?:; segment: [^\]]+?)?)\s*(\[source: [^\]]+\])?\s*$", line)
    if not m:
        return line
    tail = (m.group(2) or "").lstrip("; ")
    return f"- KPI: {m.group(1).strip()}" + (f" ({tail})" if tail else "") + (f"  {m.group(3)}" if m.group(3) else "")


def questions_block(plan: dict) -> str:
    lines = ["[KEY QUESTIONS] (set by the planner from the profile — the five questions that decide the outlook; "
             "every bull/bear point is labeled with one of them)"]
    for q in (plan or {}).get("key_questions", []):
        extras = []
        if q.get("kpis"):
            extras.append("KPIs: " + ", ".join(str(k) for k in q["kpis"][:4]))
        if q.get("segments"):
            extras.append("segments: " + ", ".join(str(s) for s in q["segments"][:3]))
        lines.append(f"- {q.get('id', 'Q?')}: {_one_line(q.get('question', ''))}" + (f" ({'; '.join(extras)})" if extras else ""))
    return "\n".join(lines)


def mislead_block(plan: dict) -> str:
    items = [_one_line(x) for x in (plan or {}).get("what_would_mislead", []) if _one_line(x)]
    lines = ["[WHAT WOULD MISLEAD] (planner: ways standard metrics could mislead for this company — both sides must respect these)"]
    lines += [f"- {x}" for x in items] or ["- none stated"]
    return "\n".join(lines)


def coverage_block(coverage: dict, plan: dict, research_pass: int, profile_gaps: list = None, detail: dict = None) -> str:
    qtext = {q.get("id"): q.get("question", "") for q in (plan or {}).get("key_questions", [])}
    lines = ["[COVERAGE GAPS] (questions the packet cannot answer for the current period; the debate must treat them as open)"]
    missing = [q for q, n in (coverage or {}).items() if not n]
    for q in missing:
        d = (detail or {}).get(q) or {}
        metrics = ", ".join(d.get("missing") or [])
        why = ("no current-period value for " + metrics if metrics else "no current-period figure or excerpt")
        if d.get("prior_only"):
            why += "; only prior-year excerpts"
        elif not d.get("relevant"):
            why += f"; no sourced evidence after {research_pass} pass(es)"
        lines.append(f"- COVERAGE GAP {q}: {why} — {_one_line(qtext.get(q, ''))}")
    for g in profile_gaps or []:
        lines.append(f"- COVERAGE GAP profile: {g}")
    if not missing and not profile_gaps:
        lines.append("- none — every question has sourced evidence")
    return "\n".join(lines)


def assemble_packet(profile: dict, plan: dict, filings: str, market: str, news: str, coverage: dict,
                    research_pass: int = 1, profile_gaps: list = None, profile_sources: list = None, detail: dict = None) -> tuple:
    """-> (packet, data_gaps). Order per the spec; the DATA GAPS block goes
    last beside the coverage gaps, with every raw error marker rewritten."""
    parts = [profile_block(profile, profile_gaps, profile_sources), questions_block(plan)]
    parts.append((filings or "").strip())
    market = (market or "").strip()
    lev = leverage_with_filed_leases(market, filings or "")
    if lev:
        market = market + "\n" + lev
    parts.append(market)
    parts.append((news or "").strip())
    parts.append(mislead_block(plan))
    parts.append(coverage_block(coverage, plan, research_pass, profile_gaps, detail))
    body = "\n\n".join(p for p in parts if p)
    return summarize_gaps(body, position="end")
