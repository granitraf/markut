"""Question-driven research: work through the planner's Q1-Q5 in order, tag
every evidence line (question id, source, filing date, period, basis,
segment), read each chunk once, say explicitly when nothing was found, and
build the general / guidance sections from the deterministic extractors.
Text excerpts are the only thing with a size limit; numbers, guidance lines,
segment figures and dollar lines are never trimmed."""
import re

from markut import config
from markut.evidence import sections as secs
from markut.evidence.edgar import normalize_dollars
from markut.evidence.rag import (DETERMINISTIC_BLOCKS, SIM_FLOOR, format_theme_block,
    retrieve_theme, split_sentences)
from markut.guardrails.tracer import classify_basis, classify_period

RELAXED_FLOOR = 0.22   # second pass on uncovered questions: wider net, still above noise
QID_RE = re.compile(r"^- \[(Q\d)\] \"")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def tag_period(text: str) -> str:
    return classify_period(text) or "unstated"


def tag_basis(text: str) -> str:
    b = classify_basis(text)
    return {"gaap": "GAAP", "non-gaap": "non-GAAP"}.get(b, "unstated")


def segment_aliases(name: str) -> list:
    # "Consumer & Community Banking (CCB)" -> the full name, the name without
    # the parenthetical, and the abbreviation itself
    name = (name or "").strip()
    out = [name]
    m = re.match(r"^(.*?)\s*\(([^)]{2,12})\)\s*$", name)
    if m:
        out += [m.group(1).strip(), m.group(2).strip()]
    return [a for a in out if len(a) > 2]


def tag_segment(text: str, names: list) -> str:
    low = (text or "").lower()
    for n in names or []:
        for alias in segment_aliases(n):
            pat = r"\b" + re.escape(alias.lower()) + r"\b" if len(alias) <= 6 else re.escape(alias.lower())
            if re.search(pat, low):
                return n
    return "unspecified"


def segment_names(profile: dict) -> list:
    names = [s.get("name", "") for s in (profile or {}).get("segments", []) if isinstance(s, dict)]
    names += [k.get("segment", "") for k in (profile or {}).get("company_kpis", []) if isinstance(k, dict)]
    out = []
    for n in names:
        n = (n or "").strip()
        if n and n.lower() not in ("unspecified", "consolidated", "total", "company", "n/a", "none") and n not in out:
            out.append(n)
    return out


def excerpt(text: str, max_words: int = None) -> str:
    """PURE. Whole sentences up to the word budget, in their original order,
    with sentences that carry a figure taken FIRST: a number is never cut and
    never crowded out by lead-in prose. Figure sentences may run to twice
    the budget; prose fills whatever budget is left."""
    max_words = max_words or config.EXCERPT_WORDS
    text = normalize_dollars(re.sub(r"\s+", " ", (text or "")).strip())
    text = re.sub(r"^\[context\]\s*", "", text)
    text = re.sub(r"\b\d{1,3} Table of Contents\b\s*", "", text)
    text = re.sub(r"\(See Note \d+[^)]*\)\s*", "", text)
    sentences = split_sentences(text) or [text]
    counts = [len(sent.split()) for sent in sentences]
    keep, total = set(), 0
    for i, sent in enumerate(sentences):
        if re.search(r"\d", sent) and total + counts[i] <= int(1.5 * max_words):
            keep.add(i)
            total += counts[i]
    for i, sent in enumerate(sentences):
        if i in keep:
            continue
        if total + counts[i] > max_words and keep:
            break
        keep.add(i)
        total += counts[i]
    return " ".join(sentences[i] for i in sorted(keep))


def source_tag(meta: dict, period: str, basis: str, segment: str) -> str:
    # form + section + filing date identify the document (its URL is listed
    # once in the [FILINGS] Sources header, not repeated on every line)
    return (f"[source: EDGAR/{meta.get('form', '?')} {meta.get('section', '?')}, filed {meta.get('filing_date', '?')}; "
            f"{period}; {basis} basis; segment {segment}]")


def question_queries(q: dict, relaxed: bool = False) -> list:
    queries = [x for x in (q.get("search_queries") or []) if isinstance(x, str) and x.strip()][:3]
    queries.append(q.get("question", ""))
    if relaxed:
        queries += [k for k in (q.get("kpis") or []) if isinstance(k, str) and k.strip()][:3]
        queries += [s for s in (q.get("segments") or []) if isinstance(s, str) and s.strip()][:2]
    return [x for x in queries if x]


