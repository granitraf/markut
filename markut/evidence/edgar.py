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


def get_older_filings(cik: str, pages: int = 2) -> list:
    """Older pages of the filing history (run #28, 2026-10-11). The 'recent'
    table holds the latest 1,000 filings; a company with heavy Form 4 / 6-K
    traffic can push its annual report out of it. Each page has the same
    parallel arrays. Never raises: no pages -> []."""
    try:
        data = edgar_get(f"https://data.sec.gov/submissions/CIK{cik}.json").json()
        out = []
        for f in (data.get("filings", {}).get("files") or [])[:pages]:
            out.append(edgar_get(f"https://data.sec.gov/submissions/{f['name']}").json())
        return out
    except Exception as e:
        print(f"EDGAR: older filing pages unavailable ({e})")
        return []


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


_ZERO_WIDTH = re.compile(r"[\u200b\u200c\u200d\ufeff]")
# "$(7,604)" — the sign before the bracket — is a figure too (2026-10-11)
_FIGURE_CELL = re.compile(r"^(?:\$\s?)?\(?[-+]?\$?\s?\d[\d,]*(?:\.\d+)?\)?\s?(?:%|x|bps)?\)?$|^[-+]\d")


def is_figure_cell(cell: str) -> bool:
    # PURE. A cell that IS a figure ("$729,476", "(3)%", "+127%", "5.8"), as
    # opposed to a label that merely contains digits ("Q3 FY26", "June 30, 2026")
    return bool(_FIGURE_CELL.match((cell or "").strip()))


def table_rows(table) -> list:
    # PURE over a BeautifulSoup <table>: one " | "-joined line per row that
    # carries a number, preceded by its column-header row. Inline-XBRL splits
    # "$" and "29,000" (and a trailing ")" or "%") into separate cells and
    # spreads a multi-word column header over several rows; the fragments are
    # merged back so a row reads "Semiconductor solutions | $24,882 | $12,598 | 98%".
    raw_rows = []
    for tr in table.find_all("tr"):
        cells = []
        for td in tr.find_all(["td", "th"]):
            c = re.sub(r"\s+", " ", _ZERO_WIDTH.sub("", td.get_text(" ", strip=True)).replace("\xa0", " ")).strip()
            if not c:
                cells.append("")
                continue
            if c in (")", "%", ")%", "%)") and any(cells):
                last = max(i for i, x in enumerate(cells) if x)
                cells[last] += c
            elif any(cells) and cells[max(i for i, x in enumerate(cells) if x)] in ("$", "(", "($", "$("):
                last = max(i for i, x in enumerate(cells) if x)
                cells[last] += c
            else:
                cells.append(c)
        cells = [c for c in cells if c not in ("$", "(", ")", "—", "-")]
        if any(cells):
            raw_rows.append(cells)
    # merge runs of header rows (no figures) of equal width column-wise:
    # a three-word brand name stacked over one column becomes one header
    merged = []
    for cells in raw_rows:
        numeric = any(is_figure_cell(c) for c in cells)
        if (not numeric and merged and merged[-1][0] is False and len(merged[-1][1]) == len(cells)):
            merged[-1] = (False, [(a + " " + b).strip() for a, b in zip(merged[-1][1], cells)])
        else:
            merged.append((numeric, cells))
    rows = []
    for numeric, cells in merged:
        cells = [c for c in cells if c]
        if numeric and len(cells) >= 2:
            rows.append(" | ".join(cells))
        elif not numeric and len(cells) >= 2 and all(len(c) < 60 for c in cells):
            rows.append(" | ".join(cells))
    return rows


def html_to_text(html: str, keep_tables: bool = False) -> str:
    # WHY BeautifulSoup + manual paragraph stitching: EDGAR HTML is tag soup —
    # inline-XBRL filings wrap single sentences in dozens of nested tags, so a
    # naive get_text() either glues the whole document into one blob or splits
    # every styled word onto its own line. We flatten to lines, then stitch
    # lines back into paragraphs, ending a paragraph where a line ends with
    # sentence-final punctuation. The RAG chunker later splits on these \n\n
    # breaks, so paragraph integrity here directly controls chunk quality.
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style"]):
        t.decompose()
    if keep_tables:
        # the segment note, the earnings release and guidance tables carry
        # their figures in cells: each row becomes its own " | " line
        for table in soup.find_all("table"):
            if table.find_parent("table") is not None:
                continue
            rows = table_rows(table)
            table.replace_with(soup.new_string("\n" + "\n".join(rows) + "\n") if rows else soup.new_string("\n"))
    else:
        # WHY drop tables: a financial table flattened to text is a context-free
        # number stream that poisons semantic search. The narrative is the value;
        # hard numbers already arrive structured via market_snapshot.
        for t in soup("table"):
            t.decompose()
    raw_lines = soup.get_text("\n").replace("\xa0", " ").splitlines()
    paras, buf = [], ""
    # WHY the heading break (added after LIVE testing on a real 10-K):
    # headings like "Item 7. Management's Discussion..." often follow a line
    # that ends WITHOUT sentence punctuation (page numbers, "PART II"), so the
    # sentence-end heuristic merged them into the middle of a paragraph where
    # the line-anchored section regexes could never see them — Item 7 came back
    # "unavailable" on a real filing. Forcing a break BEFORE any heading-shaped
    # line guarantees every "Item X." / "PART N" starts its own paragraph. The
    # required punctuation after the item number keeps bare cross-references
    # ("see Item 1A of Part I") from triggering false breaks.
    heading = re.compile(r"^(item\s+\d+[a-z]?\s*[.:\-]|part\s+[ivx]+\b)", re.IGNORECASE)
    # page furniture: "Table of Contents" back-links always; a bare 1-3 digit
    # line only BETWEEN paragraphs (buf empty) — inline-XBRL puts every tagged
    # number on its own line, so a bare "29" mid-sentence is "$ 29 billion",
    # not a page number
    toc = re.compile(r"^(?:table of contents|\d{1,3}\s+table of contents|table of contents\s+\d{1,3})$", re.IGNORECASE)
    page_no = re.compile(r"^\d{1,3}$")
    for ln in raw_lines:
        ln = re.sub(r"[ \t]+", " ", ln).strip()
        if not ln or toc.match(ln) or (page_no.match(ln) and not buf):
            continue
        if " | " in ln:   # a flattened table row keeps its own paragraph
            if buf:
                paras.append(buf)
                buf = ""
            paras.append(ln)
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


