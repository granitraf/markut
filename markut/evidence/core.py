"""Core extraction: the figures every debate needs, read from the latest
earnings exhibits and the latest 10-Q by TABLE STRUCTURE, before any question
is asked — income statement summary, the company's KPI table, segment /
revenue-line tables, cash flow summary — each figure as its own line with
its current and year-ago values, period, basis, units, segment and source.
Then every profiled KPI gets a value or an explicit "not found", derived
metrics are computed in code with their formula, and sanity checks compare
segment sums and stated growth rates with the values they come from.
Nothing here names a company, a brand or a heading one filer uses."""
import re

from markut.evidence.edgar import (fetch_filing_html, html_to_text, is_figure_cell, parse_tables,
    period_from_heading)

# generic row-label patterns for the income statement and cash flow summaries
INCOME_ROWS = [
    ("Revenue", r"^(?:total\s+)?(?:net\s+)?(?:operating\s+)?(?:revenues?|sales|net sales|total net revenue|net revenue)(?: and other income)?(?:\s*\(.*\))?$"),
    ("Gross profit", r"^gross (?:profit|margin)(?:\s*\(.*\))?$"),
    ("Operating income", r"^(?:income|loss|income/\(loss\)|earnings)\s+from operations|^operating (?:income|profit|loss)(?:\s*\(.*\))?$|^income \(loss\) from operations"),
    ("Net income", r"^net (?:income|earnings|loss|income \(loss\)|loss \(income\))(?!.*(?:noncontrolling|non-controlling|including))(?:\s+attributable to[^,]*)?$"),
    ("Diluted EPS", r"^diluted (?:net )?(?:income|earnings|loss)?\s*(?:\(loss\)\s*)?per (?:common )?share|^(?:net )?(?:income|earnings) per (?:diluted|common) share.*diluted|^diluted eps"),
    ("Adjusted net income", r"^adjusted net (?:income|earnings)|^non-gaap net income"),
    ("Adjusted diluted EPS", r"^adjusted (?:diluted )?(?:net income|earnings|eps)? ?per (?:diluted )?share|^non-gaap (?:diluted )?(?:eps|earnings per share|net income per share)"),
    ("Adjusted EBITDA", r"^adjusted ebitda$|^ebitda$"),
]
CASH_ROWS = [
    ("Operating cash flow", r"net cash (?:provided by|from|used in) operating activities|^cash (?:flows? )?from operations|^operating cash flow"),
    ("Capital expenditures", r"capital expenditures|purchases? of property|additions to property|payments? for property"),
    ("Free cash flow", r"^free cash flow$"),
]
KPI_TABLE_RE = re.compile(r"(?i)key (?:performance|operating|financial|business)|operating (?:information|metrics|data|statistics)|"
                          r"selected (?:operating|financial|business)|performance information|business performance|financial highlights|"
                          r"supplemental (?:information|data|metrics)|\bmetrics\b|\bkpis?\b")
SEGMENT_ROW_RE = re.compile(r"(?i)revenue|sales|income|profit|margin|comparable|same[- ]store|operating weeks|units|volume|members|subscribers|customers")
SEGMENT_TABLE_RE = re.compile(r"(?i)segment|revenues? by|disaggregat|revenue (?:line|mix|type)|by (?:product|geograph|brand|concept|category)")
NON_GAAP_RE = re.compile(r"(?i)non-?gaap|adjusted|excluding|ex\.?\s|constant currency")
PERCENT_COL_RE = re.compile(r"(?i)percent(?:age)? of (?:total )?(?:revenues?|sales|net sales)|% of (?:revenues?|sales)|as a percentage of")
BOILERPLATE_LABEL_RE = re.compile(r"(?i)^\(?\d+\)?$|^note|^see |weighted[- ]average|shares outstanding")
MAX_LINES = {"income": 12, "kpi": 12, "segment": 10, "cash": 8, "derived": 10, "pct": 14, "lease": 6}
# run #26 (P2, item 7): lease-liability rows (balance sheet or lease note), so
# the leverage line can add the filed lease obligations a data provider's
# "total debt" leaves out; schedules and right-of-use assets are not liabilities
LEASE_ROW_RE = re.compile(r"(?i)\blease liabilit(?:y|ies)\b")
_LEASE_SKIP_RE = re.compile(r"(?i)payments?|maturit|right-of-use|cash paid|weighted|discount|obtained in exchange|imputed interest")
_LEGEND = {}   # per extraction: (form, name, caption, date) -> "T1"

_NUM_RE = re.compile(r"^\(?[-+]?\$?\s?(\d[\d,]*(?:\.\d+)?)\s?\)?\s?(%|x)?\)?$")


def num_of(value: str):
    """PURE. '(2,057)' -> -2057.0; '$1,029,633' -> 1029633.0; '21.8%' -> 21.8; '—' -> None."""
    v = (value or "").strip()
    if not v or v in ("—", "-", "–", "n/a", "N/A", "nm", "NM"):
        return None
    neg = v.startswith("(") or v.startswith("-") or v.startswith("−")
    m = _NUM_RE.match(v.replace("−", "-"))
    if not m:
        return None
    try:
        x = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return -x if neg else x


def _words(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 2 and w not in
            ("the", "and", "for", "per", "net", "total", "our", "company", "restaurants", "restaurant")}


def aliases(name: str) -> list:
    name = (name or "").strip()
    out = [name]
    m = re.match(r"^(.*?)\s*\(([^)]{2,14})\)\s*$", name)
    if m:
        out += [m.group(1).strip(), m.group(2).strip()]
    return [a for a in out if len(a) > 1]


