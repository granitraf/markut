"""Statement layouts found by the 18-company regression check (2026-10-11).

Building the core figures for 18 filers with the last commit and with this
change showed what the table parser had wrong all along — Amazon's and
Alibaba's quarters read backwards (the year printed on its own header row),
Microsoft's "Revenue: 18%" (a growth rate as revenue), JPMorgan's capital
figures read from its "Change" columns — and what the change broke: lease
interest shown as a lease balance, a prior quarter compared as the year-ago,
consecutive quarters paired, segment margins as the company's, non-GAAP KPIs
answered with GAAP lines, restaurant counts "in thousands". One test per
layout, each mirroring the real filing's rows. Offline."""
from markut.evidence import core
from markut.evidence.edgar import parse_tables, period_from_heading


def check(label, condition):
    assert condition, label


def _tr(cells):
    return "<tr>" + "".join(f'<td colspan="{n}">{t}</td>' if n > 1 else f"<td>{t}</td>" for t, n in cells) + "</tr>"


def _doc(*tables, before=""):
    # `before` is printed above every table, as filers caption each one
    return "<html><body>" + "".join(f"{before}<table>" + "".join(_tr(r) for r in rows) + "</table>" for rows in tables) + "</body></html>"


def _core(monkeypatch, html, profile=None, form="10-Q"):
    monkeypatch.setattr(core, "core_sources", lambda fs: [{"form": form, "name": "doc.htm", "filing_date": "2026-08-01", "url": "u",
                                                           "html": html, "text": "", "headline_period": "quarter"}])
    return core.extract_core({}, profile or {"segments": [], "company_kpis": []}, None)["text"]


def _cells(html, label):
    r = next(r for r in parse_tables(html) if r["label"] == label)
    return r["cells"]


# ---------------------------------------------------------------- header rows of years and quarters
# RY: dates over the columns, then the years on a row whose first cell is the units
RY = _doc([[("", 1), ("As at or for the three months ended", 3), ("As at or for the nine months ended", 2)],
           [("", 1), ("July 31", 1), ("April 30", 1), ("July 31", 1), ("July 31", 1), ("July 31", 1)],
           [("(Millions of Canadian dollars)", 1), ("2026", 1), ("2026", 1), ("2025", 1), ("2026", 1), ("2025", 1)],
           [("Total revenue", 1), ("$18,538", 1), ("$17,453", 1), ("$16,985", 1), ("$53,951", 1), ("$49,396", 1)],
           [("Net income", 1), ("$5,434", 1), ("$5,100", 1), ("$4,900", 1), ("$17,318", 1), ("$15,000", 1)],
           [("ROE", 1), ("17.9%", 1), ("17.2%", 1), ("17.3%", 1), ("17.5%", 1), ("16.1%", 1)]])
# JPM: a section title, quarter labels and years on one header row; "Change" columns
JPM = _doc([[("", 1), ("QUARTERLY TRENDS", 5), ("SIX MONTHS ENDED JUNE 30,", 3)],
            [("", 4), ("2Q26 Change", 2), ("", 2), ("2026 Change", 1)],
            [("SELECTED INCOME STATEMENT DATA", 1), ("2Q26", 1), ("1Q26", 1), ("2Q25", 1), ("1Q26", 1), ("2Q25", 1),
             ("2026", 1), ("2025", 1), ("2025", 1)],
            [("TOTAL NET REVENUE", 1), ("57,347", 1), ("50,000", 1), ("49,825", 1), ("15", 1), ("28", 1), ("107,347", 1), ("95,000", 1), ("13", 1)],
            [("NET INCOME", 1), ("21,155", 1), ("16,494", 1), ("14,987", 1), ("28", 1), ("41", 1), ("37,649", 1), ("29,000", 1), ("30", 1)]],
           before="<p>(in millions)</p>")
# MSFT: years beside "Percentage Change Y/Y" on one header row
MSFT = _doc([[("Three Months Ended June 30", 6)],
             [("", 1), ("2026", 1), ("", 1), ("2025", 1), ("", 1), ("Percentage Change Y/Y", 1)],
             [("($ in millions)", 1), ("As Reported (GAAP)", 1), ("", 1), ("As Reported (GAAP)", 1), ("", 1), ("GAAP", 1)],
             [("Intelligent Cloud", 1), ("$39,306", 1), ("", 1), ("$29,878", 1), ("", 1), ("32%", 1)]],
            before="<p>Segment Revenue Constant Currency Reconciliation</p>")
# XOM: a year row shifted one column left of the figures — never trusted
XOM = _doc([[("(millions of dollars)", 1), ("Three Months Ended June 30", 2), ("Six Months Ended June 30,", 2)],
            [("2026", 1), ("2025", 1), ("2026", 1), ("2025", 1)],
            [("Additions to property, plant and equipment", 1), ("6,527", 1), ("6,283", 1), ("12,997", 1), ("12,181", 1)]])
