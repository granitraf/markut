"""Deterministic claim layer (moved verbatim from the notebook claims cell).
The graded parts of output review never depend on a model."""
import re

# ---------------- Output-layer guardrail: claim extraction + tracing ----------------
# WHY pure functions in their own cell: the output layer's core is
# DETERMINISTIC — extract every number the verdict asserts, trace each against
# the evidence packet, detect advice language. Deterministic parts run in the
# offline tests for free; only the one REVISION call (review_node) spends
# model tokens.

_SCALE_FACTORS = {"t": 1e12, "trillion": 1e12, "b": 1e9, "billion": 1e9,
                  "m": 1e6, "million": 1e6, "k": 1e3, "thousand": 1e3}
_FINANCE_NOUNS = ("revenue", "earnings", "eps", "margin", "margins", "growth",
                  "upside", "downside", "shares", "units", "customers")

# Ordered alternation — ranges BEFORE single percents so "30-40%" is captured
# as ONE claim instead of a stray "40%"; dollars carry their scale suffix/word.
# IDENTIFIERS are not claims: the digit inside Q4, FY2027, p25, 10-K, 10-Q,
# 8-K or a date (2026-10-27) is never the start of a number (the lookbehinds),
# and a bare year before a finance noun ("2026 revenue") is a label, not a
# quantity (the lookahead).
_CLAIM_RE = re.compile(
    r"""(?<![A-Za-z0-9.])(?<![0-9]-)(?<![0-9]/)
    (?: \$\s?\d[\d,]*(?:\.\d+)?(?:\s*(?:trillion|billion|million|thousand)\b|\s?[TBMKtbmk]\b)?
      | \d+(?:\.\d+)?\s*(?:-|–|\bto\b)\s*\d+(?:\.\d+)?\s*%
      | \d+(?:\.\d+)?\s*%
      | \d+(?:\.\d+)?\s?x\b
      | (?!(?:19|20)\d\d\s+(?:__NOUNS__)\b)\d[\d,]*(?:\.\d+)?(?:\s+(?:trillion|billion|million|thousand))?\s+(?:__NOUNS__)\b
    )""".replace("__NOUNS__", "|".join(_FINANCE_NOUNS)),
    re.VERBOSE | re.IGNORECASE)


# run #26 (P2, item 14): a bare figure right after a valuation metric's name
# ("trailing P/E of 38.61", "EV/EBITDA 14.2", "PEG ratio of 1.3") has no unit,
# so the main pattern never saw it and the governor never traced it
_METRIC_BARE_RE = re.compile(
    r"(?i)\b(?:P/?E|PE ratio|EV/EBITDA|EV/EBIT|EV/Sales|EV/Revenue|P/S|P/B|P/FCF|PEG(?: ratio)?|"
    r"price[- ]to[- ](?:earnings|book|sales|free cash flow)|multiple|ratio)"
    r"(?:\s*\([^)]{0,40}\))?\s*(?:of|at|is|was|near|around|=|:|stands at)?\s*(?:about\s+|roughly\s+|approximately\s+|~)?"
    # a figure may END its sentence ("…P/E is 38.61."): a following period is
    # fine unless a digit comes after it (2026-10-11 — the old lookahead
    # rejected every sentence-final multiple)
    r"(\d{1,3}(?:\.\d+)?)(?![\d%x]|\.\d|\s?x\b)")


# 2026-10-11: a plain-language verdict writes a multiple in words — "38.61
# times trailing earnings", "14 times forward EBITDA". No "x" and no metric
# name before it, so neither pattern above saw the figure.
_TIMES_RE = re.compile(
    r"(?i)(?<![\d.])(\d{1,3}(?:\.\d+)?)\s+times\s+(?:its\s+|the\s+)?(?:trailing\s+|forward\s+|next[- ]year'?s?\s+|this[- ]year'?s?\s+)?"
    r"(?:twelve[- ]month\s+|TTM\s+)?(?:GAAP\s+|non-GAAP\s+|adjusted\s+)?"
    r"(?:earnings|sales|revenue|book(?:\s+value)?|EBITDA|EBIT|free cash flow|cash flow|EPS)\b")


# run #28 (TSM): a bare decimal is a figure ("against the 22.05 consensus");
# a year, a quarter label or an identifier is not (no decimal point), and a
# decimal already captured with its unit ($2.02, 10.5 billion, 1.5x) is not
# counted twice (the span check below)
_BARE_DECIMAL_RE = re.compile(r"(?<![\w$€£¥.,/:\-])(\d{1,4}\.\d{1,4})(?![\w%.]|\s?(?:x\b|times\b|percent|billion|million|thousand|trillion|bn\b|[BMKT]\b))")


def extract_numeric_claims(text: str) -> list:
    # PURE. Every dollar amount, percent (ranges kept whole), multiple,
    # finance-noun-attached number, bare figure after a valuation metric and
    # bare decimal the verdict asserts, in order of appearance, deduped.
    found, spans = [], []
    for match in _CLAIM_RE.finditer(text or ""):
        found.append((match.start(), re.sub(r"\s+", " ", match.group(0)).strip()))
        spans.append((match.start(), match.end()))
    for pattern in (_METRIC_BARE_RE, _TIMES_RE, _BARE_DECIMAL_RE):
        for match in pattern.finditer(text or ""):
            s, e = match.start(1), match.end(1)
            if any(a < e and s < b for a, b in spans):
                continue                      # already a figure (with its unit)
            found.append((s, match.group(1)))
            spans.append((s, e))
    seen, claims = set(), []
    for _, claim in sorted(found):
        key = claim.lower()
        if key not in seen:
            seen.add(key)
            claims.append(claim)
    return claims


