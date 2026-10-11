"""Offline filer shapes for the run #28 (TSM) tests: an EDGAR filing history
plus the documents inside each filing, served by swap-and-restore stubs —
zero network. Four shapes:

  AAPL   — US domestic filer: 10-K, 10-Q, Item 2.02 8-K with ex99.1
  TSM    — foreign private issuer: 20-F + 6-Ks (results release whose files
           have no "99" in their names, interim financial statements,
           monthly revenue, board / dividend notices), Form 4 noise
  MAPLE  — SYNTHETIC Canadian MJDS filer: 40-F with AIF / MD&A exhibits,
           6-K interim MD&A and a 6-K results release
  NOREP  — a CIK whose history holds no periodic reports (Forms 3/4/SC 13G)

Document texts are short excerpts in each filer's real layout (AAPL and TSM
from their public filings, trimmed); MAPLE is invented."""
import json

ARCHIVE = "https://www.sec.gov/Archives/edgar/data"


def _filing(cik, form, date, acc, primary, items="", docs=None):
    return {"cik": cik, "form": form, "date": date, "acc": acc, "primary": primary, "items": items, "docs": docs or {}}


def _page(title, body):
    return f"<html><body><p>{title}</p>{body}</body></html>"


TSM_RELEASE = _page("TSMC Reports Second Quarter EPS of NT$27.25", (
    "<p>HSINCHU, Taiwan, R.O.C., Jul. 16, 2026 -- TSMC (TWSE: 2330, NYSE: TSM) today announced consolidated revenue of "
    "NT$1,270.38 billion, net income of NT$706.56 billion, and diluted earnings per share of NT$27.25 (US$4.31 per ADR unit) "
    "for the second quarter ended June 30, 2026.</p><p>Year-over-year, second quarter revenue increased 36.0%, while net income "
    "and diluted EPS both increased 77.4%. In US dollars, second quarter revenue was $40.20 billion, which increased 33.7% "
    "year-over-year.</p><p>Gross margin for the quarter was 67.7%, operating margin was 60.3%, and net profit margin was 55.6%.</p>"
    "<p>Based on the Company's current business outlook, management expects the overall performance for third quarter 2026 to be "
    "as follows: Revenue is expected to be between US$44.6 billion and US$45.8 billion; based on the exchange rate assumption of "
    "1 US dollar to 32 NT dollars, gross profit margin is expected to be between 65% and 67%.</p>"
    "<p>TSMC's 2026 second quarter consolidated results: (Unit: NT$ million, except for EPS)</p>"
    "<table><tr><td></td><td>2Q26</td><td>2Q25</td><td>YoY Inc. (Dec.) %</td></tr>"
    "<tr><td>Net sales</td><td>1,270,381</td><td>933,792</td><td>36.0</td></tr>"
    "<tr><td>Gross profit</td><td>860,311</td><td>547,369</td><td>57.2</td></tr>"
    "<tr><td>Net income</td><td>706,562</td><td>398,273</td><td>77.4</td></tr>"
    "<tr><td>EPS (NT$)</td><td>27.25</td><td>15.36</td><td>77.4</td></tr></table>"))
TSM_COVER = _page("FORM 6-K", "<p>REPORT OF FOREIGN PRIVATE ISSUER PURSUANT TO RULE 13a-16 OR 15d-16. Taiwan Semiconductor "
                  "Manufacturing Company Limited. Form 20-F x Form 40-F o</p>")
TSM_MONTHLY = _page("FORM 6-K", "<p>TSMC September 2026 Revenue Report. HSINCHU, Taiwan, R.O.C. - October 8, 2026 - TSMC today "
                    "announced its net revenue for September 2026: On a consolidated basis, revenue for September 2026 was "
                    "approximately NT$511.86 billion, a decrease of 0.6 percent from August 2026 and an increase of 54.6 percent "
                    "from September 2025.</p>")
TSM_FS = _page("Taiwan Semiconductor Manufacturing Company Limited and Subsidiaries", (
    "<p>Consolidated Financial Statements for the Six Months Ended June 30, 2026 and 2025 and Independent Auditors' Review Report</p>"
    "<p>Consolidated balance sheets (In Thousands of New Taiwan Dollars). Total liabilities NT$ 1,950,000,000. Net revenue for the "
    "three months ended June 30, 2026 was NT$1,270,381 million.</p>"))