def label_matches(label: str, name: str) -> float:
    """PURE. 0..1 how well a table row label matches a KPI / metric name:
    exact alias or abbreviation 1.0, else word-set Jaccard."""
    low = (label or "").lower().strip()
    for a in aliases(name):
        al = a.lower()
        if low == al or (len(al) <= 6 and re.search(r"\b" + re.escape(al) + r"\b", low)):
            return 1.0
        if len(al) > 6 and al in low:
            return 0.9
    wl, wn = _words(label), _words(name)
    if not wl or not wn:
        return 0.0
    return len(wl & wn) / len(wl | wn)


_UNIT_WORD = {"thousands": "thousand", "millions": "million", "billions": "billion"}
_PER_SHARE_RE = re.compile(r"(?i)per (?:common |diluted |basic |ordinary )?share|\beps\b|earnings per|income per|loss per|per (?:ads|unit holder)")
_PER_UNIT_RE = re.compile(r"(?i)\bprice\b|per (?:unit|user|customer|member|subscriber|order|account|visit|guest|room|seat)|\barpu\b|\basp\b|average (?:check|ticket|order value)")
_COUNT_RE = re.compile(r"(?i)weeks|\bdays\b|\bnumber of\b|\bcount\b|headcount|employees|restaurants|stores|locations|open at|opened during|"
                       r"\bunits\b|subscribers|members|customers|users|accounts|devices|vehicles|deliveries|orders|transactions|shipments|"
                       r"\((?:kbd|koebd|kboed|mboe|bcf|mcf|bbl|tons?|tonnes|units|x)\)|barrels|\bboe\b")
_LABEL_SCALE_RE = re.compile(r"(?i)\((?:in\s+)?(thousands|millions|billions)(?:\s+of\s+([a-z ]+?))?\)")
_COUNT_NOUN_RE = re.compile(r"(?i)\b(units|subscribers|members|customers|users|accounts|devices|vehicles|deliveries|orders|transactions|"
                            r"shipments|barrels|employees|stores|locations|restaurants)\b")


def row_units(label: str, group: str, value: str, table_units: str) -> tuple:
    """PURE. (units label, scale word to append to the value or None) from
    the ROW's type, not the table's caption: per-share rows are per share,
    percentages percent, a label's own '(in thousands)' wins, prices are per
    unit, counts are counts; only money rows inherit 'in millions'."""
    text = f"{label} {group}"
    v = (value or "").strip()
    if _PER_SHARE_RE.search(text):
        return "per share", None
    if "%" in v or re.search(r"(?i)percent(?:age)?\b|\bmargin %", label):
        return "percent", None
    own = _LABEL_SCALE_RE.search(label)
    if own:
        noun = (own.group(2) or "").strip() or (_COUNT_NOUN_RE.search(label).group(1).lower() if _COUNT_NOUN_RE.search(label) else "")
        return (f"{_UNIT_WORD.get(own.group(1).lower(), own.group(1).lower())} {noun}".strip(), None)
    if _PER_UNIT_RE.search(label):
        return "per unit", None
    if _COUNT_RE.search(label) and "$" not in v:
        return "count", None
    if table_units and ("$" in v or re.match(r"^\(?[\d,]+(?:\.\d+)?\)?$", v)):
        return table_units, _UNIT_WORD.get(table_units, table_units)
    return (table_units or "as printed"), None


def _fmt_value(value: str, units: str, label: str) -> str:
    v = (value or "").strip()
    if not v or not units or "%" in v or re.search(r"(?i)per share|per unit|ratio|rate|margin|weeks|count|number of|days|restaurants|stores|locations|"
                                                      r"\((?:kbd|koebd|kboed|mboe|bcf|mcf|bbl|tons?|tonnes|units|x)\)|per day|barrels|\bboe\b|employees|headcount", label):
        return v
    if "$" in v or re.match(r"^\(?[\d,]+(?:\.\d+)?\)?$", v):
        return f"{v} {_UNIT_WORD.get(units, units)}"
    return v


def _entity_key(cell: dict) -> str:
    # the column's thing, ignoring amount/percent markers and period words
    e = (cell.get("entity") or "").lower()
    e = re.sub(r"(?i)amount|percent(?:age)? of (?:total )?(?:revenues?|sales)|% of (?:revenues?|sales)|as a percentage of", "", e)
    return re.sub(r"\s+", " ", e).strip()


