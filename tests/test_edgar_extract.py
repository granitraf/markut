"""Press-release hygiene, the general forward-looking guidance scan and
risk-title extraction (offline, synthetic text)."""
from markut.evidence.edgar import extract_risk_titles, strip_sgml_header, html_to_text
from markut.evidence.rag import split_risk_factors
from markut.evidence.sections import guidance_lines


def check(label, condition):
    assert condition, label


def test_press_release_hygiene_and_guidance_scan():
    # strip_sgml_header: the wrapper prefix must go, the real headline must stay.
    _polluted = "EX-99.1 2 q1fy27pr.htm PRESS RELEASE Company Announces Results.\n\nRevenue was up."
    _clean = strip_sgml_header(_polluted)
    check("SGML wrapper prefix stripped, headline kept",
          _clean.startswith("PRESS RELEASE") and "Revenue was up." in _clean)
    check("text without a wrapper passes through untouched",
          strip_sgml_header("No wrapper here.") == "No wrapper here.")

    # guidance is found from forward-looking LANGUAGE, not from a heading one
    # company happens to use; boilerplate never counts; no language -> no lines
    _pr = ("Company Announces Financial Results for First Quarter.\n\n"
           "Quarterly revenue was a record $44.1 billion, up 69% from a year ago.\n\n"
           "The company's view for the second quarter is as follows: revenue is expected to be $45.0 billion.\n\n"
           "GAAP and non-GAAP gross margins are expected to be 71.8% and 72.0%.\n\n"
           "Conference Call and Webcast Information The company will conduct a conference call today at 2:00 p.m.")
    src = {"label": "8-K ex99-1", "text": _pr, "form": "8-K", "filing_date": "2026-05-28", "url": "u", "kind": "text"}
    out = guidance_lines([src])
    texts = [l["text"] for l in out["lines"]]
    check("the revenue guide is a guidance line", any("$45.0 billion" in t for t in texts))
    check("the margin guide is a guidance line", any("72.0%" in t for t in texts))
    check("past results and the conference call are not guidance",
          not any("record $44.1 billion" in t or "Conference Call" in t for t in texts))
    check("no failures when guidance was extracted", out["failures"] == [])
    none = guidance_lines([{**src, "text": "Nothing forward-looking here. Revenue was $1 billion."}])
    check("no forward-looking language -> no lines and no failure", none["lines"] == [] and none["failures"] == [])
    fail = guidance_lines([{**src, "text": "We expect strong growth next year across every market we serve."}])
    check("forward-looking language without a figure -> EXTRACTION FAILURE, never 'no guidance'",
          fail["lines"] == [] and len(fail["failures"]) == 1 and "forward-looking language is present" in fail["failures"][0])


def test_risk_title_extraction_block():
    # Bold and styled-bold captions become titles; short bold runs are ignored;
    # split_risk_factors cuts one block per risk and drops the boilerplate intro.
    _html = ("<html><body>"
             "<p>Item 1A. Risk Factors.</p>"
             "<p>The following risk factors should be considered carefully.</p>"
             "<b>We depend on a small number of customers for much of our revenue.</b>"
             "<p>Losing any large customer would hurt results. It really would.</p>"
             "<span style=\"font-weight:700\">Trade restrictions could limit our sales to key markets abroad.</span>"
             "<p>New rules may expand. We monitor them closely.</p>"
             "<b>Short.</b>"
             "</body></html>")
    _sect = html_to_text(_html)
    _titles = extract_risk_titles(_html, _sect)
    check(f"captions found via <b> AND font-weight style; short bold ignored (got {len(_titles)})",
          _titles == ["We depend on a small number of customers for much of our revenue.",
                      "Trade restrictions could limit our sales to key markets abroad."])
    _blocks = split_risk_factors(_sect, _titles)
    check("Item 1A splits into one block PER risk factor, boilerplate intro dropped",
          len(_blocks) == 2 and _blocks[0].startswith("We depend")
          and _blocks[1].startswith("Trade restrictions")
          and "following risk factors" not in _blocks[0])