TSM_BOARD = _page("FORM 6-K", "<p>TSMC Board of Directors Meeting Resolutions: approved capital appropriations of approximately "
                  "US$15,000 million and the issuance of unsecured corporate bonds.</p>")
TSM_BOARD_RESULTS = _page("FORM 6-K", (
    "<p>TSMC Board of Directors Meeting Resolutions. HSINCHU, Taiwan, R.O.C., Aug. 11, 2026 - The TSMC Board of Directors today "
    "held a meeting, which passed the following resolutions: 1. Approved the 2026 second quarter Business Report and Financial "
    "Statements. Second quarter consolidated revenue was NT$1,270.38 billion and net income was NT$706.56 billion, with diluted "
    "earnings per share of NT$27.25. 2. Approved the distribution of a NT$7.0 per share cash dividend for the second quarter of 2026.</p>"))
TSM_20F = ("<html><body><p>FORM 20-F ANNUAL REPORT</p><p>TABLE OF CONTENTS</p><p>ITEM 3. KEY INFORMATION 3</p>"
           "<p>ITEM 4. INFORMATION ON THE COMPANY 20</p><p>ITEM 5. OPERATING AND FINANCIAL REVIEWS AND PROSPECTS 40</p>"
           "<p>ITEM 3. KEY INFORMATION</p><p>Risk Factors</p><p>" + "We face intense competition in the foundry segment of the "
           "semiconductor industry, and customers may move orders to other foundries. " * 12 + "</p>"
           "<p>ITEM 4. INFORMATION ON THE COMPANY</p><p>Business Overview of the Company</p><p>" + "As a foundry, we manufacture "
           "semiconductors using our manufacturing processes for our customers based on their own or third parties' designs. " * 12 + "</p>"
           "<p>ITEM 4A. UNRESOLVED STAFF COMMENTS</p><p>None.</p>"
           "<p>ITEM 5. OPERATING AND FINANCIAL REVIEWS AND PROSPECTS</p><p>" + "Net revenue increased 31.6% in 2025, driven by "
           "strong demand for our 3-nanometer and 5-nanometer technologies in high performance computing. " * 12 + "</p>"
           "<p>ITEM 6. DIRECTORS, SENIOR MANAGEMENT AND EMPLOYEES</p><p>Board members are listed.</p></body></html>")

AAPL_10K = ("<html><body><p>Item 1. Business</p><p>" + "The Company designs, manufactures and markets smartphones, personal "
            "computers, tablets, wearables and accessories, and sells a variety of related services. " * 10 + "</p>"
            "<p>Item 1A. Risk Factors</p><p>" + "The Company's operations and performance depend significantly on global and "
            "regional economic conditions. " * 12 + "</p><p>Item 1B. Unresolved Staff Comments</p><p>None.</p>"
            "<p>Item 7. Management's Discussion and Analysis of Financial Condition and Results of Operations</p><p>" +
            "Total net sales increased 6% during 2025 compared to 2024, driven by higher net sales of Services. " * 12 + "</p>"
            "<p>Item 7A. Quantitative and Qualitative Disclosures About Market Risk</p><p>Not material.</p></body></html>")
AAPL_10Q = ("<html><body><p>Item 2. Management's Discussion and Analysis of Financial Condition and Results of Operations</p><p>" +
            "Total net sales increased 16% during the third quarter of 2026 compared to the same quarter in 2025. " * 12 + "</p>"
            "<p>Item 3. Quantitative and Qualitative Disclosures About Market Risk</p><p>None.</p></body></html>")
AAPL_RELEASE = _page("Apple reports third quarter results", "<p>CUPERTINO, California, July 30, 2026 -- Apple today announced financial "
                     "results for its fiscal 2026 third quarter ended June 27, 2026. The Company posted quarterly revenue of $109.4 "
                     "billion, up 16 percent year over year, and quarterly diluted earnings per share of $2.02.</p>")