# TM: quarters written "1Q (2025/4-6)" under fiscal-year headings
TM = _doc([[("", 1), ("FY2026", 5), ("FY2027", 1)],
           [("", 1), ("1Q (2025/4-6)", 1), ("2Q (2025/7-9)", 1), ("3Q (2025/10-12)", 1), ("4Q (2026/1-3)", 1),
            ("12 months (‘25/4-‘26/3)", 1), ("1Q (2026/4-6)", 1)],
           [("Capital Expenditures (billions of yen)", 1), ("392.4", 1), ("530.4", 1), ("594.2", 1), ("873.4", 1), ("2,390.6", 1), ("410.5", 1)]])
# undated columns of one kind: the first is not "current", the next not "year-ago"
UNDATED = _doc([[("", 1), ("Amount", 1), ("Amount", 1), ("Amount", 1)], [("Revenue", 1), ("300", 1), ("200", 1), ("100", 1)],
                [("Net income", 1), ("30", 1), ("20", 1), ("10", 1)]], before="<p>Selected data (in millions)</p>")


def test_year_and_quarter_header_rows(monkeypatch):
    ry = _core(monkeypatch, RY, form="6-K")
    check(f"RY-shaped: compared with the year-ago July, never the prior quarter ({ry.splitlines()[:30]})",
          "ROE: 17.9%" in ry and "vs 17.3% year-ago" in ry and "17.2% year-ago" not in ry)
    jpm = _core(monkeypatch, JPM, form="8-K")
    check(f"JPM-shaped: quarter labels date the columns; change columns are never a figure ({jpm[:1500]!r})",
          "21,155 million" in jpm and "vs 14,987 million year-ago" in jpm and ": 28 million" not in jpm and "vs 41" not in jpm)
    ms = _core(monkeypatch, MSFT, {"segments": [{"name": "Intelligent Cloud"}], "company_kpis": []}, form="8-K")
    check(f"MSFT-shaped: the segment's amounts, dated, with the change computed — the printed growth column is not a level ({ms[:1500]!r})",
          "$39,306 million" in ms and "vs $29,878 million year-ago" in ms and "+31.6%" in ms and "Intelligent Cloud: 32%" not in ms)
    cells = _cells(MSFT, "Intelligent Cloud")
    check(f"…and the years beside 'Percentage Change Y/Y' date the amount columns ({[c['heading'] for c in cells]})",
          "2026" in cells[0]["heading"] and "2025" in cells[1]["heading"])
    x = _cells(XOM, "Additions to property, plant and equipment")
    check(f"XOM-shaped: a misaligned year row is not attached ({[c['heading'] for c in x]})", all("2025" not in c["heading"] for c in x[:1]))
    p = period_from_heading("FY2027 1Q (2026/4-6)")
    check(f"TM-shaped: '1Q (2026/4-6)' is the quarter ended June 30, 2026 ({p})", p["period"] == "quarter" and p["ended"] == "June 30, 2026")
    tm = _core(monkeypatch, TM, form="6-K")
    check(f"…the latest quarter against the same quarter a year before ({tm[:1200]!r})",
          "410.5" in tm and "vs 392.4" in tm and "vs 530.4" not in tm)
    u = _core(monkeypatch, UNDATED)
    check(f"undated columns of one kind are not paired as current and year-ago ({u[:900]!r})", "vs 200" not in u)
    check("'Percentage Change Y/Y' is a change column", period_from_heading("Three Months Ended June 30 Percentage Change Y/Y")["period"] == "change")


# ---------------------------------------------------------------- leases
# AMZN: lease cost by quarter (a flow), then the lease note at two dates in two tables
AMZN_LEASES = _doc([[("", 1), ("Three Months Ended June 30,", 2)], [("", 1), ("2025", 1), ("2026", 1)],
                    [("Finance lease cost:", 1)], [("Interest on lease liabilities", 1), ("72", 1), ("108", 1)]],
                   [[("", 1), ("December 31, 2025", 1)], [("Gross lease liabilities", 1), ("$121,831", 1)],
                    [("Total long-term lease liabilities", 1), ("$87,339", 1)]],
                   [[("", 1), ("June 30, 2026", 1)], [("Gross lease liabilities", 1), ("$133,010", 1)],
                    [("Total long-term lease liabilities", 1), ("$94,338", 1)]],
                   [[("", 1), ("June 30, 2026", 1), ("June 30, 2025", 1)], [("Interest expense on long-term debt and lease liabilities", 1), ("258", 1), ("245", 1)],
                    [("Average lease liabilities", 1), ("3,037", 1), ("1,382", 1)]],
                   before="<p>(in millions)</p>")


