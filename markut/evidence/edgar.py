"""SEC EDGAR plumbing + filing extraction (notebook cells 16/18, verbatim;
User-Agent and cache path now read from markut.config)."""
from markut import config

import os, json, time, requests

# ---------------- EDGAR plumbing: identity, rate limit, disk cache ----------------
# WHY the User-Agent: SEC EDGAR's fair-access policy REQUIRES every request to
# identify who is asking (name + contact email) — anonymous requests get HTTP 403.
# This is not an API key, just honest self-identification.
EDGAR_UA = config.EDGAR_UA

# WHY a disk cache: filings NEVER change once filed, so re-downloading them is
# pure waste and burns against EDGAR's rate budget. Every fetched document lands
# under ./edgar_cache/ so reruns make ZERO network calls for known documents.
EDGAR_CACHE = config.EDGAR_CACHE
os.makedirs(EDGAR_CACHE, exist_ok=True)

# WHY rate limiting: EDGAR allows ~10 requests/second and blocks abusers. One
# module-level timestamp + a minimum gap between requests keeps us polite no
# matter how many different helpers call edgar_get in a row.
_edgar_last = 0.0

def edgar_get(url: str) -> requests.Response:
    global _edgar_last
    gap = 0.12 - (time.time() - _edgar_last)  # 0.12s gap ≈ 8 req/sec, safely under the cap
    if gap > 0:
        time.sleep(gap)
    resp = requests.get(url, headers=EDGAR_UA, timeout=30)
    _edgar_last = time.time()
    resp.raise_for_status()  # fail loudly HERE; callers wrap whole sections in try/except
    return resp


def ticker_to_cik(ticker: str) -> str:
    # WHY: EDGAR indexes companies by CIK (Central Index Key), not ticker. This
    # maps ticker -> zero-padded 10-digit CIK using SEC's official mapping file.
    #
    # WHY this doubles as a guardrail: company_tickers.json is the ground truth
    # for "does this ticker actually exist?". validate_ticker (helpers cell) only
    # checks the SHAPE of the string — this is the REAL existence check the
    # helpers-cell NOTE promised: input guardrail LAYER 2. It gets wired into the
    # run cell (right after validate_ticker) in the integration task.
    symbol = ticker.upper().strip()
    cache_path = os.path.join(EDGAR_CACHE, "company_tickers.json")
    if os.path.exists(cache_path):
        with open(cache_path) as f:
            mapping = json.load(f)
    else:
        print("EDGAR: downloading company_tickers.json (first run only)")
        resp = edgar_get("https://www.sec.gov/files/company_tickers.json")
        mapping = resp.json()
        with open(cache_path, "w") as f:
            json.dump(mapping, f)
    # File shape: {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
    for entry in mapping.values():
        if entry.get("ticker", "").upper() == symbol:
            return f"{int(entry['cik_str']):010d}"  # EDGAR URLs want 10 digits, zero-padded
    raise ValueError(
        f"Ticker {symbol!r} not found in SEC EDGAR's company list — it may be "
        "delisted, foreign-listed, or misspelled. (No model/API calls were made.)"
    )


def get_filing_index(cik: str) -> dict:
    # WHY no disk cache here (the one exception): unlike filed documents, this
    # index CHANGES every time the company files something new — caching it would
    # hide new filings forever. It is one small request per run, well within budget.
    print(f"EDGAR: fetching filing index for CIK {cik}")
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    data = edgar_get(url).json()
    # "recent" is a table of PARALLEL ARRAYS (form[i], accessionNumber[i],
    # filingDate[i], primaryDocument[i] all describe the same filing), newest first.
    return data["filings"]["recent"]