def _canonical_value(num_str: str) -> str:
    # "4740000000000.0000" -> "4740000000000"; "25.2000" -> "25.2"
    value = float(num_str)
    text = f"{value:.10f}".rstrip("0").rstrip(".")
    return text if text else "0"


def normalize_number(raw: str) -> str:
    # PURE. One canonical string per number so "$4.74T" == "4.74 trillion" and
    # "1,234" == "1234". Units are PART of the canonical form: percents keep
    # "%", multiples keep "x" — so 25.2% can never equal a bare 25.2.
    # WHY the ambiguous tag: a bare decimal in (0, 1) could be a fraction OR a
    # sloppy percent (0.52 vs 52%). We refuse to guess — the tag makes it
    # equal only to another bare decimal, never to a percent.
    # Leading +/- is dropped: direction lives in the prose, magnitude is what
    # we trace.
    s = re.sub(r"\s+", " ", str(raw).strip().lower()).replace(",", "").replace("$", "")
    s = s.lstrip("+-").strip()
    if s.endswith("%"):
        return f"{_canonical_value(s[:-1].strip())}%"
    m = re.fullmatch(r"([\d.]+) ?x", s)
    if m:
        # WHY the x is DROPPED (unlike %): a multiple and its bare number are
        # the same magnitude — market_snapshot prints "P/E (forward): 16.16"
        # with no unit while verdicts write "16.16x", and keeping the suffix
        # made that pair untraceable (live defect). Percent stays suffixed
        # because 25.2% and 25.2 are genuinely different quantities.
        return _canonical_value(m.group(1))
    m = re.fullmatch(r"([\d.]+) ?(t|b|m|k|trillion|billion|million|thousand)\b.*", s)
    if m:
        return _canonical_value(str(float(m.group(1)) * _SCALE_FACTORS[m.group(2)]))
    m = re.fullmatch(r"([\d.]+)(?:\s+\w+)*", s)  # bare number, maybe a trailing noun
    if m:
        value = float(m.group(1))
        canon = _canonical_value(m.group(1))
        if 0 < value < 1:
            return f"{canon} [ambiguous fraction-or-percent]"
        return canon
    return s  # unparseable: return normalized text so comparisons stay honest


# Broad by design: EVERY number in the evidence (with its adjacent unit)
# becomes a member of the anchor set. Extra members are harmless — a claim is
# only CITED when its own unit-tagged canonical form is present.
_EVIDENCE_NUM_RE = re.compile(
    r"\$?\s?\d[\d,]*(?:\.\d+)?\s*(?:%|x\b|[tbmk]\b|trillion|billion|million|thousand)?",
    re.IGNORECASE)


# a LABEL line ("- Debt/Equity (mrq, %): 401.00"); a quoted excerpt line is
# not one — its first colon sits inside "[source: …]", which made the whole
# sentence a "label" and every number in it a percent ("2 percentage
# points" -> 2%, 2026-10-11)
_PCT_LABEL_RE = re.compile(r"^\s*-\s+(?!\[Q\d)[^:\"“]*(?:%|percent|margin|rate|ratio|yield|growth)[^:\"“]*:", re.IGNORECASE)
_UNIT_HINT_RE = re.compile(r"(?i)\(?(?:units?:\s*|in\s+)(thousands|millions|billions)\b")


def extract_evidence_numbers(evidence: str) -> set:
    """Every number in the evidence, normalized — plus the forms a verdict may
    legitimately use for it: the percent form of a value whose LABEL carries
    the unit ("Debt/Equity (mrq, %): 401.00" -> "401%"), and the scaled form
    of a value whose line states its units ("$729,476 thousand" -> 729476000;
    "(units: millions) 3,583" -> 3583000000)."""
    out = set()
    for line in (evidence or "").split("\n"):
        pct_label = bool(_PCT_LABEL_RE.match(line))
        hint = _UNIT_HINT_RE.search(line)
        scale = _SCALE_FACTORS[hint.group(1).lower().rstrip("s")] if hint else None
        for m in _EVIDENCE_NUM_RE.finditer(line):
            raw = m.group(0)
            key = normalize_number(raw)
            out.add(key)
            # "50.1 percent" is 50.1% (2026-10-11: releases spell it out);
            # "2 percentage points" is not a percent and stays bare
            if not key.endswith("%") and re.match(r"\s*percent\b(?!age)", line[m.end():], re.IGNORECASE):
                out.add(key + "%")
            if pct_label and not key.endswith("%") and "[ambiguous" not in key and not re.search(r"[tbmk]\b|trillion|billion|million|thousand", raw.lower()):
                out.add(key + "%")
            if scale and re.fullmatch(r"\$?\s?\d{1,3}(?:,\d{3})+(?:\.\d+)?", raw.strip()):
                try:
                    out.add(_canonical_value(str(float(raw.replace("$", "").replace(",", "").strip()) * scale)))
                except ValueError:
                    pass
    return out


_ABSENCE_RE = re.compile(r"(?i)\b(?:does not|doesn't|did not|do not|never) (?:disclose|provide|report|give|publish|break out|quantify)|"
                         r"\b(?:not disclosed|undisclosed|no (?:such )?(?:guidance|disclosure|figure|metric|number)|is absent|absent from|"
                         r"provides no|offers no|gives no|has no (?:guidance|disclosure))\b")