def extract_20f_sections(html: str) -> dict:
    """A 20-F's narrative items (run #28, foreign private issuers): Item 3.D
    risk factors (inside Item 3 'Key Information'), Item 4 'Information on
    the Company' (the business), Item 5 'Operating and Financial Review and
    Prospects' (the annual MD&A). Same honest-marker convention as the 10-K
    reader. Headings often use a non-breaking space ('ITEM\u00a04.')."""
    text = html_to_text(html)
    item3 = _find_section(text, r"(?m)^\s*item\s+3\b", [r"(?m)^\s*item\s+4\b"])
    m = re.search(r"(?im)^\s*(?:d\.\s*)?risk factors\b", item3) or re.search(r"(?i)\brisk factors\b", item3)
    risks = item3[m.start():] if m else ""
    item4 = _find_section(text, r"(?m)^\s*item\s+4\b(?!\s*a)", [r"(?m)^\s*item\s+4\s*a\b", r"(?m)^\s*item\s+5\b"])
    item5 = _find_section(text, r"(?m)^\s*item\s+5\b", [r"(?m)^\s*item\s+6\b"])
    return {
        "Item 3.D (Risk Factors)": risks if len(risks) >= 500
        else "[section unavailable: Item 3.D (Risk Factors) not located in this 20-F]",
        "Item 4 (Information on the Company)": item4 if len(item4) >= 500
        else "[section unavailable: Item 4 (Information on the Company) not located in this 20-F]",
        "Item 5 (Operating and Financial Review)": item5 if len(item5) >= 500
        else "[section unavailable: Item 5 (Operating and Financial Review) not located in this 20-F]",
    }


def extract_40f_sections(exhibit_texts: list) -> dict:
    """A 40-F's narrative lives in its exhibits (run #28, Canadian MJDS
    filers): the Annual Information Form (business and risk factors) and
    the annual MD&A. exhibit_texts: [text, ...]. Missing parts -> ''."""
    # an exhibit is what its TITLE says it is: the MD&A mentions the AIF in its
    # first paragraph (Suncor), so the test is the opening ~300 characters
    title = lambda t: re.sub(r"[\s\u200b]+", " ", t)[:300].lower()
    aif = next((t for t in exhibit_texts if "annual information form" in title(t)), "")
    mda = next((t for t in exhibit_texts if re.search(r"management['’]s discussion and analysis", title(t))
                and "annual information form" not in title(t)[:80]), "")

    def longest(text, start, ends):
        # AIF headings are usually CAPITALIZED, while the table of contents and
        # cross-references use title case ("Risk Factors 40") — prefer
        # capitalized occurrences, fall back to any case. Among them keep the
        # longest span (a contents entry ends at the next heading one line
        # later). The text converter can fold a short heading into the line
        # before it, so occurrences are not anchored to line starts.
        best = ""
        caps = [m for m in re.finditer(start.upper(), text)]
        for m in caps or list(re.finditer(start, text, re.IGNORECASE)):
            tail = text[m.start() + 20:]
            hits = [x.start() for ep in ends for x in [re.search(ep, tail) if caps else re.search(ep, tail, re.IGNORECASE)] if x]
            if caps and not hits:
                hits = [x.start() for ep in ends for x in [re.search(ep, tail, re.IGNORECASE)] if x]
            e = m.start() + 20 + min(hits) if hits else len(text)
            if e - m.start() > len(best):
                best = text[m.start():e].strip()
        return best
    pool = aif or "\n\n".join(exhibit_texts)
    business = longest(pool, r"DESCRIPTION OF THE BUSINESS|GENERAL DEVELOPMENT OF THE BUSINESS|OUR BUSINESS", [r"RISK FACTORS"])
    # an AIF's sections follow a standard order; any of the ones after Risk
    # Factors ends it (Canadian Natural's ran 250k characters to "dividends")
    risks = longest(pool, r"RISK FACTORS", [r"DIVIDENDS|DESCRIPTION OF (?:SHARE )?CAPITAL(?: STRUCTURE)?|MARKET FOR (?:THE )?SECURITIES|"
                                           r"DIRECTORS AND (?:EXECUTIVE )?OFFICERS|LEGAL PROCEEDINGS|INTERESTS? OF EXPERTS|AUDIT COMMITTEE|"
                                           r"TRANSFER AGENTS?|MATERIAL CONTRACTS|INDUSTRY CONDITIONS|ENVIRONMENTAL (?:AND SOCIAL )?(?:MATTERS|REGULATION)"])
    if not mda:
        # the MD&A inside the main document (no titled exhibit): it runs to the
        # statements that follow it
        rest = "\n\n".join(exhibit_texts)
        mda = longest(rest, r"MANAGEMENT['’]S DISCUSSION AND ANALYSIS",
                      [r"MANAGEMENT['’]S REPORT|REPORT OF INDEPENDENT REGISTERED|INDEPENDENT AUDITOR['’]?S['’]? REPORT|CONSOLIDATED BALANCE SHEETS?\b"])
    return {"business": business if len(business) >= 300 else "", "risk": risks if len(risks) >= 300 else "",
            "mdna": mda if len(mda) >= 500 else ""}


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
    # management-anchored one missed. LIVE testing on a real 10-Q showed
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