def find_latest_filings(index: dict, cik: str, form_types=("10-K", "8-K"), max_per_form=1) -> dict:
    # WHY form_types is a list: adding "10-Q" later must be a one-element change
    # here, not a rewrite.
    # WHY max_per_form exists: the 8-K press-release hunt (extraction cell) may
    # need to walk BACK through several recent 8-Ks to find one with an earnings
    # exhibit, so the finder can return more than one filing per form on request.
    # WHY cik is a parameter: the recent-filings table does NOT carry the CIK,
    # but document URLs require it (.../edgar/data/{cik}/{accession}/{doc}).
    found = {form: [] for form in form_types}
    cik_int = int(cik)  # Archives URL paths use the UNPADDED integer form
    for i, form in enumerate(index["form"]):
        if form in found and len(found[form]) < max_per_form:
            accession = index["accessionNumber"][i]
            acc_nodash = accession.replace("-", "")
            primary = index["primaryDocument"][i]
            base_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}"
            found[form].append({
                "form": form,
                "accession": accession,
                "filing_date": index["filingDate"][i],
                # 8-K item codes, e.g. "2.02,9.01" (2.02 = earnings). Empty
                # for 10-K/10-Q. Lets the press-release hunt pick the RIGHT
                # 8-K instead of hoping the newest one is an earnings release.
                "items": (index.get("items") or [""] * len(index["form"]))[i],
                "primary_doc": primary,
                "url": f"{base_url}/{primary}",
                # base_url lets the extraction cell list ALL files inside this
                # filing — needed to hunt for exhibit 99.1 inside an 8-K.
                "base_url": base_url,
            })
        if all(len(v) >= max_per_form for v in found.values()):
            break  # arrays are newest-first: once every form has enough, stop scanning
    return found


import re, json, os
from bs4 import BeautifulSoup

def fetch_filing_html(url: str) -> str:
    # WHY cache-first: filings are IMMUTABLE once filed, so a document fetched
    # once never needs the network again. The cache key is the accession folder
    # + file name (the last two URL parts) — accession alone isn't unique enough
    # because one filing contains many files (primary doc, exhibits, index.json).
    parts = url.rstrip("/").split("/")
    cache_path = os.path.join(EDGAR_CACHE, f"{parts[-2]}_{parts[-1]}")
    if os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as f:
            return f.read()
    print(f"EDGAR: downloading {parts[-1]}")
    text = edgar_get(url).text
    with open(cache_path, "w", encoding="utf-8") as f:
        f.write(text)
    return text


def html_to_text(html: str) -> str:
    # WHY BeautifulSoup + manual paragraph stitching: EDGAR HTML is tag soup —
    # inline-XBRL filings wrap single sentences in dozens of nested tags, so a
    # naive get_text() either glues the whole document into one blob or splits
    # every styled word onto its own line. We flatten to lines, then stitch
    # lines back into paragraphs, ending a paragraph where a line ends with
    # sentence-final punctuation. The RAG chunker later splits on these \n\n
    # breaks, so paragraph integrity here directly controls chunk quality.
    soup = BeautifulSoup(html, "html.parser")
    # WHY drop tables: a financial table flattened to text is a context-free
    # number stream that poisons semantic search. The narrative is the value;
    # hard numbers already arrive structured via market_snapshot.
    for t in soup(["script", "style", "table"]):
        t.decompose()
    raw_lines = soup.get_text("\n").replace("\xa0", " ").splitlines()
    paras, buf = [], ""
    # WHY the heading break (added after LIVE testing on NVDA's real 10-K):
    # headings like "Item 7. Management's Discussion..." often follow a line
    # that ends WITHOUT sentence punctuation (page numbers, "PART II"), so the
    # sentence-end heuristic merged them into the middle of a paragraph where
    # the line-anchored section regexes could never see them — Item 7 came back
    # "unavailable" on a real filing. Forcing a break BEFORE any heading-shaped
    # line guarantees every "Item X." / "PART N" starts its own paragraph. The
    # required punctuation after the item number keeps bare cross-references
    # ("see Item 1A of Part I") from triggering false breaks.
    heading = re.compile(r"^(item\s+\d+[a-z]?\s*[.:\-]|part\s+[ivx]+\b)", re.IGNORECASE)
    for ln in raw_lines:
        ln = re.sub(r"[ \t]+", " ", ln).strip()
        if not ln:
            continue
        if buf and heading.match(ln):
            paras.append(buf)
            buf = ""
        buf = f"{buf} {ln}".strip()
        if ln.endswith((".", "?", "!")):  # heuristic: sentence end == paragraph end
            paras.append(buf)
            buf = ""
    if buf:
        paras.append(buf)
    return "\n\n".join(paras)