def find_absence_claims(verdict: str, evidence: str) -> list:
    """PURE. Sentences that assert a figure is NOT disclosed while the packet
    records an EXTRACTION FAILURE, a 'KPI value not found' or a 'could not
    find' line — unknown argued as absent. Returns [{"sentence","reason"}]."""
    ev = evidence or ""
    if not re.search(r"EXTRACTION FAILURE|KPI value not found|could not find:", ev):
        return []
    out = []
    for sentence in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9$(])", verdict or ""):
        if _ABSENCE_RE.search(sentence) and "not retrieved" not in sentence.lower() and "UNKNOWN, NOT ABSENT" not in sentence:
            out.append({"sentence": sentence.strip(),
                        "reason": "the packet records an extraction failure or a not-found line, so the figure may exist but was not retrieved"})
    return out


_RANGE_RE = re.compile(r"^\s*[+-]?([\d.,]+)\s*(?:-|–|to)\s*([\d.,]+)\s*(%|x)?\s*$",
                       re.IGNORECASE)


def trace_claim(claim: str, evidence: str) -> str:
    # PURE. CITED iff the normalized number — or BOTH endpoints of a range —
    # appears among the evidence's normalized numbers. Everything else is
    # FLAGGED, including ranges that are DERIVABLE from cited anchors.
    # WHY so strict: derivation detection by code is a rabbit hole (which
    # anchors? what arithmetic? rounding?). The revision step makes the JUDGE
    # prove each derivation and restate it with anchors inline — the code only
    # has to be certain about literal presence.
    anchors = extract_evidence_numbers(evidence)
    m = _RANGE_RE.match(claim.strip())
    if m:
        low, high, unit = m.groups()
        unit = unit or ""
        return ("CITED"
                if normalize_number(low + unit) in anchors
                and normalize_number(high + unit) in anchors
                else "FLAGGED")
    return "CITED" if normalize_number(claim) in anchors else "FLAGGED"


# Named constant so new advice phrasings can be appended without touching code.
ADVICE_PHRASES = [
    "investors should", "you should", "position siz", "prudent approach",
    "recommend buying", "recommend selling", "buy the stock", "sell the stock",
    "add to your", "trim your",
]


def find_advice_language(text: str) -> list:
    # WHY sentence-aware: the mandated disclaimer block itself says things
    # like "Investors should conduct their own due diligence and consult a
    # qualified financial professional" — that is compliance boilerplate, not
    # advice, and counting it made every compliant verdict score advice=1.
    # The disclaimer block runs from its first recognized sentence to the END
    # of the text (both the prompts and the deterministic stapler put it
    # last), so everything BEFORE it is still fully screened — advice ahead
    # of the disclaimer is real advice and the screen must not weaken.
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9$(])", text or "")
    scanned = []
    in_disclaimer = False
    for sentence in sentences:
        if has_disclaimer(sentence):
            in_disclaimer = True
        if not in_disclaimer:
            scanned.append(sentence)
    lowered = " ".join(scanned).lower()
    return [phrase for phrase in ADVICE_PHRASES if phrase in lowered]


# The judge's own prompts end verdicts with "investment RESEARCH, not
# financial advice" — the variants below match that convention and the
# obvious rewordings.
# WHY several variants: the revision model words its own disclaimer freely
# ("does not constitute investment advice", "for research purposes only");
# the stapler must RECOGNIZE those, not append a second disclaimer on top —
# substring match, case-insensitive via has_disclaimer's lower().
DISCLAIMER_PATTERNS = [
    "not financial advice", "not investment advice",
    "does not constitute investment advice",
    "does not constitute financial advice",
    "for research purposes only",
    "research, not financial", "educational purposes",
]
DISCLAIMER_TEXT = ("\n\nThis is investment research for educational purposes, "
                   "not financial advice.")


def has_disclaimer(text: str) -> bool:
    lowered = (text or "").lower()
    return any(pattern in lowered for pattern in DISCLAIMER_PATTERNS)


# ---------------- AUDIT PASS (run #2): label binding, arithmetic, scenarios ----------------
# WHY these exist: number PRESENCE is not number TRUTH. "revenue grew 85.5% in
# FY2025" passed the tracer because 85.5% sits in the packet — as the most-
# recent-QUARTER growth rate. And "46.28x on $8.12 EPS implies material
# downside" passed because both numbers exist, although 46.28 × 8.12 is the
# current price. Everything below is deterministic and runs offline.

# Period vocabulary. Two axes that the first version conflated: the measurement
# WINDOW (quarter / TTM / fiscal year) and the ORIENTATION (forward estimate).
# A consensus estimate FOR FY2027 is both "forward" and "annual" — not a
# conflict — while a quarterly rate presented as a fiscal-year rate is.
PERIOD_MARKERS = [
    ("quarter", r"\b(mrq|most recent quarter|quarter(?:ly)?|q[1-4](?:\s?fy)?\s?\d{0,4}|three months|fiscal quarter|1[2-4][- ]weeks?|thirteen weeks|twelve weeks)\b"),
    # partial years are their own classes: a six-month or nine-month figure is never annual
    ("ytd_h1",  r"\b(six months|six-month|2[67][- ]weeks?|twenty-six weeks|first half|h1|half[- ]year|year[- ]to[- ]date|ytd(?: \(h1\))?|first six months)\b"),
    ("ytd_9m",  r"\b(nine months|nine-month|39[- ]weeks?|thirty-nine weeks|first nine months|three fiscal quarters|9m|ytd \(9m\))\b"),
    ("ttm",     r"\b(ttm|trailing(?:[- ]twelve[- ]months)?|last twelve months)\b"),
    ("annual",  r"\b(fy\s?\d{2,4}|fiscal (?:year|20\d\d)|annual(?:ly|ized)?|full[- ]year|year ended|for the year|current[- ]fy|next[- ]fy|52[- ]weeks?|53[- ]weeks?)\b"),
    ("forward", r"\b(forward|consensus|estimate[sd]?|expected|guidance|guide|outlook|next (?:12 months|year)|fy\s?\d{4}e)\b"),
]
PERIOD_LABELS = {"quarter": "quarter", "ytd_h1": "YTD (H1)", "ytd_9m": "YTD (9M)", "ttm": "TTM", "annual": "annual", "forward": "forward"}
# pairs that CANNOT describe the same number; everything else is compatible
_CONFLICTS = {frozenset(p) for p in (("quarter", "ttm"), ("quarter", "annual"), ("quarter", "forward"),
                                     ("ttm", "annual"), ("ttm", "forward"),
                                     ("ytd_h1", "quarter"), ("ytd_h1", "ttm"), ("ytd_h1", "annual"), ("ytd_h1", "ytd_9m"),
                                     ("ytd_9m", "quarter"), ("ytd_9m", "ttm"), ("ytd_9m", "annual"))}
