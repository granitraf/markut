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
    from markut.guardrails.tracer import PERIOD_LABELS, period_markers
    found = period_markers(text)
    if not found:
        return "unstated"
    names = [n for n, _, _ in found]
    for ytd in ("ytd_h1", "ytd_9m"):          # a partial-year marker outranks the fiscal year it sits in
        if ytd in names and "quarter" not in names:
            return PERIOD_LABELS[ytd]
    return PERIOD_LABELS.get(names[0], names[0])


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


# a TIER, not a nudge: rerank logits run about -10..+10, so +100 puts every
# relevant current-period passage ahead of every prior-year one for a question
# about now (run #26: a prior-year passage echoing the question's wording
# outscored the current quarter's figures by more than any small bonus)
CURRENT_BONUS = 100.0
_HISTORY_Q_RE = re.compile(r"(?i)\b(?:historically|history|over the (?:past|last) (?:\d+|several|few|five|ten) years|since (?:19|20)\d\d|"
                           r"multi-year|long[- ]term track record|decade)\b")
_BOILERPLATE_EXCERPT_RE = re.compile(
    r"(?i)fiscal years? (?:\d{4}(?:,? and \d{4})? )?(?:spanned|was|were|is|are) (?:the )?(?:52|53)\b|52- or 53-week|53-week (?:fiscal )?(?:year|period)|"
    r"forward-looking statements?|safe harbor|basis of presentation|recently (?:issued|adopted) accounting|incorporated (?:herein )?by reference|"
    r"should be read in conjunction with|table of contents")
_TOPIC_STOP = {"which", "where", "while", "their", "these", "those", "about", "could", "would", "should", "versus", "given", "company",
               "business", "quarter", "fiscal", "whether", "against", "durable", "sustainable", "growth", "growing", "trend", "trends",
               "imply", "implies", "really", "still", "keep", "keeps", "comes", "from", "there", "being", "rather", "drive", "driven",
               "much", "many", "level", "levels", "exposed", "exposure", "expand", "expanding", "spike", "rebound"}


def _stem(w: str) -> str:
    return w[:-1] if len(w) > 4 and w.endswith("s") else w


def _mentions(text_low: str, name: str) -> bool:
    for a in _aliases(name):
        al = a.lower()
        if len(al) > 2 and ((len(al) <= 6 and re.search(r"(?<![a-z0-9])" + re.escape(al) + r"(?![a-z0-9])", text_low)) or (len(al) > 6 and al in text_low)):
            return True
    return False


def question_entities(q: dict, segments: list) -> list:
    blob = " ".join([q.get("question", "")] + [str(x) for x in (q.get("segments") or [])] + [str(x) for x in (q.get("kpis") or [])]).lower()
    return [sname for sname in segments if _mentions(blob, sname)]


def topic_words(q: dict) -> set:
    blob = " ".join([q.get("question", "")] + [str(x) for x in (q.get("kpis") or [])])
    return {_stem(w) for w in re.findall(r"[a-z][a-z-]{4,}", blob.lower()) if w not in _TOPIC_STOP and _stem(w) not in _TOPIC_STOP}


def excerpt_relevant(q: dict, text: str, segments: list) -> bool:
    """PURE. Run #26 (P1): an excerpt must address its question's topic.
    Generic boilerplate (fiscal-year length, safe harbor) never does. When
    the question is about named segments, the excerpt must name one of them
    or carry two of the question's topic words without naming a different
    segment; otherwise it must carry at least one topic word."""
    if _BOILERPLATE_EXCERPT_RE.search(text or ""):
        return False
    low = (text or "").lower()
    words = {_stem(w) for w in re.findall(r"[a-z][a-z-]{4,}", low)}
    hits = len(topic_words(q) & words)
    ents = question_entities(q, segments)
    if ents:
        if any(_mentions(low, e) for e in ents):
            return True
        others = [sname for sname in segments if sname not in ents and _mentions(low, sname)]
        return hits >= 2 and not others
    return hits >= 1


def filing_dates(coll) -> dict:
    """{form: newest filing date of that form} from the index — 10-K / 10-Q /
    8-K for a domestic filer, 20-F or 40-F / 6-K for a foreign one."""
    out = {}
    try:
        for m in coll.get(include=["metadatas"])["metadatas"] or []:
            f, d = m.get("form", ""), m.get("filing_date", "") or ""
            if d and d > out.get(f, ""):
                out[f] = d
    except Exception:
        pass
    return out


