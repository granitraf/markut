"""Balance-sheet figures in the core figures (2026-10-11, after run #29).

The evidence-quality block showed AAPL with no balance-sheet figure at all:
the code never extracted one, and two parser faults hid the tables anyway —
a full-width "ASSETS:" row was stacked into the column headings (Apple's
balance sheet lost its dates and its current / non-current groups), and a
statement title and units printed in a text-only table above the statement
were never read (TSM's interim statements lost "In Thousands of New Taiwan
Dollars"). Now cash, receivables, inventories, totals, each debt row and
equity are core figures, dated "as of", with a derived total debt.
Offline; AAPL-shaped, TSM-shaped and synthetic US filer HTML."""
from markut.evidence import core, quality
from markut.evidence.edgar import parse_tables


def check(label, condition):
    assert condition, label


def _core(monkeypatch, html, form="10-Q", kpis=("Inventories",)):
    monkeypatch.setattr(core, "core_sources", lambda fs: [{"form": form, "name": "doc.htm", "filing_date": "2026-07-31", "url": "u",
                                                           "html": html, "text": "", "headline_period": "quarter"}])
    return core.extract_core({}, {"segments": [], "company_kpis": [{"name": k} for k in kpis]}, None)["text"]


def _block(t):
    return t[t.index("-- Balance sheet summary --"):t.index("-- Lease liabilities")]


def _row(label, a, b, span=3):
    return (f'<tr><td colspan="{span}">{label}</td><td colspan="2">{a}</td><td colspan="4"></td>'
            f'<td colspan="2">{b}</td><td></td></tr>')


# Apple's 8-K / 10-Q layout: dates over columns 3-6 and 9-12, then a
# full-width "ASSETS:" row, then "Current assets:" in the label column
AAPL_BS = ("<html><body><p>CONDENSED CONSOLIDATED BALANCE SHEETS (Unaudited)</p>"
           "<p>(In millions, except number of shares, which are reflected in thousands, and par value)</p><table>"
           '<tr><td colspan="3"></td><td colspan="3">June 27, 2026</td><td colspan="3"></td><td colspan="3">September 27, 2025</td></tr>'
           '<tr><td colspan="12">ASSETS:</td></tr><tr><td colspan="3">Current assets:</td></tr>'
           + _row("Cash and cash equivalents", "$39,544", "$35,934") + _row("Marketable securities", "22,855", "18,763")
           + _row("Accounts receivable, net", "31,398", "39,777") + _row("Inventories", "11,092", "5,718")
           + _row("Total current assets", "149,818", "147,957")
           + '<tr><td colspan="3">Non-current assets:</td></tr>' + _row("Marketable securities", "84,118", "77,723")
           + _row("Total non-current assets", "233,448", "211,284") + _row("Total assets", "$383,266", "$359,241")
           + '<tr><td colspan="12">LIABILITIES AND SHAREHOLDERS’ EQUITY:</td></tr><tr><td colspan="3">Current liabilities:</td></tr>'
           + _row("Accounts payable", "$64,525", "$69,860") + _row("Commercial paper", "1,997", "7,979") + _row("Term debt", "11,007", "12,350")
           + _row("Total current liabilities", "149,326", "165,631")
           + '<tr><td colspan="3">Non-current liabilities:</td></tr>' + _row("Term debt", "71,340", "78,328")
           + _row("Total non-current liabilities", "126,420", "119,877") + _row("Total liabilities", "275,746", "285,508")
           + _row("Total shareholders’ equity", "107,520", "73,733") + "</table></body></html>")


def _tsm_row(label, *vals):
    cells = "".join(f'<td>{v}</td><td></td>' for v in vals)
    return f"<tr><td>{label}</td>{cells}</tr>"