_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def _pair_cells(cells: list, prefer_percent: bool = False, entity: str = None):
    """(current, year_ago) cells: the shortest period class present, the same
    column entity (a brand or segment column never pairs with its neighbour),
    amount columns unless percent-of-revenue columns are asked for, ordered by
    date. entity=None means the consolidated / Total column when the table has
    entity columns, else whatever column there is."""
    classes = [c for c in cells if c.get("period") and c["period"] not in ("", "unstated")]
    if not classes:
        return (cells[0] if cells else None), None
    order = {"quarter": 0, "YTD (H1)": 1, "YTD (9M)": 2, "annual": 3, "ttm": 4}
    def rank(c):
        p = c["period"]
        return order.get(p, 5 if p.startswith("period ended") else 6)
    best = min(rank(c) for c in classes)
    same = [c for c in classes if rank(c) == best]
    pct = [c for c in same if PERCENT_COL_RE.search(c.get("entity", ""))]
    amount = [c for c in same if c not in pct]
    use = (pct or amount) if prefer_percent else (amount or same)
    keys = {_entity_key(c) for c in use}
    if entity is not None:
        use = [c for c in use if _entity_key(c) == entity.lower()] or use
    elif len(keys) > 1:
        tot = [c for c in use if re.search(r"(?i)^total|consolidated|^company", _entity_key(c))]
        use = tot or [c for c in use if _entity_key(c) == sorted(keys, key=len)[0]]
    def when(c):
        # (year, month, day) of the column's end date; (year, 0, 0) when only a
        # year is printed; None when no date at all
        d = period_from_heading(c.get("heading", "")).get("ended") or ""
        m = re.search(r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})", d)
        if m and m.group(1)[:3].lower() in _MONTHS:
            return (int(m.group(3)), _MONTHS[m.group(1)[:3].lower()], int(m.group(2)))
        y = re.search(r"(\d{4})", d)
        return (int(y.group(1)), 0, 0) if y else None
    def key(c):
        w = when(c)
        return (-w[0], -w[1], -w[2]) if w else (0, 0, 0)
    use = sorted(use, key=key)
    cur = use[0] if use else None
    same = [c for c in use[1:] if cur and _entity_key(c) == _entity_key(cur)]
    # the comparison is the YEAR-AGO column: run #28 (TSM "2Q26 | 2Q25 | 1Q26")
    # — the next column over can be the previous quarter
    wc = when(cur) if cur else None
    year_ago = [c for c in same if wc and when(c) and when(c)[0] == wc[0] - 1 and (not wc[1] or abs(when(c)[1] - wc[1]) <= 1)]
    prev = (year_ago or [c for c in same if not (wc and when(c) and when(c)[0] == wc[0])] or same or [None])[0]
    return cur, prev


def _change(cur, prev):
    a, b = num_of(cur), num_of(prev)
    if a is None or b is None or b == 0:
        return ""
    if "%" in (cur or "") and "%" in (prev or ""):
        return f"{a - b:+.1f} pts (DERIVED: {a:g}% − {b:g}%)"
    return f"{(a / b - 1) * 100:+.1f}% (DERIVED: {cur.strip()} / {prev.strip()} − 1)"


def _source(src: dict, rec: dict) -> str:
    # one short tag per table; the legend under the block header spells it out
    key = (src["form"], src["name"], rec.get("caption", ""), src["filing_date"])
    if key not in _LEGEND:
        _LEGEND[key] = f"T{len(_LEGEND) + 1}"
    return f"[source: {_LEGEND[key]}]"


def legend_lines() -> list:
    return [f"  {tag} = EDGAR/{form} {name}" + (f' "{cap}"' if cap else "") + f", filed {date}"
            for (form, name, cap, date), tag in _LEGEND.items()]


def _period_text(cell: dict, src: dict) -> str:
    p = cell.get("period") or "unstated"
    if p.startswith("period ended") and src.get("headline_period"):
        p = f"{src['headline_period']} ({p}; duration from the release headline)"
    head = re.sub(r"(?i)\b(?:amount|percent(?:age)? of (?:total )?(?:revenues?|sales))\b", "", cell.get("heading", "")).strip(" ,;")
    head = re.sub(r"\s+", " ", head)
    return f"{p}, {head}" if head and head.lower() not in p.lower() else p


def _scaled(value: str, scale) -> str:
    v = (value or "").strip()
    return f"{v} {scale}" if scale and v else v


def figure_line(label: str, rec: dict, cur: dict, prev, src: dict, segment: str, basis: str, kind: str = "") -> str:
    # P0 (run #26): units come from the row type ("$2.02 million" was a
    # per-share figure that inherited the table's "in millions")
    units, scale = row_units(label, rec.get("group", ""), cur.get("value", ""), rec.get("units", ""))
    if rec.get("currency") and rec["currency"] != "USD" and units and units not in ("percent", "count", "as printed"):
        units = f"{rec['currency']} {units}"      # run #28: 'TWD millions', 'TWD per share' — never an unlabelled foreign amount
    cur_v = _scaled(cur["value"], scale)
    if prev is not None:
        prev_v = _scaled(prev["value"], scale)
        change = _change(cur["value"], prev["value"])
        prev_p = _period_text(prev, src)
        if prev.get("period") == cur.get("period"):
            d = period_from_heading(prev.get("heading", "")).get("ended")
            prev_p = d or prev_p
        body = f"{cur_v} ({_period_text(cur, src)}) vs {prev_v} year-ago ({prev_p})" + (f"; change {change}" if change else "")
    else:
        body = f"{cur_v} ({_period_text(cur, src)}); no year-ago column in this table"
    grp = f" [{rec['group']}]" if rec.get("group") and rec["group"].lower() not in label.lower() else ""
    kind_t = f"{kind}: " if kind and label_matches(label, kind.replace("KPI ", "")) < 0.9 else ("" if not kind.startswith("KPI ") else f"{kind}: ")
    return (f"- {kind_t}{label}{grp}: {body}  ({units or 'as printed'}; {basis}; {segment})  {_source(src, rec)}")