def test_lease_balances_are_balances_and_the_latest(monkeypatch):
    t = _core(monkeypatch, AMZN_LEASES)
    lease = t[t.index("-- Lease liabilities"):t.index("-- KPI values")]
    check(f"AMZN-shaped: interest on leases is a cost, not a balance ({lease!r})", "Interest on lease" not in lease and "Interest expense" not in lease)
    check("…an average is not a balance", "Average lease" not in lease)
    check("…the latest date of each line, once", "Gross lease liabilities: $133,010 million as of June 30, 2026" in lease
          and "121,831" not in lease and "Total long-term lease liabilities: $94,338" in lease)


# ---------------------------------------------------------------- derived margins
# the income statement (quarter), an annual highlights table, a segment table
# and a percentage row: margins come from company-wide figures, quarter first
DERIVED = _doc([[("", 1), ("Twelve Months Ended June 30, 2026", 1), ("Twelve Months Ended June 30, 2025", 1)],
                [("Revenue", 1), ("$331,839", 1), ("$281,724", 1)], [("Net income", 1), ("$133,749", 1), ("$101,832", 1)]],
               [[("", 1), ("Three Months Ended June 30, 2026", 1), ("Three Months Ended June 30, 2025", 1)],
                [("Total revenue", 1), ("90,007", 1), ("76,441", 1)], [("Gross margin", 1), ("60,482", 1), ("52,427", 1)],
                [("Net income", 1), ("$35,766", 1), ("$27,233", 1)]],
               [[("Capital Markets", 1), ("Three Months Ended June 30, 2026", 1), ("Three Months Ended June 30, 2025", 1)],
                [("Total revenue", 1), ("4,212", 1), ("3,900", 1)], [("Net income", 1), ("1,544", 1), ("1,484", 1)]],
               [[("", 1), ("Three Months Ended June 30, 2026", 1), ("Three Months Ended June 30, 2025", 1)],
                [("Revenue", 1), ("$96,221", 1), ("$46,743", 1)], [("Gross margin", 1), ("75.0%", 1), ("72.4%", 1)]],
               [[("", 1), ("Three months ended June 30, 2026", 2)], [("", 1), ("RMB", 1), ("% of revenue", 1)],
                [("Cost of revenue", 1), ("148,197", 1), ("55.1%", 1)], [("Product development expenses", 1), ("16,406", 1), ("6.1%", 1)],
                [("Sales and marketing expenses", 1), ("57,825", 1), ("21.5%", 1)], [("Cost of revenue", 1), ("148,197", 1), ("55.1%", 1)]],
               before="<p>(in millions)</p>")


def test_derived_margins(monkeypatch):
    t = _core(monkeypatch, DERIVED, form="8-K")
    d = t[t.index("-- Derived metrics"):]
    lines = [l for l in d.splitlines() if l.startswith("- DERIVED Net margin")]
    check(f"MSFT/RY-shaped: the quarter's company-wide net margin comes first ({lines})",
          lines and "Net income $35,766 / Total revenue 90,007 (quarter)" in lines[0])
    check("…never a segment's (the largest revenue for the period is the company's)", "1,544" not in d)
    check(f"NVDA-shaped: a percentage is never divided by revenue ({d[:900]!r})", "75.0% / Revenue" not in d)
    check(f"BABA-shaped: each cost ratio counted once ({[l for l in d.splitlines() if 'residual' in l]})",
          "residual margin after the listed cost ratios: 17.3%" in d)


# ---------------------------------------------------------------- KPIs
KPIS = _doc([[("", 1), ("Fiscal Quarter Ended August 2, 2026", 1), ("Fiscal Quarter Ended August 3, 2025", 1)],
             [("Net revenue", 1), ("$29,591", 1), ("$15,952", 1)], [("Operating income", 1), ("$15,955", 1), ("$6,840", 1)],
             [("Operating income on non-GAAP basis", 1), ("$20,095", 1), ("$10,455", 1)]],
            [[("", 1), ("Three Months Ended June 30, 2026", 1), ("Three Months Ended June 30, 2025", 1)],
             [("Investing activities:", 1)], [("Additions to property, plant and equipment", 1), ("(6,527)", 1), ("(6,283)", 1)],
             [("Net cash provided by operating activities", 1), ("23,555", 1), ("11,550", 1)]],
            [[("", 1), ("Three Months Ended June 30, 2026", 1), ("Three Months Ended June 30, 2025", 1)],
             [("Total Cash Capex (Non-GAAP)", 1), ("6,787", 1), ("6,603", 1)]],
            before="<p>(in millions)</p>")


