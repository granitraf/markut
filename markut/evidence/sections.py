"""Deterministic filing sections — what the profiler reads and what research
extracts WITHOUT similarity search: the latest filing set and its accession
fingerprint, the four profiler sections (Item 1, MD&A overview, segment note,
earnings exhibit), image-only exhibits transcribed once per filing, generic
forward-looking guidance, executive quotes and segment figures from the
release, and every dollar figure in the commitments / guarantees / leases /
debt notes. Nothing here knows a company name, a heading only one filer uses,
or a figure; section labels come from the heading actually found."""
import io
import json
import re

from markut import config
from markut.evidence.edgar import (EDGAR_CACHE, EXHIBIT_PATTERNS, edgar_get, extract_10k_business,
    extract_10k_sections, extract_10q_sections, fetch_filing_html, find_latest_filings,
    get_filing_index, html_to_text, is_figure_cell, normalize_dollars, strip_exhibit_label,
    strip_sgml_header, ticker_to_cik, dollar_sentences, _find_section)

CHARS_PER_TOKEN = 4


# ---------------------------------------------------------------- filing set
def filing_set(ticker: str) -> dict:
    """Latest 10-K, 10-Q and earnings (Item 2.02) 8-K plus the accession
    fingerprint that keys every cache (profile, plan, transcripts)."""
    symbol = ticker.upper().strip()
    cik = ticker_to_cik(symbol)
    recent = get_filing_index(cik)
    latest = find_latest_filings(recent, cik, ["10-K", "10-Q", "8-K"], max_per_form=12)
    tenk = latest["10-K"][0] if latest["10-K"] else None
    tenq = latest["10-Q"][0] if latest["10-Q"] else None
    earnings = next((e for e in latest["8-K"] if "2.02" in (e.get("items") or "")), None)
    fingerprint = "|".join(d["accession"] if d else "-" for d in (tenk, tenq, earnings))
    return {"ticker": symbol, "cik": cik, "10-K": tenk, "10-Q": tenq, "8-K": earnings,
            "fingerprint": fingerprint, "all_8k": latest["8-K"]}


def trim_tokens(text: str, tokens: int) -> str:
    # PURE. Cut to a token cap at a sentence or line boundary, saying so.
    limit = tokens * CHARS_PER_TOKEN
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    ends = [m.end() for m in re.finditer(r"[.!?](?=\s|$)|\n", cut)]
    if ends and ends[-1] > limit * 0.5:
        cut = cut[:ends[-1]]
    return cut.rstrip() + f" [...section trimmed to ~{tokens} tokens]"


def _section(kind, sid, name, text, doc):
    return {"kind": kind, "id": sid, "name": name, "text": text, "form": doc["form"],
            "filing_date": doc["filing_date"], "url": doc["url"], "accession": doc.get("accession", "")}


# ---------------------------------------------------------------- exhibits
def _exhibit_rank(name: str) -> int:
    low = name.lower()
    for i, pat in enumerate(EXHIBIT_PATTERNS):
        if re.search(pat, low):
            return i
    return len(EXHIBIT_PATTERNS)


def exhibits_of(doc: dict) -> list:
    """Every 99.x exhibit of one 8-K: text (tables kept as rows), image URLs,
    and whether the exhibit is image-only (a slide deck filed as pictures)."""
    try:
        listing = json.loads(fetch_filing_html(doc["base_url"] + "/index.json"))
    except Exception as e:
        print(f"SECTIONS: exhibit listing failed for {doc.get('accession')} ({e})")
        return []
    names = [it.get("name", "") for it in listing.get("directory", {}).get("item", [])]
    html_names = [n for n in names if n.lower().endswith((".htm", ".html"))
                  and "index" not in n.lower() and re.search(r"99", n)]
    from bs4 import BeautifulSoup
    out = []
    for n in sorted(html_names, key=_exhibit_rank):
        url = doc["base_url"] + "/" + n
        try:
            raw = fetch_filing_html(url)
        except Exception as e:
            print(f"SECTIONS: exhibit {n} failed ({e})")
            continue
        text = strip_exhibit_label(strip_sgml_header(html_to_text(raw, keep_tables=True)))
        soup = BeautifulSoup(raw, "html.parser")
        imgs = [img.get("src") for img in soup.find_all("img") if img.get("src")]
        stem = re.sub(r"\.html?$", "", n, flags=re.IGNORECASE).lower()
        if not imgs:
            imgs = [m for m in names if m.lower().startswith(stem) and re.search(r"\.(jpe?g|png|gif)$", m, re.IGNORECASE)]
        imgs = [i for i in imgs if re.search(r"\.(jpe?g|png|gif)(\?.*)?$", i, re.IGNORECASE)]
        image_urls = []
        for i in imgs:
            u = i if i.startswith("http") else doc["base_url"] + "/" + i.split("/")[-1]
            if u not in image_urls:
                image_urls.append(u)
        compact = re.sub(r"\s+", " ", text)
        out.append({"name": n, "url": url, "text": text, "images": image_urls,
                    "image_only": len(compact) < 400 and len(image_urls) >= 3,
                    "form": "8-K", "filing_date": doc["filing_date"], "accession": doc["accession"]})
    return out