def _find_section(text: str, start_pat: str, end_pats) -> str:
    # WHY every-occurrence + longest-span: a 10-K's TABLE OF CONTENTS lists every
    # item with the same words as the real section heading, so matching the FIRST
    # "Item 1A" almost always lands in the TOC. Instead we try every occurrence
    # of the start pattern and keep the longest start->end span: a TOC entry ends
    # one line later at the next item, while the real section runs for pages.
    best = ""
    for m in re.finditer(start_pat, text, re.IGNORECASE):
        s = m.start()
        end = len(text)
        for ep in end_pats:
            # +20 skips past the heading itself so "Item 7" doesn't end at "Item 7A"
            # sitting inside its own heading line.
            e = re.search(ep, text[s + 20:], re.IGNORECASE)
            if e:
                end = min(end, s + 20 + e.start())
        span = text[s:end].strip()
        if len(span) > len(best):
            best = span
    return best


def extract_10k_sections(html: str) -> dict:
    # Full text of Item 1A (Risk Factors) and Item 7 (MD&A) ONLY — the two
    # narrative sections a debate can actually use.
    # WHY (?m)^ anchors: cross-references ("see Item 1A") appear mid-sentence all
    # over a 10-K; real headings start a line. Anchoring to line starts keeps
    # references from becoming false section starts.
    text = html_to_text(html)
    item_1a = _find_section(
        text,
        r"(?m)^\s*item\s+1a\b",
        [r"(?m)^\s*item\s+1b\b", r"(?m)^\s*item\s+2\b"],
    )
    item_7 = _find_section(
        text,
        r"(?m)^\s*item\s+7\b",   # \b will NOT match "Item 7A" (digit->letter, no boundary)
        [r"(?m)^\s*item\s+7a\b", r"(?m)^\s*item\s+8\b"],
    )
    # WHY marker strings instead of raising: one filer's odd formatting must cost
    # one section, never the run. Downstream code checks for the "[section
    # unavailable" prefix and skips indexing that section.
    # WHY the 500-char floor: a real section is pages long; anything shorter is a
    # TOC line or stray cross-reference that slipped through the anchors.
    MIN_CHARS = 500
    return {
        "Item 1A": item_1a if len(item_1a) >= MIN_CHARS
        else "[section unavailable: Item 1A (Risk Factors) not located in this 10-K]",
        "Item 7": item_7 if len(item_7) >= MIN_CHARS
        else "[section unavailable: Item 7 (MD&A) not located in this 10-K]",
    }