# TSM's interim report: title and units in a text-only table of their own,
# then Amount / % sub-columns under three dates, a bare "TOTAL" for total assets
TSM_BS = ("<html><body><table><tr><td>Taiwan Semiconductor Manufacturing Company Limited and Subsidiaries</td></tr>"
          "<tr><td>CONSOLIDATED BALANCE SHEETS</td></tr><tr><td>(In Thousands of New Taiwan Dollars)</td></tr></table><table>"
          '<tr><td></td><td colspan="3">June 30, 2026</td><td></td><td colspan="3">December 31, 2025</td><td></td><td colspan="3">June 30, 2025</td></tr>'
          '<tr><td></td><td>Amount</td><td></td><td>%</td><td></td><td>Amount</td><td></td><td>%</td><td></td><td>Amount</td><td></td><td>%</td></tr>'
          "<tr><td>ASSETS</td></tr><tr><td>CURRENT ASSETS</td></tr>"
          + _tsm_row("Cash and cash equivalents (Note 6)", "$ 3,134,218,213", "33", "$ 2,767,856,402", "35", "$ 2,364,524,340", "34")
          + _tsm_row("Notes and accounts receivable, net (Note 11)", "435,762,477", "5", "279,051,553", "4", "260,000,000", "4")
          + _tsm_row("Inventories (Note 12)", "385,524,542", "4", "288,109,485", "4", "270,000,000", "4")
          + _tsm_row("Total current assets", "4,565,700,742", "49", "3,817,130,817", "48", "3,600,000,000", "50")
          + "<tr><td>NONCURRENT ASSETS</td></tr>"
          + _tsm_row("Total noncurrent assets", "4,809,953,985", "51", "4,115,893,061", "52", "3,500,000,000", "50")
          + _tsm_row("TOTAL", "$ 9,375,654,727", "100", "$ 7,933,023,878", "100", "$ 7,100,000,000", "100")
          + "<tr><td>CURRENT LIABILITIES</td></tr>"
          + _tsm_row("Long-term liabilities - current portion (Notes 17, 18 and 29)", "167,409,865", "2", "136,925,710", "2", "120,000,000", "2")
          + _tsm_row("Total current liabilities", "1,857,761,825", "20", "1,458,019,289", "18", "1,400,000,000", "20")
          + "<tr><td>NONCURRENT LIABILITIES</td></tr>"
          + _tsm_row("Bonds payable (Notes 17 and 29)", "815,036,716", "9", "856,227,503", "11", "900,000,000", "13")
          + _tsm_row("Long-term bank loans (Note 18)", "49,226,958", "1", "39,834,496", "1", "30,000,000", "-")
          + _tsm_row("Total liabilities", "2,901,183,746", "31", "2,472,228,595", "31", "2,400,000,000", "34")
          + _tsm_row("Equity attributable to shareholders of the parent", "6,432,518,334", "69", "5,419,595,994", "68", "4,700,000,000", "66")
          + _tsm_row("Total equity", "6,474,470,981", "69", "5,460,795,283", "69", "4,740,000,000", "66")
          + "</table></body></html>")

# a US filer: an all-caps full-width section row, "Long-term debt, net" and
# "Current portion of long-term debt", "Total stockholders' equity"
US_BS = ("<html><body><p>CONDENSED CONSOLIDATED BALANCE SHEETS (in thousands)</p><table>"
         '<tr><td colspan="3"></td><td colspan="3">July 4, 2026</td><td colspan="3"></td><td colspan="3">January 3, 2026</td></tr>'
         '<tr><td colspan="12">ASSETS</td></tr><tr><td colspan="3">Current assets:</td></tr>'
         + _row("Cash and cash equivalents", "$ 52,000", "$ 61,000") + _row("Accounts receivable", "18,000", "21,000")
         + _row("Total current assets", "120,000", "130,000") + _row("Total assets", "$ 2,900,000", "$ 2,850,000")
         + '<tr><td colspan="3">Current liabilities:</td></tr>'
         + _row("Current portion of long-term debt", "15,000", "15,000") + _row("Total current liabilities", "300,000", "310,000")
         + _row("Long-term debt, net", "480,000", "500,000") + _row("Total liabilities", "2,500,000", "2,480,000")
         + _row("Total stockholders’ equity", "400,000", "370,000") + "</table></body></html>")
INCOME_ONLY = ("<html><body><p>CONDENSED CONSOLIDATED STATEMENTS OF OPERATIONS (in millions)</p><table>"
               "<tr><td></td><td>Three Months Ended June 30, 2026</td><td>Three Months Ended June 30, 2025</td></tr>"
               "<tr><td>Revenue</td><td>1,000</td><td>900</td></tr><tr><td>Net income</td><td>100</td><td>90</td></tr></table></body></html>")


