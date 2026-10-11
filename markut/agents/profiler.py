"""Profiler and planner nodes: understand the company before researching it.

profiler_node — code extracts four filing sections by heading (Item 1, MD&A
overview, segment note, earnings exhibit; an image-only deck is transcribed
once per filing), ONE strict-JSON call turns them into a profile, a code
check retries once or records a coverage gap, and the result is cached by
ticker + the accession numbers of the latest 10-K / 10-Q / earnings 8-K.

planner_node — ONE strict-JSON call: the profile plus a short market-data
summary -> the five questions that decide the outlook, the ways standard
metrics could mislead here, and (stored only) peer tickers. Same cache key."""
import json

from markut import config, store
from markut.agents import llm, prompts
from markut.agents.schemas import PLAN_SCHEMA, PROFILE_SCHEMA
from markut.agents.state import DebateState
import re as _re

from markut.guardrails.tracer import extract_evidence_numbers, extract_numeric_claims, norm_or_none

MARKET_SUMMARY_SECTIONS = ("[QUOTE & VALUATION]", "[FUNDAMENTALS]", "[VALUATION]", "[ANALYST VIEW]", "[EVENTS]")


def _parse_json(raw: str) -> dict:
    data = json.loads((raw or "").strip())
    if not isinstance(data, dict):
        raise ValueError("reply is not a JSON object")
    return data


REMOVED_MARK = "[figure removed: not stated in the text the model was given]"


def _mantissa(raw: str) -> str:
    # the digits of a figure without currency, scale word or unit: "$20,272
    # million" -> "20272", "6.7%" -> "6.7" — a table cell under a "$ millions"
    # header is restated with its scale word, which is a correct reading
    s = _re.sub(r"[,$\s]", "", str(raw).lower())
    m = _re.match(r"[+\-]?(\d+(?:\.\d+)?)", s)
    if not m:
        return ""
    v = m.group(1)
    return v.rstrip("0").rstrip(".") if "." in v else v


def allowed_numbers(source_text: str) -> set:
    """Every normalized figure in the text PLUS every bare mantissa, so a
    restatement with a scale word or a unit still traces."""
    out = set(extract_evidence_numbers(source_text))
    out |= {_mantissa(m) for m in _re.findall(r"\d[\d,]*(?:\.\d+)?", source_text or "")}
    out.discard("")
    return out


def _rounds_to(claim_mantissa: str, allowed: set) -> bool:
    # "47%" against a shown "47.10%", "2.5x" against "2.52": a figure rounded
    # to fewer decimals than the text printed is the same figure
    try:
        target = float(claim_mantissa)
    except ValueError:
        return False
    decimals = len(claim_mantissa.split(".")[1]) if "." in claim_mantissa else 0
    for a in allowed:
        try:
            v = float(a)
        except ValueError:
            continue
        if v != target and round(v, decimals) == target:
            return True
    return False


def scrub_unverified_numbers(text, allowed: set) -> tuple:
    """PURE. Replace every figure in text whose normalized value (or bare
    mantissa, or a rounding of one) is not among `allowed` (the numbers of the
    text the model was given) with a visible marker. -> (scrubbed text,
    [removed figures]). The model may use outside knowledge to CHOOSE what to
    ask; it may never STATE a figure the filings did not give it — the rule
    the governor applies to the verdict."""
    text = str(text or "")
    removed = []
    for claim in extract_numeric_claims(text):
        key = norm_or_none(claim)
        m = _mantissa(claim)
        if key is None or key in allowed or m in allowed or _rounds_to(m, allowed):
            continue
        removed.append(claim)
        text = text.replace(claim, REMOVED_MARK, 1)
    return text, removed


def verify_profile_numbers(profile: dict, source_text: str) -> tuple:
    """Every figure in the profile must appear in the sections the profiler
    read. -> (profile with unverifiable figures marked, [removed figures])."""
    allowed = allowed_numbers(source_text)
    removed = []
    out = dict(profile or {})
    out["business"], r = scrub_unverified_numbers(out.get("business", ""), allowed)
    removed += r
    for key, fields in (("segments", ("latest_revenue", "latest_yoy", "period")), ("company_kpis", ("definition", "unit"))):
        rows = []
        for row in out.get(key) or []:
            if not isinstance(row, dict):
                continue
            row = dict(row)
            for f in fields:
                row[f], r = scrub_unverified_numbers(row.get(f, ""), allowed)
                removed += r
            rows.append(row)
        out[key] = rows
    return out, removed


