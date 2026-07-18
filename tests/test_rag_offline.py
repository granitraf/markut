"""Migrated verbatim: sentence-window chunking, [FILINGS] caps, and the
retrieval-hardening regressions (audit specimens S1-S3, fake collection
passed as a parameter; the real LOCAL embedder runs — free, no network)."""
from types import SimpleNamespace

import markut.evidence.rag as _rag_mod
from markut.evidence.rag import (split_sentences, chunk_sections,
    truncate_to_budget, preview_complete_sentences, format_theme_block,
    enforce_global_cap, retrieve_theme, is_boilerplate_chunk,
    add_number_context, months_old, CHARS_PER_TOKEN, SIM_FLOOR,
    BOILERPLATE_PHRASES, GLOBAL_TOKEN_CAP)


def check(label, condition):
    assert condition, label


def test_sentence_window_chunking_block():
    print("\n--- sentence-window chunking (offline — synthetic text, no network) ---")

    # (a) split_sentences: cuts after ./!/? before a capital or digit; decimals
    # inside numbers must NOT split ("$4.5 billion" stays whole — no space after
    # the dot, so the pattern can't fire).
    try:
        _sents = split_sentences("Revenue grew 12% in Q1. Margins fell. Why? Mix shift.")
        check("split_sentences cuts at sentence punctuation",
              _sents == ["Revenue grew 12% in Q1.", "Margins fell.", "Why?", "Mix shift."])
        _dec = split_sentences("Revenue was $4.5 billion. It grew fast.")
        check("decimal points inside numbers do not split sentences",
              _dec == ["Revenue was $4.5 billion.", "It grew fast."])
    except Exception as e:
        check(f"split_sentences (unexpectedly raised: {e})", False)

    # (b) Search cores hold 2-3 sentences; stored display text adds one neighboring
    # sentence on each side. Consecutive cores stride by two sentences, and every
    # window carries positional metadata for overlap deduplication.
    _text = " ".join(f"Sentence {i} explains filing fact {i}." for i in range(10))
    try:
        _chunks = chunk_sections(
            {"Item 1A": _text}, "10-K", "2026-02-25", "http://example.test",
            risk_title="A material test risk could harm the business.",
        )
        check("multiple sentence windows created", len(_chunks) >= 4)
        check("focused embed cores contain 2-3 sentences",
              all(2 <= len(split_sentences(c["embed_text"].split("\n", 1)[-1])) <= 3
                  for c in _chunks))
        check("stored windows include one sentence of surrounding context",
              _chunks[1]["metadata"]["window_start"] == 1
              and _chunks[1]["metadata"]["core_start"] == 2
              and _chunks[1]["metadata"]["core_end"] == 5
              and _chunks[1]["metadata"]["window_end"] == 6)
        check("consecutive search cores overlap by one sentence",
              _chunks[0]["metadata"]["core_end"] - _chunks[1]["metadata"]["core_start"] == 1)
        check("every displayed window ends on a sentence boundary",
              all(c["text"].endswith(".") for c in _chunks))
        check("risk title retained in every child window",
              all(c["metadata"].get("risk_title") == "A material test risk could harm the business."
                  and "A material test risk could harm the business." in c["embed_text"]
                  for c in _chunks))
        check("source and positional metadata attached to every window",
              all(c["metadata"].get("form") == "10-K"
                  and c["metadata"].get("section") == "Item 1A"
                  and c["metadata"].get("filing_date") == "2026-02-25"
                  and c["metadata"].get("url") == "http://example.test"
                  and "window_start" in c["metadata"] and "window_end" in c["metadata"]
                  for c in _chunks))
    except Exception as e:
        check(f"chunk_sections on synthetic text (unexpectedly raised: {e})", False)

    # (c) a section dict holding an unavailable-marker string must produce ZERO
    # chunks — markers are diagnostics for humans, never content to embed.
    try:
        _m = chunk_sections(
            {"Item 7": "[section unavailable: Item 7 (MD&A) not located in this 10-K]"},
            "10-K", "2026-02-25", "http://example.test")
        check("unavailable-marker section yields zero chunks", _m == [])
    except Exception as e:
        check(f"marker-section handling (unexpectedly raised: {e})", False)