def test_a_full_width_section_row_never_erases_the_column_dates():
    rows = {r["label"] + "|" + r["group"]: r for r in parse_tables(AAPL_BS)}
    cash = rows.get("Cash and cash equivalents|Current assets")
    check(f"AAPL-shaped: 'ASSETS:' is a section, not a column heading ({list(rows)[:4]})",
          cash and "June 27, 2026" in cash["cells"][0]["heading"] and "September 27, 2025" in cash["cells"][1]["heading"])
    check("…and the two 'Term debt' rows keep their groups",
          "Term debt|Current liabilities" in rows and "Term debt|Non-current liabilities" in rows)
    us = {r["label"]: r for r in parse_tables(US_BS)}
    check("synthetic US filer: an all-caps 'ASSETS' row does the same", "July 4, 2026" in us["Total assets"]["cells"][0]["heading"])


def test_units_in_a_title_table_above_the_statement_are_read():
    rows = parse_tables(TSM_BS)
    cash = next(r for r in rows if r["label"].startswith("Cash and cash"))
    check(f"TSM-shaped: the units and currency printed in the title table reach the statement ({cash['units']!r}, {cash['currency']!r})",
          cash["units"] == "thousands" and cash["currency"] == "TWD")
    check("…and the title is its caption", "BALANCE SHEETS" in cash["caption"].upper())


def test_the_balance_sheet_summary(monkeypatch):
    a = _block(_core(monkeypatch, AAPL_BS, form="8-K"))
    check(f"AAPL-shaped: cash, receivables, inventories, totals and equity, dated as of ({a[:900]!r})",
          all(x in a for x in ("Cash and cash equivalents", "39,544", "as of June 27, 2026", "35,934", "as of September 27, 2025",
                               "Accounts receivable, net", "Inventories", "Total current assets", "Total assets", "Total liabilities",
                               "Total shareholders’ equity", "107,520", "point-in-time balance")))
    check("…each debt row with its group", "Commercial paper" in a and "Term debt [Current liabilities]: 11,007" in a
          and "Term debt [Non-current liabilities]: 71,340" in a)
    check("…a derived total debt names every row it adds", "DERIVED Total debt: 84,344" in a and "Commercial paper 1,997" in a)
    check("…receivables are money, not a count of 'accounts'", "Accounts receivable, net [Current assets]: 31,398 million" in a)
    check("…marketable securities, current and non-current", "Marketable securities [Current assets]: 22,855" in a
          and "Marketable securities [Non-current assets]: 84,118" in a)
    t = _block(_core(monkeypatch, TSM_BS, form="6-K"))
    check(f"TSM-shaped: amounts (never the % column), in TWD thousands ({t[:900]!r})",
          "3,134,218,213" in t and "TWD thousands" in t and ": 33 " not in t)
    check("…'Notes and accounts receivable' is a line item, not a footnote row", "Notes and accounts receivable, net" in t and "435,762,477" in t)
    check("…every amount names its currency", "(TWD thousands; GAAP; point-in-time balance)" in t and "(thousands; GAAP" not in t)
    check("…and never wears a '$' (the packet labels TWD, never '$')", "$ 3,134,218,213" not in t and "Total assets (printed as TOTAL): 9,375,654,727" in t)
    check("…a US filer's dollars keep theirs", "Total assets: $383,266 million" in a)
    check("…the bare TOTAL after the non-current assets is total assets", "Total assets" in t and "9,375,654,727" in t)
    check("…both equity lines", "Equity attributable to shareholders of the parent" in t and "Total equity" in t)
    check("…total debt from the current portion, bonds and bank loans", "DERIVED Total debt: 1,031,673,539" in t)
    u = _block(_core(monkeypatch, US_BS))
    check(f"synthetic US filer ({u[:600]!r})", "52,000" in u and "Current portion of long-term debt" in u
          and "Long-term debt, net" in u and "Total stockholders’ equity" in u and "DERIVED Total debt: 495,000" in u)
    n = _block(_core(monkeypatch, INCOME_ONLY))
    check("a release with no balance sheet says so", "none found" in n)


def test_the_quality_block_sees_the_balance_sheet(monkeypatch):
    text = _core(monkeypatch, AAPL_BS, form="8-K", kpis=())
    b, _ = quality.quality_block("[FILINGS]\n" + text, [])
    check(f"AAPL-shaped: balance found ({b.splitlines()[2]!r})", "balance ✓" in b)
    text = _core(monkeypatch, INCOME_ONLY, kpis=())
    b, _ = quality.quality_block("[FILINGS]\n" + text, [])
    check("no balance sheet, no lease line: balance not found", "balance ✗" in b)