def verify_plan_numbers(plan: dict, source_text: str) -> tuple:
    """Figures in the planner's questions and pitfalls must come from the
    profile or the market summary it was shown."""
    allowed = allowed_numbers(source_text)
    removed = []
    out = dict(plan or {})
    qs = []
    for q in out.get("key_questions") or []:
        q = dict(q)
        for f in ("question", "why"):
            q[f], r = scrub_unverified_numbers(q.get(f, ""), allowed)
            removed += r
        qs.append(q)
    out["key_questions"] = qs
    mislead = []
    for m in out.get("what_would_mislead") or []:
        t, r = scrub_unverified_numbers(m, allowed)
        removed += r
        mislead.append(t)
    out["what_would_mislead"] = mislead
    return out, removed


# ---------------- run #26 (P1, item 5): what the filings actually disclose ----------------
_NEGATED_RE = _re.compile(r"(?i)\b(?:no longer|does not|do not|did not|not|ceased to)\s+(?:separately\s+)?(?:report|disclose|provide|publish|break out)")


def text_disclosed(metric: str, text: str):
    """True when a sentence of the filing text names the metric with a figure,
    False when a sentence says the metric is NOT reported, None otherwise."""
    from markut.evidence.core import _distinctive, _norm_words
    distinctive = _distinctive(metric)
    if not distinctive:
        return None
    from markut.evidence.sections import FIGURE_RE
    need = 1.0 if len(distinctive) <= 2 else 0.67
    found = None
    # a sentence may end inside a closing quote ('... all-time high.” The board ...'):
    # split there too, so the NEXT sentence's figure is never credited to this metric
    for sent in _re.split(r"(?<=[.!?])[”\"’']?\s+", text or ""):
        words = _norm_words(sent)
        if len(distinctive & words) / len(distinctive) >= need:
            if _NEGATED_RE.search(sent):
                return False
            if FIGURE_RE.search(sent):        # a real figure ($, %, scaled number) — never a bare year
                found = True
    return found


def metric_disclosed(metric: str, records: list, segments: list, text: str) -> bool:
    """A metric is disclosed when a table row of the latest earnings release
    or 10-Q carries it (the same row matcher the core figures use), or a
    filing sentence states it with a figure — and no sentence says it is
    not reported. A grouping metric ('net sales by segment') is disclosed
    when segment rows exist."""
    from markut.evidence import core
    said = text_disclosed(metric, text)
    if said is False:
        return False
    if core.GROUPING_KPI_RE.search(metric):
        return any(core._segment_of(r.get("label", ""), segments) for _, r in records)
    if core.resolve_kpi(metric, records, segments) is not None:
        return True
    return bool(said)


def mark_disclosure(profile: dict, records: list, profiled_text: str) -> dict:
    """PURE (over parsed records). Each company KPI gets disclosed: True/False
    from what was actually found in the filings; the profile lists the
    undisclosed ones so the planner can avoid them."""
    out = dict(profile or {})
    segs = [x.get("name", "") for x in out.get("segments") or [] if isinstance(x, dict) and x.get("name")]
    kpis = []
    for k in out.get("company_kpis") or []:
        if not isinstance(k, dict) or not k.get("name"):
            continue
        k = dict(k)
        k["disclosed"] = metric_disclosed(k["name"], records, segs, profiled_text)
        kpis.append(k)
    out["company_kpis"] = kpis
    out["undisclosed_metrics"] = [k["name"] for k in kpis if not k["disclosed"]]
    return out


def table_index(records: list, cap: int = 400) -> list:
    """Compact table rows (label, group, caption, first two cells) stored with
    the profile so the planner can test any metric against what is reported."""
    out = []
    for _src, r in records[:cap]:
        out.append({"label": r.get("label", ""), "group": r.get("group", ""), "caption": r.get("caption", ""),
                    "units": r.get("units", ""),
                    "cells": [{k: c.get(k, "") for k in ("heading", "period", "entity", "value")} for c in (r.get("cells") or [])[:2]]})
    return out


def records_from_index(index: list) -> list:
    return [({"form": "index", "name": "table index", "filing_date": ""}, dict(r)) for r in index or []]