TRANSCRIBE_SYSTEM = (
    "You transcribe investor-presentation slides filed with the SEC, exactly and completely. "
    "For each slide write a line 'Slide N: <title>' and then one line per statement on the slide: "
    "'label: value' for every figure, bullets verbatim, tables as 'row label | column 1 | column 2 ...' with the "
    "header row first. Copy every number with its unit, currency, period and footnote marker exactly as printed. "
    "Never summarize, never omit a number, never add commentary or interpretation. "
    "If a slide holds only a photo or a logo, write 'Slide N: (no text)'. If a slide is legal boilerplate — a safe "
    "harbor / forward-looking statements notice, a non-GAAP disclaimer, a disclosure footer — write only "
    "'Slide N: <title> (legal boilerplate, not transcribed)'."
)


def _downscale(blob: bytes, max_width: int = 1100):
    # fewer image tokens per slide; falls back to the original bytes
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(blob))
        if im.width > max_width:
            im = im.resize((max_width, int(im.height * max_width / im.width)))
        buf = io.BytesIO()
        im.convert("RGB").save(buf, format="JPEG", quality=85)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        return blob, "image/jpeg"


def transcribe_exhibit(exh: dict) -> dict:
    """Image-only exhibit -> {"text", "slides", "status", "truncated", ...}.
    One model pass per batch of slides, cached by accession + exhibit name so
    a deck is read once per filing, not once per debate. Lazy imports: the
    store and the model client stay out of the evidence layer's import graph."""
    from markut import store
    from markut.agents import llm
    key = f"transcript:{exh['accession']}:{exh['name']}"
    cached = store.cache_get(key)
    if cached:
        print(f"SECTIONS: transcript cache hit for {exh['name']} ({cached.get('slides')} slides)")
        return cached
    base = {"name": exh["name"], "url": exh["url"], "filing_date": exh["filing_date"],
            "accession": exh["accession"], "slides": len(exh["images"])}
    if not config.TRANSCRIBE_IMAGE_EXHIBITS:
        return {**base, "text": "", "status": "disabled", "truncated": False}
    images = exh["images"][: config.MAX_SLIDES_PER_EXHIBIT]
    blobs = []
    for url in images:
        try:
            blobs.append(_downscale(edgar_get(url).content))
        except Exception as e:
            print(f"SECTIONS: slide download failed ({url.rsplit('/', 1)[-1]}: {e})")
    if not blobs:
        return {**base, "text": "", "status": "download failed", "truncated": False}
    print(f"SECTIONS: transcribing {len(blobs)} slides of {exh['name']} (image-only exhibit)")
    parts, truncated = [], False
    step = config.TRANSCRIPT_SLIDES_PER_CALL
    for start in range(0, len(blobs), step):
        batch = blobs[start:start + step]
        user = (f"These are slides {start + 1}-{start + len(batch)} of {len(blobs)} from an investor presentation "
                f"filed as exhibit {exh['name']} to a Form 8-K dated {exh['filing_date']}. Number them starting at "
                f"{start + 1}. Transcribe every slide.")
        try:
            text = llm.call_claude_images(TRANSCRIBE_SYSTEM, user, batch, max_tokens=config.TRANSCRIPT_MAX_TOKENS)
        except Exception as e:
            print(f"SECTIONS: transcription failed ({e})")
            return {**base, "text": "\n\n".join(parts), "status": f"failed: {e}", "truncated": truncated}
        if llm.LAST_STOP_REASON == "max_tokens":
            truncated = True
            text += "\n[transcript of this batch cut off at the output cap]"
        parts.append(text.strip())
    result = {**base, "text": "\n\n".join(parts), "status": "ok", "truncated": truncated, "slides": len(blobs)}
    store.cache_put(key, result)
    return result