# ---------------------------------------------------------------- more layouts (live sweep, 2026-10-11)
def _table(head_rows, rows, before=""):
    def tr(cells):
        return "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"
    return f"<html><body>{before}<table>" + "".join(tr(h) for h in head_rows) + "".join(tr(r) for r in rows) + "</table></body></html>"


# Amazon: the older column first, only the latest says "(unaudited)"
AMZN_BS = _table([["", "December 31, 2025", "June 30, 2026 (unaudited)"]],
                 [["Current assets:"], ["Cash and cash equivalents", "$86,810", "$78,213"], ["Inventories", "38,325", "38,184"],
                  ["Total current assets", "229,083", "238,000"], ["Total assets", "$818,042", "$850,000"],
                  ["Long-term debt", "65,648", "60,000"], ["Total stockholders’ equity", "411,065", "450,000"]],
                 "<p>Consolidated Balance Sheets</p><p>(in millions)</p>")
# Cheesecake 10-Q: the year on its own header row, "(Unaudited)" under one column
CAKE_BS = _table([["", "June 30,", "December 30,"], ["", "2026", "2025"], ["", "(Unaudited)", ""]],
                 [["ASSETS"], ["Current assets:"], ["Cash and cash equivalents", "$195,170", "$215,729"],
                  ["Total current assets", "400,000", "420,000"], ["Total assets", "$3,294,174", "$3,261,672"],
                  ["Long-term debt", "562,895", "630,074"], ["Total liabilities", "2,775,216", "2,825,245"],
                  ["Total stockholders’ equity", "518,958", "436,427"]],
                 "<p>CONDENSED CONSOLIDATED BALANCE SHEETS</p><p>(In thousands, except share data)</p>")
# Cheesecake release: the statement named only in the table's first header cell
CAKE_REL = _table([["Selected Consolidated Balance Sheet Information", "June 30, 2026", "December 30, 2025"]],
                  [["Cash and cash equivalents", "$195,170", "$215,729"], ["Long-term debt, net of issuance costs", "562,895", "630,074"],
                   ["Stockholders’ equity", "518,958", "436,427"]], "<p>(in thousands)</p>")
# Exxon: "(millions of dollars, unless noted)", a "(Preliminary)" note row, and
# a stale first-quarter release beside the second-quarter 10-Q
XOM_REL = _table([["", "March 31, 2026", "December 31, 2025"], ["(Preliminary)"]],
                 [["Cash and cash equivalents", "8,435", "10,681"], ["Total current assets", "97,787", "83,382"],
                  ["Total Assets", "464,410", "448,980"], ["Long-term debt", "33,130", "34,241"], ["Total Liabilities", "203,414", "182,354"],
                  ["Total Equity", "260,996", "266,626"]], "<p>Condensed consolidated balance sheet (in millions)</p>")
XOM_10Q = _table([["(millions of dollars, unless noted)", "June 30, 2026", "December 31, 2025"]],
                 [["ASSETS"], ["Current assets"], ["Cash and cash equivalents", "10,588", "10,681"], ["Total current assets", "100,793", "83,382"],
                  ["Total Assets", "464,482", "448,980"], ["Long-term debt", "32,229", "34,241"], ["Total Liabilities", "198,371", "182,354"],
                  ["Total Equity", "266,111", "266,626"]], "<p>CONDENSED CONSOLIDATED BALANCE SHEET</p>")
BANK_BS = _table([["", "Jun 30, 2026", "Dec 31, 2025"]],
                 [["Assets"], ["Cash and due from banks", "$ 22,000", "$ 23,000"], ["Total assets", "4,500,000", "4,300,000"],
                  ["Long-term debt", "420,000", "410,000"], ["Total liabilities", "4,150,000", "3,960,000"],
                  ["Total stockholders’ equity", "350,000", "340,000"]], "<p>Consolidated balance sheets (in millions)</p>")


def _core_many(monkeypatch, docs):
    monkeypatch.setattr(core, "core_sources", lambda fs: [{"form": form, "name": name, "filing_date": date, "url": "u", "html": html,
                                                           "text": "", "headline_period": "quarter"} for form, name, date, html in docs])
    return _block(core.extract_core({}, {"segments": [], "company_kpis": []}, None)["text"])