PERIOD_CLASSES = PERIOD_MARKERS   # name kept for callers of the first version


def period_markers(text: str) -> list:
    # PURE. Every period marker in text with its class and position.
    low = (text or "").lower()
    found = []
    for name, pat in PERIOD_MARKERS:
        for m in re.finditer(pat, low):
            found.append((name, m.start(), m.end()))
    return sorted(found, key=lambda t: t[1])


def classify_period(text: str) -> str:
    # first marker by position (labels are short; for claims use nearest_period)
    found = period_markers(text)
    return found[0][0] if found else ""


def label_periods(label: str) -> set:
    return {name for name, _, _ in period_markers(label)}


def nearest_period(clause: str, start: int, end: int) -> str:
    # PURE. The period marker that describes the claim inside ITS clause.
    # English puts the modifier BEFORE the figure ("TTM gross margin of
    # 42.94%", "FY2025 net income growth of +292.3%"), so the nearest marker
    # before the number wins; only when nothing precedes it does the nearest
    # marker after count ("85.5% ... in FY2025", "75.52% TTM gross margin").
    before, after = None, None
    for name, a, b in period_markers(clause):
        if b <= start:
            d = start - b
            if before is None or d < before[0]:
                before = (d, name)
        elif a >= end:
            d = a - end
            if after is None or d < after[0]:
                after = (d, name)
        else:
            return name  # marker overlaps the span itself
    # "46.37x on trailing TTM GAAP EPS", "$8.12 of trailing EPS": a marker
    # joined to the figure by on/of/for/at/in names ITS basis, whatever
    # preceded the figure
    tail = clause[end:]
    if after and re.match(r"\s+(?:on|of|for|at|in|over)\s+", tail) and after[0] <= 40:
        return after[1]
    # "in the first six months of fiscal 2026": the partial-year marker names
    # the period; "fiscal 2026" only names the year it belongs to
    found = [name for name, _, _ in period_markers(clause)]
    for ytd in ("ytd_h1", "ytd_9m"):
        if ytd in found and (before or after) and {before[1] if before else None, after[1] if after else None} <= {"annual", ytd, None}:
            return ytd
    if before:
        return before[1]
    return after[1] if after else ""


def classify_basis(text: str) -> str:
    low = (text or "").lower()
    if re.search(r"\bnon[- ]gaap\b", low):
        return "non-gaap"
    if re.search(r"\bgaap\b", low):
        return "gaap"
    return ""


def compatible(claim_period: str, label_periods_: set) -> bool:
    # a claim period is fine if it does not conflict with at least one of the
    # label's periods (a label can carry two, e.g. "FY2027 consensus")
    if not claim_period or not label_periods_:
        return True
    return any(frozenset((claim_period, lp)) not in _CONFLICTS for lp in label_periods_)


def packet_labels(evidence: str) -> dict:
    # PURE. Map each normalized number in the packet's labeled lines
    # ("- Label (period): value  [source: ...]") to the labels it appears under.
    # Only labeled market lines bind a period; quoted filing prose does not,
    # because a sentence can carry several periods at once.
    labels = {}
    for line in (evidence or "").split("\n"):
        if re.match(r"^\s*-\s+(?:\[|\")", line):
            continue   # a tagged filing excerpt or a quote, not a labeled figure
        m = re.match(r"^\s*-\s+([^:]{3,120}):\s+(.+?)\s*(?:\[source:[^\]]*\])?\s*$", line)
        if not m:
            continue
        label, value = m.group(1), m.group(2)
        for num in _EVIDENCE_NUM_RE.finditer(value):
            key = norm_or_none(num.group(0))
            if key and key not in ("0",):
                labels.setdefault(key, []).append(label)
    return labels


_CLAUSE_SPLIT = re.compile(r"[;:()\"\[\]]|,\s|\s[—–-]\s|\s(?:and|or|versus|vs\.?|while|whereas|but|against|compared (?:with|to))\s",
                           re.IGNORECASE)


