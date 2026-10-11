"""Evidence quality and profile evidence (2026-10-11, after run #29 TSM).

WHY: the verdict is only as good as the packet. The judge cross-checks every
claim against the packet and the governor traces every figure to it, so a
figure the packet lacks, a figure that is wrong, or a figure that only a
model wrote all pass straight through to the conclusion. Two blocks, computed
in code before the debate and placed right after the key questions:

  [EVIDENCE QUALITY]  how complete and consistent the packet is: statements
                      found, questions covered, filing vs provider for the
                      same metric and period, out-of-range figures, figures
                      only a model wrote, and what was trimmed to fit.
                      Describes the packet; never evidence itself.
  [PROFILE EVIDENCE]  the filing sentence behind each figure the profiler and
                      planner wrote (run #29: "77% of wafer revenue", "up 42%"
                      sat in model-written text the governor may not trace),
                      carried into the data with its source.

PURE: strings in, strings out — no network, no model calls."""
import re

from markut.guardrails import tracer

# Out-of-range bounds: each is far more often a unit, scale or currency error
# than a real value (run #28, TSM: TWD figures on a USD price gave a 47.84%
# FCF yield and a 0.5x trailing P/E band).
FCF_YIELD_LIMIT = 25.0          # % either way
PE_LOW, PE_HIGH = 2.0, 500.0    # a positive P/E outside this range
PB_HIGH = 100.0
MARGIN_HIGH = 100.0             # gross / operating margin above 100% cannot happen (net margin can, on one-off gains)
GROWTH_TOLERANCE = 0.5          # points: filing vs provider revenue growth for the same quarter

HEADER = ("[EVIDENCE QUALITY] (computed by code from this packet before the debate: how complete and consistent "
          "the evidence is; it describes the evidence and is not evidence itself)")
PROFILE_HEADER = ("[PROFILE EVIDENCE] (the filing sentence behind each figure the profiler or planner wrote, "
                  "carried here by code with its source so the figure is evidence, not a model's paraphrase)")

_HEADER_RE = re.compile(r"^\[([A-Z][A-Z0-9 &/]+?)(?:\s+Q\d)?(?:\s+—[^\]]*)?\]")
_MODEL_SECTIONS = {"PROFILE": "profile", "KEY QUESTIONS": None, "WHAT WOULD MISLEAD": "mislead"}
_MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
                "November", "December")
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def _sections(packet: str) -> list:
    """[(section name, [lines])] in packet order."""
    out = [("", [])]
    for raw in (packet or "").splitlines():
        m = _HEADER_RE.match(raw.strip())
        if m:
            out.append((m.group(1), []))
        else:
            out[-1][1].append(raw)
    return out


def _sub_block(packet: str, title: str) -> list:
    """The '- ' lines under a '-- title --' heading, up to the next heading."""
    lines, inside = [], False
    for raw in (packet or "").splitlines():
        line = raw.strip()
        if line.startswith("-- ") and line.endswith(" --"):
            inside = line[3:-3].lower().startswith(title.lower())
            continue
        if _HEADER_RE.match(line):
            inside = False
            continue
        if inside and line.startswith("- ") and not line.startswith("- none found"):
            lines.append(line)
    return lines


def _statements(packet: str) -> dict:
    core = "\n".join("\n".join(ls) for name, ls in _sections(packet) if name in ("FILINGS", "CORE FIGURES"))
    return {
        "income": bool(_sub_block(packet, "Income statement summary")),
        "cash flow": bool(_sub_block(packet, "Cash flow summary")) or "DERIVED Free cash flow" in packet,
        "balance": bool(_sub_block(packet, "Balance sheet summary"))
                   or bool(re.search(r"(?im)^- .*(?:point-in-time balance|\bas of [A-Z][a-z]+ \d)", core)),
        "segment table": bool(_sub_block(packet, "Segment and revenue lines")),
    }


def _coverage(packet: str, coverage: dict = None) -> tuple:
    """(covered, total, open question ids)."""
    if coverage:
        open_ = [q for q, n in coverage.items() if not n]
        return len(coverage) - len(open_), len(coverage), open_
    secs = dict((n, ls) for n, ls in _sections(packet))
    total = sum(1 for ln in secs.get("KEY QUESTIONS", []) if re.match(r"^- Q\d+:", ln.strip()))
    open_ = sorted({m.group(1) for m in re.finditer(r"(?m)^- COVERAGE GAP (Q\d+):", packet or "")})
    return max(0, total - len(open_)), total, open_


