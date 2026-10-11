"""Analyst price targets and recent analyst actions (2026-10-11).

Shown in the governed verdict, after the debate's own conclusion and kept out
of the debate, as the analyst consensus is: a low / mean / median / high
target bar against today's price, and the key recent moves — upgrades,
downgrades, initiations, price-target cuts and raises — newest first.
Offline: CELH-shaped rows (cuts and two downgrades), an upgrades-and-
initiation shape, and a stock with no recent action or no coverage."""
from markut.evidence import consensus as cons


def check(label, condition):
    assert condition, label


# the provider's real rows for a beverage maker, August 2026
CELH = [
    {"date": "2026-08-10", "firm": "Stephens & Co.", "to_grade": "Overweight", "from_grade": "Overweight", "action": "main", "pt_action": "Lowers", "pt_now": 50.0, "pt_prior": 65.0},
    {"date": "2026-08-08", "firm": "Maxim Group", "to_grade": "Hold", "from_grade": "Buy", "action": "down", "pt_action": "", "pt_now": 0.0, "pt_prior": 0.0},
    {"date": "2026-08-07", "firm": "UBS", "to_grade": "Buy", "from_grade": "Buy", "action": "main", "pt_action": "Lowers", "pt_now": 44.0, "pt_prior": 50.0},
    {"date": "2026-08-07", "firm": "Citigroup", "to_grade": "Buy", "from_grade": "Buy", "action": "main", "pt_action": "Lowers", "pt_now": 40.0, "pt_prior": 50.0},
    {"date": "2026-08-07", "firm": "Morgan Stanley", "to_grade": "Overweight", "from_grade": "Overweight", "action": "main", "pt_action": "Lowers", "pt_now": 42.0, "pt_prior": 48.0},
    {"date": "2026-08-07", "firm": "JP Morgan", "to_grade": "Overweight", "from_grade": "Overweight", "action": "main", "pt_action": "Lowers", "pt_now": 52.0, "pt_prior": 56.0},
    {"date": "2026-08-07", "firm": "Bernstein", "to_grade": "Market Perform", "from_grade": "Outperform", "action": "down", "pt_action": "Lowers", "pt_now": 26.0, "pt_prior": 44.0},
    {"date": "2026-08-07", "firm": "Piper Sandler", "to_grade": "Overweight", "from_grade": "Overweight", "action": "main", "pt_action": "Lowers", "pt_now": 36.0, "pt_prior": 49.0},
    {"date": "2026-08-06", "firm": "Needham", "to_grade": "Buy", "from_grade": "Buy", "action": "main", "pt_action": "Lowers", "pt_now": 35.0, "pt_prior": 55.0},
    {"date": "2026-07-16", "firm": "Stifel", "to_grade": "Buy", "from_grade": "Buy", "action": "main", "pt_action": "Lowers", "pt_now": 45.0, "pt_prior": 62.0},
    {"date": "2026-07-14", "firm": "Citigroup", "to_grade": "Buy", "from_grade": "Buy", "action": "main", "pt_action": "Lowers", "pt_now": 50.0, "pt_prior": 60.0},
    {"date": "2026-07-09", "firm": "Needham", "to_grade": "Buy", "from_grade": "Buy", "action": "main", "pt_action": "Lowers", "pt_now": 55.0, "pt_prior": 75.0},
    {"date": "2026-05-02", "firm": "Deutsche Bank", "to_grade": "Hold", "from_grade": "Hold", "action": "main", "pt_action": "Lowers", "pt_now": 30.0, "pt_prior": 39.0},
]
UPS = [
    {"date": "2026-09-30", "firm": "Goldman Sachs", "to_grade": "Buy", "from_grade": "Neutral", "action": "up", "pt_action": "Raises", "pt_now": 60.0, "pt_prior": 45.0},
    {"date": "2026-09-20", "firm": "Mizuho", "to_grade": "Outperform", "from_grade": "", "action": "init", "pt_action": "Announces", "pt_now": 55.0, "pt_prior": 0.0},
    {"date": "2026-09-10", "firm": "Barclays", "to_grade": "Overweight", "from_grade": "Overweight", "action": "reit", "pt_action": "Maintains", "pt_now": 50.0, "pt_prior": 50.0},
]


