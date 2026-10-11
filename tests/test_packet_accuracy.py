"""P2 — packet accuracy (run #26 audit): every label must be literally true.
(7) a computed line's label matches what was computed; (8) templates compute
what they can and show what they omit; (9) the archetype follows the revenue
mix; (10) guidance means forward business targets only; (11) topic blocks
hold on-topic sentences only; (12) one metric, one value, one source; (13)
news status is truthful and read the same way everywhere; (14) the governor
extracts every figure in the verdict. Offline, no network, no model calls.
Each rule is checked on an AAPL-shaped example (not a target) and a
synthetic company of a different shape."""
import re

import pandas as pd
import pytest

from markut.agents import profiler
from markut.evidence import assembly, market, news, sections, valuation
from markut.guardrails import tracer


def check(label, condition):
    assert condition, label


# ======================================================================
# 7. a computed line's label matches what was computed
# ======================================================================
def _bs(total_debt, leases, equity, date="2026-06-30"):
    row = {"Total Debt": total_debt, "Stockholders Equity": equity}
    if leases is not None:
        row["Capital Lease Obligations"] = leases
    return pd.DataFrame({pd.Timestamp(date): row})


def test_leverage_line_never_claims_a_component_it_does_not_have():
    lines = market.leverage_lines(_bs(84.34e9, 0.0, 107.52e9), {"debtToEquity": 78.44})
    text = "\n".join(lines)
    check(f"AAPL-shaped: no 'including lease obligations' line when the provider's debt holds $0 of them ({text!r})",
          "including lease obligations" not in text and "of which lease obligations $0" not in text)
    check("the line says leases are excluded and still names its components",
          "lease obligations are not included" in text and "$84.34B" in text and "$107.52B" in text and "78.4%" in text)
    lines2 = market.leverage_lines(_bs(2081.0e6, 1471.0e6, 519.0e6), {"debtToEquity": 400.999})
    check("lease-heavy synthetic: with real lease obligations both lines stay, components named",
          lines2[0].startswith("- Debt/Equity including lease obligations") and "lease obligations $1.47B" in lines2[0]
          and lines2[1].startswith("- Debt/Equity excluding lease obligations"))
    lines3 = market.leverage_lines(_bs(50.0e9, None, 100.0e9), {"debtToEquity": 50.0})
    check(f"no lease row at all -> excluded is said, never 'including' ({lines3})",
          lines3 and "including lease obligations" not in lines3[0] and "not included" in lines3[0])


def test_filed_lease_liabilities_are_sourced_when_the_provider_has_none():
    market_text = "\n".join(market.leverage_lines(_bs(84.34e9, 0.0, 107.52e9), {"debtToEquity": 78.44}))
    filings = ("- Total lease liabilities: 12,600 million (period ended June 27, 2026) vs 12,100 million year-ago (September 27, 2025)  "
               "(millions; GAAP; company-wide)  [source: T7]\n  T7 = EDGAR/10-Q x.htm \"Lease liabilities\", filed 2026-07-31")
    line = assembly.leverage_with_filed_leases(market_text, filings)
    check(f"the lease-inclusive ratio is computed from the filing's lease liabilities ({line!r})",
          line and "90.2%" in line and "$12.60B" in line and "10-Q" in line)
    check("nothing is invented when the filing has no lease liability row", assembly.leverage_with_filed_leases(market_text, "") is None)


