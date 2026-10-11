"""P0 (run #28, TSM): Markut reads domestic filers (10-K / 10-Q / 8-K),
foreign private issuers (20-F / 6-K) and Canadian MJDS filers (40-F / 6-K),
names every document by its real form, gives a reason for every missing
role, and refuses a company that files no periodic reports with the SEC —
before any paid call. Offline: the filer shapes in tests/filer_shapes.py
(AAPL, TSM, a synthetic Canadian filer, a CIK with no periodic reports)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import filer_shapes as shapes  # noqa: E402


def check(label, condition):
    assert condition, label


def test_filer_type_from_the_forms_filed():
    from markut.evidence import filers
    got = {s: filers.filer_type(shapes.index_of(s)["form"]) for s in shapes.SHAPES}
    check(f"each shape is typed by what it files ({got})",
          got == {"AAPL": "domestic", "TSM": "foreign_private", "MAPLE": "canadian_mjds", "NOREP": "no_periodic_reports"})


def test_current_reports_are_sorted_by_what_they_carry():
    from markut.evidence import filers
    for text, want in ((shapes.TSM_RELEASE, "quarterly_results"), (shapes.TSM_MONTHLY, "monthly_revenue"),
                       (shapes.TSM_FS, "interim_report"), (shapes.TSM_BOARD, "other"), (shapes.TSM_BOARD_RESULTS, "other"),
                       (shapes.MAPLE_RELEASE, "quarterly_results"), (shapes.MAPLE_Q_MDA, "interim_report"),
                       (shapes.AAPL_RELEASE, "quarterly_results")):
        got = filers.classify_current_report(text)
        check(f"{text[18:70]!r} -> {want} (got {got})", got == want)


@pytest.mark.parametrize("shape,annual,interim,earnings", [
    ("AAPL", "10-K", "10-Q", "8-K"), ("TSM", "20-F", "6-K", "6-K"), ("MAPLE", "40-F", "6-K", "6-K")])
def test_filing_set_fills_each_role_with_the_real_form(monkeypatch, shape, annual, interim, earnings):
    shapes.install(monkeypatch)
    from markut.evidence import sections
    fs = sections.filing_set(shape)
    check(f"{shape}: annual role holds the {annual}", fs["10-K"] and fs["10-K"]["form"] == annual)
    check(f"{shape}: interim role holds a {interim}", fs["10-Q"] and fs["10-Q"]["form"] == interim)
    check(f"{shape}: earnings role holds a {earnings}", fs["8-K"] and fs["8-K"]["form"] == earnings)
    check(f"{shape}: the filer type is recorded", fs["filer_type"] in ("domestic", "foreign_private", "canadian_mjds"))
    if shape == "TSM":
        check("TSM: the results 6-K is the July release, the interim 6-K the August statements",
              fs["8-K"]["filing_date"] == "2026-07-16" and fs["10-Q"]["filing_date"] == "2026-08-14")
        check(f"TSM: monthly revenue 6-Ks after the release are kept ({[m['filing_date'] for m in fs.get('monthly', [])]})",
              [m["filing_date"] for m in fs.get("monthly", [])] == ["2026-10-08"])


def test_no_periodic_reports_gives_a_reason_for_every_role(monkeypatch):
    shapes.install(monkeypatch)
    from markut.evidence import sections
    fs = sections.filing_set("NOREP")
    check("no role is filled", not fs["10-K"] and not fs["10-Q"] and not fs["8-K"])
    check(f"every role says why ({fs.get('reasons')})",
          all("no annual or quarterly reports" in fs["reasons"][r] for r in ("annual", "interim", "earnings")))


def test_filings_unavailable_always_names_the_reason(monkeypatch):
    shapes.install(monkeypatch)
    from markut.evidence import rag
    text = rag.get_filings_evidence("NOREP")
    check(f"the [FILINGS] block states the reason, never a bare 'unavailable' ({text.strip()[:200]!r})",
          "no annual or quarterly reports" in text)


def test_6k_exhibits_are_found_without_99_in_their_names(monkeypatch):
    shapes.install(monkeypatch)
    from markut.evidence import sections
    fs = sections.filing_set("TSM")
    exhs = sections.exhibits_of(fs["8-K"])
    names = [e["name"] for e in exhs]
    check(f"TSM: the release exhibit is listed ({names})", "a2q26e_withguidancexfinal.htm" in names)
    check("TSM: exhibits keep the 6-K form", all(e["form"] == "6-K" for e in exhs))
    check("TSM: the cover page is not an exhibit", "tsm-20260716x6k.htm" not in names)
    maple = [e["name"] for e in sections.exhibits_of(sections.filing_set("MAPLE")["10-K"])]
    check(f"MAPLE: XBRL viewer pages (R1.htm …) are never exhibits ({maple})", "R1.htm" not in maple and "ex99-3mda.htm" in maple)


def test_annual_report_sections_by_form(monkeypatch):
    shapes.install(monkeypatch)
    from markut.evidence import sections
    for shape, needles in (("TSM", ("20-F Item 3.D (Risk Factors)", "20-F Item 4 (Information on the Company)",
                                    "20-F Item 5 (Operating and Financial Review)")),
                           ("MAPLE", ("40-F Annual Information Form (business)", "40-F Annual Information Form (risk factors)",
                                      "40-F annual MD&A")),
                           ("AAPL", ("10-K Item 1 (Business)", "10-K Item 1A (Risk Factors)", "10-K Item 7 (MD&A)"))):
        secs = sections.annual_sections(sections.filing_set(shape)["10-K"])
        labels = [label for label, text in secs.values() if text]
        for n in needles:
            check(f"{shape}: {n!r} found ({labels})", n in labels)


def test_profile_sections_name_only_forms_the_company_files(monkeypatch):
    shapes.install(monkeypatch)
    from markut.evidence import sections
    tsm = [s["name"] for s in sections.profile_sections(sections.filing_set("TSM"))]
    check(f"TSM: business from the 20-F, results from the 6-K ({tsm})",
          any(n.startswith("20-F Item 4") for n in tsm) and any(n.startswith("6-K") and "results release" in n for n in tsm))
    check("TSM: no 10-K / 10-Q / 8-K label", not any(f in n for n in tsm for f in ("10-K", "10-Q", "8-K")))
    aapl = [s["name"] for s in sections.profile_sections(sections.filing_set("AAPL"))]
    check(f"AAPL: unchanged domestic labels ({aapl})", any(n.startswith("10-K Item 1") for n in aapl) and any(n.startswith("8-K") for n in aapl))


def test_index_chunks_carry_real_forms(monkeypatch):
    shapes.install(monkeypatch)
    from markut.evidence import rag, sections
    for shape, forms in (("TSM", {"20-F", "6-K"}), ("MAPLE", {"40-F", "6-K"}), ("AAPL", {"10-K", "10-Q", "8-K"})):
        chunks = rag.filing_chunks(sections.filing_set(shape))
        got = {c["metadata"]["form"] for c in chunks}
        check(f"{shape}: indexed chunks come from {forms} ({got})", got == forms)
    tsm = rag.filing_chunks(sections.filing_set("TSM"))
    check("TSM: the monthly revenue report is indexed as current evidence",
          any("54.6 percent" in c["text"] and c["metadata"]["form"] == "6-K" for c in tsm))


def test_period_labels_for_foreign_filings():
    from markut.evidence.questions import is_current, period_label
    dates = {"20-F": "2026-04-16", "6-K": "2026-10-08"}
    check("a 6-K after the 20-F is current", is_current({"form": "6-K", "filing_date": "2026-07-16"}, dates))
    check("the 20-F is the prior fiscal year once a later 6-K exists",
          period_label({"form": "20-F", "filing_date": "2026-04-16"}, dates).startswith("prior fiscal year (20-F"))
    check("a 40-F behaves the same", period_label({"form": "40-F", "filing_date": "2026-02-27"},
                                                  {"40-F": "2026-02-27", "6-K": "2026-07-25"}).startswith("prior fiscal year"))
    check("domestic labels unchanged", period_label({"form": "10-K", "filing_date": "2025-10-31"},
                                                    {"10-K": "2025-10-31", "10-Q": "2026-07-31"}).startswith("prior fiscal year (10-K"))


def test_core_and_guidance_read_the_6k_release(monkeypatch):
    shapes.install(monkeypatch)
    from markut.evidence import core, filers, sections
    fs = sections.filing_set("TSM")
    srcs = core.core_sources(fs)
    check(f"TSM: core figures read the 6-K release and interim statements ({[(s['form'], s['name']) for s in srcs]})",
          {s["form"] for s in srcs} == {"6-K"} and any(s["name"] == "a2q26e_withguidancexfinal.htm" for s in srcs))
    g = sections.guidance_sources(fs)
    check(f"TSM: the guidance scan reads the 6-K release ({[s['label'] for s in g]})",
          any(s["label"].startswith("6-K") and "US$44.6 billion" in s["text"] for s in g))
    check(f"labels name the forms the company files ({filers.source_phrase(fs)!r})",
          "6-K" in filers.source_phrase(fs) and "8-K" not in filers.source_phrase(fs))


def test_a_company_that_is_not_an_sec_filer_is_refused_before_any_call(monkeypatch):
    shapes.install(monkeypatch)
    from markut.web import events as ev
    monkeypatch.setattr(ev, "ticker_to_cik", shapes.SHAPES and (lambda t: (_ for _ in ()).throw(
        ValueError(f"Ticker {t!r} not found in SEC EDGAR's company list"))))
    out = list(ev.stream_debate("ZZZQ", max_rounds=1, app=object()))
    check(f"refused with a clear message ({out})", out[0]["event"] == "error" and out[0]["data"]["stage"] == "validation"
          and "not an SEC filer" in out[0]["data"]["message"])
    from markut.evidence import sections
    monkeypatch.setattr(ev, "ticker_to_cik", lambda t: shapes.SHAPES["NOREP"]["cik"])
    monkeypatch.setattr(ev, "filing_history_forms", lambda cik: shapes.index_of("NOREP")["form"], raising=False)
    out2 = list(ev.stream_debate("NOREP", max_rounds=1, app=object()))
    check(f"a CIK with no periodic reports is refused too ({out2[0]['data'].get('message')})",
          out2[0]["event"] == "error" and "no annual or quarterly reports" in out2[0]["data"]["message"])


def test_40f_with_everything_in_the_main_document():
    # Canadian Natural's 40-F carries the AIF, the MD&A and the statements in
    # its main document, not in titled exhibits (live check, 2026-10-11)
    from markut.evidence import edgar
    one = ("FORM 40-F Annual report. ANNUAL INFORMATION FORM table of contents: General Development of the Business 5; Risk Factors 40. "
           "GENERAL DEVELOPMENT OF THE BUSINESS " + "The company acquired heavy oil assets and expanded its oil sands mining capacity. " * 12 +
           "RISK FACTORS " + "Commodity prices are volatile and may fall below the cost of production. " * 12 +
           "DIVIDENDS The company paid quarterly dividends. "
           "MANAGEMENT'S DISCUSSION AND ANALYSIS This MD&A should be read with the audited consolidated financial statements. " +
           "Production averaged 1,620 thousand barrels of oil equivalent per day in 2025, up 9% from 2024. " * 12 +
           "MANAGEMENT'S REPORT The management of the company is responsible for the financial statements.")
    f = edgar.extract_40f_sections(["", one])
    check(f"business found ({len(f['business'])} chars)", f["business"].startswith("GENERAL DEVELOPMENT") and "RISK FACTORS" not in f["business"])
    check(f"risk factors found ({len(f['risk'])} chars)", f["risk"].startswith("RISK FACTORS") and "DIVIDENDS" not in f["risk"])
    check(f"MD&A found and ends at the statements ({len(f['mdna'])} chars)",
          f["mdna"].startswith("MANAGEMENT'S DISCUSSION") and "1,620 thousand" in f["mdna"] and "MANAGEMENT'S REPORT" not in f["mdna"])


def test_general_and_guidance_blocks_name_the_real_form(monkeypatch):
    shapes.install(monkeypatch)
    from markut.evidence import questions, sections

    class _EmptyColl:
        def get(self, where=None, include=None):
            return {"documents": [], "metadatas": []}
    fs = sections.filing_set("TSM")
    gen = "\n".join(questions.general_blocks(_EmptyColl(), fs, {"segments": []}))
    check(f"TSM: release quotes and figures are sourced to the 6-K ({gen[:300]!r})", "EDGAR/6-K" in gen and "EDGAR/8-K" not in gen and "(8-K filed" not in gen)
    gd = "\n".join(questions.guidance_block(fs, []))
    check(f"TSM: the guidance block names the 6-K, finds the guided revenue range ({gd[:260]!r})",
          "6-K" in gd and "8-K exhibits" not in gd and "US$44.6 billion" in gd)


def test_interim_statement_assumptions_are_not_guidance():
    # TSM's interim 6-K statements: a hedge table's "Forecast transaction" and a
    # stock-award model's "Risk-free interest rate" / "Expected price volatility"
    from markut.evidence import questions, sections
    src = lambda text, label="6-K": {"label": label, "text": text, "form": "6-K", "filing_date": "2026-08-14", "url": "u", "kind": "text"}
    noise = ("Forecast transaction (capital expenditures) | $ - | $31,030 (columns: Hedging Instruments Foreign currency deposits Hedged Items)\n"
             "Risk-free interest rate | 1.45% | 1.33 % | 1.32 % (columns: Expected price volatility Residual life | 24.34%-31.55% 1 year)\n"
             "Expected dividend yield | 2.1% | 2.0%")
    out = sections.guidance_lines([src(noise)])
    check(f"TSM-shaped: hedge and option-model rows are not guidance ({[l['text'][:50] for l in out['lines']]})", out["lines"] == [])
    real = sections.guidance_lines([src("Revenue is expected to be between US$44.6 billion and US$45.8 billion.", "6-K a2q26e.htm")])
    lines = questions.format_guidance(real, "the latest 6-K results release")
    check(f"the source tag names the form once ({lines[1][-120:]!r})", "EDGAR/6-K a2q26e.htm" in lines[1] and "6-K 6-K" not in lines[1])


def test_6k_classification_by_what_the_document_is():
    # 2026-10-11 (readiness sample): two Canadian filers' interim slots held a
    # rating note and a loan refinancing, and a miner's results release was "other"
    from markut.evidence import filers
    pad = " The company operates mines in several countries and reports in U.S. dollars." * 80
    release = ("PRESS RELEASE EXHIBIT 99.1 Acme Gold reports strong 2026 second-quarter results Disciplined cost management supports "
               "robust margins and over $725 million in free cash flow. Returned ~40% of free cash flow to shareholders; operating cash flow "
               "of $1.1 billion; attributable net earnings of $480 million." + pad)
    assert filers.classify_current_report(release) == "quarterly_results", "a miner's results release, in its own words"
    note = "PRESS RELEASE Acme Gold upgraded to BBB by a rating agency. See our consolidated financial statements and management's discussion and analysis. Q2 revenue $2.1 billion, net earnings $480 million."
    assert filers.classify_current_report(note) == "other", "a short note that mentions the statements is not an interim report"
    notice = "PRESS RELEASE Acme Gold to announce Q3 results on October 28, 2026. A conference call will discuss revenue and net earnings of $1 billion."
    assert filers.classify_current_report(notice) == "other", "a notice of the results date is not the results"
    interim = ("Acme Gold Corporation management's discussion and analysis For the three and six months ended June 30, 2026. Revenue $2.1 billion, "
               "net earnings $480 million." + pad * 3)
    assert filers.classify_current_report(interim) == "interim_report", "a long document that opens with the MD&A is the interim report"


def test_a_footnote_naming_the_balance_sheet_is_not_an_interim_report():
    # the real rating note: "…net debt as reported on the condensed consolidated
    # balance sheet as at June 30, 2026" in a footnote of a short press release
    from markut.evidence import filers
    note = ("PRESS RELEASE Acme Gold upgraded to BBB by S&P Global Ratings (All dollar amounts are expressed in U.S. dollars). "
            "The upgrade reflects lower leverage, with net debt of $1.2 billion and margins near record levels. " * 6 +
            "1 Net debt as reported on the condensed consolidated balance sheet as at June 30, 2026. 2 The condensed consolidated balance sheets "
            "are in our second-quarter report.")
    assert filers.classify_current_report(note, docs=[note]) == "other"
    mda = "Acme Gold Corporation management's discussion and analysis For the three and six months ended June 30, 2026. Revenue $2.1 billion."
    cover = "FORM 6-K REPORT OF FOREIGN PRIVATE ISSUER Exhibits attached. " * 20
    assert filers.classify_current_report(cover + " " + mda, docs=[cover, mda]) == "interim_report", "the title opens its own exhibit"


def test_a_long_report_behind_an_sec_cover_is_the_interim_report():
    # the real report: one 55,000-character file — the SEC cover page, then the
    # financial report naming its statements once — no "reports … results" headline
    from markut.evidence import filers
    body = ("Financial report for the period 1 January 2026 to 30 June 2026. Sales increased by 6% to DKK 175,311 million and operating profit "
            "was DKK 86,679 million in the first six months; Q2 sales were DKK 78,488 million. ") * 140
    doc = ("6-K UNITED STATES SECURITIES AND EXCHANGE COMMISSION Washington, D.C. 20549 FORM 6-K REPORT OF FOREIGN PRIVATE ISSUER. " + body +
           " The condensed consolidated financial statements are presented in DKK millions.")
    assert len(doc) > 20000
    assert filers.classify_current_report(doc, docs=[doc]) == "interim_report"