def extract_10q_sections(html: str) -> dict:
    # Latest-quarter narrative: Part I Item 2 (MD&A) and Part II Item 1A (risk
    # factor UPDATES since the last 10-K). WHY include the 10-Q: the 10-K can be
    # nearly a year stale; the 10-Q is the freshest narrative the company filed.
    text = html_to_text(html)
    # WHY "management" in the start pattern: a 10-Q has TWO "Item 2" headings —
    # Part I Item 2 (MD&A) and Part II Item 2 (Unregistered Sales). Requiring
    # the word right after the number anchors us to the MD&A heading.
    mdna = _find_section(
        text,
        r"(?m)^\s*item\s+2\.?\s*[:\-]?\s*management",
        [r"(?m)^\s*item\s+3\b", r"(?m)^\s*item\s+4\b"],
    )
    # NOTE: an earlier draft fell back to a bare "item 2" pattern when the
    # management-anchored one missed. LIVE testing on NVDA's real 10-Q showed
    # that fallback confidently returning Part II "Unregistered Sales of Equity
    # Securities" LABELED as MD&A. A mislabeled section is far worse than a
    # missing one — the source tag would lie to the debaters — so: no fallback,
    # honest marker instead.
    risks = _find_section(
        text,
        r"(?m)^\s*item\s+1a\b",
        [r"(?m)^\s*item\s+2\b", r"(?m)^\s*item\s+6\b"],
    )
    # WHY a LOWER floor than the 10-K (300 vs 500): a quarter with no new risks
    # legitimately says only "no material changes from our last 10-K" — short,
    # but real content the debaters should see.
    return {
        "Item 2 (10-Q MD&A)": mdna if len(mdna) >= 500
        else "[section unavailable: Item 2 (MD&A) not located in this 10-Q]",
        "Item 1A (10-Q update)": risks if len(risks) >= 300
        else "[section unavailable: Item 1A (risk updates) not located in this 10-Q]",
    }

def extract_8k_press_release(eightk_entries: list) -> dict:
    # WHY select by ITEM CODE first (added after LIVE testing): frequent filers
    # like NVDA push out 8-Ks constantly (debt offerings, votes, board changes)
    # — on the real data the 4 newest 8-Ks contained NO earnings release at all.
    # EDGAR tags every 8-K with item codes, and "2.02" means "Results of
    # Operations and Financial Condition" — i.e., THE earnings 8-K. Hunting only
    # those is the precise version of "the most recent 8-K that has a press
    # release". Within them we still walk back (limit 4) past any with no
    # usable exhibit. Returns {"text", "filing_date", "url", "form"}; on total
    # failure "text" is an explicit unavailable marker, never an exception.
    earnings = [e for e in eightk_entries if "2.02" in e.get("items", "")]
    if not earnings:
        return {
            "text": "[press release unavailable: no Item 2.02 (earnings) 8-K among the recent filings scanned]",
            "filing_date": None,
            "url": None,
            "form": "8-K",
        }
    for entry in earnings[:4]:
        try:
            # index.json lists every file inside the filing folder — this is how
            # we find the exhibit, since its file name varies wildly by filer.
            listing = json.loads(fetch_filing_html(entry["base_url"] + "/index.json"))
            names = [
                it.get("name", "")
                for it in listing.get("directory", {}).get("item", [])
                if it.get("name", "").lower().endswith((".htm", ".html"))
            ]
            exhibit = None
            # Preference order: standard EX-99.1 spellings first, then
            # press-release-style names — LIVE testing showed NVDA attaches
            # "q1fy27pr.htm" / "q4fy26pr.htm", never "ex99_1.htm" — and last
            # the 99.2 / CFO-commentary fallbacks. Order encodes preference:
            # the actual press release beats the commentary document.
            for pat in (r"ex[-_]?99[._-]?1", r"99[._-]?1",
                        r"pressrelease", r"pr\.html?$",
                        r"ex[-_]?99[._-]?2", r"99[._-]?2", r"cfocommentary"):
                hits = [n for n in names if re.search(pat, n.lower())]
                if hits:
                    exhibit = hits[0]
                    break
            if not exhibit:
                print(f"EDGAR: 8-K filed {entry['filing_date']} has no 99.1/99.2 exhibit — walking back")
                continue
            url = entry["base_url"] + "/" + exhibit
            text = strip_sgml_header(html_to_text(fetch_filing_html(url)))
            if len(text) < 200:  # exhibit exists but is a stub/graphic — keep walking
                continue
            return {"text": text, "filing_date": entry["filing_date"], "url": url, "form": "8-K"}
        except Exception as e:
            # WHY swallow-and-continue: a single malformed filing folder must not
            # kill the hunt; the next-older 8-K may be perfectly fine.
            print(f"EDGAR: 8-K {entry.get('filing_date', '?')} failed ({e}) — walking back")
    return {
        "text": "[press release unavailable: no usable 99.1/99.2 exhibit in the last 4 8-Ks]",
        "filing_date": None,
        "url": None,
        "form": "8-K",
    }