# Preference order for the earnings exhibit inside an 8-K folder. Specific
# 99.1 spellings first, then press-release-style names, then 99.2 / CFO
# commentary, and LAST a bare "ex99" with no part number — AUDIT FIX (run #2):
# one filer's release name matched none of the earlier
# patterns matched, so no 8-K was ever indexed and the guidance block came up
# empty (a filer named its release "<ticker>-<date>x8kxex99.htm"). iXBRL
# filers also write "ex99d1" for 99.1.
EXHIBIT_PATTERNS = (
    r"ex[-_]?99[._-]?1", r"99[._-]?1", r"ex99d1",
    r"pressrelease", r"earningsrelease", r"pr\.html?$",
    r"ex[-_]?99[._-]?2", r"99[._-]?2", r"ex99d2", r"cfocommentary",
    r"ex[-_]?99(?![0-9])",
)


def pick_exhibit(names: list):
    # PURE. First file name matching the earliest pattern in EXHIBIT_PATTERNS;
    # index/primary documents never match. None when nothing fits.
    for pat in EXHIBIT_PATTERNS:
        hits = [n for n in names if re.search(pat, n.lower()) and "index" not in n.lower()]
        if hits:
            return hits[0]
    return None


def extract_8k_press_release(eightk_entries: list) -> dict:
    # WHY select by ITEM CODE first (added after LIVE testing): frequent filers
    # like large caps push out 8-Ks constantly (debt offerings, votes, board changes)
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
            exhibit = pick_exhibit(names)
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


# ---------------- AUDIT PASS (run #2): what the company sells; what it has promised ----------------

def extract_10k_business(html: str) -> str:
    # Item 1 (Business): the section that says what the company actually sells
    # and to whom. AUDIT FIX: a debate once called a company's own product a
    # competitor because the agents never saw Item 1. Anchored like the other
    # sections; "item 1" must not be followed by
    # A/B/C (Item 1A is Risk Factors) and ends where Item 1A starts.
    text = html_to_text(html)
    item_1 = _find_section(
        text,
        r"(?m)^\s*item\s+1(?![0-9a-c])\b",
        [r"(?m)^\s*item\s+1a\b", r"(?m)^\s*item\s+2\b"],
    )
    return item_1 if len(item_1) >= 500 else "[section unavailable: Item 1 (Business) not located in this 10-K]"


def business_overview(item_1_text: str, char_limit: int = 1000) -> str:
    # PURE. The opening paragraphs of Item 1 — the company's own description of
    # its business — cut at a sentence boundary. Deterministic (no search): the
    # overview is always the head of the section.
    if item_1_text.startswith("[section unavailable"):
        return ""
    paras = [p.strip() for p in item_1_text.split("\n\n") if p.strip()]
    # drop the heading line(s) themselves ("Item 1. Business", "Overview") —
    # html_to_text stitches a heading onto the paragraph that follows it when
    # the heading has no period, so strip a leading heading prefix as well
    heading = re.compile(r"^(?:item\s+1\.?\s*)?(?:business\.?\s*)?(?:overview|general|introduction)?\.?\s*", re.IGNORECASE)
    paras = [heading.sub("", p, count=1).strip() if heading.match(p) else p for p in paras]
    body = [p for p in paras if len(p.split()) > 8]
    out, total = [], 0
    for p in body:
        if total + len(p) > char_limit and out:
            break
        out.append(p)
        total += len(p)
    text = "\n\n".join(out)
    if len(text) > char_limit:
        cut = text[:char_limit]
        ends = [m.end() for m in re.finditer(r"[.!?](?=\s|$)", cut)]
        text = cut[:ends[-1]] if ends else cut
    return text.strip()


# ---------------- AUDIT (run #9): dollar figures from the commitments note; 8-K highlights; dated concentration ----------------

_DOLLAR_RE = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?\s*(?:billion|million|thousand|[BMK]\b)?", re.IGNORECASE)
_OBLIGATION_WORDS = ("maximum", "guarantee", "guaranteed", "liability", "exposure", "commitment",
                     "obligation", "contingen", "indemnif", "lease", "purchase")


def normalize_dollars(text: str) -> str:
    # PURE. Filings print "$ 29 billion" with a space (inline-XBRL); the
    # packet's numbers are "$29 billion" — same figure, one spelling
    return re.sub(r"\$\s+(?=\d)", "$", text or "")


def dollar_sentences(note_text: str, max_items: int = 8) -> list:
    """PURE. Every sentence of a commitments/guarantees note that states a
    dollar amount, most obligation-like first (maximum / guarantee /
    liability words), deduped, each capped at ~320 chars but never cut before
    its first dollar figure. These become separate packet lines so a maximum
    exposure is never a truncated-away tail of a longer quote."""
    text = normalize_dollars(re.sub(r"\s+", " ", note_text or ""))
    if text.startswith("[section unavailable"):
        return []
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z(\"'])", text)
    scored, seen = [], set()
    for s in sentences:
        if not _DOLLAR_RE.search(s):
            continue
        key = re.sub(r"[^a-z0-9]", "", s.lower())[:120]
        if key in seen:
            continue
        seen.add(key)
        low = s.lower()
        score = sum(w in low for w in _OBLIGATION_WORDS) + (2 if "maximum" in low else 0)
        cut = 320
        first = _DOLLAR_RE.search(s)
        if first and first.end() > cut - 20:
            cut = min(len(s), first.end() + 60)   # keep the figure, never the tail without it
        scored.append((score, len(scored), s[:cut].rstrip()))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [s for _, _, s in scored[:max_items]]