def transcripts_for(fs: dict, allow_model: bool = False) -> list:
    """Transcripts of the earnings 8-K's image-only exhibits. With
    allow_model=False only the cache is consulted (research never spends
    tokens); the profiler passes True."""
    if not fs.get("8-K"):
        return []
    out = []
    for exh in exhibits_of(fs["8-K"]):
        if not exh["image_only"]:
            continue
        if allow_model:
            out.append(transcribe_exhibit(exh))
        else:
            from markut import store
            cached = store.cache_get(f"transcript:{exh['accession']}:{exh['name']}")
            out.append(cached or {"name": exh["name"], "url": exh["url"], "filing_date": exh["filing_date"],
                                  "accession": exh["accession"], "slides": len(exh["images"]),
                                  "text": "", "status": "not transcribed", "truncated": False})
    return out


# ---------------------------------------------------------------- profiler sections
_NOTE_END = [r"(?m)^\s*(?:note\s+)?\d{1,2}\.\s+[A-Z][a-z]", r"(?m)^\s*item\s+2\b", r"(?m)^\s*item\s+4\b",
             r"(?m)^\s*item\s+9a?\b", r"(?m)^\s*part\s+ii\b"]
_SEGMENT_START = (r"(?m)^\s*(?:note\s+)?(?:\d{1,2}\s*[.\-—:]?\s*)?"
                  r"(?:segment (?:information|reporting|data|results|and geographic)|reportable segments?|business segments?|"
                  r"segment information is presented)\b")


def _best_section(text: str, start_pat: str, end_pats: list, min_chars: int = 300, max_chars: int = 60000) -> str:
    # like edgar._find_section but scored by how much of a NOTE it looks like
    # (table rows and dollar figures) rather than by raw length, so a table of
    # contents entry that runs on to the next heading never wins
    best, best_score = "", -1
    for m in re.finditer(start_pat, text, re.IGNORECASE):
        s = m.start()
        end = min(len(text), s + max_chars)
        for ep in end_pats:
            e = re.search(ep, text[s + 20:s + max_chars], re.IGNORECASE)
            if e:
                end = min(end, s + 20 + e.start())
        span = text[s:end].strip()
        if len(span) < min_chars:
            continue
        score = span.count(" | ") * 2 + span.count("$") + len(span) / 5000
        if score > best_score:
            best, best_score = span, score
    return best


def extract_segment_note(html: str) -> str:
    text = html_to_text(html, keep_tables=True)
    return _best_section(text, _SEGMENT_START, _NOTE_END, min_chars=300)


def _from_overview(text: str) -> str:
    # start the MD&A excerpt at its "Overview" heading when one opens the
    # section; otherwise drop the forward-looking-statements legalese most
    # filers lead with (up to three leading paragraphs)
    m = re.search(r"(?im)^\s*overview\b", text[:15000])
    if m:
        return text[m.start():]
    paras = text.split("\n\n")
    dropped = 0
    while paras and dropped < 3 and re.search(r"(?i)forward-looking|safe harbor|cautionary", paras[0]):
        paras.pop(0)
        dropped += 1
    return "\n\n".join(paras)


