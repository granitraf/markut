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
from markut.evidence import edgar as _edgar
from markut.evidence import filers
from markut.evidence.edgar import (EDGAR_CACHE, EXHIBIT_PATTERNS, edgar_get, extract_10k_business,
    extract_10k_sections, extract_10q_sections, extract_20f_sections, extract_40f_sections, fetch_filing_html,
    find_latest_filings, get_filing_index, html_to_text, is_figure_cell, normalize_dollars, strip_exhibit_label,
    strip_sgml_header, ticker_to_cik, dollar_sentences, _find_section)

CHARS_PER_TOKEN = 4


# ---------------------------------------------------------------- filing set
def _merge_older(recent: dict, cik: str) -> dict:
    """The recent table plus older pages, as one newest-first table."""
    merged = {k: list(v) for k, v in recent.items()}
    for page in _edgar.get_older_filings(cik):
        for k in merged:
            merged[k] += list(page.get(k) or [""] * len(page.get("form", [])))
    return merged


def filing_set(ticker: str) -> dict:
    """The filings that play each ROLE for this company, plus the accession
    fingerprint that keys every cache (profile, plan, transcripts).

    Run #28 (TSM): only 10-K / 10-Q / Item 2.02 8-K were looked for, so a
    foreign private issuer (20-F + 6-K) or a Canadian MJDS filer (40-F + 6-K)
    got nothing — with no reason. Roles now (evidence/filers.py):
      "10-K" slot = annual report (10-K | 20-F | 40-F)
      "10-Q" slot = interim report (10-Q | 6-K with interim statements / MD&A)
      "8-K"  slot = results release (Item 2.02 8-K | 6-K with the quarterly release)
    The slot keys stay for every caller; each document keeps its REAL form,
    which is what every label prints. An empty slot has a reason in
    fs["reasons"]; monthly revenue 6-Ks newer than the release are kept."""
    symbol = ticker.upper().strip()
    cik = ticker_to_cik(symbol)
    recent = get_filing_index(cik)
    ftype = filers.filer_type(recent["form"])
    annual_forms = {"domestic": ("10-K",), "foreign_private": ("20-F",), "canadian_mjds": ("40-F",)}.get(ftype, ())
    latest = find_latest_filings(recent, cik, list(annual_forms) + ["10-Q", "8-K", "6-K"], max_per_form=30)
    if annual_forms and not latest[annual_forms[0]]:
        older = find_latest_filings(_merge_older(recent, cik), cik, list(annual_forms), max_per_form=1)
        latest[annual_forms[0]] = older[annual_forms[0]]
    annual = latest[annual_forms[0]][0] if annual_forms and latest[annual_forms[0]] else None
    interim = earnings = None
    monthly = []
    if ftype == "domestic":
        interim = latest["10-Q"][0] if latest["10-Q"] else None
        earnings = next((e for e in latest["8-K"] if "2.02" in (e.get("items") or "")), None)
        current = latest["8-K"]
    else:
        # 6-Ks carry no item codes: read each (newest first, back to the annual
        # report) and sort it by what it carries
        current = latest["6-K"]
        floor = annual["filing_date"] if annual else ""
        for doc in current:
            if floor and doc["filing_date"] < floor:
                break
            kind = filers.classify_current_report(" ".join(t for _, t, _ in _filing_docs(doc, text_only=True)))
            doc["report_kind"] = kind
            if kind == "quarterly_results" and earnings is None:
                earnings = doc
            elif kind == "interim_report" and interim is None:
                interim = doc
            elif kind == "monthly_revenue" and earnings is None and len(monthly) < 3:
                monthly.append(doc)        # newer than the latest release: this quarter so far
            if earnings is not None and interim is not None:
                break
    reasons = {role: filers.slot_reason(role, ftype, recent["form"])
               for role, doc in (("annual", annual), ("interim", interim), ("earnings", earnings)) if doc is None}
    fingerprint = "|".join(d["accession"] if d else "-" for d in (annual, interim, earnings))
    return {"ticker": symbol, "cik": cik, "filer_type": ftype, "10-K": annual, "10-Q": interim, "8-K": earnings,
            "monthly": monthly, "reasons": reasons, "fingerprint": fingerprint, "all_8k": current}


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


