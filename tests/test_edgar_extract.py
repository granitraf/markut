"""Migrated verbatim: press-release hygiene + deterministic outlook +
risk-title extraction blocks."""
from markut.evidence.edgar import (extract_outlook, extract_risk_titles,
    strip_sgml_header, html_to_text)
from markut.evidence.rag import split_risk_factors


def check(label, condition):
    assert condition, label


def test_press_release_hygiene_and_outlook_block():
    print("\n--- press-release hygiene + deterministic outlook (offline) ---")

    # strip_sgml_header: the wrapper prefix must go, the real headline must stay.
    try:
        _polluted = "EX-99.1 2 q1fy27pr.htm PRESS RELEASE NVIDIA Announces Results.\n\nRevenue was up."
        _clean = strip_sgml_header(_polluted)
        check("SGML wrapper prefix stripped, headline kept",
              _clean.startswith("PRESS RELEASE") and "Revenue was up." in _clean)
        check("text without a wrapper passes through untouched",
              strip_sgml_header("No wrapper here.") == "No wrapper here.")
    except Exception as e:
        check(f"strip_sgml_header (unexpectedly raised: {e})", False)

    # extract_outlook: finds the labeled block, keeps guidance-like paragraphs,
    # stops at boilerplate, and returns "" (not noise) when there is no outlook.
    try:
        _pr = ("NVIDIA Announces Financial Results for First Quarter.\n\n"
               "Quarterly revenue was a record $44.1 billion, up 69% from a year ago.\n\n"
               "Outlook NVIDIA's outlook for the second quarter is as follows: revenue is expected to be $45.0 billion.\n\n"
               "GAAP and non-GAAP gross margins are expected to be 71.8% and 72.0%.\n\n"
               "Conference Call and Webcast Information NVIDIA will conduct a conference call today.")
        _out = extract_outlook(_pr)
        check("outlook block starts at the 'Outlook' heading paragraph",
              _out.startswith("Outlook") and "$45.0 billion" in _out)
        check("guidance-like follow-on paragraphs are kept", "72.0%" in _out)
        check("collection stops before the Conference Call boilerplate",
              "Conference Call" not in _out)
        check("no outlook heading -> empty string (honest absence)",
              extract_outlook("Nothing forward-looking here.") == "")
    except Exception as e:
        check(f"extract_outlook (unexpectedly raised: {e})", False)


def test_risk_title_extraction_block():
    print("\n--- risk-title extraction (offline — synthetic HTML) ---")

    # Bold and styled-bold captions become titles; short bold runs are ignored;
    # split_risk_factors cuts one block per risk and drops the boilerplate intro.
    _html = ("<html><body>"
             "<p>Item 1A. Risk Factors.</p>"
             "<p>The following risk factors should be considered carefully.</p>"
             "<b>We depend on a small number of customers for much of our revenue.</b>"
             "<p>Losing any large customer would hurt results. It really would.</p>"
             "<span style=\"font-weight:700\">Export controls could restrict our sales to key markets abroad.</span>"
             "<p>New rules may expand. We monitor them closely.</p>"
             "<b>Short.</b>"
             "</body></html>")
    try:
        _sect = html_to_text(_html)
        _titles = extract_risk_titles(_html, _sect)
        check(f"captions found via <b> AND font-weight style; short bold ignored (got {len(_titles)})",
              _titles == ["We depend on a small number of customers for much of our revenue.",
                          "Export controls could restrict our sales to key markets abroad."])
        _blocks = split_risk_factors(_sect, _titles)
        check("Item 1A splits into one block PER risk factor, boilerplate intro dropped",
              len(_blocks) == 2 and _blocks[0].startswith("We depend")
              and _blocks[1].startswith("Export controls")
              and "following risk factors" not in _blocks[0])
    except Exception as e:
        check(f"risk-title extraction (unexpectedly raised: {e})", False)