def disclosure_records(fs: dict) -> list:
    """[(src, record)] for every table of the latest earnings exhibits and
    10-Q (EDGAR documents are disk-cached; no model call)."""
    from markut.evidence import core
    from markut.evidence.edgar import parse_tables
    out = []
    try:
        for src in core.core_sources(fs):
            for rec in parse_tables(src["html"]):
                out.append((src, rec))
    except Exception as e:
        print(f"PROFILER: disclosure tables unavailable ({e})")
    return out


# Analyst metrics a planner often reaches for that many companies do NOT
# report (a general vocabulary, no company in it). When a question names one
# in its TEXT without listing it as a KPI, it is checked like a listed KPI.
QUESTION_METRIC_TERMS = [
    ("unit sales", r"\bunit (?:sales|volumes?|shipments?|growth|trends?)\b|\bunits? (?:sold|shipped)\b|\bunits? versus\b|\bunit and (?:asp|average)"),
    ("average selling price", r"\baverage selling prices?\b|\bASPs?\b"),
    ("installed base", r"\binstalled base\b"),
    ("average revenue per user", r"\bARPU\b|\baverage revenue per (?:user|subscriber|account)\b"),
    ("subscribers", r"\b(?:paid )?subscribers?\b"),
    ("backlog", r"\bbacklog\b"),
    ("bookings", r"\bbookings\b"),
    ("churn", r"\bchurn\b"),
    ("customer traffic", r"\b(?:customer |guest )?traffic\b"),
    ("same-store sales", r"\bsame[- ]store sales\b"),
    ("take rate", r"\btake rate\b"),
    ("net revenue retention", r"\bnet (?:revenue|dollar) retention\b"),
    ("active users", r"\b(?:daily|monthly) active users\b|\b[DM]AUs?\b"),
]


def enforce_disclosure(plan: dict, profile: dict, text: str = "") -> dict:
    """Run #26 (P1, item 5): no question may require a metric the filings do
    not disclose. Each linked KPI is checked (the profile's own disclosure
    flag, else the table index and filing text); an undisclosed one is
    removed from the question's KPIs and the question states the limitation.
    Undisclosed profile metrics named in the question text are stated too.
    A profile with no disclosure information leaves the plan untouched."""
    from markut.evidence.questions import _kpi_match
    from markut.evidence.core import _distinctive, _norm_words
    kmap = {k["name"]: k["disclosed"] for k in (profile or {}).get("company_kpis") or [] if isinstance(k, dict) and "disclosed" in k}
    index = (profile or {}).get("table_index") or []
    if not kmap and not index:
        return plan
    records = records_from_index(index)
    segs = [x.get("name", "") for x in (profile or {}).get("segments") or [] if isinstance(x, dict) and x.get("name")]
    undisclosed = list((profile or {}).get("undisclosed_metrics") or [])
    out = dict(plan or {})
    qs = []
    for q in out.get("key_questions") or []:
        q = dict(q)
        keep, dropped = [], []
        for k in q.get("kpis") or []:
            flag = kmap.get(k)
            if flag is None:
                flag = next((v for name, v in kmap.items() if _kpi_match(k, name)), None)
            if flag is None:
                flag = metric_disclosed(k, records, segs, text)
            (keep if flag else dropped).append(k)
        qwords = _norm_words(q.get("question", ""))
        for u in undisclosed:
            d = _distinctive(u)
            if u not in dropped and d and len(d & qwords) / len(d) >= 0.67:
                dropped.append(u)
        # metrics named only in the question's wording (run #26: "unit versus
        # average selling price" was never a listed KPI, so nothing checked it)
        listed = " ".join(q.get("kpis") or []).lower()
        for metric, pat in QUESTION_METRIC_TERMS:
            if _re.search(pat, q.get("question", ""), _re.IGNORECASE) and not _re.search(pat, listed, _re.IGNORECASE):
                if metric not in dropped and not metric_disclosed(metric, records, segs, text):
                    dropped.append(metric)
        if dropped:
            q["kpis"] = keep
            if "not disclosed in the filings" not in q.get("question", ""):
                q["question"] = (q.get("question", "").rstrip() + f" (limitation: {', '.join(dropped)} not disclosed in the "
                                 "filings — answer from the disclosed figures)")
        qs.append(q)
    out["key_questions"] = qs
    return out


