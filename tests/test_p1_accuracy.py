"""P1 (run #28 TSM / run #27 AAPL audit): labels and evidence that are
literally true. (1) honest rounding traces; (2) only the closer debt/equity
line 'matches'; (3) no meta-text in the verdict; (4) the claim review's
filings addendum is recorded and shown; (5) question excerpts start on their
question's topic; (6) a fetched article must match its headline in its lead.
Offline; AAPL-shaped, TSM-shaped and synthetic cases."""
import json

import pandas as pd

from markut.evidence import market, news
from markut.evidence.questions import focus_excerpt
from markut.guardrails import tracer


def check(label, condition):
    assert condition, label


# ---------------------------------------------------------------- (1) rounding
AAPL_DATA = "\n".join(["[QUOTE & VALUATION]", "- P/E (trailing TTM, GAAP EPS): 38.61  [source: yfinance/info]",
                       "[CORE FIGURES]", "- Revenue: Total net sales: 109,417 million (quarter) vs 94,036 million year-ago; change +16.4%  [source: T1]"])
TSM_DATA = "\n".join(["[FUNDAMENTALS]", "- Profit margin (TTM): 49.92%  [source: yfinance/info]",
                      "- Gross margin (TTM): 64.23%  [source: yfinance/info]"])


def test_honest_rounding_traces_to_its_data_line():
    check("AAPL-shaped: '38.6x trailing P/E' is 38.61 rounded",
          tracer.rounded_source("38.6x", "At a 38.6x trailing P/E the stock trades above its own range.", AAPL_DATA) == "38.61")
    check("AAPL-shaped: 'revenue grew 16%' is +16.4% rounded", tracer.rounded_source("16%", "Revenue grew 16% in the quarter.", AAPL_DATA) == "16.4%")
    check("TSM-shaped: '64% gross margin' is 64.23% rounded",
          tracer.rounded_source("64%", "Can gross margin of 64% hold?", TSM_DATA) == "64.23%")
    check("TSM-shaped: 'net margin near 50%' is the 49.92% profit margin rounded",
          tracer.rounded_source("50%", "and net margin near 50% be maintained", TSM_DATA) == "49.92%")
    check("TSM run #28's own wording: '64% gross and 50% net margins' — a plural label word still matches",
          tracer.rounded_source("50%", "Whether 64% gross and 50% net margins hold as depreciation from capex rises.", TSM_DATA) == "49.92%")
    check("a rounding match needs the same label: 'revenue rose 64%' is not the gross margin",
          tracer.rounded_source("64%", "Revenue rose 64% this year.", TSM_DATA) is None)
    check("a different value is not a rounding", tracer.rounded_source("40%", "gross margin of 40%", TSM_DATA) is None)


def test_governor_accepts_rounding_without_a_revision_call(monkeypatch):
    from markut.agents import llm
    from markut.guardrails import review
    def boom(*a, **k):
        raise AssertionError("rounded figures need no revision call")
    monkeypatch.setattr(llm, "call_claude", boom)
    out = review.review_node({"verdict": "At a 38.6x trailing P/E, with revenue up 16%, the price is full. Research, not investment advice.",
                              "evidence": AAPL_DATA})
    check(f"AAPL-shaped: clean, no tags ({out['review_report']['final_status']}: {out['verdict'][:90]!r})",
          out["review_report"]["final_status"] == "clean" and "UNGROUNDED" not in out["verdict"])
    src = {s["figure"]: s for s in out["review_report"]["sources"]}
    check(f"the source list says what each was rounded from ({src.get('38.6x')})",
          src.get("38.6x", {}).get("kind") == "cited" and "rounded from 38.61" in src["38.6x"]["line"])


# ---------------------------------------------------------------- (2) debt/equity
def _bs(debt, leases, equity):
    row = {"Total Debt": debt, "Stockholders Equity": equity}
    if leases is not None:
        row["Capital Lease Obligations"] = leases
    return pd.DataFrame({pd.Timestamp("2026-06-30"): row})


def test_only_the_closer_debt_equity_line_matches():
    tsm = market.leverage_lines(_bs(1.06e12, 33.28e9, 6.43e12), {"debtToEquity": 16.5}, ccy="TWD")
    inc, exc = tsm[0], tsm[1]
    check(f"TSM-shaped: the line including leases (16.5%) is the provider's ({inc[-140:]!r})", "closest to the yfinance" in inc or "matches the yfinance" in inc)
    check(f"TSM-shaped: the line excluding leases (16.0%) is not called a match ({exc[-140:]!r})", "matches" not in exc and "closest" not in exc)
    tiny = market.leverage_lines(_bs(1.06e12, 3.0e9, 6.43e12), {"debtToEquity": 16.5}, ccy="TWD")
    check(f"tiny leases (both lines within rounding of 16.50): one line, and the wording says it cannot tell ({tiny[0][-150:]!r})",
          "too small" in tiny[0] and "matches" not in tiny[1] and "closest" not in tiny[1])
    lease_heavy = market.leverage_lines(_bs(2081.0e6, 1471.0e6, 519.0e6), {"debtToEquity": 401.0})
    check("synthetic lease-heavy: the including line matches, the excluding one does not",
          "matches the yfinance" in lease_heavy[0] and "matches" not in lease_heavy[1])
    aapl = market.leverage_lines(_bs(84.34e9, None, 107.52e9), {"debtToEquity": 78.44})
    check("AAPL-shaped: a single line still says it matches", "matches the yfinance Debt/Equity figure" in aapl[0])