def question_blocks(coll, plan: dict, profile: dict, profiled_text: str, relaxed: list = None) -> list:
    """[EVIDENCE Qn] sections: up to EXCERPTS_PER_QUESTION tagged lines per
    question, every chunk used at most once across the run, nothing the
    profiler already read, and an explicit 'could not find' line where
    retrieval came up empty."""
    relaxed = set(relaxed or [])
    names = segment_names(profile)
    profiled = _norm(profiled_text)
    seen_ids = set()
    lines = []

    def already_read(cand):
        if cand.get("id") in seen_ids:
            return True
        head = _norm(cand.get("text", ""))[:160]
        return bool(head) and head in profiled

    for q in plan.get("key_questions", []):
        qid = q.get("id", "Q?")
        lines.append(f"[EVIDENCE {qid}] {q.get('question', '')}")
        is_relaxed = qid in relaxed
        queries = question_queries(q, relaxed=is_relaxed)
        results, best = retrieve_theme(
            coll, q.get("question", ""), queries, None, k=config.EXCERPTS_PER_QUESTION,
            floor=RELAXED_FLOOR if is_relaxed else SIM_FLOOR, exclude=already_read,
            per_query=config.CHUNKS_PER_QUERY + 2, dup_overlap=0.55)
        kept = 0
        for r in results:
            seen_ids.add(r.get("id"))
            text = excerpt(r["text"])
            if not text:
                continue
            period, basis, segment = tag_period(text), tag_basis(text), tag_segment(text, names)
            m = r["metadata"]
            prefix = f"- [{qid}] "
            if m.get("risk_title"):
                lines.append(f"{prefix}Risk: {m['risk_title']}")
            lines.append(f'{prefix}"{text}"  {source_tag(m, period, basis, segment)}')
            kept += 1
        if not kept:
            why = (f"best match {best:.2f} below the {RELAXED_FLOOR if is_relaxed else SIM_FLOOR} floor"
                   if best is not None else "no candidate chunks")
            lines.append(f"- [{qid}] could not find: {q.get('question', '')} ({why}; pass {'2 (relaxed)' if is_relaxed else '1'})")
        lines.append("")
    return lines


def coverage_from_text(filings_text: str, qids: list) -> dict:
    """Count sourced evidence lines per question (count-only gate, no model)."""
    counts = {q: 0 for q in qids}
    for line in (filings_text or "").splitlines():
        m = QID_RE.match(line.strip())
        if m and "[source:" in line:
            counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    return counts


def general_blocks(coll, fs: dict, profile: dict) -> list:
    """[GENERAL EVIDENCE]: the risk caption list, executive quotes and revenue /
    segment figures from the earnings release, and every dollar figure in
    the obligations notes — each block titled by what was found."""
    lines = ["[GENERAL EVIDENCE]"]
    names = segment_names(profile)
    # risk captions (deterministic block in the index)
    try:
        title, section_name, cap = DETERMINISTIC_BLOCKS[0]
        got = coll.get(where={"section": section_name}, include=["documents", "metadatas"])
        results = [{"text": d, "metadata": m} for d, m in zip(got["documents"] or [], got["metadatas"] or [])]
        if results:
            lines.extend(format_theme_block(title, results, cap))
    except Exception as e:
        lines.append(f"[risk captions unavailable: {e}]")
    # the earnings release: quotes and figures
    release = None
    if fs.get("8-K"):
        try:
            release = next((e for e in secs.exhibits_of(fs["8-K"]) if not e["image_only"] and len(e["text"]) > 200), None)
        except Exception as e:
            lines.append(f"[8-K exhibits unavailable: {e}]")
    if release:
        rl = secs.release_lines(release["text"], names, max_figures=10)
        src = f"[source: EDGAR/8-K {release['name']}, filed {release['filing_date']}, {release['url']}]"
        if rl["quotes"]:
            lines.append(f"-- Executive quotes (8-K filed {release['filing_date']}) --")
            for qt in rl["quotes"]:
                lines.append(f'- "{qt}"')
            lines.append(f"  {src}")
        if rl["figures"]:
            lines.append(f"-- Revenue and segment figures (8-K filed {release['filing_date']}) --")
            for f in rl["figures"]:
                period = tag_period(f["text"])
                if period == "unstated" and f.get("default_period"):
                    period = f["default_period"]
                lines.append(f"- {f['text']}  (segment: {f['segment']}; period: {period}; basis: {tag_basis(f['text'])})")
            lines.append(f"  {src}")
    # obligations notes: every dollar figure, by the heading found (the three
    # most figure-dense notes, six lines each — guarantees and commitments
    # notes sort first because their sentences carry the obligation words)
    try:
        for b in secs.note_dollar_blocks(fs, max_items=6)[:3]:
            lines.append(f"-- {b['title']} — dollar figures --")
            for ln in b["lines"]:
                lines.append(f"- {ln}")
            lines.append(f"  [source: EDGAR/{b['form']} {b['title']}, filed {b['filing_date']}, {b['url']}]")
    except Exception as e:
        lines.append(f"[obligation notes unavailable: {e}]")
    return lines


def guidance_block(fs: dict, transcripts: list) -> list:
    """[GUIDANCE]: every guided item as its own line, with its source; an
    EXTRACTION FAILURE line where forward-looking language exists but no
    figure could be read (never 'no guidance')."""
    lines = ["[GUIDANCE] (forward-looking statements with a figure, from the latest 8-K exhibits and 10-Q MD&A; "
             "each line is one guided item)"]
    g = secs.guidance_lines(secs.guidance_sources(fs, transcripts))
    urls = {}
    for item in g["lines"]:
        where = f"; {item['where']}" if item.get("where") and item["where"] != "text" else ""
        urls.setdefault(item["label"], item.get("url", ""))
        lines.append(f'- [guidance] "{item["text"]}"  [source: EDGAR/{item["form"]} {item["label"]}, filed {item["filing_date"]}{where}; '
                     f"{tag_period(item['text'])}; {tag_basis(item['text'])} basis]")
    if urls:
        lines.append("  Guidance sources: " + " | ".join(f"{k} {v}" for k, v in urls.items() if v))
    for f in g["failures"]:
        lines.append(f"- EXTRACTION FAILURE: {f}")
    if not g["lines"] and not g["failures"]:
        lines.append("- no forward-looking statements with a figure were found in the latest 8-K exhibits or 10-Q MD&A")
    return lines