_BOILERPLATE_START = re.compile(r"^(about\s+\S+|forward-looking statements|cautionary|non-gaap financial measures|use of non-gaap|"
                                r"conference call|webcast|contact|investor relations|source:)", re.IGNORECASE)
# "EXHIBIT 99.1 Exhibit 99.1 <Company> reports ..." — the exhibit label the
# converter stitches onto the first paragraph of a release, in any casing
_EXHIBIT_LABEL = re.compile(r"^(?:\s*(?:ex(?:hibit)?[-\s]*99\.\d\b|document)[:\s]*)+", re.IGNORECASE)


def strip_exhibit_label(text: str) -> str:
    return _EXHIBIT_LABEL.sub("", text or "", count=1).lstrip()


def extract_highlights(press_text: str, char_limit: int = 1500) -> str:
    """PURE. The head of an earnings release — headline figures, segment and
    product-line revenue lines with their YoY/QoQ changes, and the executive
    quotes — up to the outlook heading or the first boilerplate section. Keeps paragraphs that
    carry a number, a percent, or a quotation; stops at char_limit on a
    sentence boundary."""
    cleaned = strip_exhibit_label(press_text or "")
    paras = [p.strip() for p in cleaned.split("\n\n") if p.strip()]
    out, total = [], 0
    for p in paras:
        head = p[:80].lower()
        if "outlook" in head or "guidance" in head or _BOILERPLATE_START.match(p):
            break
        if not (re.search(r"\d", p) or "“" in p or '"' in p or " said " in p):
            continue
        if total + len(p) > char_limit and out:
            break
        out.append(normalize_dollars(p))
        total += len(p)
    text = "\n\n".join(out)
    if len(text) > char_limit:
        cut = text[:char_limit]
        ends = [m.end() for m in re.finditer(r"[.!?](?=\s|$)", cut)]
        text = cut[:ends[-1]] if ends else cut
    return text.strip()




# ---------------- tables with their context (caption, units, group heading, period columns) ----------------
# WHY a second table reader next to table_rows: html_to_text(keep_tables=True)
# flattens rows into text for the narrative scanners; core extraction needs
# each figure WITH the context a reader has — the table's caption and units,
# the group heading a block of rows sits under ("North Italia operating
# information:"), the row label, and the period heading of every column
# ("13 Weeks Ended June 30, 2026"). Nothing here knows a company or a heading
# only one filer uses.
# run #28 (TSM): "(Unit: NT$ million, except for EPS)" — a 'unit:' lead-in, a
# non-US dollar sign and a singular scale word all occur in foreign releases
# "(in millions)", "Unit: NT$ million" — and "(millions of dollars, unless noted)" (2026-10-11)
_UNITS_RE = re.compile(r"(?i)(?:\b(?:in|\$ in|us\$ in|dollars in|amounts in|units?\s*:|"
                       r"dkk|sek|nok|chf|eur|gbp|usd|jpy|cny|rmb|twd|cad|aud|hkd|inr|krw)|\()\s*(?:(?:us|nt|c|ca|a|hk|s|nz)\s*\$\s*|\$\s*|"
                       r"u\.s\. dollars? |(?:rmb|cny|jpy|eur|gbp|chf|krw|inr|twd|cad|aud|hkd|usd)\s+|[€£¥]\s*)?(thousands?|millions?|billions?)\b")
# the currency a table's caption names; a bare "$" names none (a domestic
# filer's dollars, a foreign filer's are labelled by the packet's currency note)
# currency names in any case ("In Thousands of New Taiwan Dollars", 2026-10-11)
_CURRENCY_HINTS = [("TWD", r"NT\$|(?i:New Taiwan dollars?)|\bTWD\b"), ("CAD", r"\bC\$|\bCA\$|(?i:Canadian dollars?)|\bCAD\b"),
                   ("AUD", r"\bA\$|(?i:Australian dollars?)|\bAUD\b"), ("HKD", r"HK\$|(?i:Hong Kong dollars?)|\bHKD\b"),
                   ("CNY", r"\bRMB\b|(?i:Renminbi)|\bCNY\b"), ("JPY", r"¥|\bJPY\b|(?i:\byen\b)"), ("EUR", r"€|\bEUR\b|(?i:\beuros?\b)"),
                   ("GBP", r"£|\bGBP\b|(?i:pounds? sterling)"), ("KRW", r"₩|\bKRW\b|(?i:Korean won)"), ("INR", r"₹|\bINR\b|(?i:Indian rupees?)"),
                   ("DKK", r"\bDKK\b|(?i:Danish kron)"), ("SEK", r"\bSEK\b|(?i:Swedish kron)"), ("NOK", r"\bNOK\b|(?i:Norwegian kron)"),
                   ("CHF", r"\bCHF\b|(?i:Swiss francs?)"),
                   ("USD", r"US\$|(?i:U\.S\. dollars?)|\bUSD\b")]


def caption_currency(text: str) -> str:
    """PURE. The ISO currency a table caption or header names ('' if none)."""
    for code, pat in _CURRENCY_HINTS:
        if re.search(pat, text or ""):
            return code
    return ""


def _units_word(m) -> str:
    w = (m.group(1) or "").lower() if m else ""
    return w if not w or w.endswith("s") else w + "s"        # "million" -> "millions"