def test_kpis_keep_their_basis_and_scope(monkeypatch):
    prof = {"segments": [{"name": "Semiconductor solutions"}, {"name": "Infrastructure software"}],
            "company_kpis": [{"name": "Non-GAAP operating income / operating margin"}, {"name": "Operating margin of software segment"},
                             {"name": "Cash Capital Expenditures (Cash Capex, non-GAAP)"}, {"name": "Capital expenditures"}]}
    t = _core(monkeypatch, KPIS, prof, form="8-K")
    k = {l.split(":")[0]: l for l in t[t.index("-- KPI values"):].splitlines() if l.startswith("- KPI")}
    check(f"AVGO-shaped: a non-GAAP KPI is the non-GAAP row, never the GAAP margin ({k})",
          "$20,095" in k.get("- KPI Non-GAAP operating income / operating margin", ""))
    check("…a segment's margin is never the company's", "53.9%" not in k.get("- KPI Operating margin of software segment", "")
          and "53.9%" not in k.get("- KPI value not found", ""))
    check("XOM-shaped: the company's own non-GAAP cash capex row, not GAAP additions",
          "6,787" in k.get("- KPI Cash Capital Expenditures (Cash Capex, non-GAAP)", ""))
    check("…while a plain capex KPI still takes the cash-flow statement's line", "(6,527)" in k.get("- KPI Capital expenditures", ""))


# ---------------------------------------------------------------- units and labels
UNITS = _doc([[("", 1), ("13 Weeks Ended June 30, 2026", 1), ("13 Weeks Ended July 1, 2025", 1)],
              [("Number of company-owned restaurants:", 1)], [("The Cheesecake Factory", 1), ("216", 1), ("216", 1)],
              [("Earnings per share (EPS) – basic", 1), ("$4.24", 1), ("$3.86", 1)], [("– diluted", 1), ("4.23", 1), ("3.85", 1)],
              [("Revenues", 1), ("$1,029,633", 1), ("$955,825", 1)]],
             before="<p>Selected Operating Information (in thousands)</p>")
REPORTED = _doc([[("", 1), ("Q2 2026 Reported", 1), ("Q2 2026 Adjusted", 1), ("Q2 2026 CER", 1)],
                 [("Net sales", 1), ("78,000", 1), ("78,488", 1), ("79,100", 1)],
                 [("Operating profit", 1), ("27,061", 1), ("33,389", 1), ("34,000", 1)]], before="<p>DKK million</p>")


def test_counts_continued_labels_and_reported_columns(monkeypatch):
    t = _core(monkeypatch, UNITS)
    check(f"CAKE-shaped: a count of restaurants never inherits 'in thousands' ({t[:1200]!r})", "216 thousand" not in t)
    check("RY-shaped: '– diluted' continues 'Earnings per share (EPS)' — per share, not millions", "4.23 thousand" not in t and "4.23 million" not in t)
    r = parse_tables(UNITS)
    check("…its label names what it is", any(x["label"] == "Earnings per share (EPS) – diluted" for x in r))
    n = _core(monkeypatch, REPORTED, form="6-K")
    check(f"NVO-shaped: the reported column, not the adjusted or constant-currency one ({n[:600]!r})", "Net sales: 78,000" in n)


# ---------------------------------------------------------------- second pass of the 18-company check
AMZN_FCF = _doc([[("", 1), ("Twelve Months Ended June 30,", 2)], [("", 1), ("2025", 1), ("2026", 1)],
                 [("Net cash provided by (used in) operating activities", 1), ("$121,137", 1), ("$161,403", 1)],
                 [("Purchases of property and equipment, net of proceeds from sales and incentives", 1), ("(102,953)", 1), ("(169,007)", 1)],
                 [("Free cash flow", 1), ("$18,184", 1), ("$(7,604)", 1)]], before="<p>Free Cash Flow (in millions)</p>")
CHANGE_ONLY = _doc([[("", 1), ("Q2 2026", 1), ("Growth in DKK", 1), ("Y/Y % Change", 1)],
                    [("Net sales", 1), ("78,488", 1), ("2%", 1), ("3%", 1)], [("Operating profit", 1), ("27,061", 1), ("(19%)", 1), ("(16%)", 1)],
                    [("Net income per diluted share", 1), ("$5.75", 1), ("242%", 1), ("240%", 1)]], before="<p>DKK million</p>")
TM_FCF = _doc([[("", 1), ("Yen in millions For the first quarter ended June 30, 2026", 1), ("Yen in millions For the first quarter ended June 30, 2025", 1)],
               [("Net cash provided by (used in) operating activities", 1), ("536,551", 1), ("600,000", 1)]],
              [[("", 1), ("FY2026", 1), ("FY2027", 1)], [("", 1), ("1Q (2025/4-6)", 1), ("1Q (2026/4-6)", 1)],
               [("Capital Expenditures (billions of yen)", 1), ("(392.4)", 1), ("(410.5)", 1)]])