# ======================================================================
# 8. templates compute what they can and say what they omit
# ======================================================================
def test_profile_template_computes_changes_and_lists_every_segment():
    segs = [{"name": n, "latest_revenue": f"${cur:,} million", "latest_yoy": f"not stated (prior year ${prev:,} million)",
             "period": "Three months ended June 27, 2026", "source": "s"} for n, cur, prev in
            (("Americas", 45781, 41198), ("Europe", 29395, 24014), ("Greater China", 18816, 15369), ("Japan", 6554, 5782),
             ("Rest of Asia Pacific", 8871, 7673), ("iPhone", 54252, 44582), ("Mac", 10352, 8046), ("iPad", 6191, 6581),
             ("Wearables, Home and Accessories", 7883, 7404), ("Services", 30739, 27423))]
    block = assembly.profile_block({"business": "b.", "archetype": "consumer_electronics", "segments": segs, "company_kpis": [], "accounting_flags": []})
    check("AAPL-shaped: 'not stated' never printed when both values exist", "not stated" not in block)
    check("the change is computed from the two values", "+11.1%" in block and "-5.9%" in block)
    check("every segment is listed (Services and Wearables were cut before)", "Segment: Services" in block and "Segment: Wearables, Home and Accessories" in block)
    many = [{"name": f"Line {i}", "latest_revenue": f"${100 + i} million", "latest_yoy": "+5%", "period": "Q3", "source": "s"} for i in range(40)]
    block2 = assembly.profile_block({"business": "b.", "archetype": "industrial", "segments": many, "company_kpis": [], "accounting_flags": []})
    shown = block2.count("- Segment: Line")
    check(f"synthetic 40-line company: a capped list says 'N of M shown' ({shown})", shown == 40 or f"{shown} of 40 shown" in block2)


# ======================================================================
# 9. the archetype follows the revenue mix
# ======================================================================
def test_archetype_is_justified_by_the_revenue_mix():
    apple_like = {"business": "Designs, manufactures and markets smartphones, personal computers, tablets, wearables and accessories, and sells related services.",
                  "archetype": "semis_hardware", "segments": [{"name": n} for n in ("iPhone", "Mac", "iPad", "Wearables, Home and Accessories", "Services")]}
    arch, note = profiler.check_archetype(apple_like)
    check(f"AAPL-shaped: a device maker with no chip revenue is not semis_hardware (got {arch})", arch == "consumer_electronics" and "semis_hardware" in note)
    chip = {"business": "Designs and sells analog semiconductors and microcontrollers built on its own wafer fabs.",
            "archetype": "semis_hardware", "segments": [{"name": "Analog"}, {"name": "Embedded processing"}]}
    check("synthetic chip maker keeps semis_hardware", profiler.check_archetype(chip) == ("semis_hardware", ""))
    saas = {"business": "Sells subscription software for payroll and human resources to small businesses.",
            "archetype": "software", "segments": [{"name": "Subscription"}]}
    check("synthetic software company keeps software", profiler.check_archetype(saas)[0] == "software")
    from markut.agents.schemas import ARCHETYPES
    check("the missing archetype exists rather than forcing the closest", "consumer_electronics" in ARCHETYPES)


# ======================================================================
# 10. guidance means forward-looking business targets only
# ======================================================================
def test_accounting_schedules_are_not_guidance():
    src = lambda text: {"label": "10-Q", "text": text, "form": "10-Q", "filing_date": "2026-07-31", "url": "u", "kind": "text"}
    deferred = ("As of June 27, 2026, the Company expects 64 % of total deferred revenue to be realized in less than a year, "
                "23 % within one-to-two years and 2 % in greater than three years.")
    leases = "Future minimum lease payments are expected to be $2.6 billion within 12 months and $16.8 billion in total."
    debt = "The Company expects $10.5 billion of term debt to mature in fiscal 2027."
    real = "For the fourth quarter, we expect revenue to grow at a mid-single-digit percentage rate year over year."
    out = sections.guidance_lines([src(deferred + " " + leases + " " + debt)])
    check(f"AAPL-shaped deferred-revenue schedule, lease and debt maturities are not guidance ({[l['text'][:50] for l in out['lines']]})", out["lines"] == [])
    kept = sections.guidance_lines([src(real + " " + deferred)])
    check("a real target is still guidance", len(kept["lines"]) == 1 and "mid-single-digit" in kept["lines"][0]["text"])
    from markut.evidence.questions import format_guidance
    none = "\n".join(format_guidance(out))
    check(f"no guidance -> said so, with the reason ({none!r})", "no forward-looking business targets" in none and "earnings call" in none)