MAPLE_AIF = _page("ANNUAL INFORMATION FORM", "<p>DESCRIPTION OF THE BUSINESS</p><p>" + "Maple Rail Corp. operates a freight railway "
                  "network across Canada and hauls grain, intermodal containers and energy products. " * 10 + "</p><p>RISK FACTORS</p><p>" +
                  "Our operations depend on weather conditions and the health of the North American economy. " * 10 + "</p>")
MAPLE_MDA = _page("MANAGEMENT'S DISCUSSION AND ANALYSIS", "<p>This MD&A should be read with our Annual Information Form and "
                  "audited financial statements.</p><p>" + "Revenues increased 7% in 2025 to C$9,120 million, driven by higher "
                  "freight rates and grain volumes. " * 12 + "</p>")
MAPLE_Q_MDA = _page("MANAGEMENT'S DISCUSSION AND ANALYSIS for the three months ended June 30, 2026", "<p>" + "Revenues for the "
                    "three months ended June 30, 2026 were C$2,410 million, an increase of 5% over the same period of 2025. " * 8 + "</p>")
MAPLE_RELEASE = _page("Maple Rail reports second quarter 2026 results", "<p>CALGARY, July 24, 2026 -- Maple Rail Corp. today announced "
                      "second quarter revenues of C$2,410 million, up 5%, net income of C$640 million and diluted earnings per share "
                      "of C$1.02.</p>")


def _shapes():
    aapl, tsm, maple, norep = "0000320193", "0001046179", "0009990001", "0009990002"
    return {
        "AAPL": {"cik": aapl, "company": "Apple Inc.", "filings": [
            _filing(aapl, "8-K", "2026-07-30", "0000320193-26-000018", "aapl-20260730.htm", "2.02,9.01",
                    {"aapl-20260730.htm": _page("FORM 8-K", "<p>Item 2.02 Results of Operations</p>"), "a8-kex991q3.htm": AAPL_RELEASE}),
            _filing(aapl, "10-Q", "2026-07-31", "0000320193-26-000020", "aapl-20260627.htm", "", {"aapl-20260627.htm": AAPL_10Q}),
            _filing(aapl, "4", "2026-07-15", "0000320193-26-000017", "form4.xml"),
            _filing(aapl, "10-K", "2025-10-31", "0000320193-25-000079", "aapl-20250927.htm", "", {"aapl-20250927.htm": AAPL_10K}),
        ]},
        "TSM": {"cik": tsm, "company": "Taiwan Semiconductor Manufacturing Co Ltd", "filings": [
            _filing(tsm, "6-K", "2026-10-08", "0001046179-26-000700", "tsm-revenue20261008.htm", "", {"tsm-revenue20261008.htm": TSM_MONTHLY}),
            _filing(tsm, "4", "2026-09-30", "0001046179-26-000690", "form4.xml"),
            _filing(tsm, "6-K", "2026-09-01", "0001046179-26-000600", "tsm-dividendadjustment.htm", "",
                    {"tsm-dividendadjustment.htm": _page("FORM 6-K", "<p>TSMC announced a cash dividend of NT$6.00 per share.</p>")}),
            _filing(tsm, "6-K", "2026-08-14", "0001046179-26-000541", "tsm-fsx20260814x6k.htm", "",
                    {"tsm-fsx20260814x6k.htm": TSM_COVER, "a2026q2consolidatedreport-.htm": TSM_FS}),
            _filing(tsm, "6-K", "2026-08-11", "0001046179-26-000530", "tsm-boardx20260811.htm", "", {"tsm-boardx20260811.htm": TSM_BOARD_RESULTS}),
            _filing(tsm, "6-K", "2026-07-16", "0001046179-26-000451", "tsm-20260716x6k.htm", "",
                    {"tsm-20260716x6k.htm": TSM_COVER, "a2q26e_withguidancexfinal.htm": TSM_RELEASE,
                     "a2q26presentatione.htm": _page("2Q26 presentation", "<img src='a2q26presentatione001.jpg'>")}),
            _filing(tsm, "6-K", "2026-07-13", "0001046179-26-000447", "tsm-revenue20260713.htm", "",
                    {"tsm-revenue20260713.htm": TSM_MONTHLY.replace("September", "June").replace("54.6", "39.6")}),
            _filing(tsm, "20-F", "2026-04-16", "0001046179-26-000231", "tsm-20251231.htm", "", {"tsm-20251231.htm": TSM_20F}),
        ]},
        "MAPLE": {"cik": maple, "company": "Maple Rail Corp", "filings": [
            _filing(maple, "6-K", "2026-07-25", "0009990001-26-000040", "maple-6k-q2mda.htm", "",
                    {"maple-6k-q2mda.htm": _page("FORM 6-K", "<p>Exhibits attached.</p>"), "ex99-2.htm": MAPLE_Q_MDA}),
            _filing(maple, "6-K", "2026-07-24", "0009990001-26-000039", "maple-6k-q2release.htm", "",
                    {"maple-6k-q2release.htm": _page("FORM 6-K", "<p>Exhibit 99.1 attached.</p>"), "ex99-1.htm": MAPLE_RELEASE}),
            _filing(maple, "40-F", "2026-02-27", "0009990001-26-000010", "maple-40f2025.htm", "",
                    {"maple-40f2025.htm": _page("FORM 40-F", "<p>Annual report pursuant to Section 13 or 15(d). Exhibits 99.1-99.3.</p>"),
                     "ex99-1aif.htm": MAPLE_AIF, "ex99-3mda.htm": MAPLE_MDA,
                     "R1.htm": _page("XBRL viewer page", "<p>Document and Entity Information</p>")}),
        ]},
        "NOREP": {"cik": norep, "company": "No Reports Holdings", "filings": [
            _filing(norep, "4", "2026-09-01", "0009990002-26-000003", "form4.xml"),
            _filing(norep, "SC 13G", "2026-02-10", "0009990002-26-000002", "sc13g.htm"),
            _filing(norep, "3", "2025-11-01", "0009990002-25-000001", "form3.xml"),
        ]},
    }