def test_negative_dollars_change_only_rows_and_fcf_units(monkeypatch):
    from markut.evidence.core import num_of
    from markut.evidence.edgar import is_figure_cell
    check("'$(7,604)' is minus 7,604", num_of("$(7,604)") == -7604 and num_of("$ (7,604)") == -7604 and is_figure_cell("$(7,604)"))
    a = _core(monkeypatch, AMZN_FCF, form="10-Q")
    check(f"AMZN-shaped: the filed free cash flow, negative this year ({a[:1600]!r})", "Free cash flow: $(7,604) million" in a and "vs $18,184" in a)
    c = _core(monkeypatch, CHANGE_ONLY, form="6-K")
    check(f"NVO/AMZN-shaped: a growth or change column is never shown as the figure ({c[:1400]!r})",
          "242%" not in c and ": 2% " not in c and "(19%)" not in c and "Growth in DKK" not in c)
    check("'Growth in DKK' heads a change column", period_from_heading("Growth in DKK")["period"] == "change")
    t = _core(monkeypatch, TM_FCF, form="6-K")
    check(f"TM-shaped: free cash flow never subtracts billions from millions ({[l for l in t.splitlines() if 'Free cash flow' in l]})",
          "DERIVED Free cash flow" not in t)


# KO: unit case volume is published only as a percent change — no level column
KO_VOLUME = _doc([[("", 1), ("Percent Change 2026 versus 2025 Three Months Ended July 3, 2026", 2)],
                  [("", 1), ("Unit Cases", 1), ("Price, Product & Geographic Mix", 1)],
                  [("Worldwide", 1), ("5%", 1), ("3%", 1)], [("Total net revenue", 1), ("12", 1), ("9", 1)],
                  [("Operating income", 1), ("20", 1), ("15", 1)]],
                 before="<p>Information about our volume growth (in millions)</p>")


def test_a_row_with_only_a_change_column_shows_it_as_a_change(monkeypatch):
    t = _core(monkeypatch, KO_VOLUME)
    w = [l for l in t.splitlines() if l.startswith("- Worldwide")]
    check(f"KO-shaped: the published growth is kept, said to be a change ({w})", w and "5%" in w[0] and "stated change" in w[0])
    check("…a change is never scaled to millions", "12 million" not in t and "20 million" not in t)
    check("…and never divided into a margin", "DERIVED Operating margin" not in t)


# ASML: a results table with only the latest two quarters
ASML_Q = _doc([[("", 1), ("Q1 2026", 1), ("Q2 2026", 1)], [("Total net sales", 1), ("8,767", 1), ("9,326", 1)],
               [("Net income", 1), ("2,757", 1), ("2,918", 1)]], before="<p>(in millions)</p>")


def test_a_prior_quarter_is_never_called_year_ago(monkeypatch):
    t = _core(monkeypatch, ASML_Q, form="6-K")
    rev = next((l for l in t.splitlines() if l.startswith("- Revenue")), "")
    check(f"ASML-shaped: Q2 against Q1 is the prior period, not the year-ago ({rev})",
          "9,326 million" in rev and "vs 8,767 million prior period (March 31, 2026)" in rev and "year-ago" not in rev)
    y = _core(monkeypatch, RY, form="6-K")
    check("…a true year-ago is still called year-ago", "vs 17.3% year-ago" in y)


# JPM's segment pages: "1Q26" / "2Q25" head only the over/(under) columns;
# the quarter labels and "$ O/(U)" share the next header row (no bare years)
JPM_LOB = _doc([[("Results for CCB", 1), ("", 3), ("1Q26", 2), ("2Q25", 2)],
                [("($ millions)", 1), ("2Q26", 1), ("1Q26", 1), ("2Q25", 1), ("$ O/(U)", 1), ("O/(U) %", 1), ("$ O/(U)", 1), ("O/(U) %", 1)],
                [("Net revenue", 1), ("$20,272", 1), ("$19,568", 1), ("$18,847", 1), ("$704", 1), ("4%", 1), ("$1,425", 1), ("8%", 1)],
                [("Net income", 1), ("$5,311", 1), ("$4,976", 1), ("$5,169", 1), ("$335", 1), ("7%", 1), ("$142", 1), ("3%", 1)]])


def test_quarter_labels_beside_over_under_columns(monkeypatch):
    t = _core(monkeypatch, JPM_LOB, form="8-K")
    rev = next((l for l in t.splitlines() if l.startswith("- Revenue") or l.startswith("- Net revenue")), "")
    check(f"JPM-shaped: 2Q26 against 2Q25, never an over/(under) column ({rev})",
          "$20,272" in rev and "vs $18,847" in rev and "year-ago" in rev and "$704" not in t)
    check("'$ O/(U)' under '1Q26' is a change column", period_from_heading("1Q26 $ O/(U)")["period"] == "change")