def profile_sections(fs: dict) -> list:
    """The sections the profiler reads, each trimmed to its cap and carrying a
    section id (accession + name). Missing sections are simply absent — the
    profile check reports what the model never saw."""
    caps = config.PROFILE_SECTION_TOKENS
    secs = []
    html10k = fetch_filing_html(fs["10-K"]["url"]) if fs.get("10-K") else ""
    if html10k:
        item1 = extract_10k_business(html10k)
        if not item1.startswith("[section unavailable"):
            secs.append(_section("item1", f"{fs['10-K']['accession']}:Item 1", "10-K Item 1 (Business)",
                                 trim_tokens(item1, caps["item1"]), fs["10-K"]))
    # MD&A: the newest filing's narrative
    mdna_text, mdna_doc = "", None
    if fs.get("10-Q") and (not fs.get("10-K") or fs["10-Q"]["filing_date"] >= fs["10-K"]["filing_date"]):
        mdna_text = extract_10q_sections(fetch_filing_html(fs["10-Q"]["url"]))["Item 2 (10-Q MD&A)"]
        mdna_doc = fs["10-Q"]
    if (not mdna_text or mdna_text.startswith("[section unavailable")) and html10k:
        mdna_text, mdna_doc = extract_10k_sections(html10k)["Item 7"], fs["10-K"]
    if mdna_text and not mdna_text.startswith("[section unavailable"):
        secs.append(_section("mdna", f"{mdna_doc['accession']}:MD&A", f"{mdna_doc['form']} MD&A (overview and results)",
                             trim_tokens(_from_overview(mdna_text), caps["mdna"]), mdna_doc))
    # segment note: newest filing that has one, tables kept
    for doc in [d for d in (fs.get("10-Q"), fs.get("10-K")) if d]:
        note = extract_segment_note(fetch_filing_html(doc["url"]))
        if note:
            secs.append(_section("segment", f"{doc['accession']}:Segment note", f"{doc['form']} segment reporting note",
                                 trim_tokens(note, caps["segment"]), doc))
            break
    # earnings 8-K: the release text, plus any image-only deck transcribed
    if fs.get("8-K"):
        exhs = exhibits_of(fs["8-K"])
        release = next((e for e in exhs if not e["image_only"] and len(e["text"]) > 200), None)
        if release:
            secs.append(_section("exhibit", f"{release['accession']}:{release['name']}",
                                 f"8-K {release['name']} (earnings release)",
                                 trim_tokens(release["text"], caps["exhibit"]), release))
        for e in exhs:
            if e["image_only"]:
                t = transcribe_exhibit(e)
                if t.get("text"):
                    secs.append(_section("slides", f"{e['accession']}:{e['name']} (transcribed)",
                                         f"8-K {e['name']} (slides, transcribed)",
                                         trim_tokens(t["text"], caps["slides"]), e))
    return secs


# ---------------------------------------------------------------- guidance (general forward-looking language)
# forward-looking language, generically: a forward verb or noun, never a
# fiscal-year mention or "approximately" on their own (those describe past
# results just as often)
FORWARD_RE = re.compile(
    r"(?i)\b(expects?|expected|expecting|anticipates?|anticipated|outlook|guidance|forecasts?|forecasted|forecasting|"
    r"projected|projections?|targets?|targeting|targeted|we plan|plans? to|planned|intends? to|will be approximately|"
    r"for the full year|full[- ]year (?:revenue|sales|outlook|guidance|growth|margin))\b")
# a statement whose verbs are all past tense describes results, not guidance
_PAST_TENSE_RE = re.compile(r"(?i)\b(?:was|were|had|increased|decreased|rose|fell|declined|grew|recorded|reported|resulted)\b")
_FORWARD_VERB_RE = re.compile(r"(?i)\b(?:expect\w*|anticipat\w*|outlook|guidance|forecast\w*|target\w*|project\w*|plan\w*|intend\w*|will be)\b")
# a figure: money, percent, a scaled number, a multiple or a counted unit —
# not a date, an address or a footnote marker
FIGURE_RE = re.compile(
    r"\$\s?\d|\d(?:\.\d+)?\s?%|\b\d[\d,.]*\s*(?:billion|million|thousand|percent|basis points|bps|cents)\b|\b\d+(?:\.\d+)?x\b|"
    r"\b\d+\s+(?:\w+\s+){0,2}(?:restaurants|locations|stores|units|openings|new units|rigs|aircraft|vessels|branches|wells|markets)\b",
    re.IGNORECASE)
_EXCLUDE_RE = re.compile(
    r"(?i)forward-looking statements?|safe harbor|private securities litigation|unrecognized compensation|"
    r"expected to be recognized|\bASU\b|\bFASB\b|adoption of|conference call|webcast|accru|amortiz|"
    r"weighted-average (?:service|remaining|period)|expected (?:term|volatility|dividend yield|life)|interest rate swap|"
    r"actuarial|discount rate|fair value hierarchy|lease term|options? (?:granted|outstanding)|"
    r"today reported|reported (?:financial )?results|results for the (?:first|second|third|fourth) quarter|"
    r"we adopted|became effective|not have a material impact|^prior to|historically|forecasted income before taxes|"
    r"effective tax rate|to be recognized as revenue|remaining performance obligations|"
    r"\bVaR\b|confidence level|holding period|(?:adverse|central|baseline) scenario|unemployment rate|"
    r"allowance for credit losses|expected credit loss|risk-weighted assets|stress test|deferred tax")