def _date(text: str):
    """(year, month) from 'June 30, 2025' or '2026-06-30'; None otherwise."""
    m = re.search(r"\b(\d{4})-(\d{2})-\d{2}\b", text or "")
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"\b([A-Z][a-z]{2})[a-z]*\.? \d{1,2}, (\d{4})\b", text or "")
    if m and m.group(1).lower() in _MONTHS:
        return int(m.group(2)), _MONTHS[m.group(1).lower()]
    return None


def _revenue_check(packet: str):
    """-> (line, disputed) or None. The filing's quarterly revenue change vs
    the provider's MRQ revenue growth, compared only for the same quarter."""
    filing = next((ln for ln in _sub_block(packet, "Income statement summary")
                   if re.match(r"^- Revenue:", ln) and "(quarter" in ln), None)
    prov = re.search(r"(?m)^- Revenue growth \(MRQ YoY([^)]*)\)[^:\n]*: ([+-]?\d+(?:\.\d+)?)%", packet or "")
    if not prov:
        return None
    p = float(prov.group(2))
    pq = _date(prov.group(1))
    if not filing:
        return (f"- Cross-check, revenue growth: no quarterly revenue line from the filings to compare with the provider's "
                f"{prov.group(2)}%", False)
    fm = re.search(r"change ([+-]?\d+(?:\.\d+)?)%", filing)
    if not fm:
        return None
    f = float(fm.group(1))
    if "prior period (" in filing:
        # the filing's table compares with the prior quarter, the provider's
        # figure is year over year — two different changes (2026-10-11)
        return ("- Cross-check, revenue growth: not compared — the filing's table compares with the prior period, the provider's "
                f"{prov.group(2)}% is year over year", False)
    ago = re.search(r"year-ago \(([^)]*)\)", filing)
    fq = _date(ago.group(1)) if ago else None
    fq = (fq[0] + 1, fq[1]) if fq else None
    period = f" for the {_MONTH_NAMES[pq[1] - 1]} {pq[0]} quarter" if pq else ""
    if fq and pq and fq != pq:
        return (f"- Cross-check, revenue growth: not compared — the filing's quarter ({fq[0]}-{fq[1]:02d}) and the provider's "
                f"({pq[0]}-{pq[1]:02d}) differ", False)
    diff = abs(f - p)
    if diff <= GROWTH_TOLERANCE:
        return (f"- Cross-check, revenue growth{period}: the filing's {fm.group(1)}% and the provider's {prov.group(2)}% agree "
                f"(within {GROWTH_TOLERANCE:g} points)", False)
    return (f"- DISPUTED, revenue growth{period}: the filing's {fm.group(1)}% and the provider's {prov.group(2)}% disagree by "
            f"{diff:.1f} points — one of them is the wrong line or period; neither may decide a question until the debate "
            f"shows which is right", True)


def _leverage_check(packet: str):
    prov = re.search(r"(?m)^- Debt/Equity \(mrq[^:\n]*: ([\d.]+)", packet or "")
    if not prov:
        return None
    computed = re.findall(r"(?m)^- Debt/Equity (including|excluding) (?:the filing's )?lease (?:obligations|liabilities) \(computed[^:\n]*: "
                          r"([\d.]+)%[^\n]*?(\(matches the [^)]*\))?\s*$", packet or "")
    if not computed:
        return None
    match = next(((kind, v) for kind, v, ok in computed if ok), None)
    if match:
        return f"- Cross-check, debt/equity: the provider's {prov.group(1)} matches the computed {match[1]}% ({match[0]} leases)"
    shown = ", ".join(f"{kind} leases {v}%" for kind, v, _ in computed)
    return (f"- Cross-check, debt/equity: the provider's {prov.group(1)} matches neither computed line ({shown}); the provider's "
            f"definition is not stated, so cite the computed lines, which show what they include")


def _value(line: str):
    """The figure after the label's colon (before any source tag)."""
    body = re.sub(r"\s*\[source:[^\]]*\].*$", "", line)
    m = re.search(r":\s*([+-]?\d[\d,]*(?:\.\d+)?)\s*(%|x)?\s*$", body)
    return (float(m.group(1).replace(",", "")), m.group(0).split(":", 1)[1].strip()) if m else (None, "")