# MSFT: As Reported (GAAP) | Impact from OpenAI* | As Adjusted (non-GAAP) per year
MSFT_RECON = _doc([[("Three Months Ended June 30", 7)],
                   [("", 1), ("2026", 3), ("2025", 3)],
                   [("($ in millions)", 1), ("As Reported (GAAP)", 1), ("Impact from OpenAI*", 1), ("As Adjusted (non-GAAP)", 1),
                    ("As Reported (GAAP)", 1), ("Impact from OpenAI*", 1), ("As Adjusted (non-GAAP)", 1)],
                   [("Revenue", 1), ("$90,007", 1), ("$0", 1), ("$90,007", 1), ("$76,441", 1), ("$0", 1), ("$76,441", 1)],
                   [("Net Income", 1), ("$35,766", 1), ("$(480)", 1), ("$35,286", 1), ("$27,233", 1), ("$1,575", 1), ("$28,808", 1)]])


def test_the_reported_column_wins_every_time(monkeypatch):
    import random
    lines = set()
    for seed in range(6):                                   # set order varied between runs before
        random.seed(seed)
        t = _core(monkeypatch, MSFT_RECON, form="8-K")
        lines.add(next((l for l in t.splitlines() if l.startswith("- Net income") or l.startswith("- Net Income")), ""))
    check(f"MSFT-shaped: As Reported (GAAP), never the 'Impact from' reconciling column ({lines})",
          len(lines) == 1 and "$35,766" in next(iter(lines)) and "vs $27,233" in next(iter(lines)))


# a small filer's 10-Q: the values sit one column right of their date headings,
# so the first overlaps the wide "Three Months Ended" more than "June 30, 2026"
OFFSET = ("<html><body><p>Condensed Consolidated Statements of Operations</p><table>"
          '<tr><td colspan="2"></td><td colspan="6">Three Months Ended</td><td colspan="2"></td><td colspan="6">Six Months Ended</td></tr>'
          '<tr><td colspan="2"></td><td colspan="2">June 30, 2026</td><td colspan="2"></td><td colspan="2">June 30, 2025</td><td colspan="2"></td>'
          '<td colspan="2">June 30, 2026</td><td colspan="2"></td><td colspan="2">June 30, 2025</td></tr>'
          '<tr><td colspan="3">Revenue</td><td colspan="2">50,000</td><td colspan="2"></td><td colspan="2">40,000</td><td colspan="2"></td>'
          '<td colspan="2">90,000</td><td colspan="2"></td><td colspan="2">80,000</td></tr>'
          '<tr><td colspan="3">Net loss</td><td colspan="2">(156,721)</td><td colspan="2"></td><td colspan="2">(148,854)</td><td colspan="2"></td>'
          '<td colspan="2">(311,523)</td><td colspan="2"></td><td colspan="2">(323,628)</td></tr></table></body></html>')


def test_values_offset_from_their_date_headings(monkeypatch):
    cells = _cells(OFFSET, "Net loss")
    check(f"each value keeps its own date heading ({[c['heading'] for c in cells]})",
          "June 30, 2026" in cells[0]["heading"] and "June 30, 2025" in cells[1]["heading"])
    t = _core(monkeypatch, OFFSET)
    nl = next((l for l in t.splitlines() if l.startswith("- Net income")), "")
    check(f"the current quarter is current, the year-old one the comparison ({nl})",
          "(156,721)" in nl and "vs (148,854)" in nl and "June 30, 2026" in nl)


def test_a_figure_dated_long_before_its_filing_is_never_current(monkeypatch):
    # whatever misreads the columns, a 10-Q filed in August 2026 has no current quarter ending in 2025
    old = OFFSET.replace("June 30, 2026", "June 30, 2024")
    t = _core(monkeypatch, old)
    check(f"no figure dated more than seven months before the filing is shown as current ({[l[:100] for l in t.splitlines() if 'Net income' in l]})",
          not any(l.startswith("- Net income") for l in t.splitlines()))


# ---------------------------------------------------------------- financial companies
_Q = [("", 1), ("Three Months Ended June 30, 2026", 1), ("Three Months Ended June 30, 2025", 1)]
BANK = _doc([_Q, [("Total interest income", 1), ("$350,000", 1), ("$330,000", 1)], [("Total interest expense", 1), ("120,000", 1), ("115,000", 1)],
             [("Net interest income", 1), ("230,000", 1), ("215,000", 1)], [("Credit loss expense", 1), ("5,000", 1), ("4,000", 1)],
             [("Total non-interest income", 1), ("40,000", 1), ("38,000", 1)], [("Total non-interest expense", 1), ("110,000", 1), ("105,000", 1)],
             [("Income before income taxes", 1), ("155,000", 1), ("144,000", 1)], [("Net income", 1), ("$119,327", 1), ("$118,403", 1)]],
            before="<p>Consolidated Statements of Income (In thousands)</p>")