def is_current(meta: dict, dates: dict) -> bool:
    """A filing is current-period when it is newer than the newest annual
    report (a 10-Q / 8-K after the 10-K, a 6-K after the 20-F or 40-F), or
    when it IS the newest annual report and no later interim report has been
    filed. Roles, not form names (run #28): a foreign issuer's interim
    reports are 6-Ks, so any later 6-K supersedes its 20-F; for a domestic
    filer only a later 10-Q supersedes the 10-K (an 8-K does not)."""
    from markut.evidence import filers
    form, d = meta.get("form", ""), meta.get("filing_date", "") or ""
    role = filers.role_of(form)
    annual = max((v for f, v in dates.items() if filers.role_of(f) == "annual" and v), default="")
    if role in ("interim", "current"):
        return not annual or d >= annual
    if role == "annual":
        return not any(v > d for f, v in dates.items()
                       if v and (filers.role_of(f) == "interim" or filers.base_form(f) == "6-K"))
    return False


def period_label(meta: dict, dates: dict) -> str:
    from markut.evidence import filers
    form, d = meta.get("form", "?"), meta.get("filing_date", "?")
    if is_current(meta, dates):
        return f"current period ({form} filed {d})"
    if filers.role_of(form) == "annual":
        return f"prior fiscal year ({form} filed {d})"
    return f"earlier filing ({form} filed {d})"


def question_blocks(coll, plan: dict, profile: dict, profiled_text: str, relaxed: list = None) -> list:
    """[EVIDENCE Qn] sections: up to EXCERPTS_PER_QUESTION tagged lines per
    question, every chunk used at most once across the run, current-period
    filings ranked ahead of prior-year text for a question about now, every
    excerpt labelled with its period, off-topic text rejected, and an
    explicit 'could not find' line where nothing qualifies.
    Run #26 (P1): profiled_text no longer excludes chunks — the profile is a
    summary, so the latest MD&A it read must still reach the debate."""
    relaxed = set(relaxed or [])
    names = segment_names(profile)
    seen_ids = set()
    dates = filing_dates(coll)
    lines = []

    for q in plan.get("key_questions", []):
        qid = q.get("id", "Q?")
        lines.append(f"[EVIDENCE {qid}] {q.get('question', '')}")
        is_relaxed = qid in relaxed
        about_now = not _HISTORY_Q_RE.search(q.get("question", ""))
        queries = question_queries(q, relaxed=is_relaxed)
        results, best = retrieve_theme(
            coll, q.get("question", ""), queries, None, k=config.EXCERPTS_PER_QUESTION + 3,
            floor=RELAXED_FLOOR if is_relaxed else SIM_FLOOR, exclude=lambda c: c.get("id") in seen_ids,
            per_query=config.CHUNKS_PER_QUERY + 2, dup_overlap=0.55,
            boost=(lambda c: CURRENT_BONUS if is_current(c["metadata"], dates) else 0.0) if about_now else None)
        kept = 0
        for r in results:
            if kept >= config.EXCERPTS_PER_QUESTION:
                break
            text = excerpt(r["text"])
            if not text or not excerpt_relevant(q, text, names):
                continue
            seen_ids.add(r.get("id"))
            stated, basis, segment = tag_period(text), tag_basis(text), tag_segment(text, names)
            m = r["metadata"]
            period = period_label(m, dates) + (f"; text says {stated}" if stated != "unstated" else "")
            prefix = f"- [{qid}] "
            if m.get("risk_title"):
                lines.append(f"{prefix}Risk: {m['risk_title']}")
            lines.append(f'{prefix}"{text}"  {source_tag(m, period, basis, segment)}')
            kept += 1
        if not kept:
            why = (f"best match {best:.2f} below the {RELAXED_FLOOR if is_relaxed else SIM_FLOOR} floor"
                   if best is not None and best < (RELAXED_FLOOR if is_relaxed else SIM_FLOOR) else
                   "no retrieved passage addressed the question" if best is not None else "no candidate chunks")
            lines.append(f"- [{qid}] could not find: {q.get('question', '')} ({why}; pass {'2 (relaxed)' if is_relaxed else '1'})")
        lines.append("")
    return lines


def coverage_from_text(filings_text: str, qids: list) -> dict:
    """Count sourced evidence lines per question (count-only, no model)."""
    counts = {q: 0 for q in qids}
    for line in (filings_text or "").splitlines():
        t = line.strip()
        m = QID_RE.match(t)
        if m and "[source:" in t:
            counts[m.group(1)] = counts.get(m.group(1), 0) + 1
        elif t.startswith("- ") and "[source:" in t:
            for tag in re.findall(r"\[(Q\d)\]", t.split("[source:")[0]):
                counts[tag] = counts.get(tag, 0) + 1
    return counts


def _aliases(name: str) -> list:
    name = (name or "").strip()
    out = [name]
    m = re.match(r"^(.*?)\s*\(([^)]{2,14})\)\s*$", name)
    if m:
        out += [m.group(1).strip(), m.group(2).strip()]
    return out