# ======================================================================
# 11. topic blocks hold on-topic sentences only
# ======================================================================
def test_topic_blocks_require_topic_words_not_substrings(monkeypatch):
    tenk = ("<html><body><p>Note 6. Leases</p><p>" + "The Company leases retail space and offices. " * 6 +
            "As of September 27, 2025, the Company had fixed lease payment obligations of $16.8 billion. "
            "During the fourth quarter of 2024 the Company recorded a one-time income tax charge of $10.2 billion, which represented $15.8 billion "
            "payable to Ireland via release of amounts held in escrow.</p>"
            "<p>Note 7. Debt</p><p>" + "The Company issues senior notes. " * 6 +
            "The Company had $84.3 billion of term debt outstanding. Its indebtedness ratio policy is reviewed by the board, which approved $2.0 billion of buybacks.</p>"
            "<p>Note 8. Segment Information</p><p>" + "Segments are described here. " * 10 + "</p></body></html>")
    monkeypatch.setattr(sections, "fetch_filing_html", lambda url: tenk)
    blocks = {b["title"]: b["lines"] for b in sections.note_dollar_blocks({"10-Q": None, "10-K": {"form": "10-K", "filing_date": "2025-10-31", "url": "u", "accession": "a"}})}
    leases = next((v for k, v in blocks.items() if k.startswith("Leases")), [])
    check(f"AAPL-shaped: the lease block keeps the lease sentence ({leases})", any("$16.8 billion" in l for l in leases))
    check("an escrow 'release' sentence is not a lease sentence", not any("Ireland" in l for l in leases))
    debt = next((v for k, v in blocks.items() if k.startswith("Debt")), [])
    check(f"synthetic: 'indebtedness ... buybacks' is not a debt figure line ({debt})", any("$84.3 billion" in l for l in debt) and not any("buybacks" in l for l in debt))


# ======================================================================
# 12. one metric, one value, one stated source
# ======================================================================
def test_forward_eps_has_one_canonical_value():
    info = {"currentPrice": 336.64, "forwardEps": 9.59, "trailingEps": 8.72, "trailingPE": 38.61,
            "_fwd_eps_canonical": {"value": 9.58, "source": "yfinance/earnings_estimate", "analysts": 40}}
    text = "\n".join(market.format_market_lines(info)) + "\n" + "\n".join(
        valuation.format_valuation_lines(info, {"+1y": {"avg": 9.58, "numberOfAnalysts": 40}}, 2026, {}))
    check(f"AAPL-shaped: one forward EPS value ($9.58), never the second source's $9.59 ({[l for l in text.splitlines() if '9.5' in l]})",
          "$9.59" not in text and "$9.58" in text)
    check("one forward P/E value (35.14), never 35.11", "35.11" not in text and "35.14" in text)
    check("the canonical source is named", "earnings_estimate" in [l for l in text.splitlines() if l.startswith("- EPS (forward")][0])
    info2 = {"currentPrice": 50.0, "forwardEps": 2.5}
    t2 = "\n".join(market.format_market_lines(info2))
    check(f"synthetic company with no estimate table: the provider field is the source, and says so ({t2!r})",
          "$2.50" in t2 and "yfinance/info" in [l for l in t2.splitlines() if l.startswith("- EPS (forward")][0] and "20.00" in t2)