INSURER = _doc([_Q, [("Net premiums earned", 1), ("3,100,000", 1), ("2,900,000", 1)], [("Net investment income", 1), ("380,000", 1), ("350,000", 1)],
                [("Total revenues", 1), ("3,600,000", 1), ("3,350,000", 1)], [("Losses and loss expenses", 1), ("1,900,000", 1), ("1,800,000", 1)],
                [("Income before income taxes", 1), ("580,000", 1), ("510,000", 1)], [("Net income to common stockholders", 1), ("$452,261", 1), ("$401,288", 1)]],
               before="<p>Consolidated Statements of Income (In thousands)</p>")
BDC = _doc([_Q, [("Interest income", 1), ("70,000", 1), ("68,000", 1)], [("Total investment income", 1), ("80,000", 1), ("77,000", 1)],
            [("Net investment income", 1), ("35,000", 1), ("33,000", 1)],
            [("Net increase in net assets resulting from operations", 1), ("30,000", 1), ("25,000", 1)]],
           before="<p>Consolidated Statements of Operations (In thousands)</p>")


def test_banks_insurers_and_funds_have_a_top_line_and_a_bottom_line(monkeypatch):
    from markut import readiness
    b = _core(monkeypatch, BANK)
    check(f"bank-shaped: net interest income and non-interest income, and their sum as revenue ({b[:1500]!r})",
          "Net interest income: 230,000 thousand" in b and "non-interest income" in b.lower() and "DERIVED Revenue: 270,000" in b
          and "vs 253,000 year-ago" in b)
    i = _core(monkeypatch, INSURER)
    check(f"insurer-shaped: total revenues and net income to common stockholders ({i[:1200]!r})",
          "Total revenues: 3,600,000 thousand" in i and "Net income to common stockholders: $452,261" in i)
    f = _core(monkeypatch, BDC)
    check(f"fund-shaped: total investment income is the top line, the increase in net assets the bottom ({f[:1200]!r})",
          "Total investment income: 80,000" in f and "Net increase in net assets resulting from operations: 30,000" in f)
    for core in (b, i, f):
        r = readiness.score("FIN", {"filings": True, "filer_type": "domestic", "annual": {"form": "10-K", "filing_date": "2026-02-27"},
                                    "interim": {"form": "10-Q", "filing_date": "2026-08-07"}, "earnings": None}, core,
                            "- Price (current): $25.00\n- Market cap: $5.0B", today="2026-10-11")
        kinds = {x["type"] for x in r["issues"]}
        check(f"…the readiness check finds both lines ({kinds})", not kinds & {"no_revenue", "no_net_income", "no_year_ago", "undated"})


def test_operating_cash_flow_in_any_wording(monkeypatch):
    from markut.evidence.core import _is_ocf
    for label in ("NET CASH FLOWS FROM OPERATING ACTIVITIES", "Net cash provided (used) by operating activities",
                  "Net cash (used in) provided by operating activities", "Net operating cash", "Net cash provided by operating activities",
                  "Cash provided by (used in) operating activities"):
        check(f"'{label}' is operating cash flow", _is_ocf({"label": label}))
    for label in ("Net cash used in investing activities", "Net cash provided by operating activities - discontinued operations",
                  "Net cash provided by financing activities"):
        check(f"'{label}' is not", not _is_ocf({"label": label}))
    cf = _doc([[("", 1), ("Six Months Ended June 30, 2026", 1), ("Six Months Ended June 30, 2025", 1)],
               [("NET CASH FLOWS FROM OPERATING ACTIVITIES", 1), ("1,971", 1), ("1,766", 1)],
               [("Utility construction expenditures", 1), ("(2,700)", 1), ("(2,400)", 1)]],
              before="<p>Consolidated Statement of Cash Flows (Millions of Dollars)</p>")
    t = _core(monkeypatch, cf)
    check(f"…and lands in the cash-flow summary ({[l[:90] for l in t.splitlines() if 'Operating cash flow' in l]})",
          any(l.startswith("- Operating cash flow") and "1,971" in l for l in t.splitlines()))