def _listing(doc: dict) -> list:
    try:
        listing = json.loads(fetch_filing_html(doc["base_url"] + "/index.json"))
    except Exception as e:
        print(f"SECTIONS: exhibit listing failed for {doc.get('accession')} ({e})")
        return []
    return [it.get("name", "") for it in listing.get("directory", {}).get("item", [])]


def _exhibit_names(doc: dict, names: list) -> list:
    """The exhibit documents of one filing. An 8-K's exhibits are its 99.x
    files. A 6-K or 40-F names them freely (TSM: 'a2q26e_withguidance…htm'),
    so every HTML document except the index pages and the cover counts — and
    the cover itself when it is the only document (a monthly revenue 6-K)."""
    # XBRL viewer pages (R1.htm … R300.htm) and the filing summary are
    # generated by EDGAR, not filed by the company: never documents to read
    html = [n for n in names if n.lower().endswith((".htm", ".html")) and "index" not in n.lower()
            and not re.match(r"(?i)^R\d+\.html?$", n) and "filingsummary" not in n.lower()]
    if filers.base_form(doc.get("form", "")) == "8-K":
        return [n for n in html if re.search(r"99", n)]
    if filers.base_form(doc.get("form", "")) == "40-F":
        # some 40-F filers put the AIF, MD&A and statements in the main
        # document itself (Canadian Natural): read it with the exhibits
        return [n for n in html if n == doc.get("primary_doc")] + [n for n in html if n != doc.get("primary_doc")]
    others = [n for n in html if n != doc.get("primary_doc")]
    return others or [n for n in html if n == doc.get("primary_doc")]


def _filing_docs(doc: dict, text_only: bool = False) -> list:
    """[(name, text, raw html)] for every exhibit document of a filing."""
    out = []
    for n in sorted(_exhibit_names(doc, _listing(doc)), key=_exhibit_rank):
        try:
            raw = fetch_filing_html(doc["base_url"] + "/" + n)
        except Exception as e:
            print(f"SECTIONS: document {n} failed ({e})")
            continue
        out.append((n, strip_exhibit_label(strip_sgml_header(html_to_text(raw, keep_tables=not text_only))), raw))
    return out


def doc_text(doc: dict, keep_tables: bool = False) -> str:
    """The readable text of a filing role: the primary document of a 10-K,
    10-Q or 20-F; the exhibits of a 6-K or 40-F (where their content lives)."""
    if not doc:
        return ""
    if filers.base_form(doc["form"]) in ("6-K", "40-F"):
        return "\n\n".join(t for _, t, _ in _filing_docs(doc, text_only=not keep_tables))
    return html_to_text(fetch_filing_html(doc["url"]), keep_tables=keep_tables)


def exhibits_of(doc: dict) -> list:
    """Every exhibit of one 8-K (99.x) or 6-K / 40-F (any document but the
    cover): text (tables kept as rows), image URLs, and whether the exhibit
    is image-only (a slide deck filed as pictures)."""
    names = _listing(doc)
    if not names:
        return []
    html_names = _exhibit_names(doc, names)
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
                    "form": doc.get("form", "8-K"), "filing_date": doc["filing_date"], "accession": doc["accession"]})
    return out