def balance_line(label: str, rec: dict, cur: dict, prev, src: dict) -> str:
    """A point-in-time balance (run #26, P2 item 7): dated "as of", never a
    duration and never "year-ago" — the comparison column of a balance sheet
    is the earlier balance-sheet date, often the fiscal year-end."""
    units, scale = row_units(label, rec.get("group", ""), cur.get("value", ""), rec.get("units", ""))
    def at(c):
        return period_from_heading(c.get("heading", "")).get("ended") or c.get("heading", "") or "date not printed"
    body = f"{_scaled(cur['value'], scale)} as of {at(cur)}"
    if prev is not None:
        body += f" vs {_scaled(prev['value'], scale)} as of {at(prev)}"
    grp = f" [{rec['group']}]" if rec.get("group") and rec["group"].lower() not in label.lower() else ""
    return f"- {label}{grp}: {body}  ({units or 'as printed'}; {_basis(rec, label)}; point-in-time balance)  {_source(src, rec)}"


def _basis(rec: dict, label: str) -> str:
    text = " ".join([rec.get("caption", ""), rec.get("group", ""), label])
    return "non-GAAP" if NON_GAAP_RE.search(text) else "GAAP"


def _segment_of(text: str, segments: list) -> str:
    low = (text or "").lower()
    for s in segments:
        for a in aliases(s):
            if len(a) <= 6 and re.search(r"\b" + re.escape(a.lower()) + r"\b", low) or (len(a) > 6 and a.lower() in low):
                return s
    return ""


def headline_period(text: str) -> str:
    """PURE. The reporting period a release announces in its first lines:
    'results for the quarter ended June 30, 2026' -> quarter; 'second quarter'
    -> quarter; 'fiscal year' -> annual; 'six months' -> YTD (H1)."""
    head = (text or "")[:700].lower()
    if re.search(r"\b(?:six[- ]month|first half|26 weeks|26-week)\b", head):
        return "YTD (H1)"
    if re.search(r"\b(?:nine[- ]month|39 weeks|39-week)\b", head):
        return "YTD (9M)"
    if re.search(r"\b(?:first|second|third|fourth|q[1-4])\b.{0,20}\bquarter|\bquarter(?:ly)? (?:ended|results|of)|\b13 weeks\b|\bthree months\b", head):
        return "quarter"
    if re.search(r"\b(?:full[- ]year|fiscal year|year ended|annual) (?:results|ended)?", head):
        return "annual"
    return ""


def core_sources(fs: dict) -> list:
    """[{form, name, filing_date, url, html, text, headline_period}] for the
    results release's text exhibits (8-K or 6-K) and the interim report
    (10-Q, or the documents of a 6-K interim report) — each under its REAL
    form (run #28: a 6-K is never labelled 8-K or 10-Q)."""
    from markut.evidence.filers import base_form
    from markut.evidence.sections import _filing_docs, exhibits_of
    out = []
    if fs.get("8-K"):
        for exh in exhibits_of(fs["8-K"]):
            if exh["image_only"]:
                continue
            raw = fetch_filing_html(exh["url"])
            out.append({"form": exh["form"], "name": exh["name"], "filing_date": exh["filing_date"], "url": exh["url"],
                        "html": raw, "text": exh["text"], "headline_period": headline_period(exh["text"])})
    q = fs.get("10-Q")
    if q and base_form(q["form"]) == "6-K":
        for name, text, raw in _filing_docs(q):
            out.append({"form": q["form"], "name": name, "filing_date": q["filing_date"], "url": q["base_url"] + "/" + name,
                        "html": raw, "text": text, "headline_period": headline_period(text) or "quarter"})
    elif q:
        raw = fetch_filing_html(q["url"])
        out.append({"form": q["form"], "name": q.get("primary_doc", q["form"]), "filing_date": q["filing_date"],
                    "url": q["url"], "html": raw, "text": "", "headline_period": "quarter"})
    return out


def classify_tables(records: list) -> dict:
    """{table index: {"income","cash","kpi","segment","percent"}} by caption and row labels."""
    by_table = {}
    for r in records:
        by_table.setdefault(r["table"], []).append(r)
    kinds = {}
    for t, rows in by_table.items():
        labels = [r["label"] for r in rows]
        caption = rows[0].get("caption", "")
        k = set()
        income_hits = sum(1 for lab in labels if any(re.search(p, lab.strip().lower()) for _, p in INCOME_ROWS))
        cash_hits = sum(1 for lab in labels if any(re.search(p, lab.strip().lower()) for _, p in CASH_ROWS))
        # an equity statement repeats "Net income" under its equity columns
        # (TSM's interim 6-K): it is not an income statement
        # equity components as its columns mark one even when the caption does not
        equity_cols = {e for r in rows for c in r["cells"] for e in [c.get("entity", "")]
                       if re.search(r"(?i)capital stock|capital surplus|share premium|retained earnings|unappropriated|treasury (?:stock|shares)|"
                                    r"other equity|non-?controlling interests?|accumulated other comprehensive", e)}
        is_equity = bool(re.search(r"(?i)changes in (?:\w+['’]?\s+)?equity|statements? of (?:\w+['’]?\s+)?equity", caption)) or len(equity_cols) >= 2
        if income_hits >= 2 and not is_equity:
            k.add("income")
        if cash_hits >= 2 or (cash_hits >= 1 and re.search(r"(?i)cash flow", caption)):
            k.add("cash")
        if KPI_TABLE_RE.search(caption) or KPI_TABLE_RE.search(" ".join(r.get("group", "") for r in rows)):
            k.add("kpi")
        if SEGMENT_TABLE_RE.search(caption) or any(SEGMENT_TABLE_RE.search(r.get("group", "")) for r in rows):
            k.add("segment")
        if any(PERCENT_COL_RE.search(c.get("entity", "")) for r in rows for c in r["cells"]):
            k.add("percent")
        kinds[t] = k
    return kinds