def disclosure_summary(profile: dict, max_rows: int = 60) -> str:
    p = profile or {}
    disclosed = [k["name"] for k in p.get("company_kpis") or [] if isinstance(k, dict) and k.get("disclosed")]
    rows = []
    for r in p.get("table_index") or []:
        lab = r.get("label", "")
        if r.get("group"):
            lab = f"{lab} [{r['group']}]"
        if lab not in rows:
            rows.append(lab)
    if not disclosed and not p.get("undisclosed_metrics") and not rows:
        return ""
    return ("DISCLOSURE (what the latest filings actually report):\n"
            f"- Disclosed metrics: {', '.join(disclosed) or 'none listed'}\n"
            f"- NOT DISCLOSED (never build a question that needs these): {', '.join(p.get('undisclosed_metrics') or []) or 'none'}\n"
            f"- Table rows in the latest earnings release / 10-Q: {'; '.join(rows[:max_rows])}"
            + (f"; … {len(rows) - max_rows} more" if len(rows) > max_rows else ""))


# ---------------- run #26 (P2, item 9): the archetype must follow the revenue mix ----------------
# Revenue words per archetype (general vocabulary, no company in it). The
# model's archetype stands when the revenue-mix text supports it; when it does
# not and another archetype is clearly supported, the supported one wins and
# the profile says so.
ARCHETYPE_EVIDENCE = {
    "semis_hardware": r"semiconductors?|\bchips?\b|wafers?|foundr(?:y|ies)|integrated circuits?|microcontrollers?|\banalog\b|processors?|memory products|networking silicon",
    "software": r"\bsoftware\b|\bsaas\b|subscription software|cloud platform|licen[cs]es? (?:revenue|fees)|enterprise applications?",
    "consumer_retail_restaurant": r"restaurants?|retail stores?|\bstores\b|merchandise|apparel|grocer(?:y|ies)|e-?commerce retail|franchis",
    "bank": r"\bbank(?:ing)?\b|deposits|\bloans\b|net interest income|lending",
    "insurer": r"\binsurance\b|premiums|underwriting|policyholders?|reinsurance",
    "reit": r"real estate investment trust|\breit\b|rental income|tenants|leases? (?:of|to) (?:office|industrial|retail) (?:space|properties)",
    "energy_commodity": r"\boil\b|natural gas|crude|refin(?:ing|ery)|petroleum|\blng\b|upstream|mining|\bcopper\b|\bcoal\b",
    "pharma_biotech": r"pharmaceuticals?|\bdrugs?\b|therapeutics?|biologics?|clinical (?:trials?|stage)|vaccines?",
    "industrial": r"aerospace|defen[cs]e systems|machinery|industrial equipment|engines|construction equipment|manufactur(?:es|ing) (?:of )?(?:equipment|components)",
    "utility": r"\butilit(?:y|ies)\b|electricity generation|regulated (?:electric|gas)|transmission and distribution",
    "consumer_electronics": r"smartphones?|personal computers?|\blaptops?\b|tablets?|wearables?|headphones|televisions?|consumer electronics|game consoles?|smart home",
    "internet_media": r"advertising|search engine|social (?:network|media)|streaming|digital content|online marketplace|\bpublishing\b",
    "payments_fintech": r"payment (?:network|processing|services)|card (?:network|issuing)|merchant acquiring|\bfintech\b|interchange",
    "telecom": r"wireless|broadband|telecommunications|spectrum|cable television|fiber network",
}


def check_archetype(profile: dict) -> tuple:
    """PURE. -> (archetype, note). The revenue-mix text is the business
    summary plus the segment names. Keeps the model's archetype when that
    text supports it; otherwise switches to the best-supported archetype and
    returns a note naming the correction. No support for any -> unchanged."""
    p = profile or {}
    arch = p.get("archetype") or "other"
    mix = " ".join([p.get("business", "")] + [x.get("name", "") for x in p.get("segments") or [] if isinstance(x, dict)]).lower()
    def hits(a):
        pat = ARCHETYPE_EVIDENCE.get(a)
        return len(_re.findall(pat, mix)) if pat else 0
    if arch in ARCHETYPE_EVIDENCE and hits(arch):
        return arch, ""
    scored = sorted(((hits(a), a) for a in ARCHETYPE_EVIDENCE), reverse=True)
    best_n, best = scored[0]
    if best_n == 0 or best == arch:
        return arch, ""
    mix_text = _re.sub(r"\s+", " ", p.get("business", ""))[:120]
    return best, (f"archetype corrected from {arch} to {best}: the revenue mix ({mix_text}…) "
                  f"has no {arch.replace('_', ' ')} revenue")