_STOP = {"which", "where", "while", "their", "these", "those", "about", "could", "would", "should", "versus", "given",
         "growth", "company", "business", "margin", "margins", "revenue", "sales", "quarter", "fiscal", "whether", "against"}


def _question_terms(q: dict) -> tuple:
    """(strong terms = KPI and segment aliases, weak terms = question words)."""
    strong = []
    for k in (q.get("kpis") or []) + (q.get("segments") or []):
        if isinstance(k, str):
            strong += [a for a in _aliases(k) if len(a) > 2]
    weak = [w for w in re.findall(r"[a-z][a-z-]{5,}", (q.get("question") or "").lower()) if w not in _STOP]
    return strong, weak


def tag_core_lines(core_text: str, plan: dict) -> str:
    """Append [Qn] tags to core figure lines whose label or segment mentions a
    KPI, segment or key term of a planner question — the coverage gate and
    the debaters then see which core figures answer which question."""
    qterms = [(q.get("id"), _question_terms(q)) for q in (plan or {}).get("key_questions", []) if q.get("id")]
    if not qterms:
        return core_text
    def hit(term, text):
        tl = term.lower()
        return bool(re.search(r"\b" + re.escape(tl) + r"\b", text)) if len(tl) <= 6 else tl in text
    out = []
    for line in (core_text or "").splitlines():
        if line.startswith("- ") and "[source:" in line and "not found" not in line:
            attrs = re.search(r"\(([^()]*; [^()]*; [^()]*)\)\s+\[source", line)
            seg = attrs.group(1).split(";")[-1].strip().lower() if attrs else ""
            head = line.split(":")[0].lower() + " " + seg
            tags = []
            for qid, (strong, weak) in qterms:
                if any(hit(t, head) for t in strong) or sum(1 for w in set(weak) if hit(w, head)) >= 2:
                    tags.append(qid)
            if tags:
                line = line.replace("  [source:", "  " + "".join(f"[{t}]" for t in tags) + "  [source:", 1)
        out.append(line)
    return "\n".join(out)


BOILERPLATE_LINE_RE = re.compile(r"(?i)forward-looking statements?|safe harbor|non-gaap (?:financial )?measures?|private securities litigation|"
                                 r"not (?:a )?(?:substitute|comparable to similarly)|reconciliation of|should not be considered in isolation|"
                                 r"table of contents|^- \[Q\d\] Risk:")
_DIRECT_RE = re.compile(r"(?i)\b(?:increased|decreased|grew|declined|rose|fell|opened|closed|launched|repaid|issued|repurchased|expects?|guided|was|were)\b")


def candidate_lines(filings_text: str, qids: list) -> dict:
    """{qid: [lines that could answer it]}: question-tagged excerpts and core
    figures carrying the tag, minus boilerplate; a line must hold a figure or
    a direct factual statement to count."""
    from markut.evidence.sections import FIGURE_RE
    cands = {q: [] for q in qids}
    for line in (filings_text or "").splitlines():
        t = line.strip()
        if not t.startswith("- ") or "[source:" not in t or BOILERPLATE_LINE_RE.search(t):
            continue
        body = t.split("[source:")[0]
        tags = re.findall(r"^- \[(Q\d)\]", t) or re.findall(r"\[(Q\d)\]", body)
        if not tags:
            continue
        if not (FIGURE_RE.search(body) or re.search(r"\d", body) or _DIRECT_RE.search(body)):
            continue
        for q in set(tags):
            if q in cands:
                cands[q].append(t)
    return cands


def _kpi_match(a: str, b: str) -> bool:
    # the same metric under its name, its abbreviation or its name without
    # the parenthetical ("GMV" ~ "Gross merchandise volume (GMV)")
    al = {x.lower() for x in _aliases(a)}
    bl = {x.lower() for x in _aliases(b)}
    if al & bl:
        return True
    wa = {w for w in re.findall(r"[a-z0-9]+", a.lower()) if len(w) > 2}
    wb = {w for w in re.findall(r"[a-z0-9]+", b.lower()) if len(w) > 2}
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) >= 0.5


_CURRENT_PERIOD_RE = re.compile(r"\((?:quarter|YTD \(H1\)|YTD \(9M\)|ttm)\b")


def _line_is_current(line: str, annual_is_current: bool) -> bool:
    if "current period (" in line:
        return True
    if "prior fiscal year (" in line or "earlier filing (" in line:
        return False
    if _CURRENT_PERIOD_RE.search(line):
        return True
    return annual_is_current and "(annual" in line