def _suspects(packet: str) -> list:
    out = []
    for raw in (packet or "").splitlines():
        line = raw.strip()
        label = line[2:].split(":", 1)[0] if line.startswith("- ") else ""
        if re.match(r"^- FCF yield\b", line):
            v, shown = _value(line)
            if v is not None and abs(v) > FCF_YIELD_LIMIT:
                out.append(f"FCF yield {shown} — beyond ±{FCF_YIELD_LIMIT:g}%, the mark of a currency or unit mismatch between "
                           "cash flow and market cap")
        elif re.match(r"^- (?:Trailing )?P/E band\b", line):
            bad = [f"{k} {v}x" for k, v in re.findall(r"\b(p25|median|p75) (\d+(?:\.\d+)?)x", line)
                   if not PE_LOW <= float(v) <= PE_HIGH]
            if bad:
                out.append(f"trailing P/E band {', '.join(bad)} — outside {PE_LOW:g}x-{PE_HIGH:g}x, the mark of EPS and price "
                           "in different currencies or units")
        elif re.match(r"^- P/E\b", line):
            v, shown = _value(line)
            if v is not None and v > 0 and not PE_LOW <= v <= PE_HIGH:
                out.append(f"{label} {shown} — outside {PE_LOW:g}-{PE_HIGH:g}")
        elif re.match(r"^- (?:Price/Book|P/B)\b", line):
            v, shown = _value(line)
            if v is not None and v > PB_HIGH:
                out.append(f"{label} {shown} — above {PB_HIGH:g}")
        elif re.match(r"^- (?:DERIVED )?(?:Gross|Operating) margin\b", line, re.IGNORECASE):
            m = re.search(r":\s*(-?\d+(?:\.\d+)?)%", line)
            if m and float(m.group(1)) > MARGIN_HIGH:
                out.append(f"{label} {m.group(1)}% — a gross or operating margin cannot exceed {MARGIN_HIGH:g}%")
    return out


_TRIM_RE = re.compile(r"\[(\d+ of \d+ (?:\w+ )?shown — [^\]]+)\]")


def _trimmed(packet: str) -> list:
    out = list(dict.fromkeys(_TRIM_RE.findall(packet or "")))
    quotes = len(re.findall(r"\[\.\.\.truncated\]", packet or ""))
    if quotes:
        out.append(f"{quotes} quoted passage{'s' if quotes != 1 else ''} cut at a sentence end to fit the budget")
    items = len(re.findall(r"\[\.\.\.more items omitted for budget\]", packet or ""))
    if items:
        out.append(f"{items} list{'s' if items != 1 else ''} cut short to fit the budget")
    sections = re.findall(r"\[\.\.\.section trimmed to ~\d+ tokens\]", packet or "")
    if sections:
        out.append(f"{len(sections)} filing section{'s' if len(sections) != 1 else ''} trimmed to its token budget")
    return out


def quality_block(packet: str, unanchored: list = None, coverage: dict = None) -> tuple:
    """-> (block text, {"suspect", "disputed", "covered", "total", "unanchored", "trimmed"})."""
    st = _statements(packet)
    covered, total, open_ = _coverage(packet, coverage)
    checks = [c for c in (_revenue_check(packet),) if c]
    disputed = sum(1 for _, d in checks if d)
    agree = sum(1 for line, d in checks if not d and " agree " in line)
    lev = _leverage_check(packet)
    suspects = _suspects(packet)
    trimmed = _trimmed(packet)
    unanchored = list(unanchored or [])
    marks = " · ".join(f"{k} {'✓' if v else '✗'}" for k, v in st.items())
    n_un = len(unanchored)
    # words, not marks, in the summary: it is printed on the page and in the
    # PDF, whose serif face has no ✗ glyph
    found = "; ".join(p for p in ("found: " + ", ".join(k for k, v in st.items() if v) if any(st.values()) else "",
                                  "not found: " + ", ".join(k for k, v in st.items() if not v) if not all(st.values()) else "") if p)
    summary = (f"{covered} of {total} questions covered · {found} · {agree} cross-check{'s agree' if agree != 1 else ' agrees'}, "
               f"{disputed} disputed · {len(suspects)} suspect · {n_un} figure{'s' if n_un != 1 else ''} not in the data · "
               f"{len(trimmed)} trimmed")
    lines = [HEADER, f"- Summary: {summary}",
             f"- Statements found in the filings: {marks} (✗ = not retrieved by code, which is not the same as not disclosed)",
             f"- Questions with current-period evidence: {covered} of {total}"
             + (f" (open: {', '.join(open_)} — the coverage gaps at the end say why)" if open_ else "")]
    lines += [line for line, _ in checks]
    if lev:
        lines.append(lev)
    lines += [f"- SUSPECT: {s} — give it no weight" for s in suspects]
    if unanchored:
        lines.append("- Not evidence (written by the profiler or planner; no data line and no filing section read states it): "
                     + "; ".join(unanchored) + " — give these no weight")
    if trimmed:
        lines.append("- Trimmed to fit: " + "; ".join(trimmed))
    return "\n".join(lines), {"suspect": len(suspects), "disputed": disputed, "covered": covered, "total": total,
                              "unanchored": n_un, "trimmed": len(trimmed), "summary": summary}