_PAGE_FIELD_RE = re.compile(r"^\s*Field:\s*/", re.IGNORECASE)
_FOOTNOTE_MARK_RE = re.compile(r"\s*\(\d{1,2}\)\s*$|\s*\(\d{1,2}\)(?=\s*:)|\*+$")
_re_pct_of = re.compile(r"(?i)^(?:as a )?(?:percent(?:age)?|%) of (?:total |net |total net )?(?:revenues?|sales|net sales)\b")
_LEAD_FOOTNOTE_RE = re.compile(r"^\s*(?:\(\d{1,2}\)|\[\d{1,2}\]|\*+)\s*")
_RESULT_ROW_RE = re.compile(r"(?i)^(?:gross (?:margin|profit)|operating (?:income|profit|loss)|income (?:\(loss\) )?from operations|"
                            r"(?:income|loss|earnings)(?:/\(loss\))? (?:from operations|before)|net (?:income|earnings|loss))")


def _group_text(text: str) -> str:
    # "(1) Net sales by reportable segment:" -> "Net sales by reportable segment"
    return _FOOTNOTE_MARK_RE.sub("", _LEAD_FOOTNOTE_RE.sub("", text or "")).rstrip(":").strip()


_MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
                "November", "December")
_PERIOD_WORDS_RE = re.compile(r"(?i)\b(?:weeks?|months?|quarter|year|fiscal|ended|ending|as of|q[1-4]\b|fy\s?\d|\d{4})")
# 2026-10-11: and day-month-year ("30 Jun 2026"), as European filers date their columns
_DATE_RE = re.compile(r"(?i)\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}\b|\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?,?\s+\d{4}\b")


def table_caption(table) -> tuple:
    """(caption, units) from the text that precedes a table: up to three
    non-empty strings back (skipping page fields), the last heading-like one
    as caption, units from any of them or from the table's own first cells."""
    caption, units, _ccy = table_caption_ex(table)
    return caption, units


def _text_only_table(table) -> bool:
    """A small table with no figure cell: a title or heading block laid out as a table."""
    cells = table.find_all(["td", "th"], limit=13)
    return len(cells) <= 12 and not any(is_figure_cell(t) for t in (c.get_text(" ", strip=True) for c in cells) if t)


def table_caption_ex(table) -> tuple:
    """(caption, units, currency) — table_caption plus the currency the
    caption names (run #28: 'Unit: NT$ million' -> millions, TWD)."""
    prevs, node = [], table
    for _ in range(20):
        node = node.find_previous(string=re.compile(r"\S"))
        if node is None:
            break
        outer = node.find_parent("table")
        if outer is not None and outer is not table and not _text_only_table(outer):
            break   # the previous table's cells are not this table's caption
        # a text-only table above is the statement's title block (2026-10-11:
        # TSM prints "CONSOLIDATED BALANCE SHEETS" / "(In Thousands of New
        # Taiwan Dollars)" in one) — read it like the paragraphs it stands for
        s = _ZERO_WIDTH.sub("", str(node)).replace("\xa0", " ").strip()
        s = re.sub(r"\s+", " ", s)
        if not s or _PAGE_FIELD_RE.match(s):
            continue
        prevs.append(s)
        if len(prevs) == 6:
            break
    # units come from the caption lines only — never from a row label inside
    # the table ("Units shipped (in thousands)" is that row's unit, not the
    # table's); header rows are checked by parse_tables
    head_text = " ".join(prevs)
    units = ""
    m = _UNITS_RE.search(head_text)
    if m:
        units = _units_word(m)
    currency = caption_currency(" ".join(p for p in prevs if _UNITS_RE.search(p)) or (prevs[0] if prevs else ""))
    caption = ""
    for s in prevs:                      # nearest first
        if _UNITS_RE.search(s) and len(re.sub(r"\([^)]*\)", " ", s).split()) < 2:
            continue                     # a units-only line ("(In US $ millions, except percentages)") is not the caption,
                                         # but "Consolidated balance sheets (in millions)" is (2026-10-11)
        cleaned = _FOOTNOTE_MARK_RE.sub("", s).strip()
        if not cleaned or re.fullmatch(r"[\(\)\d\s,*•·\-–—]+", cleaned) or len(cleaned) < 8 or not re.search(r"[A-Za-z]{3,}\s+[A-Za-z]{2,}", cleaned):
            continue                     # a footnote marker, a bullet, or a stray word is not a caption
        if len(s) <= 120 and not s.endswith("."):
            caption = cleaned
            break
    return caption, units, currency


def _cells_positional(tr) -> list:
    # [[col_start, col_end, text], ...] with colspans honoured, so header rows
    # and value rows of one table line up by column even when iXBRL pads them
    # with spacer cells; "$" / "(" fragments merge into the next cell and
    # ")" / "%" into the previous one
    col, out = 0, []
    for td in tr.find_all(["td", "th"]):
        try:
            span = max(1, int(td.get("colspan", 1) or 1))
        except (TypeError, ValueError):
            span = 1
        text = re.sub(r"\s+", " ", _ZERO_WIDTH.sub("", td.get_text(" ", strip=True)).replace("\xa0", " ")).strip()
        if text:
            out.append([col, col + span, text])
        col += span
    merged = []
    for c0, c1, t in out:
        if merged and t in (")", "%", ")%", "%)"):
            merged[-1][1] = c1; merged[-1][2] += t
        elif merged and merged[-1][2] in ("$", "(", "($", "$("):
            merged[-1][1] = c1; merged[-1][2] += t
        else:
            merged.append([c0, c1, t])
    return [m for m in merged if m[2] not in ("$", "(", ")", "—", "-", "–")]


def _overlap(a, b) -> int:
    return max(0, min(a[1], b[1]) - max(a[0], b[0]))