# ======================================================================
# 13. news status is truthful and reads the same everywhere
# ======================================================================
def test_body_must_match_its_headline(monkeypatch):
    ident = {"ticker": "XYZ", "company": "Apple Inc."}
    headline = "Apple's App Store Growth Improved in September. What UBS Sees Coming in October Is Harder"
    unrelated = ("Hedge fund interest in Apple remained broadly stable, falling from 170 funds to 169 funds. Short interest also remains low "
                 "at 0.88% of float. The P/E is above its five-year average.")
    related = "App Store revenue growth improved in September, UBS analysts wrote, citing download data; October looks harder to compare."
    check("AAPL-shaped: an unrelated body does not match its headline", not news.body_matches_headline(headline, unrelated, ident))
    check("a related body matches", news.body_matches_headline(headline, related, ident))
    check("synthetic: a plant-expansion body matches its headline",
          news.body_matches_headline("Example Devices to double plant capacity in Ohio", "Example Devices said it will double capacity at its Ohio plant by 2027.",
                                     {"ticker": "EXD", "company": "Example Devices Inc."}))
    monkeypatch.setattr(news, "fetch_accessible_article", lambda url: {"url": url, "text": unrelated})
    monkeypatch.setattr(news, "_is_blocked_publisher", lambda p, u: False)
    item = news.hydrate_news_item({"title": headline, "link": "https://example.com/a", "publisher": "x", "snippet": "App Store growth improved."}, ident)
    check(f"a mismatched body is downgraded to lead ({item['status']}, {item['fetch_state']})", item["status"] == "lead" and "did not match" in item["fetch_state"])


def test_news_labels_use_the_vocabulary_the_prompts_use():
    from markut.agents import prompts
    body = news.format_news_items([{"title": "XYZ t", "publisher": "P", "url": "u", "context": "Fetched text.", "status": "body"}], "[NEWS]", "cov", 300)
    lead = news.format_news_items([{"title": "XYZ t", "publisher": "P", "url": "u", "context": "Feed blurb.", "status": "lead"}], "[NEWS]", "cov", 300)
    check(f"a body item's text is labelled body, never 'lead:' ({body!r})", "  body: Fetched text." in body and "  lead:" not in body and "Status: body" in body)
    check(f"a lead item reads 'lead only', the prompts' own words ({lead!r})", "Status: lead only" in lead and "lead only" in prompts.BULL_SYSTEM_PROMPT)


# ======================================================================
# 14. the governor extracts every figure in the verdict
# ======================================================================
RUN26_FINAL = ("Valuation is the sharper constraint: price $336.64 is above the mean analyst target of $328.09, trailing P/E of 38.61 is above "
               "its own median of 34.1x, and the grid shows only the p75 multiple yielding upside (+6.2%). The tariff refund was $0.11 against "
               "diluted EPS of $2.02; revenue +16.4%, EPS +28.7%, FCF $136.68B TTM; cells $326.69 (-3.0%) and $279.75 (-16.9%).")


def test_every_figure_in_the_verdict_is_extracted():
    claims = tracer.extract_numeric_claims(RUN26_FINAL)
    for fig in ("$336.64", "$328.09", "38.61", "34.1x", "$136.68B", "$0.11", "$2.02", "16.4%", "28.7%", "6.2%", "$326.69", "$279.75"):
        check(f"AAPL-shaped: {fig} is extracted ({claims})", fig in claims)
    synth = "Example Devices trades at an EV/EBITDA of 14.2 and a PEG ratio of 1.3, with a price-to-book of 4.75 in fiscal 2026."
    c2 = tracer.extract_numeric_claims(synth)
    check(f"synthetic: bare multiples next to their metric names are claims ({c2})", "14.2" in c2 and "1.3" in c2 and "4.75" in c2 and "2026" not in c2)
    ev = "- P/E (trailing TTM, GAAP EPS): 38.61  [source: yfinance/info]"
    check("a bare P/E traces to the packet's bare value", tracer.trace_claim("38.61", ev) == "CITED")
    from markut.web.events import review_stats
    s = review_stats("Short pre-review verdict with 16.4% only.", ev, RUN26_FINAL, {"final_status": "revised", "resolutions": []})
    check(f"the stats count the FINAL verdict's figures, not the pre-review draft's ({s})", s["claims"] == len(claims) == 14 and s["pre_claims"] == 1)