def test_filings_caps_block():
    print("\n--- [FILINGS] token/char caps (offline — fake theme results) ---")

    # (b) oversized fake theme: 4 x 2,000-word excerpts forced through an
    # 850-token theme cap, then 3 huge blocks through the 10,000-char global cap.
    _fake = [{"text": " ".join(["word"] * 2000),
              "metadata": {"form": "10-K", "section": "Item 1A",
                           "filing_date": "2026-02-25", "url": "http://example.test"}}
             for _ in range(4)]
    try:
        _block = format_theme_block("Key risks", _fake, 850)
        _quotes = [l for l in _block if l.startswith('- "')]
        _budget = (850 * CHARS_PER_TOKEN) // 4  # even split across the 4 excerpts
        check("per-theme cap: every excerpt within its per-excerpt budget",
              len(_quotes) == 4 and all(len(q) <= _budget + 10 for q in _quotes))
        check("truncation is visibly marked, never silent",
              all("[...truncated]" in q for q in _quotes))
        _big = [format_theme_block(t, _fake, 5000) for t in ("A", "B", "C")]
        _capped = enforce_global_cap(_big, 10_000)
        _total = sum(len(l) for b in _capped for l in b)
        _tags = sum(1 for b in _capped for l in b if l.startswith("  [source:"))
        check(f"global cap never blown ({_total:,} chars <= 10,000)", _total <= 10_000)
        check("all 12 source tags survive global truncation", _tags == 12)
    except Exception as e:
        check(f"cap enforcement (unexpectedly raised: {e})", False)

    # empty retrieval -> one explicit '[theme unavailable...]' line, no crash.
    try:
        _empty = format_theme_block("Guidance & outlook", [], 600)
        check("empty theme -> '[theme unavailable...]' line",
              _empty[1].startswith("[theme unavailable"))
    except Exception as e:
        check(f"empty-theme marker (unexpectedly raised: {e})", False)