def _stack_headers(headers: list, cells: list) -> list:
    # a new header row under the current stack: every new cell inherits the
    # text of the stacked cell above it (by column overlap); a stacked cell
    # keeps heading the columns no new cell sits under
    out = []
    for c0, c1, t in cells:
        above = [h for h in headers if _overlap(h, (c0, c1))]
        covered = [h for h in above if c0 <= h[0] and h[1] <= c1]
        if len(covered) > 1:
            # a wide cell over several columns ("(in millions)" across RMB,
            # RMB and US$) adds its text to each, never merges them into one
            # (2026-10-11: every column of a balance sheet read "March 31")
            out.extend([h[0], h[1], h[2] if t.lower() in h[2].lower() else (h[2] + " " + t).strip()] for h in covered)
            continue
        if above:
            top = max(above, key=lambda h: (_overlap(h, (c0, c1)), -(h[1] - h[0])))
            text = t if top[2].lower() in t.lower() else (top[2] + " " + t).strip()
        else:
            text = t
        out.append([c0, c1, text])
    for h in headers:
        # "QUARTERLY TRENDS" over seven columns still heads the five that
        # "2Q26 Change" (over the last two) does not (2026-10-11) — kept as a
        # fallback (the 4th field), used only where no newer heading sits
        if sum(_overlap(h, (c0, c1)) for c0, c1, _ in cells) < h[1] - h[0]:
            out.append(list(h[:3]) + [True])
    return sorted(out, key=lambda x: x[0])


def _heading_for(headers: list, span) -> str:
    # the most specific heading over the column: a heading of the latest
    # header row before a carried-over fallback (a value printed one column
    # right of "June 30, 2026" overlaps the wide "Three Months Ended" more —
    # 2026-10-11), then the most overlap, then the narrowest
    over = [h for h in headers if _overlap(h, span)]
    if not over:
        return ""
    fresh = [h for h in over if not (len(h) > 3 and h[3])] or over
    return max(fresh, key=lambda h: (_overlap(h, span), -(h[1] - h[0])))[2]


_YEAR_CELL_RE = re.compile(r"^(?:19|20)\d\d$")
# a column's period printed alone: "2026", "2Q26", "Q2 2026", "FY2026", "H1 2026"
_PERIOD_CELL_RE = re.compile(r"(?i)^(?:(?:19|20)\d\d|[1-4]Q\s?'?\d{2,4}|Q[1-4]\s?'?\d{2,4}|FY\s?'?\d{2,4}|H[12]\s?'?\d{2,4})$")
_NOTE_ROW_RE = re.compile(r"(?i)^\(\s*(?:un)?audited|^\(\s*reviewed|^\(\s*preliminary|^\(\s*continued|^\(\s*concluded")