SHAPES = _shapes()


def index_of(shape: str) -> dict:
    """The 'recent' parallel arrays get_filing_index returns, newest first."""
    fl = SHAPES[shape]["filings"]
    return {"form": [f["form"] for f in fl], "accessionNumber": [f["acc"] for f in fl], "filingDate": [f["date"] for f in fl],
            "primaryDocument": [f["primary"] for f in fl], "items": [f["items"] for f in fl]}


def documents() -> dict:
    """url -> text for every document and every filing's index.json."""
    out = {}
    for shape in SHAPES.values():
        for f in shape["filings"]:
            base = f"{ARCHIVE}/{int(f['cik'])}/{f['acc'].replace('-', '')}"
            names = list(f["docs"]) or [f["primary"]]
            out[f"{base}/index.json"] = json.dumps({"directory": {"item": [{"name": n} for n in
                                                    [f"{f['acc']}-index.html"] + names + ["a2q26presentatione001.jpg"]]}})
            for name, text in f["docs"].items():
                out[f"{base}/{name}"] = text
    return out


def install(monkeypatch):
    """Swap every EDGAR entry point the pipeline uses for the offline shapes."""
    from markut.evidence import core, edgar, rag, sections
    docs = documents()
    ciks = {s: v["cik"] for s, v in SHAPES.items()}

    def fake_cik(ticker):
        t = ticker.upper().strip()
        if t not in ciks:
            raise ValueError(f"Ticker {t!r} not found in SEC EDGAR's company list")
        return ciks[t]

    def fake_index(cik):
        shape = next(s for s, c in ciks.items() if c == cik)
        return index_of(shape)

    def fake_fetch(url):
        if url not in docs:
            raise RuntimeError(f"offline: no document {url}")
        return docs[url]

    for mod in (edgar, sections, rag, core):
        for name, fn in (("ticker_to_cik", fake_cik), ("get_filing_index", fake_index), ("fetch_filing_html", fake_fetch)):
            if hasattr(mod, name):
                monkeypatch.setattr(mod, name, fn)
    if hasattr(edgar, "get_older_filings"):
        monkeypatch.setattr(edgar, "get_older_filings", lambda cik, pages=2: [])
    return docs