def test_retrieval_hardening_block():
    print("\n--- retrieval hardening: boilerplate, risk dedup, number-context (offline) ---")
    # Each S-fixture below is a VERBATIM specimen from the NVDA retrieval audit —
    # every observed failure became a named filter rule plus this regression test.
    # The selection pipeline runs against a FAKE collection and fake local models
    # (swap-and-restore, the same pattern the news tests use for the fetch stub):
    # zero network, zero model loads, so Run All stays fast and offline here.
    from types import SimpleNamespace

    _S1_CROSSREF = ("A discussion regarding our financial condition and results of operations "
                    "for fiscal year 2025 compared to fiscal year 2024 can be found under "
                    "Item 7 in our Annual Report on Form 10-K ... available free of charge "
                    "on the SEC's website")
    _S2_SCAFFOLD = ("25 Results of Operations The following table sets forth, for the periods "
                    "indicated, certain items in our Condensed Consolidated Statements of "
                    "Income expressed as a percentage of revenue.")
    _S3_TRANSITION = ("Any one of those risks could harm our business, financial condition and "
                      "results of operations or reputation, which could cause our stock price "
                      "to decline. Additional risks, trends and uncertainties not presently "
                      "known to us or that we currently believe are immaterial may also harm "
                      "our business...")
    _RISK_NORMAL = ("We depend on a small number of customers for a significant portion of our "
                    "revenue. The loss of any of these customers would materially reduce our "
                    "revenue and operating results.")
    _MARGINS_KEEPER = ("Gross margin was approximately flat sequentially ... Operating expenses "
                       "were up 52% from a year ago ...")

    # (a) the three audit specimens are boilerplate; (b) real content is not.
    try:
        check("S1 cross-reference pointer flagged as boilerplate", is_boilerplate_chunk(_S1_CROSSREF))
        check("S2 table scaffold flagged as boilerplate", is_boilerplate_chunk(_S2_SCAFFOLD))
        check("S3 generic risk transition flagged as boilerplate", is_boilerplate_chunk(_S3_TRANSITION))
        check("normal risk paragraph passes the filter", not is_boilerplate_chunk(_RISK_NORMAL))
        check("margins/opex keeper passes the filter", not is_boilerplate_chunk(_MARGINS_KEEPER))
    except Exception as e:
        check(f"boilerplate filter tests (unexpectedly raised: {e})", False)


    def _rt_meta(n, title=""):
        # Disjoint windows (n*10) so the positional-overlap dedup never fires here;
        # these tests isolate the boilerplate filter and the risk-title dedup.
        meta = {"form": "10-K", "section": "Item 1A", "filing_date": "2026-02-25",
                "url": "http://fixture.test/10k", "window_start": n * 10,
                "window_end": n * 10 + 3, "core_start": n * 10, "core_end": n * 10 + 2}
        if title:
            meta["risk_title"] = title
        return meta


    def _rt_coll(rows):
        # Fakes the TWO collection methods retrieve_theme touches. rows are
        # (id, text, metadata) triples; every query returns them in order at a
        # similarity of 0.8, safely above SIM_FLOOR.
        def _rt_query(query_embeddings, n_results, where):
            take, n = rows[:n_results], len(query_embeddings)
            return {"ids": [[r[0] for r in take]] * n,
                    "documents": [[r[1] for r in take]] * n,
                    "metadatas": [[r[2] for r in take]] * n,
                    "distances": [[0.2] * len(take)] * n}
        return SimpleNamespace(count=lambda: 40, query=_rt_query)


    def _rt_fake_embedder():
        return SimpleNamespace(encode=lambda texts, show_progress_bar=False:
                               [SimpleNamespace(tolist=lambda: [0.0, 0.0]) for _ in texts])


    def _rt_fake_reranker():
        # Descending scores by pair position -> reranking preserves fixture order,
        # so each test controls exactly which candidate the pick loop meets first.
        return SimpleNamespace(predict=lambda pairs: list(range(len(pairs), 0, -1)))


    _real_get_embedder, _real_get_reranker = _rag_mod.get_embedder, _rag_mod.get_reranker
    _rag_mod.get_embedder, _rag_mod.get_reranker = _rt_fake_embedder, _rt_fake_reranker
    try:
        # (c) one-risk-one-slot: titles [A, A, B, C] with k=3 -> [A, B, C]
        _rows_c = [
            ("c0", "Our cybersecurity systems face persistent intrusion attempts. A breach could disrupt operations.", _rt_meta(0, "A")),
            ("c1", "Attackers may also target our incident response processes. Remediation could be slow and costly.", _rt_meta(1, "A")),
            ("c2", "Foundry capacity constraints could limit our wafer supply. Packaging bottlenecks would delay shipments.", _rt_meta(2, "B")),
            ("c3", "Export licensing requirements could restrict sales abroad. New rules may expand over time.", _rt_meta(3, "C")),
        ]
        _picked_c, _ = retrieve_theme(_rt_coll(_rows_c), "risks", ["q"], ["Item 1A"], k=3)
        check("risk dedup: titles [A,A,B,C] with k=3 -> [A,B,C]",
              [p["metadata"]["risk_title"] for p in _picked_c] == ["A", "B", "C"])

        # (d) 8 candidates: 3 boilerplate (S1-S3, reranked into slots 1/3/5) plus
        # 5 real chunks of which 2 share title D; k=3 must still come back full,
        # non-boilerplate, and title-distinct.
        _rows_d = [
            ("d0", _S2_SCAFFOLD, _rt_meta(0)),
            ("d1", "Demand from data center customers may fluctuate sharply. Order cancellations would reduce revenue.", _rt_meta(1, "D")),
            ("d2", _S1_CROSSREF, _rt_meta(2)),
            ("d3", "A concentrated customer base heightens fluctuation risk. Losing one buyer would hurt results.", _rt_meta(3, "D")),
            ("d4", _S3_TRANSITION, _rt_meta(4)),
            ("d5", "Competitors may introduce superior accelerated computing products. Pricing pressure could compress margins.", _rt_meta(5, "E")),
            ("d6", "Long-term purchase commitments may exceed actual demand. Inventory write-downs would follow.", _rt_meta(6, "F")),
            ("d7", "Government regulation of artificial intelligence is evolving. Compliance costs could rise materially.", _rt_meta(7, "G")),
        ]
        _picked_d, _ = retrieve_theme(_rt_coll(_rows_d), "risks", ["q"], ["Item 1A"], k=3)
        _titles_d = [p["metadata"]["risk_title"] for p in _picked_d]
        check("filter+dedup+k: 3 non-boilerplate, title-distinct chunks returned",
              len(_picked_d) == 3 and len(set(_titles_d)) == 3
              and not any(is_boilerplate_chunk(p["text"]) for p in _picked_d))
    except Exception as e:
        check(f"retrieval selection tests (unexpectedly raised: {e})", False)
    finally:
        _rag_mod.get_embedder, _rag_mod.get_reranker = _real_get_embedder, _real_get_reranker

    # (e) number-context guard, driven through a fake .get()-only collection.
    try:
        _pred_doc = ("Total revenue reached a record for the quarter. "
                     "Data Center revenue represented the largest share of the total.")
        _pred_rows = {"documents": [_pred_doc], "metadatas": [_rt_meta(0)]}
        _get_coll = SimpleNamespace(get=lambda where=None, include=None: _pred_rows)
        _numeric = {"text": "42% of total revenue came from two customers. We expect this concentration to persist.",
                    "metadata": _rt_meta(1)}
        _decorated = add_number_context(_get_coll, _numeric)
        check("42%-leading chunk gains '[context] ' + predecessor's final sentence",
              _decorated["text"].startswith(
                  "[context] Data Center revenue represented the largest share of the total. "
                  "42% of total revenue"))
        _prose = {"text": "Management expects demand to remain strong. Supply conditions improved during the quarter.",
                  "metadata": _rt_meta(1)}
        check("prose-leading chunk untouched by the guard",
              add_number_context(_get_coll, _prose) is _prose)
    except Exception as e:
        check(f"number-context guard tests (unexpectedly raised: {e})", False)
