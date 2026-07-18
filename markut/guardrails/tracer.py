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
_CLAIM_RE = re.compile(
    r"""\$\s?\d[\d,]*(?:\.\d+)?(?:\s*(?:trillion|billion|million|thousand)\b|\s?[TBMKtbmk]\b)?
      | \d+(?:\.\d+)?\s*(?:-|–|\bto\b)\s*\d+(?:\.\d+)?\s*%
      | \d+(?:\.\d+)?\s*%
      | \d+(?:\.\d+)?\s?x\b
      | \d[\d,]*(?:\.\d+)?(?:\s+(?:trillion|billion|million|thousand))?\s+(?:__NOUNS__)\b
    """.replace("__NOUNS__", "|".join(_FINANCE_NOUNS)),
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