def test_more_balance_sheet_layouts(monkeypatch):
    a = _core_many(monkeypatch, [("8-K", "ex99.htm", "2026-07-30", AMZN_BS)])
    check(f"AMZN-shaped: the '(unaudited)' column is the latest balance, not a different kind of column ({a[:400]!r})",
          "Cash and cash equivalents [Current assets]: $78,213 million as of June 30, 2026 vs $86,810 million as of December 31, 2025" in a)
    c = _core_many(monkeypatch, [("10-Q", "10q.htm", "2026-07-31", CAKE_BS)])
    check(f"CAKE-shaped 10-Q: a year on its own header row dates the column ({c[:400]!r})",
          "$195,170 thousand as of June 30, 2026 vs $215,729 thousand as of December 30, 2025" in c and "Total stockholders’ equity" in c)
    r = _core_many(monkeypatch, [("8-K", "ex99.htm", "2026-07-30", CAKE_REL)])
    check(f"CAKE-shaped release: a table named in its first header cell is a balance sheet ({r[:400]!r})",
          "195,170" in r and "Long-term debt, net of issuance costs" in r and "DERIVED Total debt: 562,895" in r)
    x = _core_many(monkeypatch, [("8-K", "q1.htm", "2026-05-01", XOM_REL), ("10-Q", "q2.htm", "2026-08-03", XOM_10Q)])
    check(f"XOM-shaped: one balance sheet, the latest — never a stale release beside the 10-Q ({x[:600]!r})",
          "as of June 30, 2026" in x and "as of March 31, 2026" not in x and "DERIVED Total debt: 32,229" in x)
    check("…'(millions of dollars, unless noted)' is the units", "10,588 million" in x and "as printed" not in x)
    x1 = _core_many(monkeypatch, [("8-K", "q1.htm", "2026-05-01", XOM_REL)])
    check(f"…and '(Preliminary)' is a note, not a group ({x1[:300]!r})", "Preliminary" not in x1)
    b = _core_many(monkeypatch, [("10-Q", "10q.htm", "2026-08-01", BANK_BS)])
    check(f"bank-shaped: 'Cash and due from banks' is the cash line ({b[:300]!r})", "Cash and due from banks" in b and "22,000" in b)


# a bank's 10-Q: the balance sheet in two titled tables, a derivatives table
# with "Total equity contracts", and a parent-only table — one document
BANK_DOC = ("<html><body><p>Selected Consolidated balance sheets data (in millions)</p>"
            + _table([["", "Jun 30, 2026", "Dec 31, 2025"]], [["Cash and due from banks", "$ 24,720", "$ 21,742"],
                                                              ["Total assets", "$ 5,015,069", "$ 4,424,900"]])[12:-14]
            + "<p>Selected Consolidated balance sheets data (continued) (in millions)</p>"
            + _table([["", "Jun 30, 2026", "Dec 31, 2025"]], [["Short-term borrowings", "72,430", "64,776"], ["Long-term debt", "460,523", "435,206"],
                                                              ["Total liabilities", "4,640,471", "4,062,462"], ["Stockholders’ equity", "374,598", "362,438"]])[12:-14]
            + "<p>Derivative receivables and payables (in millions)</p>"
            + _table([["", "Jun 30, 2026", "Dec 31, 2025"]], [["Total assets", "$ 41,006", "$ 43,295"], ["Total liabilities", "$ 30,287", "$ 28,642"],
                                                              ["Total equity contracts", "(134,116)", "(91,856)"]])[12:-14]
            + "<p>Consolidated balance sheets (in millions)</p>"
            + _table([["", "Jun 30, 2026", "Dec 31, 2025"]], [["Cash and due from banks", "$ 24,720", "$ 21,742"], ["Total assets", "$ 5,015,069", "$ 4,424,900"],
                                                              ["Short-term borrowings (included $ 29,967 and $ 32,460 at fair value)", "72,430", "64,776"],
                                                              ["Long-term debt (included $ 156,056 and $ 134,559 at fair value)", "460,523", "435,206"],
                                                              ["Total liabilities", "4,640,471", "4,062,462"], ["Total stockholders’ equity", "374,598", "362,438"]])[12:-14]
            + "</body></html>")