def claim_contexts(text: str) -> list:
    # PURE. Each numeric claim with the CLAUSE it sits in (bounded by ; : , ( )
    # quotes, brackets and dashes) so period words from a neighbouring clause —
    # or the next JSON field — can never be read as describing it.
    out = []
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9$(])", text or "")
    for sentence in sentences:
        bounds = [0] + [m.end() for m in _CLAUSE_SPLIT.finditer(sentence)] + [len(sentence)]
        for m in _CLAIM_RE.finditer(sentence):
            claim = re.sub(r"\s+", " ", m.group(0)).strip()
            lo = max(b for b in bounds if b <= m.start())
            hi = min(b for b in bounds if b >= m.end()) if any(b >= m.end() for b in bounds) else len(sentence)
            clause = sentence[lo:hi]
            out.append({"claim": claim, "context": clause, "sentence": sentence,
                        "period": nearest_period(clause, m.start() - lo, m.end() - lo)})
    return out


def find_mislabeled(verdict: str, evidence: str) -> list:
    """PURE. Numbers that trace to the packet but under an INCOMPATIBLE period
    or accounting basis. Returns [{"claim","packet_label","packet_period",
    "claim_period","reason","sentence"}]."""
    labels = packet_labels(evidence)
    findings, seen = [], set()
    for item in claim_contexts(verdict):
        claim = item["claim"]
        key = norm_or_none(claim)
        if key is None or key not in labels or claim.lower() in seen:
            continue
        claim_period = item["period"]
        claim_basis = classify_basis(item["context"])
        reason = None
        # period: the claim must be compatible with at least ONE label it could come from
        if claim_period and any(label_periods(l) for l in labels[key]) \
                and not any(compatible(claim_period, label_periods(l)) for l in labels[key]):
            packet_period = "/".join(PERIOD_LABELS.get(p, p) for p in sorted(label_periods(labels[key][0])))
            reason = f"{claim} is {packet_period} in the evidence ({labels[key][0]}), not {PERIOD_LABELS.get(claim_period, claim_period)}"
        else:
            bases = {classify_basis(l) for l in labels[key]} - {""}
            if claim_basis and bases and claim_basis not in bases:
                reason = f"{claim} is {sorted(bases)[0]} in the evidence ({labels[key][0]}), not {claim_basis}"
        if reason:
            seen.add(claim.lower())
            findings.append({"claim": claim, "packet_label": labels[key][0],
                             "packet_period": "/".join(sorted(label_periods(labels[key][0]))),
                             "claim_period": claim_period, "reason": reason, "sentence": item["sentence"]})
    return findings


def norm_or_none(raw: str):
    # normalize_number raises on ranges ("30-40%"); the audit-pass helpers
    # treat an un-normalizable claim as "no single value" rather than crash
    try:
        return normalize_number(raw)
    except (ValueError, TypeError):
        return None


def _as_float(raw: str):
    # numeric magnitude of a claim/anchor string: "$531.31" -> 531.31, "41.3%" -> 41.3, "46.28x" -> 46.28
    s = norm_or_none(raw)
    if s is None:
        return None
    s = s.replace("%", "").replace(" [ambiguous fraction-or-percent]", "")
    try:
        return float(s)
    except ValueError:
        return None


def recompute_derived(claim: str, anchors: list) -> dict:
    """PURE. Can `claim` be produced from `anchors` by one arithmetic step?
    Tries ratio-minus-one (as %), difference-over-base (as %), plain ratio
    (multiple), difference and product over every ordered pair. A percent claim
    matches within 0.15 percentage points (rounding/truncation slack — 41.3% vs
    the exact 41.38% passes, 41.0% does not); anything else within 0.5%.
    Returns {"ok", "implied", "formula"}; implied is the exact value of the
    best formula so a MISCOMPUTED tag can state the right number."""
    values = [(a, _as_float(a)) for a in anchors or []]
    values = [(a, v) for a, v in values if v is not None]
    if not values:
        return {"ok": False, "implied": None, "formula": ""}
    # a RANGE derives when BOTH endpoints derive (20-50% from $244.85/$195.55
    # and $301.62/$195.55); the implied value then lists both endpoints
    rm = _RANGE_RE.match(str(claim).strip())
    if rm:
        low, high, unit = rm.groups()
        unit = unit or ""
        lo, hi = recompute_derived(low + unit, anchors), recompute_derived(high + unit, anchors)
        implied = (f"{lo['implied']:g}{unit}–{hi['implied']:g}{unit}"
                   if lo["implied"] is not None and hi["implied"] is not None else None)
        # WHY a range is NOT refereed strictly: "20-50% upside" over anchors that
        # imply 25.2% and 54.2% is a rounded judgment span, which the notebook's
        # labeling regressions accept as DERIVED. Code can referee one number;
        # it cannot referee how wide a span someone chose. The implied endpoints
        # are still recorded so a reader can compare.
        return {"ok": True, "implied": implied, "formula": f"{lo['formula']}; {hi['formula']}"}
    target = _as_float(claim)
    if target is None:
        # not a quantity code can referee (words, dates): anchors tracing is
        # all we can check — do not block, do not call it miscomputed
        return {"ok": True, "implied": None, "formula": "unverifiable"}
    is_pct = str(claim).strip().endswith("%")
    best = None
    for (na, a) in values:
        for (nb, b) in values:
            if na == nb:
                continue
            cands = []
            if b:
                cands.append((abs((a / b - 1) * 100), f"({na} / {nb} - 1)", True))
                cands.append((abs(a / b), f"{na} / {nb}", False))
            cands.append((abs(a - b), f"{na} - {nb}", False))
            if str(na).strip().endswith("%") and not str(nb).strip().endswith("%"):
                # a percentage minus percentage points is a percentage (2026-10-11,
                # run #27: 50.1% gross margin less ~2 points of refunds = 48.1%)
                cands.append((a - b, f"{na} - {nb} points", True))
            cands.append((a * b, f"{na} × {nb}", False))
            for value, formula, pct_formula in cands:
                if is_pct != pct_formula:
                    continue
                err = abs(value - target)
                if best is None or err < best[0]:
                    best = (err, value, formula)
    if best is None:
        return {"ok": False, "implied": None, "formula": ""}
    err, value, formula = best
    # tolerance follows the PRECISION the claim was written at: "41%" means
    # 41 ± 0.5, "41.3%" means ± 0.15 (rounding/truncation slack), a multiple
    # or price ± 0.5% — so a rounded label is not "wrong arithmetic", while
    # 44% against an implied 41.38% is
    decimals = len(str(claim).split(".")[1].rstrip("%x ").strip()) if "." in str(claim) else 0
    precision = 0.5 * 10 ** (-decimals)
    tol = max(0.15, precision) if is_pct else max(0.005 * abs(target), precision, 0.01)
    return {"ok": err <= tol, "implied": round(value, 2), "formula": formula}