def quality_summary(packet: str) -> str:
    """The one-line summary from a packet's [EVIDENCE QUALITY] block ('' if none)."""
    m = re.search(r"(?m)^\[EVIDENCE QUALITY\][^\n]*\n- Summary: ([^\n]+)", packet or "")
    return m.group(1).strip() if m else ""


def quality_flags(packet: str) -> dict:
    """{"suspect": n, "disputed": n, "items": [...]} read back from the block."""
    s = quality_summary(packet)
    if not s:
        return {"suspect": 0, "disputed": 0, "items": []}
    num = lambda pat: int(m.group(1)) if (m := re.search(pat, s)) else 0
    items = re.findall(r"(?m)^- (?:SUSPECT|DISPUTED)[:,] ?([^\n—]+)", packet or "")
    return {"suspect": num(r"(\d+) suspect"), "disputed": num(r"(\d+) disputed"), "items": [i.strip() for i in items]}


# ---------------- profile evidence ----------------

_SENT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(“]|[a-z][A-Z])")   # evidence.rag.split_sentences' rule
_STOP = set("the and for with from that this was were are its our their which into over than been have has had per of to in on at by as an or "
            "what how does will whether given versus vs".split())


def _model_lines(packet: str) -> list:
    """[(label, line)] for every line a model wrote: the profile, the key
    questions and the 'what would mislead' list. Sources, gaps and markers
    are not figures the model asserted."""
    out = []
    for name, lines in _sections(packet):
        if name not in _MODEL_SECTIONS:
            continue
        for raw in lines:
            line = raw.strip()
            if not line.startswith("- ") or re.match(r"^- (?:Profile sources|Profile gap|\[\d+ of \d+)", line):
                continue
            q = re.match(r"^- (Q\d+):", line)
            label = q.group(1) if q else _MODEL_SECTIONS[name]
            if not label:
                continue
            out.append((label, re.sub(r"\[source:[^\]]*\]|\[[^\]]*not found[^\]]*\]|\(limitation:[^)]*\)?", " ", line)))
    return out


def _figure_pattern(fig: str):
    """A regex for the figure as a filing would print it, and (value,
    decimals, unit) for the rounding fallback; None when unparseable."""
    s = fig.strip()
    m = re.fullmatch(r"(\$?)\s?(\d[\d,]*(?:\.(\d+))?)\s*(%|x|trillion|billion|million|thousand|[TBMK])?(?:\s+\w+)?", s, re.IGNORECASE)
    if not m:
        return None
    dollar, num, dec, unit = m.group(1), m.group(2).replace(",", ""), m.group(3) or "", (m.group(4) or "").lower()
    whole, _, frac = num.partition(".")
    body = (re.escape(whole) if len(whole) <= 3 else r",?".join(re.escape(c) for c in whole)) + (r"\." + re.escape(frac) if frac else "") + r"(?:\.0+)?"
    pre = r"\$\s?" if dollar else r"(?<![\d.,])"
    if unit == "%":
        post = r"\s?(?:%|percent\b)"
    elif unit == "x":
        post = r"\s?(?:x\b|times\b)"
    elif unit:
        post = r"\s?" + {"t": "(?:t|trillion)", "b": "(?:b|bn|billion)", "m": "(?:m|mn|million)", "k": "(?:k|thousand)"}.get(unit, re.escape(unit)) + r"\b"
    else:
        post = r"(?![\d]|\.\d)"
    return re.compile(pre + body + post, re.IGNORECASE), (float(num), len(dec), unit if unit in ("%", "x") else None)