def profile_check(profile: dict, sections: list) -> list:
    """PURE. Why a profile is not good enough: missing archetype, no segments
    although a segment note was read, fewer than 3 KPIs."""
    problems = []
    if not (profile or {}).get("archetype"):
        problems.append("archetype missing")
    have_segment_note = any(s.get("kind") == "segment" for s in sections)
    if have_segment_note and not (profile or {}).get("segments"):
        problems.append("segments empty although a segment reporting note was provided")
    if len((profile or {}).get("company_kpis") or []) < 3:
        problems.append(f"fewer than 3 KPIs extracted ({len((profile or {}).get('company_kpis') or [])})")
    return problems


def sections_prompt(sections: list) -> str:
    parts = []
    for s in sections:
        parts.append(f"SECTION id={s['id']} ({s['name']}, {s['form']} filed {s['filing_date']}):\n{s['text']}")
    return "\n\n".join(parts) if parts else "(no filing sections could be extracted)"


def profiler_node(state: DebateState) -> dict:
    from markut.evidence import sections as secs
    ticker = state["ticker"]
    print(f"PROFILER: reading the latest filings for {ticker}")
    try:
        fs = secs.filing_set(ticker)
    except Exception as e:
        print(f"PROFILER: filing set unavailable ({e}) — profile skipped")
        return {"profile": {}, "profile_gaps": [f"filings unavailable: {e}"], "profiled_text": "",
                "filing_fingerprint": ""}
    key = f"profile:{ticker}:{fs['fingerprint']}"
    cached = store.cache_get(key)
    if cached and cached.get("profile"):
        print(f"PROFILER: cache hit ({fs['fingerprint']}) — extraction and the model call skipped")
        arch, note = check_archetype(cached["profile"])
        if note:
            cached["profile"] = dict(cached["profile"], archetype=arch)
            cached["gaps"] = list(cached.get("gaps", [])) + [note]
            store.cache_put(key, cached)
            print(f"PROFILER: {note}")
        if "table_index" not in cached["profile"]:
            # profiles cached before the disclosure map: add it once (tables only, no model call)
            recs = disclosure_records(fs)
            cached["profile"] = dict(mark_disclosure(cached["profile"], recs, cached.get("profiled_text", "")), table_index=table_index(recs))
            store.cache_put(key, cached)
        return {"profile": cached["profile"], "profile_gaps": cached.get("gaps", []),
                "profiled_text": cached.get("profiled_text", ""), "filing_fingerprint": fs["fingerprint"],
                "profile_cached": True, "profile_sources": cached.get("sources", [])}
    sections = secs.profile_sections(fs)
    print("PROFILER: sections — " + (", ".join(f"{s['name']} ({len(s['text']) // 4} tok)" for s in sections) or "none"))
    user = (f"Ticker: {ticker}\n\nFILING SECTIONS:\n\n{sections_prompt(sections)}\n\n"
            "Profile this company from the sections above only.")
    profile, gaps = {}, []
    from markut.agents.structured import call_structured
    res = call_structured(prompts.PROFILER_SYSTEM_PROMPT, user, PROFILE_SCHEMA, _parse_json,
                          max_tokens=config.PROFILER_MAX_TOKENS, label="PROFILER")
    profile = res["value"] or {}
    problems = profile_check(profile, sections)
    if problems:
        print(f"PROFILER: profile check failed ({'; '.join(problems)}) — one retry")
        retry = (user + "\n\nYour previous profile was incomplete: " + "; ".join(problems)
                 + ". Read the sections again and fill those fields from them; if a section genuinely does not "
                   "disclose a value, say so in the field rather than leaving the list empty.")
        try:
            second = _parse_json(llm.call_claude(prompts.PROFILER_SYSTEM_PROMPT, retry,
                                                 max_tokens=config.PROFILER_MAX_TOKENS, schema=PROFILE_SCHEMA))
            if len(profile_check(second, sections)) <= len(problems):
                profile = second
        except Exception as e:
            print(f"PROFILER: retry failed ({e})")
        problems = profile_check(profile, sections)
        if problems:
            gaps = [f"profile check: {p}" for p in problems]
            print("PROFILER: still incomplete — recorded as COVERAGE GAP: " + "; ".join(problems))
    sources = [f"{s['name']} (filed {s['filing_date']}, id {s['id']})" for s in sections]
    profiled_text = "\n\n".join(s["text"] for s in sections)
    if profile:
        profile, removed = verify_profile_numbers(profile, profiled_text)
        if removed:
            gaps.append(f"{len(removed)} figure(s) the profiler stated were not in the filing sections and were removed: "
                        + ", ".join(removed[:6]) + ("…" if len(removed) > 6 else ""))
            print(f"PROFILER: {len(removed)} figure(s) not traceable to the sections removed from the profile: {removed[:6]}")
    if profile:
        arch, note = check_archetype(profile)
        if note:
            profile = dict(profile, archetype=arch)
            gaps.append(note)
            print(f"PROFILER: {note}")
        recs = disclosure_records(fs)
        profile = dict(mark_disclosure(profile, recs, profiled_text), table_index=table_index(recs))
        if profile.get("undisclosed_metrics"):
            print(f"PROFILER: not disclosed in the latest filings: {profile['undisclosed_metrics']}")
    if profile:
        store.cache_put(key, {"profile": profile, "gaps": gaps, "profiled_text": profiled_text, "sources": sources,
                              "section_ids": [s["id"] for s in sections]})
    print(f"PROFILER: archetype={profile.get('archetype', '?')} segments={len(profile.get('segments') or [])} "
          f"kpis={len(profile.get('company_kpis') or [])} flags={profile.get('accounting_flags') or []}")
    return {"profile": profile, "profile_gaps": gaps, "profiled_text": profiled_text,
            "filing_fingerprint": fs["fingerprint"], "profile_cached": False, "profile_sources": sources}