_PRICE_LINE_RE = re.compile(r"Price \(current\):\s*\$([\d,]+(?:\.\d+)?)")
_SCENARIO_RE = re.compile(
    r"(?P<mult>\d+(?:\.\d+)?)\s?x\b[^.;]{0,80}?\$(?P<eps>\d+(?:\.\d+)?)\s*(?:of\s+)?(?:trailing|forward|gaap|non-gaap|\w+\s)?\s*eps"
    r"|\$(?P<eps2>\d+(?:\.\d+)?)\s*(?:trailing|forward)?\s*eps[^.;]{0,80}?(?P<mult2>\d+(?:\.\d+)?)\s?x\b",
    re.IGNORECASE)
_DOWN_WORDS = re.compile(r"\b(downside|decline|fall|drop|compress\w*|revert\w* (?:lower|down)|de-?rat\w*|correction|material downside)\b", re.IGNORECASE)
_UP_WORDS = re.compile(r"\b(upside|rise|rally|re-?rat\w* (?:higher|up)|appreciat\w*|gain)\b", re.IGNORECASE)


def check_scenarios(verdict: str, evidence: str) -> list:
    """PURE. Find 'multiple × EPS' scenario statements, recompute the implied
    price against the packet's current price, and flag ones whose stated
    direction the arithmetic does not support (46.28x × $8.12 = $375.79, the
    current price, is not 'material downside'). Returns
    [{"sentence","multiple","eps","implied","price","direction","reason"}]."""
    m = _PRICE_LINE_RE.search(evidence or "")
    if not m:
        return []
    price = float(m.group(1).replace(",", ""))
    findings = []
    for sentence in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9$(])", verdict or ""):
        # WHY per clause: a verdict packs "in a bull scenario ... upside; in a
        # bear scenario ... downside" into ONE sentence, so direction must be
        # read from the clause that holds the arithmetic, not the whole sentence
        for clause in re.split(r";\s*", sentence):
            for sm in _SCENARIO_RE.finditer(clause):
                mult = float(sm.group("mult") or sm.group("mult2"))
                eps = float(sm.group("eps") or sm.group("eps2"))
                implied = mult * eps
                gap = (implied / price - 1) * 100
                down, up = bool(_DOWN_WORDS.search(clause)), bool(_UP_WORDS.search(clause))
                direction = "downside" if down and not up else "upside" if up and not down else ""
                # the stated direction contradicts the arithmetic when the implied
                # price sits within 2% of today's price, or on the other side of it
                contradicts = (direction == "downside" and gap > -2.0) or (direction == "upside" and gap < 2.0)
                if direction and contradicts:
                    findings.append({
                        "sentence": sentence, "clause": clause.strip(), "multiple": mult, "eps": eps,
                        "implied": round(implied, 2), "price": price, "direction": direction,
                        "reason": (f"{mult:g}x × ${eps:g} = ${implied:,.2f}, {gap:+.1f}% vs the current price "
                                   f"${price:,.2f}; the sentence claims {direction}"),
                    })
    return findings


def trim_to_sentence(text: str) -> str:
    # PURE. Cut a truncated reply back to its last complete sentence so a
    # half-sentence never reaches the judge. Falls back to the last complete
    # line, then to the text itself.
    t = (text or "").rstrip()
    ends = [m.end() for m in re.finditer(r"[.!?](?=[\"')\]]*\s|[\"')\]]*$)", t)]
    if ends and ends[-1] >= len(t) * 0.4:
        return t[:ends[-1]].rstrip()
    if "\n" in t:
        return t.rsplit("\n", 1)[0].rstrip()
    return t


# ---------------- readable verdict (2026-10-11): sources beneath, not inline ----------------
# WHY: the governor used to restate every derived figure's anchors inside the
# sentence ("…48.1% (anchors: 50.1% company gross margin, …)"), and run #27's
# verdict grew from 6 figures to 20 — accurate, but it read as fact after
# fact. Traceability now lives in a source list built here, in code, from the
# same trace the governor already runs; the prose keeps only its own figures.
_ANCHOR_OPEN_RE = re.compile(r"\s*\((?:anchors?|derived from|DERIVED from|DERIVED)\s*:?", re.IGNORECASE)