def _rounds_to(sentence: str, value: float, decimals: int, unit: str) -> bool:
    if not unit:
        return False
    post = r"\s?(?:%|percent\b)" if unit == "%" else r"\s?(?:x\b|times\b)"
    return any(round(float(n), decimals) == value and n != f"{value:.{decimals}f}"
               for n in re.findall(r"(?<![\d.,])(\d+\.\d+)" + post, sentence, re.IGNORECASE))


def _words(text: str) -> set:
    return {w for w in re.findall(r"[a-z][a-z\-]{2,}", (text or "").lower()) if w not in _STOP}


def _quote(sentence: str, span: tuple, limit: int = 320) -> str:
    s = re.sub(r"\s+", " ", sentence).strip()
    if len(s) <= limit:
        return s
    mid = (span[0] + span[1]) // 2
    a, b = max(0, mid - limit // 2), min(len(s), mid + limit // 2)
    a = s.rfind(" ", 0, a) + 1 if a else 0
    b = s.find(" ", b) if b < len(s) and s.find(" ", b) > 0 else len(s)
    return ("…" if a else "") + s[a:b].strip() + ("…" if b < len(s) else "")


def _find_sentence(fig: str, context: str, sections: list):
    """(quote, section) for the filing sentence that states the figure — exact
    first, then a printed value that rounds to it — the sentence sharing the
    most words with the model's own line, the latest filing on a tie."""
    pat = _figure_pattern(fig)
    if not pat:
        return None
    regex, (value, decimals, unit) = pat
    ctx = _words(context)
    best = None
    for rank, sec in enumerate(sorted(sections or [], key=lambda s: str(s.get("filing_date", "")), reverse=True)):
        for sent in _SENT_RE.split(sec.get("text") or ""):
            m = regex.search(sent)
            exact = bool(m)
            if not exact and not _rounds_to(sent, value, decimals, unit):
                continue
            score = (1 if exact else 0, len(ctx & _words(sent)), -rank)
            if not best or score > best[0]:
                span = m.span() if m else (0, len(sent))
                best = (score, _quote(sent, span), sec)
    return (best[1], best[2]) if best else None


def profile_evidence(packet: str, sections: list) -> tuple:
    """-> (block text or '', ["figure (where)", ...] stated nowhere in the data
    or the sections read). A figure the data already states exactly needs
    nothing; one a filing sentence states is carried with that sentence."""
    data = tracer.tracing_evidence(packet)
    anchors = tracer.extract_evidence_numbers(data)
    carried, unanchored, seen = [], {}, {}
    for label, line in _model_lines(packet):
        for fig in tracer.extract_numeric_claims(line):
            key = tracer.normalize_number(fig)
            if key in seen:
                if seen[key] in unanchored and label not in unanchored[seen[key]]:
                    unanchored[seen[key]].append(label)
                continue
            seen[key] = fig
            if tracer.trace_claim(fig, data) == "CITED":
                continue
            ends = re.match(r"^\s*[+-]?([\d.,]+)\s*(?:-|–|to)\s*([\d.,]+)\s*(%|x)?\s*$", fig)
            parts = [ends.group(1) + (ends.group(3) or ""), ends.group(2) + (ends.group(3) or "")] if ends else [fig]
            found = [_find_sentence(p, line, sections) for p in parts]
            if all(found):
                for quote, sec in found:
                    if not any(f'"{quote}"' in c for c in carried):
                        carried.append(f'- [{label}] "{quote}"  [source: EDGAR/{sec.get("name") or "filing section"}'
                                       + (f', filed {sec["filing_date"]}' if sec.get("filing_date") else "") + "]")
                continue
            # a rounded form of a figure the data states is not "not evidence"
            if _rounded_in(fig, anchors):
                continue
            unanchored[fig] = [label]
    block = "\n".join([PROFILE_HEADER] + carried) if carried else ""
    return block, [f"{fig} ({', '.join(where)})" for fig, where in unanchored.items()]


def _rounded_in(fig: str, anchors: set) -> bool:
    pat = _figure_pattern(fig)
    if not pat:
        return False
    _, (value, decimals, unit) = pat
    for a in anchors:
        a_unit = "%" if a.endswith("%") else None
        if (unit == "%") != (a_unit == "%") or "[" in a:
            continue
        try:
            n = float(a.rstrip("%"))
        except ValueError:
            continue
        if round(n, decimals) == value and n != value:
            return True
    return False