def coverage_assess(filings_text: str, plan: dict, kpi_values: dict, relevance: dict = None) -> dict:
    """{qid: {"lines", "relevant", "current", "kpis_ok", "covered", "missing"}}.
    Run #26 (P1): a question counts as covered only if at least one KPI the
    planner linked to it has a CURRENT-PERIOD value, or a current-period
    excerpt or figure addressing it is attached (per the relevance call when
    available). Prior-year text alone does not answer a question about now.
    missing = the linked KPIs with no current-period value — the coverage
    gap names them. The routing decision itself stays in code."""
    qs = (plan or {}).get("key_questions", [])
    qids = [q.get("id") for q in qs if q.get("id")]
    cands = candidate_lines(filings_text, qids)
    text = filings_text or ""
    annual_is_current = not re.search(r"\(quarter\b|\(YTD \(", text)       # the latest filing is an annual report
    kpi_lines = {}
    for line in text.splitlines():
        m = re.match(r"^- KPI (?!value not found)(.+?): ", line.strip())
        if m:
            kpi_lines[m.group(1).strip()] = line
    out = {}
    for q in qs:
        qid = q.get("id")
        lines = cands.get(qid, [])
        if relevance is not None and qid in relevance:
            idx = {i for i in relevance[qid] if isinstance(i, int)}
            chosen = [l for i, l in enumerate(lines) if i in idx]
        else:
            chosen = list(lines)
        current = [l for l in chosen if _line_is_current(l, annual_is_current)]
        linked = [k for k in (q.get("kpis") or []) if isinstance(k, str) and k.strip()]
        kpis_ok = all(any(_kpi_match(k, name) for name in (kpi_values or {})) for k in linked)
        current_kpis = [k for k in linked
                        if any(_kpi_match(k, name) and _line_is_current(line, annual_is_current) for name, line in kpi_lines.items())]
        covered = bool(current_kpis) or bool(current)
        out[qid] = {"lines": len(lines), "relevant": len(chosen), "current": len(current), "kpis_ok": kpis_ok,
                    "covered": covered, "missing": [k for k in linked if k not in current_kpis],
                    "prior_only": bool(chosen) and not current}
    return out


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
            lines.append(f"[{fs['8-K'].get('form', '8-K')} exhibits unavailable: {e}]")
    if release:
        rl = secs.release_lines(release["text"], names, max_figures=10)
        rf = release.get("form", "8-K")        # run #28: the release's real form (8-K or 6-K)
        src = f"[source: EDGAR/{rf} {release['name']}, filed {release['filing_date']}, {release['url']}]"
        if rl["quotes"]:
            lines.append(f"-- Executive quotes ({rf} filed {release['filing_date']}) --")
            for qt in rl["quotes"]:
                lines.append(f'- "{qt}"')
            lines.append(f"  {src}")
        # prose revenue statements only: table figures live in [CORE FIGURES] with their context
        prose = [f for f in rl["figures"] if f.get("kind") == "sentence"][:6]
        if prose:
            lines.append(f"-- Revenue statements in the release ({rf} filed {release['filing_date']}) --")
            for f in prose:
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
    from markut.evidence.filers import source_phrase
    return format_guidance(secs.guidance_lines(secs.guidance_sources(fs, transcripts)), source_phrase(fs))


def format_guidance(g: dict, sources: str = "the latest 8-K exhibits or 10-Q") -> list:
    """PURE. The [GUIDANCE] lines from guidance_lines' result. When no business
    target was found it says so and names why (run #26, P2 item 10).
    sources names the filings read — the forms this company files (run #28)."""
    lines = [f"[GUIDANCE] (forward-looking business targets with a figure, from {sources}; "
             "accounting schedules excluded; each line is one guided item)"]
    urls = {}
    for item in g["lines"]:
        where = f"; {item['where']}" if item.get("where") and item["where"] != "text" else ""
        urls.setdefault(item["label"], item.get("url", ""))
        label = item["label"] if not item["label"].startswith(item["form"] + " ") else item["label"][len(item["form"]) + 1:]
        lines.append(f'- [guidance] "{item["text"]}"  [source: EDGAR/{item["form"]} {label}, filed {item["filing_date"]}{where}; '
                     f"{tag_period(item['text'])}; {tag_basis(item['text'])} basis]")
    if urls:
        lines.append("  Guidance sources: " + " | ".join(f"{k} {v}" for k, v in urls.items() if v))
    for f in g["failures"]:
        lines.append(f"- EXTRACTION FAILURE: {f}")
    if not g["lines"] and not g["failures"]:
        lines.append(f"- no forward-looking business targets found in {sources} (accounting schedules such as "
                     "deferred-revenue, lease and debt-maturity tables are excluded). A company that gives guidance only on its "
                     "earnings call is a known gap: no call transcript is available.")
    return lines