def extract_core(fs: dict, profile: dict, plan: dict = None) -> dict:
    """-> {"text": the [CORE FIGURES] section, "kpi_values": {name: bool},
    "lines": [...], "checks": [...]}."""
    profile = profile or {}
    _LEGEND.clear()
    from markut.evidence.filers import source_phrase
    _SRC = source_phrase(fs)   # run #28: name the forms this company files
    segments = [s.get("name", "") for s in profile.get("segments", []) if isinstance(s, dict) and s.get("name")]
    kpi_names = [k.get("name", "") for k in profile.get("company_kpis", []) if isinstance(k, dict) and k.get("name")]
    for q in (plan or {}).get("key_questions", []):
        for k in q.get("kpis") or []:
            if isinstance(k, str) and k.strip() and all(label_matches(k, n) < 0.6 for n in kpi_names):
                kpi_names.append(k.strip())
    blocks = {"income": [], "kpi": [], "segment": [], "cash": [], "derived": [], "pct": [], "lease": []}
    seen = set()
    all_records = []          # (src, rec)
    for src in core_sources(fs):
        try:
            records = parse_tables(src["html"])
        except Exception as e:
            blocks["income"].append(f"- [table parsing failed for {src['form']} {src['name']}: {e}]")
            continue
        kinds = classify_tables(records)
        for rec in records:
            all_records.append((src, rec))
            if BOILERPLATE_LABEL_RE.search(rec["label"]):
                continue
            k = kinds.get(rec["table"], set())
            label = rec["label"]
            low = label.strip().lower()
            cur, prev = _pair_cells(rec["cells"])
            if cur is None or num_of(cur["value"]) is None:
                continue
            # a balance needs a dated column: a maturity table's "Less Than 1
            # Year" / "5-10 Years" columns are a schedule (run #28, TSM)
            if LEASE_ROW_RE.search(label) and not _LEASE_SKIP_RE.search(label) and \
                    not period_from_heading(cur.get("heading", "")).get("ended"):
                continue
            if LEASE_ROW_RE.search(label) and not _LEASE_SKIP_RE.search(label):
                key = ("lease", low, rec.get("group", ""), cur["period"])
                if key not in seen and len(blocks["lease"]) < MAX_LINES["lease"]:
                    seen.add(key)
                    blocks["lease"].append(balance_line(label, rec, cur, prev, src))
                continue
            # entity tables (a column per segment / brand): one line per matching entity for revenue-type rows
            entity_cells = [c for c in rec["cells"] if c.get("entity") and _segment_of(c["entity"], segments)]
            if entity_cells and SEGMENT_ROW_RE.search(label) and not re.search(r"(?i)cost|expense", label):
                for c in entity_cells:
                    ent = _segment_of(c["entity"], segments)
                    cur_e, prev_e = _pair_cells(rec["cells"], entity=_entity_key(c))
                    key = ("seg", label, ent, c["period"])
                    if key in seen or len(blocks["segment"]) >= MAX_LINES["segment"] or cur_e is None:
                        continue
                    seen.add(key)
                    blocks["segment"].append(figure_line(label, rec, cur_e, prev_e, src, ent, _basis(rec, label), ""))
            if entity_cells and not re.search(r"(?i)^total|consolidated|^company", _entity_key(cur)):
                continue      # a brand/segment column value is not a consolidated figure
            seg_label = _segment_of(" ".join([rec.get("group", ""), label]), segments)
            seg = seg_label or "company-wide"
            basis = _basis(rec, label)
            placed = False
            if seg_label and (SEGMENT_ROW_RE.search(label) or re.search(r"(?i)revenue|sales|income|profit", rec.get("group", ""))) \
                    and not re.search(r"(?i)cost|expense", label + " " + rec.get("group", "")):
                key = ("seg", label, seg, cur["period"])
                if key not in seen and len(blocks["segment"]) < MAX_LINES["segment"]:
                    seen.add(key)
                    blocks["segment"].append(figure_line(label, rec, cur, prev, src, seg, basis, ""))
                continue
            for name, pat in INCOME_ROWS:
                if re.search(pat, low) and ("income" in k or "kpi" in k) and len(blocks["income"]) < MAX_LINES["income"]:
                    key = ("inc", name, seg, cur["period"], basis)
                    if key not in seen:
                        seen.add(key)
                        blocks["income"].append(figure_line(label, rec, cur, prev, src, seg, basis, name if name.lower() != low else ""))
                    placed = True
                    break
            if not placed:
                for name, pat in CASH_ROWS:
                    if re.search(pat, low) and len(blocks["cash"]) < MAX_LINES["cash"]:
                        key = ("cash", name, cur["period"])
                        if key not in seen:
                            seen.add(key)
                            blocks["cash"].append(figure_line(label, rec, cur, prev, src, seg, basis, name if name.lower() != low else ""))
                        placed = True
                        break
            if not placed and "kpi" in k and len(blocks["kpi"]) < MAX_LINES["kpi"]:
                key = ("kpi", low, rec.get("group", ""), cur["period"])
                if key not in seen:
                    seen.add(key)
                    blocks["kpi"].append(figure_line(label, rec, cur, prev, src, seg, "company-defined", ""))
                placed = True
            # run #26 (P1): every percentage row (margin by segment or line,
            # current vs year-ago) — not just the total line
            # ownership tables (a subsidiary's "Percentage of Ownership") are
            # not performance figures (run #28, TSM's interim statements)
            if re.search(r"(?i)ownership|voting|subsidiar|interest held|shareholding",
                         " ".join([rec.get("caption", "")] + [c.get("heading", "") for c in rec["cells"]])):
                continue
            # percentages from performance tables only (income / segment / KPI /
            # percent-of-revenue) or rows that name a margin or a growth rate:
            # a compensation note's "Risk-free interest rate 1.40%" is a model
            # assumption, not a result (run #28, TSM's interim statements)
            performance = bool(k & {"income", "segment", "kpi", "percent"}) or re.search(
                r"(?i)margin|growth|percentage of (?:total |net )?(?:sales|revenues?)|% of (?:sales|revenues?)", label + " " + rec.get("group", ""))
            if not placed and performance and "%" in (cur.get("value") or "") and len(blocks["pct"]) < MAX_LINES["pct"]:
                key = ("pct", low, rec.get("group", ""), cur["period"])
                if key not in seen:
                    seen.add(key)
                    blocks["pct"].append(figure_line(label, rec, cur, prev, src, seg, basis, ""))

    # KPI values for every profiled (and planner-linked) KPI. P0 (run #26):
    # "iPhone net sales" resolved to Total net sales because word overlap
    # was the only test. A KPI must now be matched by a row that names what
    # the KPI names — its segment / product (never a company total), or its
    # distinctive words — and a grouping KPI ("net sales by segment") is
    # satisfied by the parsed segment lines. A wrong value is worse than none.
    kpi_lines, kpi_values = [], {}
    seg_rows = blocks["segment"]
    for name in kpi_names:
        if GROUPING_KPI_RE.search(name):
            if seg_rows:
                shown = [re.match(r"^- ([^:\[]+)", l).group(1).strip() for l in seg_rows if re.match(r"^- ([^:\[]+)", l)]
                uniq = list(dict.fromkeys(shown))
                kpi_lines.append(f"- KPI {name}: shown as {len(seg_rows)} segment lines above ({', '.join(uniq[:12])}"
                                 + (f", … {len(uniq) - 12} more" if len(uniq) > 12 else "") + ")")
                kpi_values[name] = True
            else:
                kpi_lines.append(f"- KPI value not found: {name} (no segment or revenue-line table in {_SRC})")
                kpi_values[name] = False
            continue
        found = resolve_kpi(name, all_records, segments)
        if found:
            src, rec, cur, prev, note = found
            seg = _segment_of(" ".join([rec.get("group", ""), rec["label"], cur.get("entity", "")]), segments) or "company-wide"
            line = figure_line(rec["label"], rec, cur, prev, src, seg, "company-defined", f"KPI {name}")
            kpi_lines.append(line.replace("  [source:", f"  ({note})  [source:", 1) if note else line)
            kpi_values[name] = True
        else:
            kpi_lines.append(f"- KPI value not found: {name} (no table row in {_SRC} names this metric)")
            kpi_values[name] = False
    derived, seen_d = [], set()
    for ln in derived_lines(all_records, segments):
        # one line per (metric, value, period): the same margin computed from
        # two tables of the same release is one fact
        m = re.match(r"^- (DERIVED [A-Za-z ]+?|SANITY CHECK [a-z ]+?)(?: OK| MISMATCH[^:]*)?: ([-\d.,]+%?)", ln)
        per = re.search(r"\((quarter|YTD \(H1\)|YTD \(9M\)|annual|ttm)[;)]", ln)
        key = (m.group(1), m.group(2), per.group(1) if per else "") if m else re.sub(r"\s*\[source:.*$", "", ln)[:120]
        if key not in seen_d:
            seen_d.add(key); derived.append(ln)
    blocks["derived"] = derived
    lines = [f"[CORE FIGURES] (extracted by code from the tables of {_SRC} before any question was asked; "
             "each line: figure (period) vs year-ago; change — then (units; basis; segment) and its source table; "
             "'KPI value not found' means the metric was not in a table, not that it does not exist)"]
    for title, key in (("Income statement summary", "income"), ("KPI / operating metrics table", "kpi"),
                       ("Segment and revenue lines", "segment"), ("Percentage tables (each line with its comparison column)", "pct"),
                       ("Cash flow summary", "cash")):
        lines.append(f"-- {title} --")
        lines.extend(blocks[key] or [f"- none found by table structure in {_SRC}"])
    lines.append("-- Lease liabilities (balance sheet or lease note) --")
    lines.extend(blocks["lease"] or [f"- no lease-liability row in {_SRC} tables"])
    lines.append("-- KPI values for the profiled metrics --")
    lines.extend(kpi_lines or ["- the profile listed no KPIs"])
    lines.append("-- Derived metrics and sanity checks (computed in code) --")
    lines.extend(blocks["derived"] or ["- no derivable pairs found"])
    if _LEGEND:
        lines.append("-- Source tables --")
        lines.extend(legend_lines())
    return {"text": "\n".join(lines), "kpi_values": kpi_values, "lines": lines,
            "checks": [l for l in blocks["derived"] if "SANITY" in l]}