def strip_inline_anchors(text: str) -> tuple:
    """PURE. -> (text without '(anchors: …)' blocks, [the blocks' contents]).
    Bracket-balanced, so a block holding its own parentheses ('= $279.75
    (-16.9%)') leaves whole; the sentence around it is left intact."""
    out, moved, i, s = [], [], 0, text or ""
    while True:
        m = _ANCHOR_OPEN_RE.search(s, i)
        if not m:
            out.append(s[i:])
            break
        depth, j = 1, m.end()
        while j < len(s) and depth:
            depth += {"(": 1, ")": -1}.get(s[j], 0)
            j += 1
        if depth:                       # unbalanced: leave the text as written
            out.append(s[i:])
            break
        out.append(s[i:m.start()])
        moved.append(s[m.end():j - 1].strip())
        i = j
    return "".join(out), moved


# packet sections whose text is written by a model or describes the packet
# itself: a figure there is not a source for anything
_NOT_SOURCES = ("PROFILE", "KEY QUESTIONS", "WHAT WOULD MISLEAD", "COVERAGE GAPS", "DATA GAPS")
_NOT_DATA_LINE = re.compile(r"could not find:|KPI value not found|^-?\s*COVERAGE GAP|\(limitation:", re.IGNORECASE)


def tracing_evidence(packet: str) -> str:
    """PURE. The packet as the governor may trace against it — DATA only
    (run #28, TSM): the profile, the planner's questions, 'could not find'
    lines that repeat them, the section headers that carry a question's text,
    the coverage / data-gap notes and the 'what would mislead' list are
    written by a model or describe the packet; a figure there is not evidence."""
    out, section = [], ""
    for raw in (packet or "").splitlines():
        line = raw.strip()
        # a header may carry text after a dash ("[FILINGS ADDENDUM — passages …]",
        # appended after [DATA GAPS] by the claim review — data, not a gap note)
        m = re.match(r"^\[([A-Z][A-Z0-9 &/]+?)(?:\s+Q\d)?(?:\s+—[^\]]*)?\](.*)$", line)
        if m:
            section = m.group(1)
            out.append(line[:m.start(2)])          # keep the header, drop a question's text after it
            continue
        if section in _NOT_SOURCES or _NOT_DATA_LINE.search(line):
            continue
        out.append(raw)
    return "\n".join(out)


def _source_lines(evidence: str) -> list:
    """[(line, numbers, rank)] for every packet line that can be a source;
    rank 0 = a sourced fact line, 1 = other evidence, 2 = news (untrusted)."""
    out, section = [], ""
    for raw in (evidence or "").splitlines():
        line = raw.strip()
        m = re.match(r"^\[([A-Z][A-Z0-9 &/]+?)(?:\s+Q\d)?(?:\s+—[^\]]*)?\]", line)
        if m:
            section = m.group(1)
            continue
        if not line or section in _NOT_SOURCES or _NOT_DATA_LINE.search(line):
            continue
        # question tags ("[Q2]") and source tags ("[source: T4]") are labels,
        # not figures: a bare anchor like "2" must not match them
        nums = extract_evidence_numbers(re.sub(r"\[(?:Q\d+|source:[^\]]*)\]", " ", line))
        if nums:
            rank = 2 if section == "NEWS" else (0 if "[source:" in line else 1)
            out.append((line, nums, rank))
    return out


def _short(line: str, limit: int = 220) -> str:
    line = re.sub(r"^-\s+", "", line)
    tag = re.search(r"\s*(\[source:[^\]]*\])\s*$", line)
    body = line[:tag.start()] if tag else line
    if len(body) > limit:
        body = body[:limit].rsplit(" ", 1)[0] + " …"
    return body + (f"  {tag.group(1)}" if tag else "")


def figure_sources(verdict: str, evidence: str, resolutions=None, moved_anchors=None, rounded=None) -> list:
    """PURE. One entry per figure in the verdict: {"figure", "kind", "line"}.
    kind: cited (the packet line it traces to, preferring a sourced fact
    line), derived (computed from anchors the governor verified), judgment
    (labeled as qualitative), untraced (no anchor — listed, never hidden)."""
    lines = _source_lines(evidence)
    def find(fig, must=None):
        try:
            key = normalize_number(fig)
        except Exception:
            return None
        m = _RANGE_RE.match(fig.strip())
        keys = [normalize_number(m.group(1) + (m.group(3) or "")), normalize_number(m.group(2) + (m.group(3) or ""))] if m else [key]
        hits = [(rank, i, line) for i, (line, nums, rank) in enumerate(lines)
                if all(k in nums for k in keys) and (must is None or must.search(line))]
        return _short(min(hits)[2]) if hits else None
    # a unitless anchor in a percent derivation is a count of percentage
    # points: look for it only where the packet speaks of points — a bare "2"
    # otherwise lands on any line holding a 2 ("$2.02", a table number)
    points = re.compile(r"(?i)\bpoints?\b|\bpts\b|basis points")
    def find_anchor(anchor, claim):
        a = str(anchor).strip()
        unitless = not re.search(r"[%$x]|[A-Za-z]", re.sub(r"(?i)percentage points?|points?|pts", "", a))
        if str(claim).strip().endswith("%") and unitless:
            return find(a, points)
        return find(a)
    by_claim = {}
    for r in resolutions or []:
        for c in extract_numeric_claims(str(r.get("claim", ""))) or [str(r.get("claim", "")).strip()]:
            by_claim[c] = r
    out, seen = [], set()
    for fig in extract_numeric_claims(verdict or ""):
        if fig in seen:
            continue
        seen.add(fig)
        line = find(fig) if trace_claim(fig, evidence or "") == "CITED" else None
        res = by_claim.get(fig)
        src_round = (rounded or {}).get(fig)
        if not line and src_round:
            base = find(src_round)
            line = f"{base} — rounded from {src_round}" if base else f"rounded from {src_round}"
        if line:
            out.append({"figure": fig, "kind": "cited", "line": line})
        elif res and res.get("resolution") == "DERIVED" and res.get("qualified", True):
            anchors = [str(a) for a in res.get("anchors") or []]
            # the one line that holds EVERY anchor is the derivation's source
            # (run #27: "gross margin was 50.1 percent, including … approximately
            # 2 percentage points"); only then fall back to one line per anchor
            keys = []
            for a in anchors:
                try:
                    keys.append(normalize_number(a))
                except Exception:
                    keys = []
                    break
            joint = [(rank, i, line) for i, (line, nums, rank) in enumerate(lines) if keys and all(k in nums for k in keys)]
            if joint:
                text = f"computed from {' and '.join(anchors)} ({_short(min(joint)[2])})"
            else:
                text = "computed from " + "; ".join(f"{a} ({find_anchor(a, fig) or 'in the packet'})" for a in anchors)
            out.append({"figure": fig, "kind": "derived", "line": text})
        elif res and res.get("resolution") == "LABELED":
            out.append({"figure": fig, "kind": "judgment", "line": "the judge's estimate, labeled as qualitative judgment"})
        else:
            hint = next((a for a in moved_anchors or [] if fig in a), "")
            out.append({"figure": fig, "kind": "untraced",
                        "line": "no line in the evidence packet holds this figure" + (f"; the governor's note: {hint}" if hint else "")})
    return out