_SLIDE_TITLE_RE = re.compile(r"(?i)outlook|guidance|assumptions|targets?|20\d\d (?:key|underlying|financial|guidance|outlook)|"
                             r"fiscal 20\d\d|full[- ]year|\bfy\s?'?\d\d|long[- ]term (?:algorithm|model|targets?)")
_SLIDE_HEAD_RE = re.compile(r"(?im)^slide\s+(\d+)\s*[:\-—]\s*(.*)$")
_PAST_RESULT_RE = re.compile(r"(?i)\b(?:were|was|increased|decreased|grew|declined|rose|fell)\b.{0,60}\b(?:in|for) the (?:first|second|third|fourth) quarter of fiscal 20\d\d\b")


def forward_language_present(text: str) -> bool:
    return bool(FORWARD_RE.search(text or ""))


def _split_statements(text: str) -> list:
    # sentences, bullets and table rows as separate statements; a numeric row
    # carries the last header row seen so its columns keep their names
    text = normalize_dollars(re.sub(r"[ \t]+", " ", text or ""))
    pieces, header = [], ""
    for para in re.split(r"\n+", text):
        para = para.strip()
        if not para:
            continue
        if " | " in para:
            if not any(is_figure_cell(c) for c in para.split("|")):
                header = para
                continue
            pieces.append(para + (f"  (columns: {header})" if header else ""))
            continue
        para = re.split(r"_{5,}", para)[0]                      # a footnote rule ends the statement
        para = re.sub(r"\s*\(\d\)\s+The Company is not readily able.*$", "", para)
        for part in re.split(r"\s+[•●▪]\s+|(?<=[.!?])\s+(?=[A-Z0-9\"'(“])", para):
            if part.strip():
                pieces.append(part.strip(" •●▪"))
    return pieces


def _key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())[:140]


def guidance_lines(sources: list, max_lines: int = 40) -> dict:
    """sources = [{"label","text","form","filing_date","url","kind":"text"|"slides"}].
    -> {"lines": [{"text","where","label","form","filing_date","url"}],
        "failures": [str]}. Guidance is every statement with forward-looking
    language AND a figure; in a slide transcript, every figure under a slide
    whose title says outlook / guidance / assumptions / targets counts too.
    Forward-looking language with no extractable figure is an EXTRACTION
    FAILURE, never 'no guidance'."""
    lines, failures, seen = [], [], set()

    def add(text, where, src):
        k = _key(text)
        if not k or k in seen or len(text) < 12:
            return
        seen.add(k)
        lines.append({"text": text[:600], "where": where, "label": src["label"], "form": src.get("form", ""),
                      "filing_date": src.get("filing_date", ""), "url": src.get("url", "")})

    for src in sources:
        text = src.get("text") or ""
        if not text.strip():
            if src.get("status") and src["status"] != "ok":
                failures.append(f"{src['label']}: {src['status']} — image-only exhibit with {src.get('slides', '?')} slides, "
                                "no guidance could be read from it")
            continue
        found_here = 0
        if src.get("kind") == "slides":
            current_title, under_outlook = "", False
            for raw in text.splitlines():
                line = raw.strip()
                if not line:
                    continue
                m = _SLIDE_HEAD_RE.match(line)
                if m:
                    current_title = m.group(2).strip()
                    under_outlook = bool(_SLIDE_TITLE_RE.search(current_title))
                    continue
                if not FIGURE_RE.search(line):
                    continue
                if under_outlook or (FORWARD_RE.search(line) and not _EXCLUDE_RE.search(line)):
                    add(line, f"slide: {current_title}" if current_title else "slide", src)
                    found_here += 1
        else:
            for stmt in _split_statements(text):
                if not (FORWARD_RE.search(stmt) and FIGURE_RE.search(stmt)):
                    continue
                if _EXCLUDE_RE.search(stmt) or len(stmt) > 500 or len(stmt) < 20:
                    continue
                if _PAST_RESULT_RE.search(stmt) and not re.search(r"(?i)expect|anticipat|outlook|guidance|target|forecast|project", stmt):
                    continue
                if _PAST_TENSE_RE.search(stmt) and not _FORWARD_VERB_RE.search(stmt):
                    continue
                add(stmt, "text", src)
                found_here += 1
        if not found_here and forward_language_present(text):
            failures.append(f"{src['label']}: forward-looking language is present but no guided figure could be extracted")
    if len(lines) > max_lines:
        failures.append(f"guidance lines capped at {max_lines} ({len(lines) - max_lines} more omitted)")
        lines = lines[:max_lines]
    return {"lines": lines, "failures": failures}