# ---------------- targeted extraction helpers (RAG quality pass) ----------------

def strip_sgml_header(text: str) -> str:
    # PURE. Some exhibits carry an SGML document-header line that html_to_text
    # merges into the first paragraph as "EX-99.1 2 q1fy27pr.htm ...". That
    # junk sits at the exact spot the embedder weighs most — the head of the
    # press release's first chunk — so strip the wrapper prefix (through the
    # file name) and keep the real first words. Anchored to the string start:
    # nothing after the first real word can ever be touched.
    return re.sub(r"(?i)^\s*ex-?\d+(\.\d+)?\s+\d+\s+\S+\.html?\s*", "", text)


def extract_outlook(press_text: str) -> str:
    # PURE. Deterministic guidance extraction — NOT semantic search. The
    # forward guidance in an earnings release sits under a heading that
    # literally says "Outlook" (or "Guidance"); the retrieval audit scored
    # 0.08-0.13 cosine (noise) trying to *search* for what a string match
    # finds exactly. Scan paragraphs for the heading, then keep the following
    # paragraphs only WHILE they still read like guidance ("expected to be...")
    # — the first paragraph without guidance language is boilerplate
    # (conference-call info, forward-looking-statements legalese) and stops
    # the collection. Returns "" when no heading is found, so the caller can
    # report an honest absence instead of quoting noise.
    paras = [p.strip() for p in press_text.split("\n\n") if p.strip()]
    guidance_like = re.compile(r"(?i)\b(expected|expects|anticipates|estimated|guidance|outlook)\b")
    for i, p in enumerate(paras):
        if "outlook" in p[:80].lower() or "guidance" in p[:80].lower():
            block = [p]
            for q in paras[i + 1: i + 8]:
                if not guidance_like.search(q[:200]) or sum(len(b) for b in block) > 1200:
                    break
                block.append(q)
            return "\n\n".join(block)
    return ""


def extract_risk_titles(html: str, section_text: str) -> list:
    # Risk Factors is a TITLED LIST: each risk opens with a bold/italic caption
    # ("We depend on a limited number of customers...") that is management's
    # own one-line summary of that risk — the densest sentences in the whole
    # 10-K. html_to_text flattens that markup away, so this goes back to the
    # RAW HTML for visually-emphasized runs, then keeps only candidates that
    # actually appear inside the extracted Item 1A text (membership is the
    # cheap way to scope titles to the right section without re-doing section
    # math on HTML offsets).
    soup = BeautifulSoup(html, "html.parser")
    normalized = re.sub(r"\s+", " ", section_text.replace("\xa0", " "))
    titles, seen = [], set()
    for el in soup.find_all(["b", "strong", "i", "em", "span", "p", "div"]):
        # <b>/<strong>/<i>/<em> are emphasis by definition; span/p/div count
        # only when their inline style says bold (how inline-XBRL filers do it).
        if el.name in ("span", "p", "div"):
            style = (el.get("style") or "").replace(" ", "").lower()
            if "font-weight:700" not in style and "font-weight:bold" not in style:
                continue
        t = re.sub(r"\s+", " ", el.get_text(" ", strip=True).replace("\xa0", " ")).strip()
        # Caption shape: a full sentence of 6-60 words. Short bold runs are
        # inline emphasis ("Item 1A.", defined terms), not risk captions.
        if not (6 <= len(t.split()) <= 60 and t.endswith((".", "?"))):
            continue
        if t in seen or t not in normalized:
            continue
        seen.add(t)
        titles.append(t)  # find_all walks in document order, so titles stay ordered
    return titles
