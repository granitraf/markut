"""Which SEC filer a company is, and which of its filings play which role
(run #28, TSM — 2026-10-11).

WHY: the pipeline asked EDGAR for 10-K / 10-Q / Item 2.02 8-K only. A foreign
private issuer files a 20-F once a year and furnishes everything else —
quarterly results, interim financial statements, monthly revenue — on Form
6-K; a Canadian MJDS filer files a 40-F plus 6-Ks. For TSM every slot came back
empty, the empty result carried no reason, and the whole debate ran on market
data alone. The pipeline now works in ROLES:

  annual   — 10-K | 20-F | 40-F (business, risk factors, annual MD&A)
  interim  — 10-Q | a 6-K carrying interim financial statements / MD&A
  earnings — Item 2.02 8-K | a 6-K carrying the quarterly results release

and every document keeps its REAL form, so every label a reader sees names
the filing it came from. A slot that stays empty carries a specific reason.
"""
import re

ANNUAL_FORMS = ("10-K", "20-F", "40-F")
CURRENT_FORMS = ("8-K", "6-K")
PERIODIC_FORMS = ANNUAL_FORMS + ("10-Q",) + CURRENT_FORMS
FILER_LABELS = {"domestic": "US domestic filer (10-K / 10-Q / 8-K)",
                "foreign_private": "foreign private issuer (20-F annual report, 6-K current reports)",
                "canadian_mjds": "Canadian MJDS filer (40-F annual report, 6-K current reports)",
                "no_periodic_reports": "files no annual or quarterly reports with the SEC"}


def base_form(form: str) -> str:
    return (form or "").split("/")[0].strip().upper()


def filer_type(forms: list) -> str:
    """PURE. The filer type from the forms in the filing history, newest
    first: the newest annual report decides (a company that moved from 20-F
    to 10-K is domestic now); without one, 10-Qs mean domestic and 6-Ks mean
    a foreign issuer whose 20-F / 40-F is older than the history shown."""
    for f in forms or []:
        b = base_form(f)
        if b == "10-K":
            return "domestic"
        if b == "20-F":
            return "foreign_private"
        if b == "40-F":
            return "canadian_mjds"
    bases = {base_form(f) for f in forms or []}
    if "10-Q" in bases or "8-K" in bases:
        return "domestic"
    if "6-K" in bases:
        return "foreign_private"
    return "no_periodic_reports"


def role_of(form: str) -> str:
    """PURE. annual | interim | current | other, by form."""
    b = base_form(form)
    if b in ANNUAL_FORMS:
        return "annual"
    if b == "10-Q":
        return "interim"
    if b in CURRENT_FORMS:
        return "current"
    return "other"


_MONTHS = r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
_MONTHLY_RE = re.compile(r"(?i)\b(?:net )?revenues? for " + _MONTHS + r"\b|\brevenue report\b|\bmonthly (?:net )?revenues?\b|"
                         r"\b" + _MONTHS + r" \d{4} revenues?\b")
_QUARTER_RE = re.compile(r"(?i)\b(?:first|second|third|fourth)[- ]quarter\b|\b[1-4]Q\s?\d{2}\b|\bQ[1-4]\b|\bthree months ended\b|\bquarter ended\b")
_RESULTS_RE = re.compile(r"(?i)\b(?:net )?revenues?\b|\bnet (?:income|sales|profit|earnings)\b|\bearnings per (?:share|ADS|ADR)\b|\bgross margin\b|\bEPS\b")
# a results release in any industry's words — a miner leads with margins, free
# cash flow and adjusted earnings (2026-10-11) — counted only under a
# "reports … results" headline, so an operational update is not the release
_RESULTS_WIDE_RE = re.compile(r"(?i)\bmargins?\b|\bfree cash flow\b|\b(?:operating )?cash flows?\b|\badjusted (?:net )?(?:earnings|income)\b|"
                              r"\bEBITDA\b|\boperating (?:income|profit|earnings)\b")
_ANNOUNCE_RE = re.compile(r"(?i)\breports?\b|\bannounced\b|\bresults\b|\btoday\b")
_INTERIM_FS_RE = re.compile(r"(?i)\b(?:condensed |interim )?consolidated (?:interim )?(?:financial statements|balance sheets?)\b|"
                            r"\bindependent auditors?['’]? review report\b|\breview report of independent\b|"
                            r"\bmanagement['’]s discussion and analysis\b|\binterim report\b")
# an interim report names its statements as a heading with the period after
# it, or names them again and again; a release mentions them once in passing
# (2026-10-11: a rating note and a loan refinancing took the interim slot)
_INTERIM_TITLE_RE = re.compile("(?:" + _INTERIM_FS_RE.pattern[4:] + ")" + r".{0,40}?(?:for the (?:three|six|nine|twelve|first|second|third|fourth)\b|"
                               r"\b(?:three|six|nine) months\b|\bquarter ended\b|\bperiod ended\b|\bas (?:of|at)\b)", re.I)