# ---------------------------------------------------------------- the earnings release: figures and quotes
_YOY_RE = re.compile(r"(?i)year[- ]over[- ]year|\byoy\b|y/y|quarter[- ]over[- ]quarter|sequential|prior[- ]year|"
                     r"compared (?:to|with)|increas\w+|decreas\w+|\bgrew\b|\bup \d|\bdown \d|\bflat\b|\bfrom \$?\d")
_REV_RE = re.compile(r"(?i)\b(?:revenues?|net revenues?|sales|net sales|comparable (?:restaurant |store )?sales|same[- ]store sales)\b")
_QUOTE_RE = re.compile(r"[\"“”]")
_SPEAKER_RE = re.compile(r"(?i)\bsaid\b|\bCEO\b|\bCFO\b|chief executive|chief financial|\bpresident\b|\bchairman\b|commented")


def release_lines(text: str, segment_names: list = None, max_figures: int = 12, max_quotes: int = 3) -> dict:
    """PURE. From an earnings release (tables kept as rows):
    figures — every revenue / sales statement with a figure and a change
    marker, and every table row naming revenue, sales or a known segment;
    quotes — the executive quote paragraphs, whole sentences only."""
    names = [n for n in (segment_names or []) if n and len(n) > 2]
    figures, quotes, seen = [], [], set()
    label_count = {}
    head = re.search(r"(?i)\b(first|second|third|fourth)[- ]quarter\b", (text or "")[:600])
    default_period = f"quarter ({head.group(1).lower()}-quarter release)" if head else ""

    def segment_of(s):
        low = s.lower()
        return next((n for n in names if n.lower() in low), "unspecified")

    quote_text = ""
    for para in re.split(r"\n{2,}", normalize_dollars(text or "")):
        p = re.sub(r"\s+", " ", para).strip()
        if _QUOTE_RE.search(p) and _SPEAKER_RE.search(p) and len(p) > 60:
            quote_text += " " + p.lower()
    for stmt in _split_statements(text):
        if len(figures) >= max_figures:
            break
        k = _key(stmt)
        if k in seen:
            continue
        if " | " not in stmt and _key(stmt)[:80] and _key(stmt)[:80] in re.sub(r"[^a-z0-9]", "", quote_text):
            continue   # the sentence is already shown inside an executive quote
        if " | " in stmt:
            cells = [c.strip() for c in stmt.split("  (columns:")[0].split("|")]
            numeric = sum(1 for c in cells if is_figure_cell(c))
            head = cells[0].lower()
            if numeric >= 2 and (_REV_RE.search(head) or any(n.lower() in head for n in names)):
                # the same row label repeats once per period column block (quarter,
                # year-to-date, prior year): keep the first two occurrences
                label_count[head] = label_count.get(head, 0) + 1
                if label_count[head] > 2:
                    continue
                seen.add(k)
                figures.append({"text": stmt[:400], "segment": segment_of(stmt), "kind": "row", "default_period": default_period})
            continue
        if _REV_RE.search(stmt) and re.search(r"\$\s?\d|\d(?:\.\d+)?\s?%", stmt) and _YOY_RE.search(stmt) and len(stmt) <= 450:
            if _EXCLUDE_RE.search(stmt):
                continue
            seen.add(k)
            figures.append({"text": stmt, "segment": segment_of(stmt), "kind": "sentence", "default_period": default_period})
    for para in re.split(r"\n{2,}", normalize_dollars(text or "")):
        p = re.sub(r"\s+", " ", para).strip()
        if len(quotes) >= max_quotes:
            break
        if not (_QUOTE_RE.search(p) and _SPEAKER_RE.search(p) and len(p) > 60):
            continue
        if len(p) > 900:
            cut = p[:900]
            ends = [m.end() for m in re.finditer(r"[.!?](?=[\"”')]*\s|[\"”')]*$)", cut)]
            p = (cut[:ends[-1]] if ends else cut).rstrip() + " [...]"
        quotes.append(p)
    return {"figures": figures, "quotes": quotes}