def test_recent_actions_newest_first_with_what_changed():
    a = cons.analyst_actions(CELH, today="2026-10-11", days=90, cap=8)
    texts = [i["text"] for i in a["items"]]
    check(f"CELH-shaped: the Bernstein downgrade says from and to, and the target cut ({texts})",
          any("Bernstein downgraded to Market Perform from Outperform" in t and "$26 from $44" in t for t in texts))
    check("…a downgrade with no target says only the rating", any(t.startswith("Maxim Group downgraded to Hold from Buy") and "$" not in t for t in texts))
    check("…a target cut on a kept rating", any("UBS cut its price target to $44 from $50" in t and "Buy" in t for t in texts))
    check("…newest first", [i["date"] for i in a["items"]] == sorted((i["date"] for i in a["items"]), reverse=True))
    check(f"…capped, rating changes always kept ({len(a['items'])})", len(a["items"]) == 8 and sum(i["kind"] == "downgrade" for i in a["items"]) == 2)
    check(f"…an action older than the window is left out ({texts})", not any("Deutsche Bank" in t for t in texts))
    s = a["summary"]
    check(f"…the window counted ({s})", s["downgrades"] == 2 and s["upgrades"] == 0 and s["target_cuts"] == 10 and s["target_raises"] == 0 and s["days"] == 90)
    u = cons.analyst_actions(UPS, today="2026-10-11")
    ut = [i["text"] for i in u["items"]]
    check(f"upgrade and initiation shapes ({ut})", "Goldman Sachs upgraded to Buy from Neutral; price target $60 from $45" in ut[0]
          and "Mizuho initiated at Outperform; price target $55" in ut[1])
    check("…a reiteration with an unchanged target is not a key move", not any("Barclays" in t for t in ut))
    old = cons.analyst_actions(CELH, today="2027-03-01", days=90)
    check(f"no action in the window: none listed, the latest one named ({old['latest']})",
          old["items"] == [] and old["latest"] and old["latest"]["firm"] == "Stephens & Co.")
    check("no rows at all", cons.analyst_actions([], today="2026-10-11") == {"items": [], "summary": None, "latest": None})


def test_the_consensus_carries_targets_and_actions():
    c = cons.analyst_consensus({"recommendationMean": 2.0, "numberOfAnalystOpinions": 21, "currentPrice": 28.58, "currency": "USD"},
                               None, {"current": 28.58, "high": 64.0, "low": 26.0, "mean": 42.19, "median": 41.0},
                               actions=cons.analyst_actions(CELH, today="2026-10-11"))
    check(f"targets low/mean/median/high and the price ({c})", c["target_low"] == 26.0 and c["target_median"] == 41.0
          and c["target_high"] == 64.0 and c["price"] == 28.58 and c["actions"]["summary"]["downgrades"] == 2)
    check("upside to the mean target", abs(cons.target_upside(c) - (42.19 / 28.58 - 1) * 100) < 0.01)
    check("no targets -> no upside", cons.target_upside({"price": 10.0}) is None)


def test_the_page_data_and_the_pdf_show_the_section():
    import re
    import pypdfium2 as pdfium
    from markut.report import build_report
    c = cons.analyst_consensus({"recommendationMean": 2.0, "numberOfAnalystOpinions": 21, "currentPrice": 28.58, "currency": "USD"},
                               None, {"current": 28.58, "high": 64.0, "low": 26.0, "mean": 42.19, "median": 41.0},
                               actions=cons.analyst_actions(CELH, today="2026-10-11"))
    run = {"id": 1, "ticker": "CELH", "started_at": "2026-10-11", "events": [
        {"event": "start", "data": {"ticker": "CELH", "max_rounds": 2}},
        {"event": "review", "data": {"verdict": "v. Research, not investment advice.", "stats": {}, "consensus": c,
                                     "lean": {"lean": "neutral", "confidence": "low", "reason": "offset"}}}]}
    doc = pdfium.PdfDocument(build_report(run))
    t = re.sub(r"\s+", " ", " ".join(doc[i].get_textpage().get_text_range() for i in range(len(doc))))
    check("the PDF names the section and keeps it out of the debate", "Analyst price targets" in t and "not used in the debate" in t)
    check(f"…the low, mean, high and price ({t[t.find('Analyst price targets'):][:400]!r})",
          "$26.00" in t and "$42.19" in t and "$64.00" in t and "$28.58" in t and "+47.6%" in t)
    check("…and the recent moves", "Bernstein downgraded to Market Perform from Outperform" in t and "2 downgrades" in t)
    none = dict(run, events=[run["events"][0], {"event": "review", "data": {"verdict": "v.", "stats": {}, "consensus": None,
                                                                             "lean": {"lean": "neutral", "confidence": "low", "reason": "x"}}}])
    t2 = " ".join(pdfium.PdfDocument(build_report(none))[i].get_textpage().get_text_range() for i in range(1))
    check("no coverage: no section", "Analyst price targets" not in t2)