def annual_sections(doc: dict) -> dict:
    """{"business"|"risk"|"mdna": (label, text)} from the annual report, by
    form (run #28): 10-K Items 1 / 1A / 7, 20-F Items 4 / 3.D / 5, 40-F
    Annual Information Form + annual MD&A exhibits. Text '' when absent."""
    if not doc:
        return {}
    form = filers.base_form(doc["form"])
    ok = lambda t: t if t and not t.startswith("[section unavailable") else ""
    if form == "10-K":
        html = fetch_filing_html(doc["url"])
        k = extract_10k_sections(html)
        return {"business": ("10-K Item 1 (Business)", ok(extract_10k_business(html))),
                "risk": ("10-K Item 1A (Risk Factors)", ok(k["Item 1A"])), "mdna": ("10-K Item 7 (MD&A)", ok(k["Item 7"]))}
    if form == "20-F":
        f = extract_20f_sections(fetch_filing_html(doc["url"]))
        return {"business": ("20-F Item 4 (Information on the Company)", ok(f["Item 4 (Information on the Company)"])),
                "risk": ("20-F Item 3.D (Risk Factors)", ok(f["Item 3.D (Risk Factors)"])),
                "mdna": ("20-F Item 5 (Operating and Financial Review)", ok(f["Item 5 (Operating and Financial Review)"]))}
    if form == "40-F":
        f = extract_40f_sections([t for _, t, _ in _filing_docs(doc, text_only=True)])
        return {"business": ("40-F Annual Information Form (business)", f["business"]),
                "risk": ("40-F Annual Information Form (risk factors)", f["risk"]), "mdna": ("40-F annual MD&A", f["mdna"])}
    return {}


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
    """Transcripts of the results release's (8-K / 6-K) image-only exhibits. With
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
    profile check reports what the model never saw. Read by ROLE (run #28):
    the annual report's business section (10-K Item 1 | 20-F Item 4 | 40-F
    AIF), the newest MD&A (10-Q | 6-K interim report | the annual one), a
    segment note, and the results release (8-K | 6-K exhibit)."""
    caps = config.PROFILE_SECTION_TOKENS
    secs = []
    annual, interim, earnings = fs.get("10-K"), fs.get("10-Q"), fs.get("8-K")
    ann = annual_sections(annual) if annual else {}
    label, business = ann.get("business", ("", ""))
    if business:
        secs.append(_section("item1", f"{annual['accession']}:{label}", label, trim_tokens(business, caps["item1"]), annual))
    # MD&A: the newest filing's narrative
    mdna_text, mdna_doc, mdna_name = "", None, ""
    if interim and (not annual or interim["filing_date"] >= annual["filing_date"]):
        if filers.base_form(interim["form"]) == "10-Q":
            mdna_text = extract_10q_sections(fetch_filing_html(interim["url"]))["Item 2 (10-Q MD&A)"]
            mdna_name = "10-Q MD&A (overview and results)"
        else:
            mdna_text = doc_text(interim)
            mdna_name = f"{interim['form']} interim report (overview and results)"
        mdna_doc = interim
    if (not mdna_text or mdna_text.startswith("[section unavailable")) and ann.get("mdna", ("", ""))[1]:
        mdna_text, mdna_doc = ann["mdna"][1], annual
        mdna_name = f"{ann['mdna'][0]} (overview and results)"
    if mdna_text and not mdna_text.startswith("[section unavailable"):
        secs.append(_section("mdna", f"{mdna_doc['accession']}:MD&A", mdna_name,
                             trim_tokens(_from_overview(mdna_text), caps["mdna"]), mdna_doc))
    # segment note: newest filing that has one, tables kept
    for doc in [d for d in (interim, annual) if d]:
        if filers.base_form(doc["form"]) in ("6-K", "40-F"):
            note = next((extract_segment_note(raw) for _, _, raw in _filing_docs(doc) if extract_segment_note(raw)), "")
        else:
            note = extract_segment_note(fetch_filing_html(doc["url"]))
        if note:
            secs.append(_section("segment", f"{doc['accession']}:Segment note", f"{doc['form']} segment reporting note",
                                 trim_tokens(note, caps["segment"]), doc))
            break
    # results release: the release text, plus any image-only deck transcribed
    if earnings:
        exhs = exhibits_of(earnings)
        release = next((e for e in exhs if not e["image_only"] and len(e["text"]) > 200), None)
        if release:
            secs.append(_section("exhibit", f"{release['accession']}:{release['name']}",
                                 f"{release['form']} {release['name']} (results release)",
                                 trim_tokens(release["text"], caps["exhibit"]), release))
        for e in exhs:
            if e["image_only"]:
                t = transcribe_exhibit(e)
                if t.get("text"):
                    secs.append(_section("slides", f"{e['accession']}:{e['name']} (transcribed)",
                                         f"{e['form']} {e['name']} (slides, transcribed)",
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
    r"\b\d+\s+(?:\w+\s+){0,2}(?:restaurants|locations|stores|units|openings|new units|rigs|aircraft|vessels|branches|wells|markets)\b|"
    r"\b(?:low|mid|high)[- ](?:to[- ](?:low|mid|high)[- ])?(?:single[- ]digits?|teens|twenties|thirties|forties|fifties|sixties|seventies|eighties|nineties)\b|"
    r"\b(?:roughly |approximately |about )?flat\b|"
    r"\b(?:similar to|in line with|consistent with|comparable to|unchanged from)\s+(?:the\s+)?(?:first|second|third|fourth|prior|previous)\s+quarter\b",
    re.IGNORECASE)
_EXCLUDE_RE = re.compile(
    r"(?i)forward-looking statements?|safe harbor|private securities litigation|unrecognized compensation|"
    r"expected to be recognized|\bASU\b|\bFASB\b|adoption of|conference call|webcast|accru|amortiz|"
    r"weighted-average (?:service|remaining|period)|expected (?:term|volatility|dividend yield|life)|interest rate swap|"
    r"actuarial|discount rate|fair value hierarchy|lease term|options? (?:granted|outstanding)|"
    r"today reported|reported (?:financial )?results|results for the (?:first|second|third|fourth) quarter|"
    r"we adopted|became effective|not have a material impact|^prior to|historically|forecasted income before taxes|"
    r"effective tax rate|to be recognized as revenue|remaining performance obligations|"
    # run #26 (P2, item 10): accounting SCHEDULES are not business targets
    r"deferred revenue|contract liabilit|performance obligations?|to be realized|realized in less than|"
    r"lease payments?|lease liabilit|minimum lease|lease obligations?|\bmatur(?:e|es|ity|ities)\b|principal (?:payments|amounts)|"
    r"unrecognized compensation|expected amortization|amortization expense|expected to be paid|interest payments|"
    r"purchase obligations?|future (?:minimum )?payments|"
    # run #28 (TSM interim statements): hedge tables and option-pricing
    # inputs are accounting model assumptions, not business targets
    r"hedg(?:e|es|ed|ing)|forecast(?:ed)? transactions?|risk-free (?:interest )?rate|expected (?:price )?volatility|"
    r"expected (?:dividend yield|term|life)|residual life|option[- ]pricing|black-scholes|monte carlo|"
    r"\bVaR\b|confidence level|holding period|(?:adverse|central|baseline) scenario|unemployment rate|"
    r"allowance for credit losses|expected credit loss|risk-weighted assets|stress test|deferred tax")
_SLIDE_TITLE_RE = re.compile(r"(?i)outlook|guidance|assumptions|targets?|20\d\d (?:key|underlying|financial|guidance|outlook)|"
                             r"fiscal 20\d\d|full[- ]year|\bfy\s?'?\d\d|long[- ]term (?:algorithm|model|targets?)")
_SLIDE_HEAD_RE = re.compile(r"(?im)^slide\s+(\d+)\s*[:\-—]\s*(.*)$")
_PAST_RESULT_RE = re.compile(r"(?i)\b(?:were|was|increased|decreased|grew|declined|rose|fell)\b.{0,60}\b(?:in|for) the (?:first|second|third|fourth) quarter of fiscal 20\d\d\b")


def forward_language_present(text: str) -> bool:
    # run #26 (P2, item 10): forward words inside an accounting schedule
    # ("expects 64% of deferred revenue to be realized…", "debt to mature")
    # are not a missed business target, so they never raise an EXTRACTION FAILURE
    return any(FORWARD_RE.search(st) and not _EXCLUDE_RE.search(st) for st in _split_statements(text or ""))


_BULLET_SPLIT = re.compile(r"\s+[•●▪]\s+|(?<=[.!?;])\s+(?=[A-Z0-9\"'(“])")


def _split_statements(text: str) -> list:
    # sentences, bullets and table rows as separate statements; a numeric row
    # carries the last header row seen so its columns keep their names; a
    # lead-in that ends with ":" ("For the third quarter of 2026, we expect:")
    # is carried into each bullet that follows it, so a bullet keeps the
    # forward verb and the period of its lead-in
    text = normalize_dollars(re.sub(r"[ \t]+", " ", text or ""))
    pieces, header, lead, lead_uses = [], "", "", 0
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
        bulleted = bool(re.match(r"^[•●▪\-–]\s", para)) or " • " in para
        if lead and not bulleted and not re.match(r"^[a-z]", para):
            lead, lead_uses = "", 0                                  # a new ordinary paragraph ends the lead-in
        parts = [p.strip(" •●▪-–") for p in _BULLET_SPLIT.split(para) if p.strip(" •●▪-–")]
        if parts and parts[0].endswith(":") and len(parts[0]) < 220:
            lead = parts[0].rstrip(":").strip()
            lead_uses = 0
            parts = parts[1:]
        for part in parts:
            if lead and lead_uses < 12:
                pieces.append(f"{lead}: {part}")
                lead_uses += 1
            else:
                pieces.append(part)
    return pieces


# ---- worded guidance ranges -> an interpreted numeric band ----
_TENS = {"single digit": (1, 9), "single-digit": (1, 9), "single digits": (1, 9), "teens": (10, 19), "twenties": (20, 29),
         "thirties": (30, 39), "forties": (40, 49), "fifties": (50, 59), "sixties": (60, 69), "seventies": (70, 79),
         "eighties": (80, 89), "nineties": (90, 99)}
# offsets within a ten-span: low 0-3, mid 4-6, high 7-9 (twenties: 20-23 / 24-26 / 27-29)
_THIRDS = {"low": (0, 3), "mid": (4, 6), "high": (7, 9)}
_WORDED_RE = re.compile(r"(?i)\b(?:(low|mid|high)(?:[- ]to[- ](low|mid|high))?[- ])?(single[- ]digits?|teens|twenties|thirties|forties|fifties|sixties|seventies|eighties|nineties)\b(?:\s*(?:percent(?:age)?(?:\s+rate)?|%))?")
_RANGE_RE = re.compile(r"(?i)\b(?:between\s+)?(\d+(?:\.\d+)?)\s?%?\s*(?:to|and|-|–)\s*(\d+(?:\.\d+)?)\s?%")
_APPROX_RE = re.compile(r"(?i)\b(?:approximately|about|around|roughly|~)\s*(\$?\d[\d,]*(?:\.\d+)?\s?(?:%|percent|billion|million|thousand|[bBmMkK])?)")


def interpret_band(text: str) -> list:
    """PURE. Worded or ranged guidance -> [(original phrase, interpreted band)].
    'low-thirties percentage rate' -> 30–33%; 'mid-to-high twenties' -> 24–29%;
    '33% to 34%' -> 33–34%; 'approximately $4.0 billion' -> ≈$4.0 billion;
    'flat' -> 0%."""
    out = []
    for m in _WORDED_RE.finditer(text or ""):
        q1, q2, tens = (m.group(1) or "").lower(), (m.group(2) or "").lower(), m.group(3).lower().replace("-", " ")
        lo, hi = _TENS.get(tens, _TENS.get(tens.rstrip("s"), (None, None)))
        if lo is None:
            continue
        base = 0 if lo == 1 else lo                      # single digits count from 0
        if q1:
            a = max(lo, base + _THIRDS[q1][0])
            b = min(hi, base + _THIRDS[q2 or q1][1])
        else:
            a, b = lo, hi
        out.append((m.group(0).strip(), f"{a}–{b}%"))
    # "similar to / in line with / consistent with the second quarter": the
    # prior period's value is the guide
    for m in re.finditer(r"(?i)\b(?:similar to|in line with|consistent with|comparable to|unchanged from)\s+(?:the\s+)?((?:first|second|third|fourth|prior|previous)\s+quarter(?:\s+of\s+\d{4})?|(?:fiscal|full)\s+(?:year\s+)?\d{4}|last year|the prior year)", text or ""):
        out.append((m.group(0).strip(), f"≈ the {m.group(1)} value (see the core figures)"))
    for m in _RANGE_RE.finditer(text or ""):
        out.append((m.group(0).strip(), f"{m.group(1)}–{m.group(2)}%"))
    for m in _APPROX_RE.finditer(text or ""):
        out.append((m.group(0).strip(), f"≈{m.group(1).strip()}"))
    if re.search(r"(?i)\b(?:roughly |approximately |about )?flat\b", text or ""):
        out.append(("flat", "≈0%"))
    seen, uniq = set(), []
    for phrase, band in out:
        if phrase.lower() not in seen:
            seen.add(phrase.lower()); uniq.append((phrase, band))
    return uniq


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
        found_here, unreadable = 0, 0
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
                if _EXCLUDE_RE.search(stmt) or len(stmt) < 20:
                    continue
                if len(stmt) > 500:
                    unreadable += 1          # a figure-bearing target too long to isolate
                    continue
                if _PAST_RESULT_RE.search(stmt) and not re.search(r"(?i)expect|anticipat|outlook|guidance|target|forecast|project", stmt):
                    continue
                if _PAST_TENSE_RE.search(stmt) and not _FORWARD_VERB_RE.search(stmt):
                    continue
                bands = interpret_band(stmt)
                worded = [(p, b) for p, b in bands if not re.match(r"^(?:approximately|about|around|roughly|~)", p, re.I)]
                add(stmt + (f"  (interpreted: {'; '.join(f'{p} → {b}' for p, b in worded)})" if worded else ""), "text", src)
                found_here += 1
        # run #26 (P2, item 10): a 10-Q / 10-K is full of forward-looking risk-factor
        # and MD&A prose ("expects these trends to intensify…", "intends to increase
        # its dividend") — no figure in it means no target, not a missed one. Only an
        # earnings release, slide deck or transcript, where targets live, raises the
        # failure on forward language alone; a periodic report raises it only for a
        # figure-bearing forward statement that could not be isolated.
        periodic = src.get("form") in ("10-Q", "10-K")
        if not found_here and (unreadable if periodic else forward_language_present(text)):
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
        text = doc_text(doc)
        for title, pat, topics in NOTE_HEADINGS:
            if title in found:
                continue
            start = r"(?m)^\s*(?:note\s+)?(?:\d{1,2}\s*[.\-—:]?\s*)?" + pat + r"\b"
            note = _best_section(text, start, _NOTE_END, min_chars=200, max_chars=40000)
            if not note:
                continue
            # run #26 (P2, item 11): a topic word must START a word — "release"
            # is not a lease sentence, "indebtedness" is not a debt figure
            lines = [ln for ln in dollar_sentences(note, max_items=max_items * 2)
                     if _key(ln) not in seen_lines and any(re.search(r"\b" + re.escape(t), ln, re.IGNORECASE) for t in topics)][:max_items]
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
    rel_form = (fs.get("8-K") or {}).get("form", "8-K")
    if fs.get("8-K"):
        for exh in exhibits_of(fs["8-K"]):
            if exh["image_only"]:
                continue
            sources.append({"label": f"{exh['form']} {exh['name']}", "text": exh["text"], "form": exh["form"],
                            "filing_date": exh["filing_date"], "url": exh["url"], "kind": "text"})
    for t in transcripts or []:
        sources.append({"label": f"{rel_form} {t.get('name', 'exhibit')} (slides)", "text": t.get("text", ""), "form": rel_form,
                        "filing_date": t.get("filing_date", ""), "url": t.get("url", ""), "kind": "slides",
                        "status": t.get("status"), "slides": t.get("slides")})
    if fs.get("10-Q"):
        # the WHOLE interim narrative, tables kept: an outlook paragraph can sit
        # anywhere (banks put it under an executive overview ahead of the
        # MD&A heading), so the scan is not limited to the extracted MD&A
        q = fs["10-Q"]
        sources.append({"label": q["form"], "text": doc_text(q, keep_tables=True), "form": q["form"],
                        "filing_date": q["filing_date"], "url": q["url"], "kind": "text"})
    return sources