# ---------------------------------------------------------------- notes: every dollar figure
# (block title, heading pattern, topic words a kept sentence must mention —
# a note found by heading can run into a neighbour, so lines off-topic for
# the heading are dropped rather than labeled with it)
NOTE_HEADINGS = [
    ("Commitments and contingencies", r"commitments and contingencies",
     ("commit", "guarant", "oblig", "liabil", "exposure", "litigation", "purchase", "indemn", "contingen", "maximum", "lease", "notes")),
    ("Guarantees", r"guarantees", ("guarant", "maximum", "exposure", "oblig", "liabil")),
    ("Leases", r"leases", ("lease", "rent")),
    ("Debt", r"(?:long-term )?debt", ("debt", "notes", "principal", "borrow", "facility", "indenture", "loan", "matur")),
    ("Borrowings", r"borrowings", ("debt", "notes", "principal", "borrow", "facility", "indenture", "loan", "matur", "commercial paper")),
    ("Credit facility", r"credit (?:facility|facilities|agreement)", ("facility", "revolv", "loan", "borrow", "commitment", "credit")),
    ("Contingencies", r"contingencies", ("contingen", "litigation", "accru", "liabil", "exposure", "claim")),
]


def note_dollar_blocks(fs: dict, max_items: int = 8) -> list:
    """Dollar sentences from the commitments / guarantees / leases / debt notes
    of the newest 10-Q (then the 10-K for notes the 10-Q lacks). Block titles
    are the headings actually found. Never cuts a sentence before its first
    dollar figure (dollar_sentences)."""
    blocks, seen_lines, found = [], set(), set()
    for doc in [d for d in (fs.get("10-Q"), fs.get("10-K")) if d]:
        text = html_to_text(fetch_filing_html(doc["url"]))
        for title, pat, topics in NOTE_HEADINGS:
            if title in found:
                continue
            start = r"(?m)^\s*(?:note\s+)?(?:\d{1,2}\s*[.\-—:]?\s*)?" + pat + r"\b"
            note = _best_section(text, start, _NOTE_END, min_chars=200, max_chars=40000)
            if not note:
                continue
            lines = [ln for ln in dollar_sentences(note, max_items=max_items * 2)
                     if _key(ln) not in seen_lines and any(t in ln.lower() for t in topics)][:max_items]
            if not lines:
                continue
            for ln in lines:
                seen_lines.add(_key(ln))
            found.add(title)
            blocks.append({"title": f"{title} ({doc['form']} note)", "lines": lines, "form": doc["form"],
                           "filing_date": doc["filing_date"], "url": doc["url"]})
    return blocks


# ---------------------------------------------------------------- sources for the guidance scan
def guidance_sources(fs: dict, transcripts: list = None) -> list:
    """Texts the guidance scan reads: every 99.x exhibit of the earnings 8-K
    (tables kept), its transcribed decks, and the newest 10-Q MD&A."""
    sources = []
    if fs.get("8-K"):
        for exh in exhibits_of(fs["8-K"]):
            if exh["image_only"]:
                continue
            sources.append({"label": f"8-K {exh['name']}", "text": exh["text"], "form": "8-K",
                            "filing_date": exh["filing_date"], "url": exh["url"], "kind": "text"})
    for t in transcripts or []:
        sources.append({"label": f"8-K {t.get('name', 'exhibit')} (slides)", "text": t.get("text", ""), "form": "8-K",
                        "filing_date": t.get("filing_date", ""), "url": t.get("url", ""), "kind": "slides",
                        "status": t.get("status"), "slides": t.get("slides")})
    if fs.get("10-Q"):
        # the WHOLE 10-Q narrative, tables kept: an outlook paragraph can sit
        # anywhere (banks put it under an executive overview ahead of the
        # MD&A heading), so the scan is not limited to the extracted MD&A
        full = html_to_text(fetch_filing_html(fs["10-Q"]["url"]), keep_tables=True)
        sources.append({"label": "10-Q", "text": full, "form": "10-Q",
                        "filing_date": fs["10-Q"]["filing_date"], "url": fs["10-Q"]["url"], "kind": "text"})
    return sources