# ======================================================================
# 7b. the lease-inclusive ratio has a real source in the core figures
# ======================================================================
def _core_from(monkeypatch, html_name):
    import os
    from markut.evidence import core
    html = open(os.path.join(os.path.dirname(__file__), "fixtures", html_name)).read()
    monkeypatch.setattr(core, "core_sources", lambda fs: [{"form": "10-Q", "name": "q.htm", "filing_date": "2026-07-31", "url": "u",
                                                           "html": html, "text": "", "headline_period": "quarter"}])
    return core.extract_core({}, {"segments": [], "company_kpis": []}, None)["text"]


def test_core_figures_carry_lease_liabilities_for_the_ratio(monkeypatch):
    market_text = "\n".join(market.leverage_lines(_bs(84.34e9, 0.0, 107.52e9), {"debtToEquity": 78.44}))
    t = _core_from(monkeypatch, "aapl_10q_lease_note.html")
    check(f"AAPL-shaped: the lease note's total row is in the core figures ({t[-900:]!r})",
          re.search(r"(?m)^- Total lease liabilities: \$?12,600", t) is not None)
    line = assembly.leverage_with_filed_leases(market_text, t)
    check(f"AAPL-shaped: the ratio adds the filed total ({line!r})", line and "$12.60B" in line and "90.2%" in line)
    market2 = "\n".join(market.leverage_lines(_bs(519.0e6, None, 300.0e6), {"debtToEquity": 173.0}))
    t2 = _core_from(monkeypatch, "synthetic_restaurant_balance_sheet.html")
    check("synthetic: both operating lease rows are in the core figures",
          "Current portion of operating lease liabilities" in t2 and "Non-current operating lease liabilities" in t2)
    line2 = assembly.leverage_with_filed_leases(market2, t2)
    # (519 + 160 + 1,311) / 300 = 663.3%; both inputs named, never the current row alone
    check(f"synthetic: no total row -> current + non-current summed, both named ({line2!r})",
          line2 and "663.3%" in line2 and "$160.00M" in line2 and "$1.31B" in line2)


# ======================================================================
# 10b. risk-factor prose in a periodic report is not a missed target
# ======================================================================
def test_periodic_report_risk_language_is_not_an_extraction_failure():
    q = lambda text, form="10-Q": {"label": form, "text": text, "form": form, "filing_date": "2026-07-31", "url": "u", "kind": "text"}
    aapl = ("The Company expects these trends to intensify, which may materially negatively impact the Company's revenue, costs and gross margin. "
            "The Company intends to increase its dividend on an annual basis, subject to declaration by the Board of Directors. "
            "The Company believes it has substantial defenses and intends to vigorously defend itself.")
    synth = ("We plan to open new restaurants in additional markets over time. "
             "We expect commodity inflation and wage pressure to remain a headwind for the foreseeable future.")
    for name, text in (("AAPL-shaped", aapl), ("synthetic restaurant", synth)):
        out = sections.guidance_lines([q(text)])
        check(f"{name} 10-Q: forward prose with no figure is not an EXTRACTION FAILURE ({out['failures']})", out["failures"] == [] and out["lines"] == [])
        from markut.evidence.questions import format_guidance
        check(f"{name}: the block says no business targets and why", "no forward-looking business targets" in "\n".join(format_guidance(out)))
        rel = sections.guidance_lines([q(text, "8-K ex99-1") | {"form": "8-K"}])
        check(f"{name} earnings release: forward language with no readable figure is still a failure", len(rel["failures"]) == 1)
    long_target = ("We expect revenue of $1.20 billion to $1.25 billion for the full fiscal year, " + "reflecting new openings, menu pricing, "
                   "and continued traffic trends across each of our concepts and regions, " * 6 + "as described above.")
    kept = sections.guidance_lines([q(long_target)])
    check("10-Q: a figure-bearing target that could not be isolated is still a failure", len(kept["failures"]) == 1)