# ---------------------------------------------------------------- (3) meta-text
def test_no_meta_text_in_the_verdict(monkeypatch):
    from markut.agents import llm, prompts
    from markut.guardrails import review
    check("the judge is told to write the figure, never point at the page", "never refer to the page" in prompts.JUDGE_SYSTEM_PROMPT)
    monkeypatch.setattr(llm, "call_claude", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call needed")))
    v = ("At consensus EPS the price implies roughly flat returns, after a strong year-to-date run (the page lists the exact "
         "figure and its source). Margins held (see the sources below). Research, not investment advice.")
    out = review.review_node({"verdict": v, "evidence": TSM_DATA})["verdict"]
    check(f"TSM run #28: the pointer to the page is removed ({out[:160]!r})", "the page lists" not in out and "see the sources" not in out
          and "year-to-date run." in out and "Margins held." in out)


# ---------------------------------------------------------------- (4) the claim review's addendum
ADDENDUM = ('[FILINGS ADDENDUM — passages found in the indexed filings for the flagged claims]\n'
            '- $0.11: "diluted earnings per share of $2.02 included a favorable impact of $0.11 from tariff refunds"  '
            '[source: EDGAR/8-K press release, filed 2026-07-30]')


def test_the_addendum_is_recorded_and_shown():
    from markut.web import events as ev
    base = "[QUOTE & VALUATION]\n- Price (current): $336.64  [source: yfinance/info]"
    upd = {"claim_verification": {"claim_reviews": [], "reasoning": "r"}, "evidence": base + "\n\n" + ADDENDUM}
    e = ev.event_for("news_verify", upd, {"evidence": base}, {"evidence": base + "\n\n" + ADDENDUM})
    check(f"AAPL run #27: the claim-review event carries the addendum ({str(e['data'].get('addendum'))[:80]!r})",
          "$0.11" in (e["data"].get("addendum") or ""))
    none = ev.event_for("news_verify", {"claim_verification": {}, "evidence": base}, {"evidence": base}, {"evidence": base})
    check("TSM-shaped: no addendum -> empty, never invented", not none["data"].get("addendum"))
    import pypdfium2 as pdfium
    from markut.report import build_report
    run = {"id": 1, "ticker": "AAPL", "started_at": "2026-10-11", "events": [
        {"event": "start", "data": {"ticker": "AAPL", "max_rounds": 2}},
        {"event": "research", "data": {"evidence": base}},
        {"event": "news_verify", "data": e["data"]},
        {"event": "review", "data": {"verdict": "v. Research, not investment advice.", "stats": {}}}]}
    doc = pdfium.PdfDocument(build_report(run))
    text = " ".join(doc[i].get_textpage().get_text_range() for i in range(len(doc)))
    check("the PDF appendix shows the addendum the claim review used", "filings addendum" in text.lower() and "tariff refunds" in text)


# ---------------------------------------------------------------- (5) excerpts start on the question's topic
AAPL_SEGS = ["Americas", "Europe", "Greater China", "Japan", "Rest of Asia Pacific", "iPhone", "Mac", "iPad",
             "Wearables, Home and Accessories", "Services"]


def test_excerpts_start_on_the_questions_topic():
    q1 = {"id": "Q1", "question": "Is the iPhone growth rate, driven by Pro model mix, sustainable?", "kpis": ["iPhone net sales"]}
    t1 = ("The strength in the renminbi relative to the U.S. dollar had a favorable year-over-year impact on Greater China net sales "
          "during the third quarter. iPhone net sales increased during the third quarter due to higher net sales of Pro models.")
    f1 = focus_excerpt(q1, t1, AAPL_SEGS)
    check(f"AAPL run #27 Q1: starts at the iPhone sentence ({f1[:60]!r})", f1.startswith("iPhone net sales increased") and "renminbi" not in f1)
    q2 = {"id": "Q2", "question": "Can Services keep growing double digits?", "kpis": ["Services net sales"]}
    t2 = ("Wearables, Home and Accessories net sales increased during the third quarter compared to the same quarter in 2025. "
          "Services net sales increased due to higher net sales of advertising and cloud services.")
    check("AAPL run #27 Q2: the Wearables paragraph is dropped", focus_excerpt(q2, t2, AAPL_SEGS).startswith("Services net sales"))
    q5 = {"id": "Q5", "question": "Is the Mac strength, against iPad's decline, a durable upgrade trend?", "kpis": ["Mac net sales"]}
    t5 = "Japan net sales increased during 2025 compared to 2024 primarily due to higher net sales of iPhone, Services and iPad."
    check("AAPL run #27 Q5: a Japan paragraph is not a Mac / iPad excerpt", focus_excerpt(q5, t5, AAPL_SEGS) == "")
    segs = ["Harbor Grill", "Pasta Lane"]
    qs = {"id": "Q1", "question": "Can Pasta Lane keep comparable sales growth as it opens units?", "kpis": []}
    ts = "Harbor Grill comparable sales rose 2% on menu pricing. Pasta Lane comparable sales rose 6% as it opened four new units."
    check("synthetic restaurant: starts at the brand asked about", focus_excerpt(qs, ts, segs).startswith("Pasta Lane comparable sales"))
    qt = {"id": "Q3", "question": "How large is the capital expenditure plan relative to operating cash flow?", "kpis": []}
    tt = ("The Board approved a cash dividend of NT$7.0 per share. Capital expenditures for the quarter were US$10.3 billion, "
          "funded by operating cash flow of NT$611 billion.")
    check("TSM-shaped (no segments): topic words decide where the excerpt starts",
          focus_excerpt(qt, tt, []).startswith("Capital expenditures for the quarter"))


# ---------------------------------------------------------------- (6) a fetched article must match its headline
def test_the_article_lead_must_match_its_headline():
    ident = {"company": "Apple Inc.", "ticker": "AAPL"}
    ubs_title = "Apple's App Store Growth Improved in September. What UBS Sees Coming in October Is Harder"
    ubs_body = ("Apple valuation comparison reveals which company has the stronger investment case. The earnings outlook shows why the "
                "recovery matters. EPS is expected to grow 18.38% in 2026, slow to 8.51% in 2027, and then accelerate. " * 3 +
                "Related headlines: Apple's App Store Growth Improved in September. What UBS Sees Coming in October Is Harder.")
    check("AAPL run #27: a valuation article that only lists the UBS headline under 'related' is not that article",
          news.body_matches_headline(ubs_title, ubs_body, ident) is False)
    huxe = ("Apple discloses deal to hire team and license tech from personalized podcast startup Huxe",
            "Apple revealed in a regulatory filing that it has reached an agreement to bring on team members and technology from "
            "personalized audio startup Huxe, in what's commonly known as a reverse acqui-hire deal.")
    check("AAPL run #27: the Huxe article matches", news.body_matches_headline(*huxe, ident))
    munster = ("Apple's iPhone 18 Pro Demand Is Likely Slipping — But Gene Munster Says That's Not The Real Story",
               "Apple Inc.'s (AAPL) iPhone 18 Pro demand may be losing some momentum after a strong start, but Deepwater Asset "
               "Management's Gene Munster believes the latest data needs to be viewed in the broader context.")
    check("AAPL run #27: the Munster article matches", news.body_matches_headline(*munster, ident))
    t_ident = {"company": "Taiwan Semiconductor Manufacturing", "ticker": "TSM"}
    check("TSM-shaped: a matching monthly-revenue article matches",
          news.body_matches_headline("TSMC September Revenue Jumps 31% on AI Demand",
                                     "Taiwan Semiconductor said September revenue rose 31.4% from a year earlier as AI chip demand stayed strong.", t_ident))
    check("TSM-shaped: an unrelated foundry article under that headline does not",
          news.body_matches_headline("TSMC September Revenue Jumps 31% on AI Demand",
                                     "Intel announced layoffs at its foundry unit as it reorganizes manufacturing. " * 3 +
                                     "More: TSMC September Revenue Jumps 31% on AI Demand", t_ident) is False)


def test_page_headers_and_company_boilerplate_do_not_hide_off_topic_text():
    from markut.evidence.questions import excerpt_relevant
    q1 = {"id": "Q1", "question": "Is the iPhone growth rate sustainable?", "kpis": ["iPhone net sales"]}
    t = ("Apple Inc. | Q3 2026 Form 10-Q | 14 Japan Japan net sales increased during the third quarter compared to the same "
         "quarter in 2025 primarily due to higher net sales of iPhone.")
    check("AAPL live: a running page header does not hide that the paragraph is Japan's", focus_excerpt(q1, t, AAPL_SEGS) == "")
    segs = ["Harbor Grill", "Pasta Lane"]
    qs = {"id": "Q1", "question": "Can Pasta Lane keep comparable sales growth?", "kpis": []}
    ts = "Harbor Holdings | 2026 Form 10-Q | 22 Harbor Grill Harbor Grill comparable sales rose 2%, helped by Pasta Lane cross-promotion."
    check("synthetic: the same with a restaurant brand", focus_excerpt(qs, ts, segs) == "")
    q5 = {"id": "Q5", "question": "Is the Mac strength, against iPad's decline, durable?", "kpis": ["Mac net sales"]}
    check("AAPL live: the release's 'leads the world in innovation' boilerplate is not a Mac / iPad excerpt",
          not excerpt_relevant(q5, "Today, Apple leads the world in innovation with iPhone, iPad, Mac, AirPods and Apple Watch.", AAPL_SEGS))
    check("synthetic: 'is a leading operator' boilerplate is not a brand excerpt",
          not excerpt_relevant(qs, "Harbor Holdings is a leading operator of casual dining restaurants, including Pasta Lane and Harbor Grill.", segs))
