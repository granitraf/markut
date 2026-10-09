"""Packet assembly in the order every agent reads it:
profile -> key questions -> evidence under Q1-Q5 -> general evidence ->
guidance -> valuation and market data -> news -> what would mislead ->
coverage gaps -> data gaps. PURE: strings in, one packet out."""
import re

from markut.evidence.gaps import summarize_gaps

PROFILE_TOKEN_CAP = 1000


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def profile_block(profile: dict, gaps: list = None, sources: list = None) -> str:
    p = profile or {}
    lines = ["[PROFILE] (what kind of business this is — read by the profiler from the filings; the debate is organized around it)"]
    if p.get("business"):
        lines.append(f"- Business: {_one_line(p['business'])}")
    if p.get("archetype"):
        lines.append(f"- Archetype: {p['archetype']}")
    for s in (p.get("segments") or [])[:8]:
        if not isinstance(s, dict) or not s.get("name"):
            continue
        parts = []
        if s.get("latest_revenue"):
            parts.append(f"latest revenue {_one_line(s['latest_revenue'])}")
        if s.get("latest_yoy"):
            parts.append(f"{_one_line(s['latest_yoy'])} YoY")
        if s.get("period"):
            parts.append(f"({_one_line(s['period'])})")
        src = f"  [source: {s['source']}]" if s.get("source") else ""
        lines.append(f"- Segment: {_one_line(s['name'])} — {' '.join(parts) if parts else 'figures not stated'}{src}")
    for k in (p.get("company_kpis") or [])[:8]:
        if not isinstance(k, dict) or not k.get("name"):
            continue
        detail = _one_line(k.get("definition", ""))
        unit = f"; unit: {_one_line(k['unit'])}" if k.get("unit") else ""
        seg = f"; segment: {_one_line(k['segment'])}" if k.get("segment") else ""
        src = f"  [source: {k['source']}]" if k.get("source") else ""
        lines.append(f"- KPI: {_one_line(k['name'])} — {detail}{unit}{seg}{src}")
    if p.get("accounting_flags"):
        lines.append("- Accounting flags: " + ", ".join(str(f) for f in p["accounting_flags"]))
    if sources:
        lines.append("- Profile sources: " + " | ".join(sources))
    for g in gaps or []:
        lines.append(f"- Profile gap: {g}")
    text = "\n".join(lines)
    limit = PROFILE_TOKEN_CAP * 4
    if len(text) > limit:
        kept = []
        for ln in lines:
            if sum(len(x) + 1 for x in kept) + len(ln) > limit:
                kept.append("- [profile trimmed to its token cap]")
                break
            kept.append(ln)
        text = "\n".join(kept)
    return text


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


def coverage_block(coverage: dict, plan: dict, research_pass: int, profile_gaps: list = None) -> str:
    qtext = {q.get("id"): q.get("question", "") for q in (plan or {}).get("key_questions", [])}
    lines = ["[COVERAGE GAPS] (questions with no sourced evidence after research; the debate must treat them as open)"]
    missing = [q for q, n in (coverage or {}).items() if not n]
    for q in missing:
        lines.append(f"- COVERAGE GAP {q}: no sourced evidence found after {research_pass} pass(es) — {_one_line(qtext.get(q, ''))}")
    for g in profile_gaps or []:
        lines.append(f"- COVERAGE GAP profile: {g}")
    if not missing and not profile_gaps:
        lines.append("- none — every question has sourced evidence")
    return "\n".join(lines)


def assemble_packet(profile: dict, plan: dict, filings: str, market: str, news: str, coverage: dict,
                    research_pass: int = 1, profile_gaps: list = None, profile_sources: list = None) -> tuple:
    """-> (packet, data_gaps). Order per the spec; the DATA GAPS block goes
    last beside the coverage gaps, with every raw error marker rewritten."""
    parts = [profile_block(profile, profile_gaps, profile_sources), questions_block(plan)]
    parts.append((filings or "").strip())
    parts.append((market or "").strip())
    parts.append((news or "").strip())
    parts.append(mislead_block(plan))
    parts.append(coverage_block(coverage, plan, research_pass, profile_gaps))
    body = "\n\n".join(p for p in parts if p)
    return summarize_gaps(body, position="end")