def market_summary(market_text: str, max_lines: int = 45) -> str:
    """PURE. The planner's market context: the quote, fundamentals, valuation,
    analyst view and events lines of the market section — no filing text."""
    keep, current = [], None
    for line in (market_text or "").splitlines():
        t = line.strip()
        if t.startswith("[") and t.endswith("]") or t.startswith("[VALUATION]"):
            current = next((h for h in MARKET_SUMMARY_SECTIONS if t.startswith(h)), None)
            if current:
                keep.append(t.split(" (")[0])
            continue
        if current and t.startswith("- ") and "Implied price" not in t:
            keep.append(t.split("  [source:")[0])
        if len(keep) >= max_lines:
            break
    return "\n".join(keep)


FALLBACK_QUESTIONS = [
    # generic analyst questions, filled from the profile when the planner call fails twice
    ("What is driving revenue growth, and is it accelerating or slowing?",
     ["revenue increased compared with the prior year period driven by", "net sales grew due to higher volume and pricing"]),
    ("Are margins expanding or compressing, and why?",
     ["gross margin changed due to product mix and costs", "operating margin was impacted by expenses as a percentage of revenue"]),
    ("How are the company's own operating metrics trending?", []),
    ("How much leverage and fixed obligation does the balance sheet carry?",
     ["total debt outstanding and interest expense", "operating lease liabilities and purchase commitments"]),
    ("What has management guided for the coming periods?",
     ["we expect revenue for the fiscal year to be approximately", "outlook for the next quarter"]),
]


def fallback_plan(profile: dict) -> dict:
    """PURE. A generic five-question plan built from the profile's own KPIs and
    segments — used only when the planner call fails twice. Never cached."""
    kpis = [k.get("name", "") for k in (profile or {}).get("company_kpis", []) if isinstance(k, dict) and k.get("name")]
    segs = [s.get("name", "") for s in (profile or {}).get("segments", []) if isinstance(s, dict) and s.get("name")]
    qs = []
    for i, (question, queries) in enumerate(FALLBACK_QUESTIONS, 1):
        q = {"id": f"Q{i}", "question": question, "why": "fallback question (the planner call failed)",
             "kpis": kpis[:3] if i == 3 else [], "segments": segs[:2] if i == 1 else [],
             "search_queries": list(queries) + (kpis[:3] if i == 3 else [])}
        qs.append(q)
    return {"key_questions": qs, "what_would_mislead": [], "peer_tickers": [], "fallback": True}


_MECHANICS_RE = _re.compile(r"(?i)\b(?:ignores?|includes?|excludes?|is (?:computed|calculated|based on|derived from)|counts?|measures?|"
                            r"uses?|does not (?:include|count|capture)|omits?|captures?)\b")


