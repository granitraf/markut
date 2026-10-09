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


def extract_numeric_claims(text: str) -> list:
    # PURE. Every dollar amount, percent (ranges kept whole), multiple, and
    # finance-noun-attached number the verdict asserts, in order, deduped.
    seen, claims = set(), []
    for match in _CLAIM_RE.finditer(text or ""):
        claim = re.sub(r"\s+", " ", match.group(0)).strip()
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


def extract_evidence_numbers(evidence: str) -> set:
    return {normalize_number(m.group(0)) for m in _EVIDENCE_NUM_RE.finditer(evidence or "")}


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
    ("quarter", r"\b(mrq|most recent quarter|quarter(?:ly)?|q[1-4](?:\s?fy)?\s?\d{0,4}|three months|three fiscal quarters|fiscal quarter)\b"),
    ("ttm",     r"\b(ttm|trailing(?:[- ]twelve[- ]months)?|last twelve months)\b"),
    ("annual",  r"\b(fy\s?\d{2,4}|fiscal (?:year|20\d\d)|annual(?:ly|ized)?|full[- ]year|year ended|for the year|current[- ]fy|next[- ]fy)\b"),
    ("forward", r"\b(forward|consensus|estimate[sd]?|expected|guidance|guide|outlook|next (?:12 months|year)|fy\s?\d{4}e)\b"),
]
# pairs that CANNOT describe the same number; everything else is compatible
_CONFLICTS = {frozenset(p) for p in (("quarter", "ttm"), ("quarter", "annual"), ("quarter", "forward"),
                                     ("ttm", "annual"), ("ttm", "forward"))}
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
            packet_period = "/".join(sorted(label_periods(labels[key][0])))
            reason = f"{claim} is {packet_period} in the evidence ({labels[key][0]}), not {claim_period}"
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