MSFT_AR = _table([["", "June 30, 2026", "June 30, 2025"]],
                 [["Current assets:"], ["Cash and cash equivalents", "$ 20,935", "$ 30,242"],
                  ["Accounts receivable, net of allowance for doubtful accounts of $1,040 and $944", "80,876", "69,905"],
                  ["Total current assets", "207,710", "191,131"], ["Total assets", "$ 758,376", "$ 619,003"], ["Total liabilities", "315,989", "275,524"]],
                 "<p>BALANCE SHEETS</p><p>(In millions)</p>")


def test_one_statement_from_a_document_full_of_balance_tables(monkeypatch):
    b = _core_many(monkeypatch, [("10-Q", "10q.htm", "2026-08-01", BANK_DOC)])
    check(f"bank-shaped: the derivatives table is not the balance sheet ({b!r})", "41,006" not in b and "equity contracts" not in b)
    check("…each figure once", all(b.count(f"{v} million as of") == 1 for v in ("24,720", "72,430", "374,598")))
    check("…total debt from the statement's own debt rows", "DERIVED Total debt: 532,953" in b)
    m = _core_many(monkeypatch, [("10-Q", "10q.htm", "2026-07-30", MSFT_AR)])
    check(f"MSFT-shaped: 'doubtful accounts' does not make receivables a count ({m[:500]!r})", "80,876 million" in m and "(count;" not in m)


def _cells(spec):
    """'<td colspan=..>' rows from [(colspan, text), ...]."""
    return "<tr>" + "".join(f'<td colspan="{n}">{t}</td>' if n > 1 else f"<td>{t}</td>" for n, t in spec) + "</tr>"


# BABA: dates over RMB, RMB and a US$ convenience column; one "(in millions)" row
# spanning all three was stacked over them and every column read "March 31"
BABA_BS = ("<html><body><p>UNAUDITED CONSOLIDATED BALANCE SHEETS</p><table>"
           + _cells([(2, ""), (2, "As of March 31,"), (2, ""), (6, "As of June 30,")])
           + _cells([(2, ""), (2, "2026"), (2, ""), (6, "2026")])
           + _cells([(2, ""), (2, "RMB"), (2, ""), (2, "RMB"), (2, ""), (2, "US$")])
           + _cells([(2, ""), (10, "(in millions)")])
           + _cells([(2, "Current assets:")])
           + "".join(_cells([(2, lab), (1, ""), (1, a), (3, ""), (1, b), (3, ""), (1, c)])
                     for lab, a, b, c in [("Cash and cash equivalents", "131,530", "142,914", "21,063"),
                                          ("Total current assets", "724,854", "610,769", "90,000"),
                                          ("Total assets", "1,962,109", "1,909,570", "281,000")])
           + "</table><p>UNAUDITED CONSOLIDATED BALANCE SHEETS (CONTINUED)</p><table>"
           + _cells([(2, ""), (2, "As of March 31,"), (2, ""), (6, "As of June 30,")])
           + _cells([(2, ""), (2, "2026"), (2, ""), (6, "2026")])
           + _cells([(2, ""), (2, "RMB"), (2, ""), (2, "RMB"), (2, ""), (2, "US$")])
           + _cells([(2, ""), (10, "(in millions)")])
           + "".join(_cells([(2, lab), (1, ""), (1, a), (3, ""), (1, b), (3, ""), (1, c)])
                     for lab, a, b, c in [("Total liabilities", "848,215", "783,300", "115,000"),
                                          ("Total shareholders’ equity", "1,049,038", "1,060,886", "156,000")])
           + "</table></body></html>")
# KO: the balance sheet, and a debt note with its own "Long-term debt" rows
KO_DOC = (_table([["", "July 3, 2026", "December 31, 2025"]],
                 [["Cash and cash equivalents", "$ 12,907", "$ 10,270"], ["Total Assets", "$ 107,922", "$ 104,816"],
                  ["Current maturities of long-term debt", "6,494", "1,822"], ["Long-term debt", "37,001", "42,119"],
                  ["Total Equity", "38,315", "34,275"]], "<p>CONSOLIDATED BALANCE SHEETS</p><p>(In millions)</p>")[:-14]
          + _table([["Balance Sheet Location of Hedged Items", "Carrying Values of Hedged Items July 3, 2026",
                      "Carrying Values of Hedged Items December 31, 2025"]],
                   [["Current maturities of long-term debt", "$ 3,166", "$ 1,491"], ["Long-term debt", "9,715", "11,648"]],
                   "<p>The following table summarizes the hedged items (in millions):</p>")[12:])