def parse_tables(html: str) -> list:
    """Every top-level table -> rows with context:
    {"table": i, "caption", "units", "group", "label",
     "cells": [{"heading", "period", "entity", "value"}], "periods": [column headings],
     "duration": table-level duration heading, "has_period_headings": bool}.
    Header rows (no figure cells) stack into column headings by column
    position; a single-cell duration row ("For the 13 Weeks Ended June 30,
    2026") is the table-level duration; any other single-cell text row is the
    group heading for the rows that follow; each value maps to the heading
    over its own columns, so a row label is never emitted without its
    heading, group and period."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    prev = None    # (headers, duration, caption, units) of the previous table that had column headings
    for ti, table in enumerate(soup.find_all("table")):
        if table.find_parent("table") is not None:
            continue
        caption, units, currency = table_caption_ex(table)
        headers, group, duration = [], "", ""
        seen_figure_row = False
        inherited = False
        for tr in table.find_all("tr"):
            cells = _cells_positional(tr)
            if not cells:
                continue
            numeric = [is_figure_cell(c[2]) for c in cells]
            # a header row of period labels — bare years under "June 30," (a 10-Q
            # that prints the year on its own row), quarter labels ("2Q26 | 1Q26"),
            # beside a units cell, a section title or "Percentage Change Y/Y" — is
            # part of the column headings, not a row of figures (2026-10-11: read
            # as figures, two filers' quarters came out backwards).
            # Attached only when every period cell sits under a column heading:
            # a year row shifted off its columns would swap current and prior
            # (only a row that would otherwise read as figures — one with bare
            # years: a row of "2Q26 | 1Q26 | $ O/(U)" is a header row already)
            periods = [c for c in cells if _PERIOD_CELL_RE.match(c[2])]
            if periods and any(is_figure_cell(c[2]) for c in periods) and \
                    all(_PERIOD_CELL_RE.match(c[2]) or not is_figure_cell(c[2]) for c in cells):
                # under a heading over the figure columns, not the label column's
                # "(millions of dollars)" (a year row shifted left sits there)
                if headers and not all(any(_overlap(h, c) for h in headers if h[0] > 0) for c in periods):
                    continue
                numeric = [False] * len(cells)
            # "(Preliminary)", "(Unaudited)" on a row of its own is a note on
            # the statement — neither a section heading nor a column heading
            if len(cells) == 1 and _NOTE_ROW_RE.match(cells[0][2]):
                continue
            if not units and not seen_figure_row and not any(numeric):
                m = _UNITS_RE.search(" ".join(c[2] for c in cells))
                if m:                          # "(in millions)" printed in the table's own header row
                    units = _units_word(m)
                    currency = currency or caption_currency(" ".join(c[2] for c in cells))
            if len(cells) == 1 and not numeric[0]:
                text = cells[0][2]
                label_col = min([h[0] for h in headers] + [cells[0][0]]) if headers else 0
                # 2026-10-11 (after run #29): a row that starts in the label
                # column is a section heading even when it spans the whole
                # table — Apple's full-width "ASSETS:" was stacked into the
                # column headings and erased both balance-sheet dates
                over_values = bool(headers) and cells[0][0] >= min(h[0] for h in headers)
                if _DURATION_RE.search(text.lower()) and not text.rstrip().endswith(":") and len(text) < 80:
                    duration = text
                elif not text.rstrip().endswith(":") and (cells[0][0] > label_col or (
                        over_values and any(_overlap(h, cells[0]) for h in headers))):
                    # a fragment of a stacked column heading (a brand name split over
                    # three rows above one column): it sits over a value column
                    headers = _stack_headers(headers, cells)
                else:
                    group = _group_text(text)
                continue
            if not any(numeric):
                if all(len(c[2]) < 80 for c in cells):
                    label_cell = cells[0] if cells[0][2].rstrip().endswith(":") else None
                    if label_cell is not None:
                        group = _group_text(label_cell[2])
                        cells = cells[1:]
                    headers = _stack_headers(headers, cells) if cells else headers
                continue
            vals = cells[1:] if not numeric[0] else cells
            if (not seen_figure_row and not headers and not duration and prev is not None and not caption
                    and vals and all(any(_overlap(h, (c0, c1)) for h in prev[0]) for c0, c1, _ in vals)):
                # run #26 (P1): a HEADERLESS table directly after a headed one
                # is its continuation (a filer splits one visual table in two:
                # "Gross margin" amounts, then "Gross margin percentage"); it
                # inherits the column headings, so its percentages keep their
                # current and year-ago periods instead of "unstated"
                headers, duration = [list(h) for h in prev[0]], prev[1]
                caption, units = (prev[2] + " (continued)") if prev[2] else "", units or prev[3]
                currency = currency or (prev[4] if len(prev) > 4 else "")
                inherited = True
            seen_figure_row = True
            label = "" if numeric[0] else cells[0][2]
            values = cells[1:] if label else cells
            label = _FOOTNOTE_MARK_RE.sub("", label).strip() or (f"Total ({group}, unlabeled row)" if group else "(unlabeled row)")
            # run #26 (P1): a "Percentage of total net sales" row describes the
            # row ABOVE it; on its own the label names nothing
            if _re_pct_of.match(label) and out and out[-1]["table"] == ti and not _re_pct_of.match(out[-1]["label"]):
                label = f"{out[-1]['label']} — {label}"
            # "– diluted" under "Earnings per share (EPS) – basic" continues that
            # label's stem (2026-10-11: on its own it read as millions)
            stem = re.match(r"^(.+?)\s+[–—-]\s+\S", out[-1]["label"]) if out and out[-1]["table"] == ti else None
            if stem and re.match(r"^[–—]\s*\w|^-\s+\w", label):
                label = f"{stem.group(1)} {label}"
            # P0 (run #26): a result line (gross margin, operating income, net
            # income...) is never part of the cost or expense section above it
            # — "Operating income [Operating expenses]" was a label the row
            # does not have
            if _RESULT_ROW_RE.match(label) and re.search(r"(?i)cost|expense", group):
                group = ""
            row_cells = []
            for c0, c1, v in values:
                heading = _heading_for(headers, (c0, c1))
                full = (duration + " " + heading).strip() if duration and heading and not _DURATION_RE.search(heading.lower()) else (heading or duration)
                pf = period_from_heading(full) if full else {"period": "", "ended": ""}
                entity = _strip_period_words(heading) if heading else ""
                row_cells.append({"heading": full, "period": pf["period"] if full else "", "entity": entity, "value": v})
            periods = [h[2] for h in headers]
            has_periods = bool(duration) or any(_PERIOD_WORDS_RE.search(h) or _DATE_RE.search(h) for h in periods)
            out.append({"table": ti, "caption": caption, "units": units, "currency": currency, "group": group, "label": label,
                        "cells": row_cells, "periods": periods, "duration": duration, "has_period_headings": has_periods})
            # a section ends at its Total row: the rows after it belong to no
            # group until the next heading
            if group and re.match(r"(?i)^total\b", label):
                group = ""
        if headers and not inherited:
            prev = (headers, duration, caption, units, currency)
    return out


def _strip_period_words(heading: str) -> str:
    # the part of a column heading that names a thing (a brand, a segment,
    # "Amount", "Percent of Revenues") once its duration and date are removed
    t = _DATE_RE.sub("", heading or "")
    t = re.sub(r"(?i)\b(?:for the\s+)?(?:\d{1,2}|three|six|nine|twelve|thirteen|twenty-six|thirty-nine)\s*[- ]?(?:weeks?|months?)\s+(?:ended|ending)\b", "", t)
    t = re.sub(r"(?i)\b(?:year|years|quarter)\s+(?:ended|ending)\b|\bas of\b|\bended\b|\bending\b", "", t)
    # run #28 (TSM): compact quarter labels ("2Q26", "Q3 2026", "3Q FY26") and
    # change columns ("YoY Inc. (Dec.) %") name a period, not a segment
    t = re.sub(r"(?i)\b[1-4]Q\s?(?:FY\s?)?'?(?:\d{2}|\d{4})\b|\bQ[1-4]\s?(?:FY\s?)?'?(?:\d{2}|\d{4})\b", "", t)
    if re.search(r"(?i)\b(?:yoy|qoq|y/y|q/q)\b|inc\.?\s*\(dec\.?\)|\bincrease\s*\(decrease\)|%\s*change", t):
        t = ""
    # an amount is never part of a column's name: a continuation table's
    # headings can carry stray figures ("Six Months Ended June 30 $ (744,405)",
    # TSM's interim cash flows), which split one column pair into two entities
    t = re.sub(r"[$€£¥]?\s*\(?-?[\d][\d,]*(?:\.\d+)?\)?%?|(?<!\w)[$€£¥-](?!\w)", " ", t)
    # an audit status ("June 30, 2026 (unaudited)") names no kind of column:
    # a filer that marks only its latest column never paired it (2026-10-11)
    t = re.sub(r"(?i)\(?\s*\b(?:un)?audited\b\s*\)?|\(?\s*\breviewed\b\s*\)?|\(?\s*\bpreliminary\b\s*\)?", " ", t)
    # a month with no year ("Six Months Ended June 30") names the period too
    t = re.sub(r"(?i)\b(?:january|february|march|april|may|june|july|august|september|october|november|december)\b", " ", t)
    # a trailing footnote marker ("2Q26 Amount a", "Amount (b)", "Amount*") is
    # not part of the column's name — "Amount a" and "Amount" are one kind
    t = re.sub(r"(?:\s+\(?[a-z]\)?|\s*\*+)$", "", re.sub(r"\s+", " ", t).strip(" ,"))
    return re.sub(r"\s+", " ", t).strip(" ,")


_DURATION_RE = re.compile(
    r"(?i)(?P<n>\d{1,2}|three|six|nine|twelve|thirteen|twenty-six|thirty-nine|fifty-two|fifty-three)\s*[- ]?(?P<unit>weeks?|months?)|"
    r"(?P<q>quarter(?:ly)?|q[1-4])|(?P<h>first half|six[- ]month|half[- ]year|h1)|(?P<nm>nine[- ]month|9m)|"
    r"(?P<y>year ended|fiscal year|full year|twelve months|annual|fy\s?\d{2,4}|year ending|years? ended)")
_WORD_NUM = {"three": 3, "six": 6, "nine": 9, "twelve": 12, "thirteen": 13, "twenty-six": 26, "thirty-nine": 39,
             "fifty-two": 52, "fifty-three": 53}


def period_from_heading(heading: str, fallback: str = "") -> dict:
    """PURE. A column heading -> {"period": class, "label": heading, "ended": date}.
    Classes: quarter, YTD (H1), YTD (9M), annual, ttm, forward. A date-only
    heading takes the duration from `fallback` (the table caption or the
    release headline); otherwise it is 'period ended <date>' — never
    'unstated' when the table has period headings."""
    h = (heading or "").strip()
    low = h.lower()
    # run #28 (TSM): change columns ("YoY Inc. (Dec.) %", "QoQ", "% change")
    # are not periods — never a current or year-ago figure
    # "change" anywhere: "2Q26 Change 1Q26", "Percentage Change Y/Y"; and a
    # growth column ("Growth in DKK", "Growth at CER") (2026-10-11)
    if re.search(r"(?i)\b(?:yoy|qoq|y/y|q/q)\b|inc\.?\s*\(dec\.?\)|\bincrease\s*\(decrease\)|%\s*change|\bchange\b|\bgrowth\b|"
                 r"\bo/\(u\)", h):     # "$ O/(U)": over/(under) the period named above it
        return {"period": "change", "label": h, "ended": ""}
    # compact quarter labels: "2Q26", "Q3 2026", "3Q FY26" -> a quarter with a
    # quarter-end date, so the newest column and its year-ago are known
    # "1Q (2026/4-6)": a quarter printed with its calendar months (Japanese filers)
    mq = re.search(r"\b[1-4]Q\s*\((\d{4})/(\d{1,2})-(\d{1,2})\)", h)
    if mq:
        y, m1, m2 = int(mq.group(1)), int(mq.group(2)), int(mq.group(3))
        y += 1 if m2 < m1 else 0
        last = (31, 29 if y % 4 == 0 else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)[m2 - 1]
        return {"period": "quarter", "label": h, "ended": f"{_MONTH_NAMES[m2 - 1]} {last}, {y}"}
    cq = re.search(r"(?i)\b([1-4])Q\s?(?:FY\s?)?'?(\d{2}|\d{4})\b|\bQ([1-4])\s?(?:FY\s?)?'?(\d{2}|\d{4})\b", h)
    if cq:
        qn = int(cq.group(1) or cq.group(3))
        yr = cq.group(2) or cq.group(4)
        yr = int(yr) + 2000 if len(yr) == 2 else int(yr)
        end = ("March 31", "June 30", "September 30", "December 31")[qn - 1]
        return {"period": "quarter", "label": h, "ended": f"{end}, {yr}"}
    ended = _DATE_RE.search(h)
    period = ""
    if re.search(r"(?i)\b(ttm|trailing|last twelve months)\b", low):
        period = "ttm"
    for m in _DURATION_RE.finditer(low):
        if m.group("n"):
            n = _WORD_NUM.get(m.group("n"), None)
            n = int(m.group("n")) if n is None and m.group("n").isdigit() else n
            unit = m.group("unit")
            if unit.startswith("week"):
                period = "quarter" if n and n <= 14 else "YTD (H1)" if n and n <= 27 else "YTD (9M)" if n and n <= 40 else "annual"
            else:
                period = "quarter" if n and n <= 3 else "YTD (H1)" if n and n <= 6 else "YTD (9M)" if n and n <= 9 else "annual"
            break
        if m.group("q"):
            period = "quarter"; break
        if m.group("h"):
            period = "YTD (H1)"; break
        if m.group("nm"):
            period = "YTD (9M)"; break
        if m.group("y"):
            period = "annual"; break
    if not period and fallback:
        period = period_from_heading(fallback)["period"] if not fallback.startswith(("quarter", "YTD", "annual", "ttm")) else fallback
    when = ended.group(0) if ended else ""
    dmy = re.match(r"(\d{1,2})\s+([A-Za-z]+)\.?,?\s+(\d{4})$", when)
    if dmy:
        when = f"{dmy.group(2)} {int(dmy.group(1))}, {dmy.group(3)}"      # one form downstream: "June 30, 2026"
    if not period and ended:
        period = f"period ended {when}"
    return {"period": period or "unstated", "label": h, "ended": when}