# ---------------- P1 (run #28 / #27 audit): honest rounding traces ----------------
# WHY: "38.6x trailing P/E" for 38.61 and "64% gross margin" for 64.23% are
# fair roundings, but the literal tracer flagged them (run #27's 38.6x) or —
# before P0 — matched them against the planner's own text. A rounding counts
# only when (a) the data value rounds to the claim at the CLAIM's precision,
# with a matching unit, and (b) the data line's label shares a word with the
# words around the claim — so "revenue rose 64%" never borrows the 64.23%
# gross margin.
_ROUND_STOP = {"the", "and", "for", "with", "from", "this", "that", "its", "are", "was", "were", "has", "have", "year", "years",
               "quarter", "quarters", "annual", "total", "net", "per", "share", "rate", "about", "near", "roughly", "around",
               "over", "under", "into", "than", "more", "less", "after", "before", "while", "which", "source", "computed",
               "data", "provider", "field", "yfinance", "info"}


def _round_words(text: str) -> set:
    t = (text or "").lower().replace("p/e", " pe ").replace("ev/", " ev ")
    # a plural matches its label ("margins" / "Profit margin"): drop a final s
    return {(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w)
            for w in re.findall(r"[a-z][a-z]{1,}", t) if w not in _ROUND_STOP and len(w) >= 2}


def rounded_source(claim: str, sentence: str, evidence: str):
    """PURE. The data value a claim honestly rounds, or None. sentence is the
    claim's own sentence (its nearby words are matched to the line label)."""
    c = (claim or "").strip()
    m = re.match(r"^([+-]?)\$?\s?(\d[\d,]*(?:\.(\d+))?)\s*(%|x)?$", c)
    if not m:
        return None
    sign, body, frac, unit = m.group(1), m.group(2), m.group(3) or "", m.group(4) or ""
    dollars = "$" in c
    target = float(body.replace(",", "")) * (-1 if sign == "-" else 1)
    decimals = len(frac)
    i = (sentence or "").find(c.lstrip("+"))
    ctx = _round_words((sentence or "")[max(0, i - 50): i + len(c) + 25] if i >= 0 else sentence)
    for raw in tracing_evidence(evidence).splitlines():
        line = raw.strip()
        if not line.startswith("- ") or ":" not in line:
            continue
        label = line[2:line.index(":")]
        if not (ctx & _round_words(label)):
            continue
        multiple_label = re.search(r"(?i)p/e|ev/|multiple|ratio|x\b", label)
        for tok in re.finditer(r"([+-]?)(\$?)\s?(\d[\d,]*\.(\d+))\s*(%|x)?", line[line.index(":"):]):
            t_sign, t_dollar, t_body, t_frac, t_unit = tok.groups()
            t_unit = t_unit or ""
            if len(t_frac) <= decimals:
                continue                                   # not a more precise value — not a rounding
            if unit == "%" and t_unit != "%":
                continue
            if unit == "x" and not (t_unit == "x" or (t_unit == "" and multiple_label)):
                continue
            if unit == "" and t_unit:
                continue
            if dollars != bool(t_dollar):
                continue
            value = float(t_body.replace(",", "")) * (-1 if t_sign == "-" else 1)
            if abs(round(value, decimals) - target) < 1e-9:
                return f"{'-' if t_sign == '-' else ''}{t_dollar}{t_body}{t_unit}"
    return None


# ---------------- P1: no meta-text in the verdict ----------------
# WHY: told that sources are listed by code, the judge wrote "(the page lists
# the exact figure and its source)" in place of the figure (run #28). A
# reference to the page or a source list is never content: the parenthetical
# is removed (the prompt now asks for the figure itself).
_META_RE = re.compile(r"\s*\((?:[^()]*?)\b(?:the page|this page|the sources?(?: list)?|source list|listed (?:below|beneath)|"
                      r"see (?:the )?sources?|shown below|listed on the page)\b[^()]*\)", re.IGNORECASE)


def strip_meta_references(text: str) -> tuple:
    """PURE. (text without parentheticals that point at the page / a source
    list, [what was removed])."""
    removed = [m.group(0).strip() for m in _META_RE.finditer(text or "")]
    return _META_RE.sub("", text or ""), removed