def enforce_mislead_rules(items: list) -> list:
    """PURE. A what-would-mislead item may not state how a data field is
    computed or what it includes; unless it cites a packet line it is
    rephrased as a check ('check whether ...')."""
    out = []
    for item in items or []:
        t = str(item).strip()
        if not t:
            continue
        cites = bool(_re.search(r"(?i)\[source|per the (?:packet|market)|the packet (?:line|shows|says)|market summary line", t))
        if _MECHANICS_RE.search(t) and not cites and not t.lower().startswith("check whether"):
            t = "Check whether " + t[0].lower() + t[1:]
        out.append(t)
    return out


def normalize_plan(plan: dict) -> dict:
    """PURE. Exactly five questions ids Q1..Q5 (renumbered in order, padded
    with an explicit placeholder if the model returned fewer), 1-3 pitfalls,
    4-6 peers (stored only)."""
    qs = [q for q in (plan or {}).get("key_questions", []) if isinstance(q, dict) and q.get("question")][:5]
    while len(qs) < 5:
        qs.append({"question": "(the planner returned fewer than five questions)", "why": "", "kpis": [],
                   "segments": [], "search_queries": []})
    for i, q in enumerate(qs, 1):
        q["id"] = f"Q{i}"
        q.setdefault("kpis", []); q.setdefault("segments", []); q.setdefault("search_queries", []); q.setdefault("why", "")
    mislead = enforce_mislead_rules([str(x) for x in (plan or {}).get("what_would_mislead", []) if str(x).strip()][:3])
    peers = [str(x).upper().strip() for x in (plan or {}).get("peer_tickers", []) if str(x).strip()][:6]
    return {"key_questions": qs, "what_would_mislead": mislead, "peer_tickers": peers}


def planner_node(state: DebateState) -> dict:
    from markut.mcp.client import call_one_tool
    ticker = state["ticker"]
    print(f"PLANNER: choosing the five questions for {ticker}")
    market = state.get("market_evidence") or ""
    if not market:
        try:
            market = call_one_tool("market_snapshot_tool", {"ticker": ticker})
        except Exception as e:
            market = f"[market snapshot unavailable: {e}]"
    fp = state.get("filing_fingerprint") or "nofilings"
    key = f"plan:{ticker}:{fp}"
    cached = store.cache_get(key)
    if cached and cached.get("key_questions"):
        print(f"PLANNER: cache hit ({fp}) — the model call skipped")
        cached = dict(cached)
        cached["what_would_mislead"] = enforce_mislead_rules(cached.get("what_would_mislead") or [])
        cached = enforce_disclosure(cached, state.get("profile") or {}, state.get("profiled_text") or "")
        return {"plan": cached, "market_evidence": market, "plan_cached": True}
    user = (f"Ticker: {ticker}\n\nPROFILE (JSON):\n{json.dumps(state.get('profile') or {}, ensure_ascii=False)}\n\n"
            f"MARKET DATA (summary):\n{market_summary(market)}\n\n"
            + (disclosure_summary(state.get("profile") or {}) + "\n\n" if disclosure_summary(state.get("profile") or {}) else "")
            + "Choose the five questions that decide this company's outlook, the pitfalls, and the peers.")
    from markut.agents.structured import call_structured

    def parse_plan(raw):
        data = _parse_json(raw)
        if not isinstance(data.get("key_questions"), list) or not data["key_questions"]:
            raise ValueError("plan JSON must have a non-empty key_questions list")
        return normalize_plan(data)
    res = call_structured(prompts.PLANNER_SYSTEM_PROMPT, user, PLAN_SCHEMA, parse_plan,
                          max_tokens=config.PLANNER_MAX_TOKENS, label="PLANNER",
                          shorter="Reply again MUCH SHORTER: questions under 25 words, why under 15 words, two search "
                                  "queries each under 12 words, pitfalls under 30 words.")
    plan = res["value"] or {}
    if plan and not plan.get("fallback"):
        plan = enforce_disclosure(plan, state.get("profile") or {}, state.get("profiled_text") or "")
        shown = json.dumps(state.get("profile") or {}, ensure_ascii=False) + "\n" + market_summary(market)
        plan, removed = verify_plan_numbers(plan, shown)
        if removed:
            print(f"PLANNER: {len(removed)} figure(s) not in the profile or market summary removed from the questions: {removed[:6]}")
        store.cache_put(key, plan)
    else:
        plan = fallback_plan(state.get("profile") or {})
        print("PLANNER: using the generic fallback questions built from the profile (not cached)")
    for q in plan["key_questions"]:
        print(f"PLANNER: {q['id']} {q['question']}")
    return {"plan": plan, "market_evidence": market, "plan_cached": False}
