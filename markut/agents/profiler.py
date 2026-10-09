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
        return {"profile": cached["profile"], "profile_gaps": cached.get("gaps", []),
                "profiled_text": cached.get("profiled_text", ""), "filing_fingerprint": fs["fingerprint"],
                "profile_cached": True, "profile_sources": cached.get("sources", [])}
    sections = secs.profile_sections(fs)
    print("PROFILER: sections — " + (", ".join(f"{s['name']} ({len(s['text']) // 4} tok)" for s in sections) or "none"))
    user = (f"Ticker: {ticker}\n\nFILING SECTIONS:\n\n{sections_prompt(sections)}\n\n"
            "Profile this company from the sections above only.")
    profile, gaps = {}, []
    try:
        profile = _parse_json(llm.call_claude(prompts.PROFILER_SYSTEM_PROMPT, user,
                                              max_tokens=config.PROFILER_MAX_TOKENS, schema=PROFILE_SCHEMA))
    except Exception as e:
        print(f"PROFILER: first call failed ({e})")
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
    mislead = [str(x) for x in (plan or {}).get("what_would_mislead", []) if str(x).strip()][:3]
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
        return {"plan": cached, "market_evidence": market, "plan_cached": True}
    user = (f"Ticker: {ticker}\n\nPROFILE (JSON):\n{json.dumps(state.get('profile') or {}, ensure_ascii=False)}\n\n"
            f"MARKET DATA (summary):\n{market_summary(market)}\n\n"
            "Choose the five questions that decide this company's outlook, the pitfalls, and the peers.")
    plan, content = {}, user
    for attempt in range(2):
        try:
            raw = llm.call_claude(prompts.PLANNER_SYSTEM_PROMPT, content, max_tokens=config.PLANNER_MAX_TOKENS, schema=PLAN_SCHEMA)
            if llm.LAST_STOP_REASON == "max_tokens":
                raise ValueError("reply cut off at the output cap")
            plan = normalize_plan(_parse_json(raw))
            break
        except Exception as e:
            print(f"PLANNER: call failed ({e}){' — one shorter retry' if attempt == 0 else ''}")
            # a reply cut off at the cap is too long, not malformed: ask for less
            content = (user + "\n\nYour previous reply was cut off before the JSON closed. Reply again MUCH SHORTER: "
                       "questions under 25 words, why under 15 words, two search queries each under 12 words, pitfalls "
                       "under 30 words. Finish the JSON.")
    if plan and not plan.get("fallback"):
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