# RY: no table titled as the statement — a highlights table, a one-column
# segment table, and the statement itself
RY_DOC = (_table([["", "July 31 2026", "October 31 2025"]],
                 [["Total assets", "2,498,817", "2,325,006"], ["Total liabilities", "2,353,723", "2,185,855"], ["Total equity", "145,094", "139,151"]],
                 "<p>Selected highlights (Millions of Canadian dollars)</p>")[:-14]
          + _table([["", "July 31, 2026"]], [["Total assets", "708,870"], ["Total liabilities", "630,324"]],
                   "<p>Wealth Management (Millions of Canadian dollars)</p>")[12:-14]
          + _table([["", "July 31 2026", "October 31 2025"]],
                   [["Cash and due from banks", "64,140", "37,024"], ["Total assets", "2,498,817", "2,325,006"],
                    ["Total liabilities", "2,353,723", "2,185,855"], ["Total equity", "145,094", "139,151"]],
                   "<p>Interim Consolidated Statements (Millions of Canadian dollars)</p>")[12:])


def test_currency_columns_and_one_statement(monkeypatch):
    b = _core_many(monkeypatch, [("6-K", "ex99.htm", "2026-08-20", BABA_BS)])
    check(f"BABA-shaped: each date stays on its own column, the RMB column is the figure ({b[:700]!r})",
          "Cash and cash equivalents [Current assets]: 142,914 million as of June 30, 2026 vs 131,530 million as of March 31, 2026" in b
          and "21,063" not in b)
    check("…the currency the columns name is on the line", "(CNY millions; GAAP" in b)
    check("…and the continued table is part of the statement", "Total shareholders’ equity: 1,060,886" in b)
    k = _core_many(monkeypatch, [("10-Q", "10q.htm", "2026-07-29", KO_DOC)])
    check(f"KO-shaped: the debt note is not the balance sheet ({k!r})", "3,166" not in k and "9,715" not in k
          and "DERIVED Total debt: 43,495" in k)
    r = _core_many(monkeypatch, [("6-K", "rpt.htm", "2026-08-27", RY_DOC)])
    check(f"RY-shaped: one table — the statement — never a segment's totals beside it ({r!r})",
          "708,870" not in r and "Cash and due from banks" in r and r.count("2,498,817 million as of") == 1)


# a European filer: day-month-year column dates ("30 Jun 2026")
NVO_BS = _table([["DKK million", "30 Jun 2026", "31 Dec 2025"]],
                [["Cash and cash equivalents", "26,464", "15,655"], ["TOTAL ASSETS", "594,941", "560,412"],
                 ["Borrowings", "120,000", "110,000"], ["Total equity", "180,000", "170,000"]],
                "<p>Balance sheet</p>")


def test_day_month_year_dates(monkeypatch):
    from markut.evidence.edgar import period_from_heading
    p = period_from_heading("30 June 2026")
    check(f"'30 June 2026' is a date ({p})", p["ended"] == "June 30, 2026" and p["period"].startswith("period ended"))
    n = _core_many(monkeypatch, [("6-K", "rpt.htm", "2026-08-04", NVO_BS)])
    check(f"NVO-shaped: a European balance sheet is read ({n!r})",
          "Cash and cash equivalents: 26,464 million as of Jun 30, 2026 vs 15,655 million as of Dec 31, 2025" in n
          and "TOTAL ASSETS: 594,941" in n and "DKK millions" in n)


# Toyota: the assets half titled as the statement, the liabilities-and-equity
# half in the very next table under a page header
TM_DOC = (_table([["Yen in millions", "March 31, 2026", "June 30, 2026"]],
                 [["Current assets"], ["Cash and cash equivalents", "12,659,622", "10,343,085"], ["Total current assets", "42,824,081", "38,852,813"],
                  ["Total assets", "105,522,331", "102,635,116"]],
                 "<p>1. Unaudited Condensed Quarterly Consolidated Statement of Financial Position</p>")[:-14]
          + _table([["Yen in millions", "March 31, 2026", "June 30, 2026"]],
                   [["Short-term borrowings", "12,000,000", "12,500,000"], ["Total current liabilities", "33,605,019", "32,000,000"],
                    ["Long-term debt", "25,000,000", "24,000,000"], ["Total liabilities", "64,502,263", "62,000,000"],
                    ["Total shareholders’ equity", "39,000,000", "38,500,000"]],
                   "<p>TOYOTA MOTOR CORPORATION FY2027 First Quarter Financial Results</p>")[12:])
