"""The notebook news-test cell (43 checks) migrated verbatim: fixtures and
parameter-injected stubs unchanged; the namespace-leftover sweep now
inspects the news MODULE namespace instead of notebook globals()."""
import os
import time
from datetime import datetime, timezone

import markut.evidence.news as _news_mod
from markut.evidence.news import (CHARS_PER_TOKEN,
    CLAIM_META_WORDS,
    NEWS_BASELINE_MAX,
    NEWS_BASELINE_TOKEN_CAP,
    NEWS_SNIPPET_MAX_CHARS,
    YAHOO_RSS_URL,
    _estimated_item_chars,
    _is_blocked_publisher,
    _load_yahoo_store,
    _update_yahoo_store,
    _yahoo_store_path,
    clean_claim_for_embedding,
    company_identity,
    datetime,
    derive_snippet,
    fetch_accessible_article,
    fetch_yahoo_rss,
    format_news_items,
    get_embedder,
    get_news_candidate_pool,
    get_news_evidence,
    get_related_leads,
    get_targeted_news_evidence,
    hydrate_news_item,
    json,
    os,
    parse_news_review_json,
    parse_yahoo_rss,
    score_news_item,
    select_news_items,
    time,
    timezone)


def news_check(label, condition):
    assert condition, label


def test_news_offline_suite():
    # News offline tests + optional FREE live audit. Run All stays bounded:
    # the audit is disabled by default and no Claude call occurs in this cell.

    print("--- Yahoo news RSS helpers (offline fixture, no network) ---")
    _NEWS_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item><title>NVIDIA reports quarterly revenue growth</title>
        <link>https://www.reuters.com/technology/example1</link>
        <pubDate>Wed, 15 Jul 2026 12:00:00 GMT</pubDate>
        <description><![CDATA[<p>NVIDIA reported higher quarterly revenue and updated guidance.</p>]]></description></item>
      <item><title>Unrelated sports result</title>
        <link>https://example.test/sports/example2</link>
        <pubDate>Wed, 15 Jul 2026 11:00:00 GMT</pubDate>
        <description>Nothing about the company.</description></item>
    </channel></rss>"""
    try:
        _news_items = parse_yahoo_rss(_NEWS_FIXTURE)
        news_check("RSS fixture parses both items", len(_news_items) == 2)
        news_check("publisher derived from the direct link domain",
                   _news_items[0]["publisher"] == "reuters.com")
        news_check("HTML description cleaned", "<p>" not in _news_items[0]["summary"])
        _identity = {"ticker": "NVDA", "company": "NVIDIA CORP"}
        _selected = select_news_items(_news_items, _identity, 5)
        news_check("company disambiguation rejects unrelated result",
                   len(_selected) == 1 and _selected[0]["publisher"] == "reuters.com")
        _formatted = format_news_items(
            [{**_selected[0], "url": "https://www.reuters.com/technology/example1",
              "context": "NVIDIA reported higher quarterly revenue. Management also updated its outlook.",
              "status": "body"}],
            "[NEWS]", "Coverage: fixture", 300,
        )
        news_check("formatted news includes status and source tag",
                   "Status: body" in _formatted and "[source: Yahoo RSS/reuters.com" in _formatted)
        news_check("formatted news respects its cap", len(_formatted) <= 300 * CHARS_PER_TOKEN)
        _review = parse_news_review_json(
            '{"claim_reviews":[{"claim":"x","status":"SUPPORTED"}],'
            '"reasoning":"checked","verdict_changed":false,"revised_verdict":"hold"}'
        )
        news_check("claim-review statuses normalize to the allowed schema",
                   _review["claim_reviews"][0]["status"] == "supported")
    except Exception as e:
        news_check(f"Yahoo news helper tests (unexpectedly raised: {e})", False)

    print("\n--- News layer upgrades: snippets, attempt policy, merge, cap (offline) ---")
    try:
        _identity2 = {"ticker": "NVDA", "company": "NVIDIA CORP"}

        # (a) description mining — a snippet must EARN its packet tokens.
        _title2 = "NVIDIA reports quarterly revenue growth"
        _rich_desc = ("Chipmaker NVIDIA posted record data center sales this quarter and "
                      "raised full-year guidance, with management citing sustained AI "
                      "infrastructure demand from the largest cloud providers.")
        news_check("title-echo description mines snippet=None",
                   derive_snippet(_title2, _title2 + " - Reuters plus related coverage links here") is None)
        news_check("too-short description mines snippet=None",
                   derive_snippet(_title2, "NVIDIA posted record sales.") is None)
        _snip2 = derive_snippet(_title2, _rich_desc)
        news_check("genuinely longer description mines a snippet",
                   _snip2 is not None and _snip2.startswith("Chipmaker NVIDIA"))
        _long_desc = _rich_desc + " " + "Analysts also debated export limits and pricing power at length. " * 8
        _long_snip = derive_snippet(_title2, _long_desc)
        _cut = _long_snip.removesuffix(" [...truncated]") if _long_snip else ""
        news_check("long description truncates inside the snippet cap with a visible marker",
                   _long_snip is not None and len(_long_snip) <= NEWS_SNIPPET_MAX_CHARS
                   and _long_snip.endswith("[...truncated]"))
        news_check("snippet truncation never cuts mid-word",
                   bool(_cut) and _long_desc.startswith(_cut + " "))

        # (b) skip-list matching — the label form AND the domain form must both hit.
        news_check("'Seeking Alpha' publisher label skips",
                   _is_blocked_publisher("Seeking Alpha", "https://example.com/some-article"))
        news_check("seekingalpha.com URL skips",
                   _is_blocked_publisher("Unknown publisher", "https://seekingalpha.com/article/1"))
        news_check("Reuters with a direct URL attempts",
                   not _is_blocked_publisher("Reuters", "https://www.reuters.com/technology/x"))

        # (c) attempt policy — swap _news_mod.fetch_accessible_article for a counting stub
        # (restored in the finally) so the test stays fully offline: policy, not
        # network.
        _fetch_calls = []
        _real_fetch_accessible_article = _news_mod.fetch_accessible_article
        def _counting_fetch(url):
            _fetch_calls.append(url)
            return {"url": url, "text": ""}
        _news_mod.fetch_accessible_article = _counting_fetch
        try:
            _direct = hydrate_news_item(
                {"title": "NVIDIA product update", "publisher": "Reuters",
                 "link": "https://www.reuters.com/technology/abc", "snippet": None, "summary": ""},
                _identity2)
            news_check("direct non-blocked link IS attempted (exactly once)",
                       _fetch_calls == ["https://www.reuters.com/technology/abc"]
                       and _direct["fetch_state"] == "failed (no extractable text)")
            news_check("failed fetch without snippet degrades to headline-only",
                       _direct["status"] == "headline-only")
            _blocked_item = hydrate_news_item(
                {"title": "NVIDIA coverage note", "publisher": "Seeking Alpha",
                 "link": "https://seekingalpha.com/article/1", "snippet": None, "summary": ""},
                _identity2)
            news_check("blocked publisher is skipped, never attempted",
                       len(_fetch_calls) == 1
                       and _blocked_item["fetch_state"] == "skipped (blocked publisher)")
        finally:
            _news_mod.fetch_accessible_article = _real_fetch_accessible_article

        # (d) cap enforcement — 12 snippet-rich candidates against one 800-token budget.
        _topics = ["data center revenue records", "export control investigation report",
                   "gross margin outlook debate", "automotive segment growth story",
                   "buyback program announcement today", "cloud demand forecast update",
                   "supply chain constraint coverage", "analyst rating upgrade note",
                   "networking product launch event", "sovereign AI deal coverage",
                   "gaming segment recovery signs", "capital expenditure plan details"]
        _fake_summary = ("NVIDIA management commentary on this topic ran considerably longer than "
                         "the headline itself, covering plans, expectations and several open "
                         "questions in real depth for readers.")
        _fake_items = [{"title": f"NVIDIA {topic}", "publisher": f"Outlet{n}",
                        "link": f"https://outlet{n}.example.com/nvidia-story-{n}",
                        "published": "2026-07-14T09:30:00+00:00", "summary": _fake_summary,
                        "snippet": derive_snippet(f"NVIDIA {topic}", _fake_summary),
                        "source_feed": "yahoo_rss"} for n, topic in enumerate(_topics)]
        _capped = select_news_items(_fake_items, _identity2, NEWS_BASELINE_MAX,
                                    token_cap=NEWS_BASELINE_TOKEN_CAP)
        news_check("cap stops selection early (fewer than the 9-story ceiling)",
                   0 < len(_capped) < NEWS_BASELINE_MAX)
        _estimate = sum(_estimated_item_chars(item) for item in _capped)
        news_check("running selection estimate never exceeds the 800-token cap",
                   _estimate <= NEWS_BASELINE_TOKEN_CAP * CHARS_PER_TOKEN)
        _leads = [{**item, "url": item["link"], "context": item["snippet"], "status": "lead"}
                  for item in _capped]
        _cap_packet = format_news_items(_leads, "[NEWS]", "Coverage: offline cap test",
                                        NEWS_BASELINE_TOKEN_CAP)
        news_check("formatted packet with snippets also stays within the cap",
                   len(_cap_packet) <= NEWS_BASELINE_TOKEN_CAP * CHARS_PER_TOKEN)
    except Exception as e:
        news_check(f"news upgrade tests (unexpectedly raised: {e})", False)

    print("\n--- related leads: cleaning, ranking, floor, gate, alias (offline) ---")
    # The embedder is mocked with a tiny fake returning HAND-SET vectors and the
    # pool accessor with a fixed list (swap-and-restore, the cell's established
    # pattern) — no model download, no network, so Run All stays fast here.
    from types import SimpleNamespace

    try:
        # (a) meta-word cleaning: pipeline jargon must not reach the embedding
        _lead_claim = "china h200 revenue exclusion lead-only extrapolation"
        _cleaned = clean_claim_for_embedding(_lead_claim)
        news_check("meta-words removed from claim", _cleaned == "china h200 revenue exclusion")
        news_check("no meta-word survives cleaning",
                   not any(w in _cleaned.lower().split() for w in CLAIM_META_WORDS))

        _LEAD_TITLES = ["NVIDIA H200 exports to China resume",
                        "NVIDIA data center revenue sets record",
                        "NVIDIA gaming card pricing update",
                        "NVIDIA automotive segment expands"]
        _lead_pool = [{"title": t, "publisher": f"Outlet{n}",
                       "link": f"https://outlet{n}.example.com/s{n}",
                       "published": "2026-07-15T09:00:00+00:00", "summary": "",
                       "snippet": None, "source_feed": "yahoo_rss"}
                      for n, t in enumerate(_LEAD_TITLES)]
        # Hand-set vectors: cosine against the claim [1, 0] is 1.00 / 0.60 / 0.00 /
        # 0.50, so the expected ranking is titles [0], [1], [3] with [2] cut.
        _LEAD_VECS = {"china h200 revenue exclusion": [1.0, 0.0],
                      _LEAD_TITLES[0]: [1.0, 0.0],
                      _LEAD_TITLES[1]: [0.6, 0.8],
                      _LEAD_TITLES[2]: [0.0, 1.0],
                      _LEAD_TITLES[3]: [0.5, 0.866],
                      "orthogonal topic nobody wrote about": [0.0, 0.0]}

        def _fake_lead_embedder():
            return SimpleNamespace(encode=lambda texts, show_progress_bar=False:
                                   [_LEAD_VECS.get(t, [0.0, 0.0]) for t in texts])

        _real_get_embedder, _real_pool_fn = _news_mod.get_embedder, _news_mod.get_news_candidate_pool
        _news_mod.get_embedder = _fake_lead_embedder
        _news_mod.get_news_candidate_pool = lambda ticker: [dict(item) for item in _lead_pool]
        try:
            _leads_out = get_related_leads([_lead_claim], "NVDA")
            _lead_lines = [l for l in _leads_out.splitlines() if l.startswith("  - ")]
            # (b) ranking order for hand-set vectors + the top-3 cut
            news_check("ranking follows hand-set cosine order",
                       _LEAD_TITLES[0] in _lead_lines[0] and "sim 1.00" in _lead_lines[0]
                       and _LEAD_TITLES[1] in _lead_lines[1] and "sim 0.60" in _lead_lines[1]
                       and _LEAD_TITLES[3] in _lead_lines[2] and "sim 0.50" in _lead_lines[2])
            news_check("top-3 cut respected (4 candidates -> 3 leads)",
                       len(_lead_lines) == 3 and _LEAD_TITLES[2] not in _leads_out)

            # (c) low-similarity floor: labeled fallback block, NEVER empty output
            _low_out = get_related_leads(["orthogonal topic nobody wrote about"], "NVDA")
            news_check("all-low sims -> labeled fallback, never empty",
                       "no closely related leads in current pool" in _low_out
                       and "baseline rank" in _low_out and len(_low_out) > 50)

            # (e) deprecated alias: old name + old argument order, new format out
            news_check("alias get_targeted_news_evidence returns the new format",
                       get_targeted_news_evidence("NVDA", [_lead_claim]) == _leads_out
                       and _leads_out.startswith("[RELATED LEADS — context only, NOT verification]"))
        finally:
            _news_mod.get_embedder, _news_mod.get_news_candidate_pool = _real_get_embedder, _real_pool_fn

        # (d) stale-gate regression. The old claim-review gate matched the retired
        # string "accessible article excerpt" and so could NEVER fire — the bug
        # that silently disabled verification. The gate itself was removed in the
        # demotion; this pins the rule for any future gate: key on the LIVE enum.
        def _gate_rule(text):
            # The corrected form (case matters: per-item "Status: body" lines only,
            # never lowercase prose mentions).
            return "Status: body" in text

        _body_render = format_news_items(
            [{"title": "NVIDIA t", "publisher": "P", "url": "u", "context": "c", "status": "body"}],
            "[NEWS]", "cov", 300)
        _legacy_render = format_news_items(
            [{"title": "NVIDIA t", "publisher": "P", "url": "u", "context": "c",
              "access": "accessible article excerpt"}],
            "[NEWS]", "cov", 300)
        news_check("gate rule: live enum body rendering passes", _gate_rule(_body_render))
        news_check("gate rule: literal legacy string in a field does NOT pass",
                   not _gate_rule(_legacy_render))
        news_check("gate rule: related-leads output can never pass (no body possible)",
                   not _gate_rule(_leads_out) and not _gate_rule(_low_out))
    except Exception as e:
        news_check(f"related-leads tests (unexpectedly raised: {e})", False)

    print("\n--- yahoo-only pipeline: removal, packet filter, identity, store, gate (offline) ---")
    try:
        # (a) Google is GONE — introspect the LIVE namespace and constants, not
        # comments (the removal WHY at the top of the news cell legitimately
        # mentions Google; defined objects must not).
        _g_leftover = [name for name in list(vars(_news_mod))
                       if "google" in name.lower() and name != "_g_leftover"]
        news_check("a: no google-named objects remain in the namespace", _g_leftover == [])
        news_check("a: no google feed URL constant and no wrapper predicate",
                   "NEWS_RSS_URL" not in vars(_news_mod) and "_is_google_wrapper" not in vars(_news_mod)
                   and "news.google.com" not in YAHOO_RSS_URL)

        _t6_now_iso = datetime.fromtimestamp(time.time() - 3600, tz=timezone.utc).isoformat()

        def _t6_item(n, title, snippet=None):
            return {"title": title, "publisher": f"T6Pub{n}",
                    "link": f"https://t6pub{n}.example.com/story{n}",
                    "published": _t6_now_iso, "summary": snippet or "",
                    "snippet": snippet, "source_feed": "yahoo_rss"}

        _T6_SNIP = ("NVIDIA management commentary on this story ran much longer than the "
                    "headline, covering demand, margins and several open questions in depth.")
        _T6_BODY = ("NVIDIA posted record data center revenue this quarter. Management raised "
                    "guidance on sustained AI demand. Analysts flagged export controls as a risk.")

        # (b)+(c) drive the REAL get_news_evidence with swap-and-restore stubs —
        # no network, no EDGAR lookup, statuses controlled by the fetch stub.
        _real_fetch_yahoo = _news_mod.fetch_yahoo_rss
        _real_fetch_article = _news_mod.fetch_accessible_article
        _real_company_identity = _news_mod.company_identity

        def _t6_identity(ticker):
            return {"ticker": ticker.upper(), "company": "NVIDIA CORP"}

        def _t6_fetch_article(url):
            # only story0's URL yields body text; every other fetch "fails"
            return {"url": url, "text": _T6_BODY if "story0" in url else ""}

        _t6_pool = [_t6_item(0, "NVIDIA data center revenue sets record"),
                    _t6_item(1, "NVIDIA guidance outlook impresses analysts", _T6_SNIP),
                    _t6_item(2, "NVIDIA supply chain update from partners", _T6_SNIP),
                    _t6_item(3, "NVIDIA stock moves in premarket trade"),
                    _t6_item(4, "NVIDIA event schedule announced for fall")]

        def _t6_feed(ticker):
            return [dict(item) for item in _t6_pool]

        def _t6_feed_bare(ticker):
            return [dict(_t6_item(3, "NVIDIA stock moves in premarket trade")),
                    dict(_t6_item(4, "NVIDIA event schedule announced for fall"))]

        try:
            _news_mod.fetch_yahoo_rss, _news_mod.fetch_accessible_article = _t6_feed, _t6_fetch_article
            _news_mod.company_identity = _t6_identity
            _t6_packet = get_news_evidence("NVDA")
            news_check("b: mixed statuses -> packet keeps body and lead",
                       "Status: body" in _t6_packet and "Status: lead" in _t6_packet)
            news_check("b: packet contains ZERO headline-only items",
                       "Status: headline-only" not in _t6_packet)
            _news_mod.fetch_yahoo_rss = _t6_feed_bare
            _t6_marker = get_news_evidence("NVDA")
            news_check("c: all headline-only -> fail-soft marker, not empty, not bare headlines",
                       _t6_marker == "\n\n[NEWS]\n[no news items with usable blurb or body in the baseline window]"
                       and "- Headline:" not in _t6_marker)
        finally:
            _news_mod.fetch_yahoo_rss, _news_mod.fetch_accessible_article = _real_fetch_yahoo, _real_fetch_article
            _news_mod.company_identity = _real_company_identity

        # (d)+(e) identity twins: rejection needs NO identity token anywhere;
        # acceptance may come from the blurb alone (title+summary are scored
        # combined — title-only matching would over-filter the depth carriers).
        _t6_id = {"ticker": "NVDA", "company": "NVIDIA CORP"}
        news_check("d: unrelated company item (no NVIDIA/NVDA tokens) rejected",
                   score_news_item({"title": "Rival chipmaker teases flagship accelerator",
                                    "summary": "A competitor updated its product roadmap.",
                                    "published": _t6_now_iso}, _t6_id) < 0)
        news_check("e: NVDA only in the snippet/blurb (not the title) accepted",
                   score_news_item({"title": "Chip stocks extend their winning streak",
                                    "summary": "Gains were led by NVDA after fresh guidance.",
                                    "published": _t6_now_iso}, _t6_id) > 0)

        # (f)+(g) persistent store — real store functions against a throwaway
        # ZZTEST file (never the real ticker stores), removed in the finally.
        _t6_store_file = _yahoo_store_path("ZZTEST")
        try:
            if os.path.exists(_t6_store_file):
                os.remove(_t6_store_file)
            _t6_old_iso = datetime.fromtimestamp(time.time() - 40 * 86400, tz=timezone.utc).isoformat()  # outside the 30-day window
            _batch1 = [{**_t6_item(0, "ZZ story alpha"), "link": "https://pub.example.com/alpha?tsrc=rss"},
                       _t6_item(1, "ZZ story beta"),
                       {**_t6_item(2, "ZZ story ancient"), "published": _t6_old_iso}]
            _batch2 = [{**_t6_item(0, "ZZ story alpha"), "link": "https://pub.example.com/alpha?tsrc=partner"},
                       _t6_item(3, "ZZ story gamma")]
            _after1 = _update_yahoo_store("ZZTEST", _batch1)
            news_check("f: 40-day-old pubDate dropped by the 30-day window",
                       len(_after1) == 2 and all(i["title"] != "ZZ story ancient" for i in _after1))
            _after2 = _update_yahoo_store("ZZTEST", _batch2)
            news_check("f: two fetches, one overlapping story (tracking-param variant) -> deduped",
                       len(_after2) == 3 and sorted(i["title"] for i in _after2)
                       == ["ZZ story alpha", "ZZ story beta", "ZZ story gamma"])
            with open(_t6_store_file, "w", encoding="utf-8") as _t6_f:
                _t6_f.write("{corrupt json!!")
            news_check("g: corrupt store file -> warn + fresh list, no exception escapes",
                       _load_yahoo_store("ZZTEST") == [])
            news_check("g: missing store file -> fresh list, no exception escapes",
                       _load_yahoo_store("ZZNOFILE") == [])
        finally:
            if os.path.exists(_t6_store_file):
                os.remove(_t6_store_file)

        # (h) gate semantics: only "Status: body" may ever count as accessible.
        def _t6_gate(text):
            return "Status: body" in text
        _t6_body_packet = format_news_items(
            [{**_t6_item(0, "NVIDIA gate check story"), "url": "u",
              "context": "Fetched article text.", "status": "body"}], "[NEWS]", "cov", 300)
        _t6_lead_packet = format_news_items(
            [{**_t6_item(1, "NVIDIA other gate story"), "url": "u",
              "context": _T6_SNIP, "status": "lead"}], "[NEWS]", "cov", 300)
        news_check("h: 'Status: body' counts as accessible", _t6_gate(_t6_body_packet))
        news_check("h: 'Status: lead' does not count as accessible", not _t6_gate(_t6_lead_packet))
    except Exception as e:
        news_check(f"yahoo-only pipeline tests (unexpectedly raised: {e})", False)