GROUPING_KPI_RE = re.compile(r"(?i)\bby\s+(?:reportable\s+|operating\s+|geographic(?:al)?\s+|product[/ ]?(?:service\s+)?)?"
                             r"(?:segments?|products?|categor(?:y|ies)|geograph\w*|regions?|brands?|business(?:es)?|lines?|types?|channels?|concepts?|divisions?)\b|"
                             r"\b(?:segment|revenue|sales) (?:mix|breakdown)\b")
_GENERIC_WORDS = {"revenue", "revenues", "sale", "sales", "net", "total", "margin", "margins", "percentage", "percent", "rate", "growth",
                  "income", "profit", "earning", "earnings", "share", "shares", "per", "eps", "value", "amount", "figure", "figures",
                  "metric", "metrics", "ratio", "ratios", "yoy", "year", "quarter", "change", "reported", "company", "the", "and", "of",
                  "for", "from", "in", "on", "to", "a", "an", "with", "its", "as", "by", "vs"}
_COST_RE = re.compile(r"(?i)\bcost|expense")


def _norm_words(text: str) -> set:
    t = re.sub(r"(?i)\beps\b", "earnings per share", text or "")
    return {w.rstrip("s") if len(w) > 3 else w for w in re.findall(r"[a-z0-9]+", t.lower())}