def test_top_and_bottom_lines_in_other_standards_wording():
    import re
    from markut.evidence.core import INCOME_ROWS
    rev, ni = INCOME_ROWS[0][1], INCOME_ROWS[3][1]
    for label in ("metal sales", "sales", "total revenues"):
        check(f"'{label}' is revenue", re.search(rev, label))
    for label in ("cost of sales", "production cost of sales", "product sales"):
        check(f"'{label}' is not the total revenue line", not re.search(rev, label))
    for label in ("earnings (loss)", "earnings", "profit for the period", "profit attributable to owners of the parent",
                  "net earnings attributable to common shareholders"):
        check(f"'{label}' is the bottom line", re.search(ni, label))
    for label in ("earnings (loss) before tax", "earnings per share", "profit before tax"):
        check(f"'{label}' is not", not re.search(ni, label))


# one document: the statement says "(In millions)", a segment table says nothing
DOC_SCALE = _doc([_Q, [("Net operating revenues", 1), ("$13,380", 1), ("$12,535", 1)], [("Net income", 1), ("$4,425", 1), ("$3,810", 1)]],
                 before="<p>CONDENSED CONSOLIDATED STATEMENTS OF INCOME (In millions except per share data)</p>")[:-14] + \
            _doc([[("", 1), ("Six Months Ended July 3, 2026", 1), ("Six Months Ended June 27, 2025", 1)],
                  [("Total net operating revenues", 1), ("25,852", 1), ("24,307", 1)], [("Operating income", 1), ("8,900", 1), ("8,100", 1)]],
                 before="<p>Six-month results</p>")[12:]
# a small filer that states no scale anywhere: its figures are whole dollars
WHOLE = _doc([_Q, [("Total revenue", 1), ("6,712,805", 1), ("3,085,439", 1)], [("Net loss", 1), ("(1,204,330)", 1), ("(2,011,100)", 1)]],
             before="<p>CONDENSED STATEMENTS OF OPERATIONS (Unaudited)</p>")


def test_units_from_the_document_or_said_to_be_whole(monkeypatch):
    t = _core(monkeypatch, DOC_SCALE)
    seg = next((l for l in t.splitlines() if "25,852" in l), "")
    check(f"KO-shaped: a table with no units takes the document's one stated scale, and says so ({seg})",
          "25,852 million" in seg and "the document's stated scale" in seg and "(as printed" not in seg)
    w = _core(monkeypatch, WHOLE)
    rev = next((l for l in w.splitlines() if "6,712,805" in l), "")
    check(f"a document that states no scale: whole units, said plainly ({rev})", "whole units" in rev and "(as printed" not in rev
          and "6,712,805 million" not in rev and "6,712,805 thousand" not in rev)


def test_the_income_summary_keeps_the_key_lines_when_it_is_full(monkeypatch):
    many = _doc([_Q, [("Income before income taxes", 1), ("155,000", 1), ("144,000", 1)], [("Adjusted EBITDA", 1), ("200,000", 1), ("180,000", 1)],
                 [("Total revenues", 1), ("900,000", 1), ("850,000", 1)], [("Net income", 1), ("119,327", 1), ("118,403", 1)]],
                before="<p>Consolidated Statements of Income (In thousands)</p>")
    monkeypatch.setitem(core.MAX_LINES, "income", 2)
    t = _core(monkeypatch, many)
    inc = t[t.index("-- Income statement summary --"):t.index("-- KPI / operating")]
    check(f"revenue and net income before pre-tax income and EBITDA ({inc!r})",
          "Total revenues: 900,000" in inc and "Net income: 119,327" in inc and "Pre-tax" not in inc and "EBITDA" not in inc)


def test_the_same_figure_from_several_tables_is_one_line(monkeypatch):
    # the report prints Q2 net sales in three tables; the summary is not
    # filled with copies while net income and EPS fall off the end
    t1 = [_Q, [("Net sales", 1), ("78,488", 1), ("74,000", 1)], [("Operating profit", 1), ("27,061", 1), ("26,000", 1)]]
    t2 = [[("", 1), ("Q2 2026", 1)], [("Net sales", 1), ("78,488", 1)], [("Operating profit", 1), ("27,061", 1)]]
    t3 = [[("", 1), ("Reported", 1)], [("Net sales", 1), ("78,488", 1)], [("Operating profit", 1), ("27,061", 1)]]
    t4 = [_Q, [("Net sales", 1), ("78,488", 1), ("74,000", 1)], [("Net profit", 1), ("20,000", 1), ("21,500", 1)],
          [("Diluted earnings per share", 1), ("4.75", 1), ("5.96", 1)]]
    monkeypatch.setitem(core.MAX_LINES, "income", 4)
    t = _core(monkeypatch, _doc(t1, t2, t3, t4, before="<p>DKK million</p>"))
    inc = t[t.index("-- Income statement summary --"):t.index("-- KPI / operating")]
    check(f"one revenue line (the one with its year-ago), and room for net income and EPS ({inc!r})",
          inc.count("78,488 million (") == 1 and "vs 74,000" in inc and "20,000" in inc and "4.75" in inc)