# debt and cash as other filers name them; a bank's assets are not debt
OTHER_NAMES = _table([["", "June 30, 2026", "March 31, 2026"]],
                     [["Cash at bank", "44,482", "26,464"], ["Loans", "1,300,000", "1,250,000"], ["Debt securities", "500,000", "480,000"],
                      ["Total assets", "2,900,000", "2,800,000"], ["Current bank borrowings", "28,224", "30,614"],
                      ["Non-current unsecured senior notes", "117,485", "115,716"], ["Non-current exchangeable bonds", "10,976", "9,288"],
                      ["Total liabilities", "1,000,000", "990,000"], ["Total equity", "1,900,000", "1,810,000"]],
                     "<p>Consolidated balance sheets (in millions)</p>")


def test_split_statements_and_other_names(monkeypatch):
    t = _core_many(monkeypatch, [("6-K", "ex99.htm", "2026-08-04", TM_DOC)])
    check(f"TM-shaped: the liabilities-and-equity half beside the assets half is the same statement ({t!r})",
          "Total assets: 102,635,116" in t and "Total liabilities: 62,000,000" in t and "Total shareholders’ equity: 38,500,000" in t
          and "DERIVED Total debt: 36,500,000" in t)
    o = _core_many(monkeypatch, [("6-K", "rpt.htm", "2026-08-20", OTHER_NAMES)])
    check(f"'Cash at bank'; bank borrowings, unsecured senior notes, exchangeable bonds are debt ({o!r})",
          "Cash at bank: 44,482" in o and "DERIVED Total debt: 156,685" in o)
    check("…a bank's loans and debt securities are assets, never debt", "Loans" not in o and "Debt securities" not in o)


NVO_LIAB = _table([["DKK million", "30 Jun 2026", "31 Dec 2025"]],
                  [["Cash at bank", "44,482", "26,464"], ["TOTAL ASSETS", "594,941", "542,902"],
                   ["Borrowings", "121,794", "118,941"], ["Total non-current liabilities", "134,581", "133,194"],
                   ["Borrowings", "18,333", "12,017"], ["Total current liabilities", "239,083", "215,661"],
                   ["TOTAL LIABILITIES", "373,664", "348,855"], ["TOTAL EQUITY", "221,277", "194,047"]], "<p>Balance sheet</p>")
BABA_DEBT = (BABA_BS.replace('("Total current assets", "724,854", "610,769", "90,000")', "")
             .replace("</table><p>UNAUDITED CONSOLIDATED BALANCE SHEETS (CONTINUED)</p>",
                      _cells([(2, "Current bank borrowings"), (1, ""), (1, "28,224"), (3, ""), (1, "30,614"), (3, ""), (1, "4,500")])
                      + "</table><p>UNAUDITED CONSOLIDATED BALANCE SHEETS (CONTINUED)</p>")
             .replace("</table></body></html>",
                      _cells([(2, "Non-current unsecured senior notes"), (1, ""), (1, "117,485"), (3, ""), (1, "115,716"), (3, ""), (1, "16,000")])
                      + "</table></body></html>"))


def test_debt_across_the_statement_and_repeated_labels(monkeypatch):
    b = _core_many(monkeypatch, [("6-K", "ex99.htm", "2026-08-20", BABA_DEBT)])
    check(f"BABA-shaped: total debt adds the debt rows of both halves of the statement ({b[-500:]!r})",
          "DERIVED Total debt: 146,330" in b)
    n = _core_many(monkeypatch, [("6-K", "rpt.htm", "2026-08-04", NVO_LIAB)])
    check(f"NVO-shaped: a repeated label is told apart by the subtotal under it ({n!r})",
          "Borrowings [non-current liabilities]: 121,794" in n and "Borrowings [current liabilities]: 18,333" in n)