def _distinctive(name: str, entity_aliases: list = ()) -> set:
    words = _norm_words(name)
    for a in entity_aliases:
        words -= _norm_words(a)
    return {w for w in words if w not in {g.rstrip("s") if len(g) > 3 else g for g in _GENERIC_WORDS} and len(w) > 2}


def _kpi_entities(name: str, segments: list) -> list:
    low = (name or "").lower()
    out = []
    for seg in segments:
        for a in aliases(seg):
            al = a.lower()
            if (len(al) <= 6 and re.search(r"(?<![a-z0-9])" + re.escape(al) + r"(?![a-z0-9])", low)) or (len(al) > 6 and al in low):
                out.append(seg); break
    return out


def resolve_kpi(name: str, all_records: list, segments: list):
    """-> (src, rec, cur, prev, note) or None. The row must name the KPI's
    segment/product when the KPI names one (a company-total row never
    qualifies, unless the company has a single reportable segment — then the
    consolidated figure IS that segment and the line says so), and must carry
    at least half of the KPI's distinctive words otherwise. Rows under a
    cost or expense heading never satisfy a non-cost metric."""
    wants_pct = bool(re.search(r"(?i)%|percent|margin|ratio|rate|penetration|mix", name))
    wants_cost = bool(_COST_RE.search(name))
    base = re.sub(r"(?i)\s*(?:%|percent(?:age)?)\s*of\s*(?:total\s+)?(?:revenues?|sales)\s*$", "", name)
    entities = _kpi_entities(name, segments)
    ent_aliases = [a for e in entities for a in aliases(e)]
    distinctive = _distinctive(base, ent_aliases)
    metric_words = _norm_words(base) - {w for a in ent_aliases for w in _norm_words(a)}
    best, best_score = None, 0.0
    for src, rec in all_records:
        label, group = rec["label"], rec.get("group", "")
        if BOILERPLATE_LABEL_RE.search(label):
            continue
        context = f"{label} {group}"
        if _COST_RE.search(context) and not wants_cost:
            continue
        if entities:
            if re.match(r"(?i)^(?:total|consolidated)\b", label):
                continue
            ent_cells = [c for c in rec["cells"] if c.get("entity") and _segment_of(c["entity"], entities)]
            row_names_entity = bool(_segment_of(label, entities))
            if not row_names_entity and not ent_cells:
                continue
            ctx_words = _norm_words(f"{label} {group} {rec.get('caption', '')}")
            overlap = len((metric_words - {"net", "total"}) & ctx_words) / max(1, len(metric_words - {"net", "total"}))
            phrase = 1.0 if re.sub(r"\s+", " ", " ".join(w for w in re.findall(r"[a-z]+", base.lower()) if w not in {a.lower() for a in ent_aliases})).strip() in context.lower() else 0.0
            score = overlap + phrase
            if score <= 0 and metric_words - {"net", "total"}:
                continue
            ent_key = _entity_key(ent_cells[0]) if (ent_cells and not row_names_entity) else None
            cur, prev = _pair_cells(rec["cells"], prefer_percent=wants_pct, entity=ent_key)
        else:
            score = max(label_matches(label, base), label_matches(f"{label} {group}", base))
            exact = any(label.strip().lower() == a.lower() for a in aliases(base))
            ctx_words = _norm_words(f"{label} {group}")
            if not exact:
                if score < 0.5:
                    continue
                if distinctive and len(distinctive & ctx_words) / len(distinctive) < 0.5:
                    continue
            cur, prev = _pair_cells(rec["cells"], prefer_percent=wants_pct)
            if wants_pct and cur is not None and "%" not in (cur.get("value") or "") and not PERCENT_COL_RE.search(cur.get("entity", "")):
                score -= 0.3                     # a percent metric matched an amount row: weaker
        if cur is None or num_of(cur.get("value")) is None:
            continue
        if score > best_score:
            best, best_score = (src, rec, cur, prev, ""), score
    if best is None and entities and len(segments) == 1:
        # single reportable segment: the consolidated row is that segment
        metric = re.sub(r"(?i)\b" + "|".join(re.escape(a) for a in ent_aliases) + r"\b", "", base).strip() or "revenue"
        for src, rec in all_records:
            if re.match(r"(?i)^(?:total|consolidated)\b", rec["label"]) and label_matches(rec["label"], metric) >= 0.5 and not _COST_RE.search(rec["label"] + " " + rec.get("group", "")):
                cur, prev = _pair_cells(rec["cells"], prefer_percent=wants_pct)
                if cur is not None and num_of(cur.get("value")) is not None:
                    return src, rec, cur, prev, "single reportable segment: the consolidated figure is the segment's"
    return best