INTERIM_LONG_CHARS = 20000       # an interim report runs to tens of pages; the press releases that mention one do not
_FIGURE_RE = re.compile(r"\d[\d,]*(?:\.\d+)?\s?(?:%|percent|billion|million|thousand)|[$€£¥]\s?\d|NT\$\s?\d|[A-Z]{2}\$\s?\d")


def classify_current_report(text: str, docs: list = None) -> str:
    """PURE. What a 6-K (or an 8-K without item codes) carries:
    monthly_revenue | interim_report | quarterly_results | other.
    Monthly revenue is checked first (its text names a month and revenue);
    an interim report has financial statements or an MD&A and no
    'announced' release; a results release names a quarter, results terms,
    figures and an announcement."""
    t = re.sub(r"\s+", " ", text or "")
    if not _FIGURE_RE.search(t):
        return "other"
    if _MONTHLY_RE.search(t) and not _QUARTER_RE.search(t[:1500]):
        return "monthly_revenue"
    head = t[:6000]
    results_terms = len(_RESULTS_RE.findall(head))
    # a release discusses results at length (TSM's: 24 results terms); a board
    # notice that restates the quarter in one sentence (3 terms) is not the
    # release (run #28 live check: a board-resolutions 6-K was taken for it)
    if re.search(r"(?i)\bresolutions?\b|\bboard of directors meeting\b", head[:2500]) and results_terms < 10:
        return "other"
    headline = re.search(r"(?i)\breports?\b.{0,80}\bresults\b|\bannounced\b.{0,120}\bresults\b|\breports?\b.{0,60}\bquarter\b", head[:2500])
    wide = results_terms + (len(_RESULTS_WIDE_RE.findall(head)) if headline else 0)
    quarterly = bool(_QUARTER_RE.search(head) and (results_terms >= 4 or (wide >= 2 and headline)))
    # the title opens its own document (an exhibit) — a footnote naming "the
    # condensed consolidated balance sheet as at June 30" deep in a release
    # is not a title (2026-10-11); `docs` are the filing's documents
    openings = [re.sub(r"\s+", " ", d or "")[:400] for d in (docs or [t])]
    interim = len(_INTERIM_FS_RE.findall(t)) >= 3 or any(_INTERIM_TITLE_RE.search(o) for o in openings)
    if interim and not (quarterly and re.search(r"(?i)\btoday announced\b|\breports? (?:first|second|third|fourth|q[1-4])", head)):
        return "interim_report"
    # a long document naming its statements once — a whole report in one file
    # behind the SEC cover page — is the interim report unless it is headlined
    # as a results release; the short notes that mention them are not
    if len(t) >= INTERIM_LONG_CHARS and _INTERIM_FS_RE.search(t) and not (quarterly and headline):
        return "interim_report"
    if quarterly and _ANNOUNCE_RE.search(head):
        return "quarterly_results"
    return "other"


def source_phrase(fs: dict) -> str:
    """PURE. How labels name the sources the core figures and guidance scan
    read: 'the latest 8-K exhibits or 10-Q' for a domestic filer, the 6-K
    roles for a foreign issuer — never a form the company does not file."""
    parts = []
    e, i = (fs or {}).get("8-K"), (fs or {}).get("10-Q")
    if e:
        parts.append(f"{e['form']} results release")
    if i and i is not e:
        parts.append(f"{i['form']} interim report" if base_form(i["form"]) == "6-K" else i["form"])
    if parts:
        return "the latest " + " or ".join(parts)
    ft = (fs or {}).get("filer_type", "domestic")
    return "the latest 8-K exhibits or 10-Q" if ft == "domestic" else "the latest 6-K results release or interim report"


def slot_reason(role: str, ftype: str, forms_seen: list) -> str:
    """PURE. Why a role has no document — specific to the filer type."""
    seen = ", ".join(sorted({base_form(f) for f in forms_seen or [] if f}))[:120] or "none"
    if ftype == "no_periodic_reports":
        return f"the company files no annual or quarterly reports with the SEC (forms in its history: {seen})"
    if role == "annual":
        want = {"domestic": "10-K", "foreign_private": "20-F", "canadian_mjds": "40-F"}.get(ftype, "annual report")
        return f"no {want} in the filing history searched"
    if role == "interim":
        if ftype == "domestic":
            return "no 10-Q after the latest 10-K"
        return "no 6-K carrying interim financial statements or MD&A since the latest annual report"
    if role == "earnings":
        if ftype == "domestic":
            return "no Item 2.02 (earnings) 8-K among the recent filings"
        return "no 6-K carrying a quarterly results release since the latest annual report"
    return "not found"