def derived_lines(all_records: list, segments: list) -> list:
    """Margins from revenue + profit rows in the same table and period, the
    residual margin after listed cost ratios, segment-sum and stated-growth
    sanity checks. Every line shows its formula and inputs."""
    out = []
    by_table = {}
    for src, rec in all_records:
        by_table.setdefault((id(src), rec["table"]), []).append((src, rec))
    for (_, _t), rows in by_table.items():
        src = rows[0][0]
        recs = [r for _, r in rows]
        def find(pat):
            for r in recs:
                if re.search(pat, r["label"].strip().lower()):
                    return r
            return None
        rev = find(INCOME_ROWS[0][1])
        if rev:
            rc, _ = _pair_cells(rev["cells"])
            rv = num_of(rc["value"]) if rc else None
            if rv:
                for name, pat in (("Gross margin", INCOME_ROWS[1][1]), ("Operating margin", INCOME_ROWS[2][1]), ("Net margin", INCOME_ROWS[3][1])):
                    r = find(pat)
                    if not r:
                        continue
                    c, _ = _pair_cells(r["cells"])
                    v = num_of(c["value"]) if c and c["period"] == rc["period"] else None
                    if v is not None and len(out) < MAX_LINES["derived"]:
                        out.append(f"- DERIVED {name}: {v / rv * 100:.1f}% = {r['label']} {c['value']} / {rev['label']} {rc['value']} ({rc['period']})  {_source(src, rev)}")
        # residual margin after cost ratios printed as % of revenue
        pct_rows = []
        for r in recs:
            if not re.search(r"(?i)cost|expense|labor|labour|occupancy|food|beverage", r["label"]) or re.search(r"(?i)total|income|tax|interest|depreciation|amortization|impairment|preopening|general and administrative|g&a", r["label"]):
                continue
            pc = [c for c in r["cells"] if PERCENT_COL_RE.search(c.get("entity", "")) and "%" in (c.get("value") or "")]
            if pc:
                pct_rows.append((r["label"], pc[0]))
        if len(pct_rows) >= 2 and len(out) < MAX_LINES["derived"]:
            vals = [(lab, num_of(c["value"])) for lab, c in pct_rows]
            vals = [(lab, v) for lab, v in vals if v is not None]
            if vals:
                total = sum(v for _, v in vals)
                out.append(f"- DERIVED residual margin after the listed cost ratios: {100 - total:.1f}% = 100% − "
                           + " − ".join(f"{lab} {v:g}%" for lab, v in vals) + f" ({pct_rows[0][1]['period']}; the cost lines a filer prints as % of revenue; "
                           f"not a reported margin)  {_source(src, recs[0])}")
        # segment sum: entity columns of a revenue row vs its Total column
        for r in recs:
            if not re.search(INCOME_ROWS[0][1], r["label"].strip().lower()):
                continue
            ents = [c for c in r["cells"] if c.get("entity") and _segment_of(c["entity"], segments)]
            tot = next((c for c in r["cells"] if re.search(r"(?i)^total|consolidated", c.get("entity", ""))), None)
            if len(ents) >= 2 and tot and len(out) < MAX_LINES["derived"]:
                parts = [(c["entity"], num_of(c["value"])) for c in ents]
                tv = num_of(tot["value"])
                if tv and all(v is not None for _, v in parts):
                    s = sum(v for _, v in parts)
                    gap = (s / tv - 1) * 100
                    other = [c for c in r["cells"] if c.get("entity") and c not in ents and c is not tot]
                    verdict = "OK" if abs(gap) <= 1.0 or other else f"MISMATCH ({gap:+.1f}%)"
                    out.append(f"- SANITY CHECK segment sum {verdict}: " + " + ".join(f"{e} {v:,.0f}" for e, v in parts)
                               + (f" + other columns {', '.join(c['entity'] for c in other)}" if other else "")
                               + f" vs {tot['entity']} {tv:,.0f} ({r['label']}, {tot['period']})  {_source(src, r)}")
        # stated growth vs computed from the two values of a matching value row
        for r in recs:
            if not re.search(r"(?i)growth|change|vs\.? prior|year[- ]over[- ]year|yoy", r["label"]):
                continue
            gcell, _ = _pair_cells(r["cells"])
            if not gcell or "%" not in (gcell.get("value") or ""):
                continue
            base_label = re.sub(r"(?i)\b(?:yoy|growth|rate|change|vs\.? prior year|year[- ]over[- ]year)\b", "", r["label"]).strip(" :-")
            base = next((b for b in recs if b is not r and label_matches(b["label"], base_label) >= 0.6 and "%" not in (b["cells"][0]["value"] if b["cells"] else "%")), None)
            if base and len(out) < MAX_LINES["derived"]:
                bc, bp = _pair_cells(base["cells"])
                a, b = (num_of(bc["value"]) if bc else None), (num_of(bp["value"]) if bp else None)
                g = num_of(gcell["value"])
                if a is not None and b and g is not None:
                    implied = (a / b - 1) * 100
                    verdict = "OK" if abs(implied - g) <= 0.6 else f"MISMATCH (stated {g:g}%, implied {implied:.1f}%)"
                    out.append(f"- SANITY CHECK stated growth {verdict}: {r['label']} {gcell['value']} vs {base['label']} {bc['value']} / {bp['value']} − 1 = {implied:.1f}%  {_source(src, r)}")
    return out
