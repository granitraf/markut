"""Filings RAG: chunk -> embed -> index -> fenced themed retrieval +
deterministic blocks (notebook cells 22/24, verbatim; model names from
markut.config). Lazy singletons preserved: _EMBEDDER/_RERANKER,
_FILING_INDEX_CACHE session reuse guard."""
from markut import config
from markut.evidence.edgar import (ticker_to_cik, get_filing_index,
    find_latest_filings, fetch_filing_html, extract_10k_sections,
    extract_10q_sections, extract_8k_press_release, extract_risk_titles,
    extract_outlook, extract_10k_business, business_overview,
    extract_10q_commitments, dollar_sentences, extract_highlights,
    concentration_sentences, normalize_dollars)
from markut.guardrails.tracer import extract_numeric_claims

import re
from sentence_transformers import SentenceTransformer, CrossEncoder
import chromadb

# ---------------- RAG index: chunk -> embed -> ChromaDB ----------------

def split_sentences(text: str) -> list:
    # PURE. Crude sentence splitter: cut after . ! ? when the next word starts
    # with a capital, digit, or quote. WHY not a library: this handles filing
    # prose well enough, and the lookahead keeps decimals ("$4.5 billion") from
    # producing fake sentence breaks — there is no space after the dot inside
    # a number, so the split pattern can never fire there.
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])", text)
    return [p.strip() for p in parts if p.strip()]


def chunk_sections(sections: dict, form: str, filing_date: str, url: str,
                   core_sentences: int = 3, stride_sentences: int = 2,
                   context_sentences: int = 1, risk_title: str = "") -> list:
    # Sentence-window retrieval: EMBED a focused 2-3 sentence core, but STORE
    # one complete neighboring sentence on either side for human/model context.
    # This keeps MiniLM's ~256-token input focused on the claim that should match
    # a query while preventing the displayed evidence from becoming an isolated
    # sentence with no setup or consequence.
    #
    # stride=2 makes consecutive cores overlap by one sentence. Retrieval later
    # deduplicates overlapping selected windows, so the index gets high recall
    # without spending multiple final evidence slots on the same passage.
    #
    # risk_title is copied into EVERY child window's metadata and embedding text.
    # Long Item 1A risks therefore retain management's original bold caption even
    # when the retrieved window comes from the middle of that risk's body.
    chunks = []
    for section_name, text in sections.items():
        if text.startswith("[section unavailable") or text.startswith("[press release unavailable"):
            continue  # markers are diagnostics for humans, not content to index

        sentences = []
        for para in text.split("\n\n"):
            if para.strip():
                sentences.extend(split_sentences(para))
        if not sentences:
            continue

        starts = list(range(0, len(sentences), stride_sentences))
        for start in starts:
            core_end = min(start + core_sentences, len(sentences))
            core = sentences[start:core_end]
            # A trailing one-sentence core is already covered by the preceding
            # overlapping window. Keep a one-sentence window only when the whole
            # section itself has one sentence.
            if len(core) < 2 and start > 0:
                break

            window_start = max(0, start - context_sentences)
            window_end = min(len(sentences), core_end + context_sentences)
            display_text = " ".join(sentences[window_start:window_end])
            core_text = " ".join(core)
            if risk_title and not core_text.startswith(risk_title):
                embed_text = f"Risk: {risk_title}\n{core_text}"
            else:
                embed_text = core_text

            metadata = {
                "form": form,
                "section": section_name,
                "filing_date": filing_date,
                "url": url,
                "window_start": window_start,
                "window_end": window_end,
                "core_start": start,
                "core_end": core_end,
            }
            if risk_title:
                metadata["risk_title"] = risk_title
            chunks.append({
                "text": display_text,
                "embed_text": embed_text,
                "metadata": metadata,
            })
    return chunks


def split_risk_factors(item_1a_text: str, titles: list) -> list:
    # PURE. Cut Item 1A at each bold caption so chunk boundaries land ON risk
    # factors instead of at arbitrary word counts — one risk (caption + body)
    # becomes one or a few chunks that are "about" exactly that risk.
    # WHY rfind from the END: many 10-Ks repeat the captions in a summary list
    # near the top of the section; the LAST occurrence is the real body heading,
    # so we search backwards, constraining each earlier title to sit before the
    # one we just placed.
    # WHY the intro is dropped: everything before the first caption is meta-text
    # ("The following risk factors should be considered...") — the retrieval
    # audit showed exactly that boilerplate outranking real risks at sim 0.572.
    flat = re.sub(r"\s+", " ", item_1a_text).strip()
    positions, end = [], len(flat)
    for t in reversed(titles):
        p = flat.rfind(t, 0, end)
        if p != -1:
            positions.append(p)
            end = p
    positions.reverse()
    if not positions:
        return [item_1a_text]
    bounds = positions + [len(flat)]
    return [flat[a:b].strip() for a, b in zip(bounds, bounds[1:])]


# WHY a lazy singleton: the MiniLM model is ~90MB — download once ever (HF
# cache), load once per kernel session, NOT once per call. Lazy so this cell
# defines instantly and the cost lands on the first build instead of Run All
# stalling here.
# WHY all-MiniLM-L6-v2: local and free (no API), fast on CPU, and good enough
# for "which passage talks about X" retrieval. Its window is ~256 tokens, so
# chunk_sections embeds only a focused 2-3 sentence CORE; neighboring sentences
# are stored for display context but do not dilute or overflow the embedding.
_EMBEDDER = None

def get_embedder():
    global _EMBEDDER
    if _EMBEDDER is None:
        print("RAG: loading sentence-transformers model (first call this session)")
        _EMBEDDER = SentenceTransformer(config.EMBEDDER_MODEL)
    return _EMBEDDER


# WHY a second model: the bi-encoder embeds query and chunk SEPARATELY and
# compares vectors — fast, but it can only measure rough topic overlap. A
# cross-encoder reads the (query, chunk) PAIR together, token by token, so its
# relevance ordering is far more faithful. Too slow to score every chunk in
# the index, exactly right for re-ranking a dozen candidates. Same deal as the
# embedder: local, free, ~80MB one-time download — zero API tokens.
_RERANKER = None

def get_reranker():
    global _RERANKER
    if _RERANKER is None:
        print("RAG: loading cross-encoder re-ranker (first call this session)")
        _RERANKER = CrossEncoder(config.RERANKER_MODEL)
    return _RERANKER


# WHY an IN-MEMORY Chroma client (not PersistentClient): the durable layer is
# ./edgar_cache/ (raw documents, immutable). Embeddings are derived data and
# cheap to recompute from cache — persisting them risks silently serving stale
# vectors after the chunker or extractor changes. Rebuild-per-session is the
# safer default at this scale.
CHROMA = chromadb.Client()


# Session reuse guard (D4): symbol -> (filing-URL fingerprint, collection)
# for indexes already built in this kernel. Lives OUTSIDE the function so it
# survives across calls; a fresh kernel starts empty and builds once.
_FILING_INDEX_CACHE = {}


def build_filing_index(ticker: str):
    # Full pipeline for one ticker: filings -> sections -> chunks -> vectors ->
    # a per-ticker Chroma collection. Returns the collection.
    # Besides the semantic chunks, TWO DETERMINISTIC blocks are stored under
    # their own section names ("Item 1A titles", "Outlook"). They live in the
    # same collection for provenance, but get_filings_evidence fetches them by
    # metadata EQUALITY (coll.get), never by similarity search — and no theme
    # fence includes those names, so semantic queries can't return them either.
    symbol = ticker.upper().strip()
    cik = ticker_to_cik(symbol)
    recent = get_filing_index(cik)
    latest = find_latest_filings(recent, cik, ["10-K", "10-Q", "8-K"], max_per_form=10)

    # REUSE GUARD: same ticker + same filing vintage already indexed in this
    # kernel -> return it instead of re-fetching and re-embedding everything
    # (a live harness run built the identical NVDA index twice). WHY the
    # fingerprint is the candidate filing URLs: each URL carries its
    # accession number, so a newly published filing — or a changed pick —
    # changes the fingerprint and forces a genuine rebuild.
    fingerprint = tuple(doc["url"] for form in ("10-K", "10-Q", "8-K")
                        for doc in latest[form])
    _cached = _FILING_INDEX_CACHE.get(symbol)
    if _cached and _cached[0] == fingerprint:
        print(f"RAG: reusing existing collection for {symbol} — same filings, skipping re-embed")
        return _cached[1]

    all_chunks = []
    # ---- 10-K: Item 1A gets per-risk-factor treatment when its structure is
    # recoverable; Item 7 (and Item 1A as a fallback) chunk normally.
    if latest["10-K"]:
        doc = latest["10-K"][0]
        html = fetch_filing_html(doc["url"])
        sections = extract_10k_sections(html)
        titles = extract_risk_titles(html, sections["Item 1A"])
        # WHY the >=5 gate: a real Risk Factors section lists DOZENS of risks.
        # A handful of matches means caption detection only partly worked on
        # this filer's formatting — splitting on partial structure could
        # silently drop real risks, so we fall back to plain chunking instead.
        if len(titles) >= 5:
            print(f"RAG: Item 1A split into {len(titles)} titled risk factors")
            for block in split_risk_factors(sections.pop("Item 1A"), titles):
                # A recovered block begins with its management-written caption.
                # Carry that caption into every sentence window so a middle-body
                # match never loses the name of the risk it belongs to.
                risk_title = next((t for t in titles if block.startswith(t)), "")
                all_chunks += chunk_sections(
                    {"Item 1A": block}, "10-K", doc["filing_date"], doc["url"],
                    risk_title=risk_title,
                )
            # The caption list IS management's own one-line-per-risk summary of
            # everything that scares them — stored whole as one deterministic
            # block (~300 packet tokens buys COMPLETE risk coverage).
            all_chunks.append({
                "text": " • ".join(titles),
                "metadata": {"form": "10-K", "section": "Item 1A titles",
                             "filing_date": doc["filing_date"], "url": doc["url"]},
            })
        all_chunks += chunk_sections(sections, "10-K", doc["filing_date"], doc["url"])
        # AUDIT (run #9, item 6): the 10-K's own concentration sentences, dated
        conc_10k = concentration_sentences(sections.get("Item 1A", "") if "Item 1A" in sections else "")
        if not conc_10k and titles:
            conc_10k = concentration_sentences(" ".join(split_risk_factors(extract_10k_sections(html)["Item 1A"], titles)))
        if conc_10k:
            all_chunks.append({"text": "\n".join(f"- (10-K filed {doc['filing_date']}) {s}" for s in conc_10k),
                               "metadata": {"form": "10-K", "section": "Concentration (dated)",
                                            "filing_date": doc["filing_date"], "url": doc["url"]}})
        # AUDIT FIX (run #2): what the company SELLS — the head of Item 1,
        # stored as a deterministic block so every debate opens with it
        overview = business_overview(extract_10k_business(html))
        if overview:
            all_chunks.append({
                "text": overview,
                "metadata": {"form": "10-K", "section": "Item 1 overview",
                             "filing_date": doc["filing_date"], "url": doc["url"]},
            })
        else:
            print("RAG: Item 1 (Business) not located — business overview block will be absent")
    else:
        print(f"RAG: no recent 10-K for {symbol} — skipping")

    # ---- 10-Q: quarterly MD&A + risk updates, plain chunking.
    if latest["10-Q"]:
        doc = latest["10-Q"][0]
        q_html = fetch_filing_html(doc["url"])
        sections = extract_10q_sections(q_html)
        # AUDIT FIX (run #2): the newest 10-Q's Commitments and Contingencies
        # note — guarantees, backstops, purchase obligations — indexed under
        # its own section name so the obligations theme can fence to it
        sections["Commitments (10-Q note)"] = extract_10q_commitments(q_html)
        all_chunks += chunk_sections(sections, "10-Q", doc["filing_date"], doc["url"])
        # AUDIT (run #9, item 2): every dollar figure in the commitments note as
        # its own line — never the truncated tail of a longer quote
        dollars = dollar_sentences(sections["Commitments (10-Q note)"])
        if dollars:
            all_chunks.append({"text": "\n".join(f"- {s}" for s in dollars),
                               "metadata": {"form": "10-Q", "section": "Commitments $ lines",
                                            "filing_date": doc["filing_date"], "url": doc["url"]}})
        # AUDIT (run #9, item 6): newer customer facts, dated by the 10-Q
        conc_q = concentration_sentences(" ".join(v for k, v in sections.items() if not v.startswith("[section unavailable")))
        if conc_q:
            all_chunks.append({"text": "\n".join(f"- (10-Q filed {doc['filing_date']}) {s}" for s in conc_q),
                               "metadata": {"form": "10-Q", "section": "Concentration (dated)",
                                            "filing_date": doc["filing_date"], "url": doc["url"]}})
    else:
        print(f"RAG: no recent 10-Q for {symbol} — skipping")

    # ---- 8-K press release: semantic chunks PLUS the deterministic Outlook
    # block — guidance lives under a literal "Outlook" heading, and finding a
    # labeled paragraph is a string problem, not a semantic-search problem.
    pr = extract_8k_press_release(latest["8-K"])
    if not pr["text"].startswith("[press release unavailable"):
        all_chunks += chunk_sections({"press release": pr["text"]}, "8-K",
                                     pr["filing_date"], pr["url"])
        # AUDIT (run #9, item 3): headline figures, segment/AI revenue lines and
        # the CEO quote from the top of the release — deterministic, like Outlook
        highlights = extract_highlights(pr["text"])
        if highlights:
            all_chunks.append({"text": highlights,
                               "metadata": {"form": "8-K", "section": "8-K highlights",
                                            "filing_date": pr["filing_date"], "url": pr["url"]}})
        conc_pr = concentration_sentences(pr["text"])
        if conc_pr:
            all_chunks.append({"text": "\n".join(f"- (8-K filed {pr['filing_date']}) {s}" for s in conc_pr),
                               "metadata": {"form": "8-K", "section": "Concentration (dated)",
                                            "filing_date": pr["filing_date"], "url": pr["url"]}})
        outlook = extract_outlook(pr["text"])
        if outlook:
            all_chunks.append({
                "text": outlook,
                "metadata": {"form": "8-K", "section": "Outlook",
                             "filing_date": pr["filing_date"], "url": pr["url"]},
            })
        else:
            print("RAG: no Outlook/Guidance heading in the press release — guidance block will be absent")
    else:
        print(f"RAG: {pr['text']}")

    # WHY delete-then-create: "rebuild" must mean REPLACE — appending to an old
    # collection would mix chunks from different filing vintages of the same
    # ticker, and retrieval would quote superseded documents.
    name = f"filings_{symbol.replace('.', '_')}"  # Chroma names disallow "."
    try:
        CHROMA.delete_collection(name)
    except Exception:
        pass  # first build this session — nothing to delete
    coll = CHROMA.create_collection(name, metadata={"hnsw:space": "cosine"})
    # WHY cosine space: Chroma defaults to L2 distance; cosine makes the scores
    # readable as similarity (1 - distance), which the audit cell relies on.

    if not all_chunks:
        print(f"RAG: WARNING — zero chunks extracted for {symbol}; collection is empty")
        return coll  # NOT cached — an empty build should retry next call, not stick

    texts = [c["text"] for c in all_chunks]
    # Semantic windows embed only their focused core; deterministic blocks do
    # not carry embed_text and fall back to their complete stored text. Chroma
    # returns `documents=texts`, so agents/audits still receive the expanded
    # sentence window with neighboring context.
    embed_texts = [c.get("embed_text", c["text"]) for c in all_chunks]
    print(f"RAG: embedding {len(texts)} sentence windows for {symbol} (local model, no API cost)")
    vectors = get_embedder().encode(embed_texts, show_progress_bar=False)
    coll.add(
        ids=[f"{symbol}-{i}" for i in range(len(texts))],
        embeddings=[v.tolist() for v in vectors],
        documents=texts,
        metadatas=[c["metadata"] for c in all_chunks],
    )
    print(f"RAG: collection '{name}' ready with {coll.count()} chunks")
    _FILING_INDEX_CACHE[symbol] = (fingerprint, coll)
    return coll


# WHY a similarity floor: MiniLM cosine scores calibrate roughly as 0.6+ =
# genuinely related, below ~0.2 = noise. The audit showed 0.08-0.13 "hits"
# being quoted as evidence — text that matched nothing. Below the floor we
# would rather SAY there is nothing than quote garbage that costs tokens on
# every debate round.
SIM_FLOOR = 0.35
POOL_PER_QUERY = 6  # candidates fetched per sub-query before re-ranking

# ONE visible, tunable place for the retrieval-time boilerplate rules. Each
# phrase is a named failure from the NVDA retrieval audit; the lists are tuned
# on NVDA/KO filing style and may need additions for other filers.
BOILERPLATE_PHRASES = [
    # (a) cross-reference pointers — text ABOUT where disclosure lives,
    #     not disclosure itself (audit specimen S1)
    "can be found under",
    "incorporated by reference",
    "available free of charge",
    # (b) table scaffold lead-ins — the sentence announces a table whose cells
    #     the text extraction cannot see (audit specimen S2)
    "the following table sets forth",
    # (c) generic risk transitions — true of every public company, argue
    #     nothing about THIS one (audit specimen S3)
    "any one of those risks",
    "not presently known to us",
]


def is_boilerplate_chunk(text: str) -> bool:
    # PURE. WHY filter at retrieval instead of dropping at indexing: chunks
    # stay in the index harmlessly (provenance intact, nothing silently
    # missing from the corpus); the rules live in ONE visible, tunable place
    # above; and the audit showed these specimens SPENDING evidence slots —
    # retrieval is where slots are spent, so retrieval is where the filter
    # belongs.
    lowered = " ".join(text.lower().split())
    if any(phrase in lowered for phrase in BOILERPLATE_PHRASES):
        return True
    # Table scaffolds are mostly numbers and labels. Strip digits, then demand
    # at least 2 real prose sentences ending in a period — a chunk that only
    # ANNOUNCES a table ("...expressed as a percentage of revenue.") has one.
    no_numbers = re.sub(r"[0-9]+", " ", text)
    prose_sentences = [s for s in split_sentences(no_numbers) if s.rstrip().endswith(".")]
    return len(prose_sentences) < 2


def retrieve_theme(collection, rerank_query: str, sub_queries: list,
                   section_filter: list, k: int = 4):
    # Three retrieval stages, ALL local and free (zero API tokens):
    #   1. FAN-OUT — embed every concrete sub-query and pull POOL_PER_QUERY
    #      nearest chunks for each, inside the section fence. Embeddings match
    #      like-to-like: the old single ABSTRACT query ("key business risks and
    #      threats") retrieved text that talks about risk in the abstract —
    #      section intros and boilerplate. Concrete sub-queries phrased like
    #      actual filing sentences match the real thing, and different
    #      questions can't all return the same generic fragment, so fan-out
    #      doubles as diversity control.
    #   2. MERGE + FLOOR — dedup by chunk id, keep each chunk's best cosine
    #      score, drop everything under SIM_FLOOR.
    #   3. RERANK — the cross-encoder re-orders the surviving pool by reading
    #      (rerank_query, chunk) pairs together; keep the top k.
    # Returns (results, best_similarity_seen). The second value lets the
    # caller report "best match 0.21" honestly when the floor empties a theme.
    #
    # WHY the metadata fence: pure semantic similarity happily returns MD&A
    # prose for a "risks" query — the wording overlaps. Restricting each theme
    # to its home section(s) guarantees a risk excerpt really comes from a Risk
    # Factors section, which is exactly what its source tag will claim.
    if collection.count() == 0:
        return [], None
    qvecs = get_embedder().encode(list(sub_queries), show_progress_bar=False)
    # WHY fetch at least 3*k per sub-query: the boilerplate filter below
    # discards candidates AFTER reranking, so the pool must be deep enough
    # that a theme can still fill k REAL slots once junk is skipped. Extra
    # candidates cost only local embedding lookups — zero API tokens.
    per_query = max(POOL_PER_QUERY, 3 * k)
    res = collection.query(
        query_embeddings=[v.tolist() for v in qvecs],
        n_results=min(per_query, collection.count()),
        where={"section": {"$in": list(section_filter)}},
    )
    pool, best = {}, None
    for qi, sub_q in enumerate(sub_queries):
        rows = zip(res["ids"][qi], res["documents"][qi],
                   res["metadatas"][qi], res["distances"][qi])
        for cid, doc, meta, dist in rows:
            sim = round(1 - dist, 4)  # cosine distance -> similarity (higher = better)
            if best is None or sim > best:
                best = sim
            if sim < SIM_FLOOR:
                continue
            if cid not in pool or sim > pool[cid]["similarity"]:
                pool[cid] = {"text": doc, "metadata": meta,
                             "similarity": sim, "matched": sub_q}
    if not pool:
        return [], best
    candidates = list(pool.values())
    # Risk captions participate in reranking even when the matched core comes
    # from the middle of the risk body. They are metadata rather than repeated
    # document text, so prepend them only for the local cross-encoder input.
    rerank_texts = [
        (f"Risk: {c['metadata']['risk_title']}\n{c['text']}"
         if c["metadata"].get("risk_title") else c["text"])
        for c in candidates
    ]
    scores = get_reranker().predict([(rerank_query, text) for text in rerank_texts])
    for cand, score in zip(candidates, scores):
        cand["rerank"] = round(float(score), 3)
    candidates.sort(key=lambda c: c["rerank"], reverse=True)

    # Deduplicate sentence windows in rerank order. First use positional overlap:
    # windows from the same filing/section/risk that share sentence ranges are
    # alternate views of one passage, so only the strongest consumes an evidence
    # slot. Keep lexical near-duplicate detection as a fallback for repeated
    # filing prose that appears at different positions.
    def same_source(a, b):
        keys = ("form", "section", "url", "risk_title")
        return all(a.get(key, "") == b.get(key, "") for key in keys)

    def windows_overlap(a, b):
        required = ("window_start", "window_end")
        if not same_source(a, b) or not all(key in a and key in b for key in required):
            return False
        return a["window_start"] < b["window_end"] and b["window_start"] < a["window_end"]

    picked, picked_word_sets = [], []
    seen_risk_titles = set()
    skipped_boilerplate = 0
    for cand in candidates:
        # Boilerplate never spends an evidence slot — see is_boilerplate_chunk.
        # We keep walking DOWN the reranked list until k REAL chunks are
        # collected or candidates run out.
        if is_boilerplate_chunk(cand["text"]):
            skipped_boilerplate += 1
            continue
        meta = cand["metadata"]
        # WHY one-risk-one-slot: several sentence windows from the SAME long
        # risk factor can all rerank well (audit specimen S4: cybersecurity
        # took 2 of Key risks' 4 slots, crowding out supply-chain content).
        # Only the FIRST KEPT window per distinct risk title spends a slot, so
        # risk DIVERSITY is guaranteed structurally instead of hoping the
        # embeddings spread out. Chunks without risk_title (MD&A, press
        # release) are untouched — harmless outside the Key risks theme.
        if meta.get("risk_title") and meta["risk_title"] in seen_risk_titles:
            continue
        if any(windows_overlap(meta, prior["metadata"]) for prior in picked):
            continue
        words = set(cand["text"].lower().split())
        dup_at = next((i for i, prior_words in enumerate(picked_word_sets)
                       if len(words & prior_words) > 0.9 * min(len(words), len(prior_words))), None)
        if dup_at is not None:
            # AUDIT FIX (run #2): prefer the NEWEST filing when a passage is
            # repeated — a 10-Q restating 10-K language supersedes it
            if cand["metadata"].get("filing_date", "") > picked[dup_at]["metadata"].get("filing_date", ""):
                picked[dup_at], picked_word_sets[dup_at] = cand, words
            continue
        picked.append(cand)
        picked_word_sets.append(words)
        # Claim the risk title only on a successful pick: if this window loses
        # to the overlap/word dedup above, a later window of the SAME risk may
        # still take the slot — a risk is never locked out with zero slots.
        if meta.get("risk_title"):
            seen_risk_titles.add(meta["risk_title"])
        if len(picked) == k:
            break
    if skipped_boilerplate:
        print(f"RAG: boilerplate filter skipped {skipped_boilerplate} candidate(s) in rerank order")
    return picked, best


from datetime import date

# ---------------- [FILINGS] evidence section ----------------

# WHY tokens-to-chars at 4:1: caps below are specified in tokens (what the
# model bills by), but Python only sees characters. ~4 chars/token is the
# standard rough ratio for English prose — close enough for budget enforcement.
CHARS_PER_TOKEN = 4
GLOBAL_TOKEN_CAP = 4200  # hard ceiling for the whole [FILINGS] section (2500 -> 3300 audit pass -> 4200 with dollar lines, highlights, concentration)

# SEMANTIC themes: (title, rerank query, sub-queries, section fence, k, cap).
# WHY sub-query FAN-OUT instead of one abstract query per theme: embeddings
# match like-to-like. "key business risks and threats" is abstract, so it
# retrieved text that talks about risk in the ABSTRACT — intros, transitions,
# boilerplate. Sub-queries phrased like actual filing sentences match actual
# filing sentences. They are worded generically (no company names) so the same
# THEMES work for any ticker; every extra sub-query is a free local embed.
# WHY a fence per theme: the source tag on each excerpt claims a section, and
# the fence makes that claim structurally true (a "risk" can only come from a
# Risk Factors section).
THEMES = [
    ("Key risks",
     "the most important specific business risks facing the company",
     ["export controls, sanctions or government regulation restrict our sales in key markets",
      "we depend on a limited number of customers and partners for a significant portion of revenue",
      # WHY reworded (audit specimen S4): the old phrasing "we rely on
      # third-party foundries and suppliers to manufacture and package our
      # products" had its semantic center on RELYING ON THIRD PARTIES — which
      # matched the third-party/vendor SECURITY risk factor, not supply chain.
      # This wording centers the manufacturing activity itself (wafers,
      # packaging, capacity) and stays company-agnostic for any ticker.
      "foundry manufacturing capacity, wafer supply and packaging constraints at our suppliers",
      "intense competition and rapid technological change could reduce our market share and margins",
      "supply constraints, long-term purchase commitments and inventory write-downs"],
     ["Item 1A", "Item 1A (10-Q update)"], 4, 850),
    ("Results & drivers",
     "what actually drove revenue, margins and segment results this period",
     ["revenue increased compared with the prior year, driven by strong demand",
      "gross margin changed due to product mix, costs and inventory charges",
      "segment revenue grew to a record during the quarter",
      "operating expenses increased due to compensation and infrastructure"],
     ["Item 7", "Item 2 (10-Q MD&A)", "press release"], 4, 850),
    # AUDIT FIX (run #2): targeted retrieval for guidance and OBLIGATIONS —
    # the guarantee/backstop/residual-value language that a $29B maximum
    # exposure lives in, plus forward guidance wherever it is stated.
    ("Guidance, commitments & obligations",
     "forward guidance, guarantees, backstops, purchase commitments and off-balance-sheet obligations",
     ["we expect revenue for the next fiscal quarter to be approximately",
      "guidance for the full fiscal year revenue and gross margin",
      "we have guaranteed the obligations and our maximum exposure under the guarantee",
      "backstop commitment and residual value guarantee",
      "unconditional purchase obligations and contractual commitments",
      "off-balance-sheet arrangements and contingent liabilities"],
     ["press release", "Outlook", "Commitments (10-Q note)"], 2, 400),   # trimmed after run #6 (was 3 quotes/600 over MD&A too)
]

# DETERMINISTIC blocks: (title, section name in the index, token cap).
# WHY no semantic search here: this content lives under a KNOWN label — the
# press release's "Outlook" paragraph, the Item 1A caption list — and finding
# a labeled block is a string problem. The old RAG approach scored 0.08-0.13
# cosine (noise) trying to SEARCH for guidance that a string match finds
# exactly. These are fetched by metadata equality (coll.get), never by query.
DETERMINISTIC_BLOCKS = [
    ("Risk factor titles", "Item 1A titles", 300),
    ("Guidance & outlook", "Outlook", 400),
    ("Business overview", "Item 1 overview", 350),   # what the company sells (10-K Item 1 head)
    ("Guarantees & commitments — dollar figures", "Commitments $ lines", 400),   # run #9 item 2
    ("8-K highlights", "8-K highlights", 400),                                   # run #9 item 3
    ("Customer concentration (dated)", "Concentration (dated)", 350),           # run #9 item 6
]

# WHY these budgets: retrieved quotes carry the debate (850 + 850); the
# caption list is so dense that 300 tokens covers a dozen-plus risks; the
# outlook paragraph is short and numeric (400). Quote budgets sum to 2,400,
# and the global enforcer below guarantees the WHOLE section — headers and
# source tags included — fits GLOBAL_TOKEN_CAP.


def truncate_to_budget(text: str, char_budget: int) -> str:
    # PURE. Prefer the last COMPLETE sentence that fits the character budget.
    # The old implementation cut only at a word boundary, so valid retrieved
    # chunks could still reach the agents as unfinished thoughts. The fallback
    # remains word-aligned for unusually long sentences where no sentence end
    # fits; either way the explicit marker makes omitted text visible.
    marker = " [...truncated]"
    if len(text) <= char_budget:
        return text
    if char_budget <= len(marker):
        return marker[:char_budget]

    available = char_budget - len(marker)
    candidate = text[:available]
    sentence_ends = [m.end() for m in re.finditer(r"[.!?](?=(?:[\"')\]]*)\s|$)", candidate)]
    # Avoid returning only a tiny heading/fragment when the first sentence is
    # much shorter than the available budget; a word-boundary fallback carries
    # more useful context in that rare case.
    minimum_useful = min(120, max(40, available // 3))
    if sentence_ends and sentence_ends[-1] >= minimum_useful:
        cut = candidate[:sentence_ends[-1]].rstrip()
    else:
        cut = candidate.rsplit(" ", 1)[0].rstrip()
    return cut + marker


def preview_complete_sentences(text: str, char_limit: int) -> str:
    # Local audit display only. Reuse the production truncation rule so the
    # audit previews exactly how sentence-complete evidence will read, without
    # paying model tokens or changing retrieval/ranking behavior.
    return truncate_to_budget(text, char_limit)


def format_theme_block(title: str, results: list, token_cap: int) -> list:
    # PURE: retrieval results in -> evidence lines out, hard-capped. Testable
    # offline with fake results (no Chroma, no network).
    # WHY split the budget evenly across excerpts: k excerpts of similar length
    # beat one giant quote + three stubs — the debaters get k distinct points.
    lines = [f"-- {title} --"]
    if not results:
        lines.append(f"[theme unavailable: no retrievable content for '{title}']")
        return lines
    per_excerpt = (token_cap * CHARS_PER_TOKEN) // len(results)
    for r in results:
        m = r["metadata"]
        # Every child window from a titled 10-K risk retains management's exact
        # caption. Show it separately so agents never have to infer which risk a
        # middle-body excerpt belongs to.
        if m.get("risk_title"):
            lines.append(f"- Risk: {m['risk_title']}")
        if r["text"].startswith("- "):
            # a deterministic LIST block (dollar lines, dated concentration): one
            # line per item, the whole block trimmed to its budget, no quote wrapper
            kept, used = [], 0
            for item in r["text"].split("\n"):
                if used + len(item) > per_excerpt and kept:
                    kept.append("- [...more items omitted for budget]")
                    break
                kept.append(item)
                used += len(item)
            lines.extend(kept)
            lines.append(f"  [source: EDGAR/{m['form']} {m['section']}, filed {m['filing_date']}, {m['url']}]")
            continue
        budget = per_excerpt
        if str(m.get("section", "")).startswith("Commitments") and "$" in r["text"]:
            # AUDIT (run #9, item 2): a guarantee quote cut before its first
            # dollar figure is worthless — extend the cut to the end of that
            # sentence (up to 2x budget)
            first = re.search(r"[^.]*\$\s?\d[^.]*\.", normalize_dollars(r["text"]))
            if first and first.end() > budget:
                budget = min(first.end() + 1, 2 * per_excerpt)
        lines.append(f'- "{truncate_to_budget(normalize_dollars(r["text"]), budget)}"')
        # Source tag on its own line: the global-cap enforcer below must be able
        # to shrink quotes WITHOUT ever eating a source attribution.
        lines.append(f"  [source: EDGAR/{m['form']} {m['section']}, filed {m['filing_date']}, {m['url']}]")
    return lines


def enforce_global_cap(blocks: list, char_cap: int) -> list:
    # PURE. blocks = list of theme blocks (each a list of lines). If the total
    # busts the cap, shrink ONLY the quote lines, all by the same factor.
    # WHY proportional: uniform scaling preserves the intended budget RATIO
    # between themes instead of deleting whichever theme happens to come last.
    # WHY exact: we scale against the quote mass only, so headers and source
    # tags survive untouched and the result provably fits the cap.
    total = sum(len(ln) for b in blocks for ln in b)
    if total <= char_cap:
        return blocks
    quote_mass = sum(len(ln) for b in blocks for ln in b if ln.startswith('- "'))
    fixed_mass = total - quote_mass
    allowed = max(0, char_cap - fixed_mass)
    scale = allowed / quote_mass if quote_mass else 0
    return [
        [truncate_to_budget(ln, int(len(ln) * scale)) if ln.startswith('- "') else ln
         for ln in b]
        for b in blocks
    ]


def months_old(date_str: str) -> int:
    d = date.fromisoformat(date_str)
    today = date.today()
    return (today.year - d.year) * 12 + (today.month - d.month)


def add_number_context(coll, result: dict) -> dict:
    # DISPLAY-TIME guard — chunk overlap and the index stay untouched. WHY:
    # chunk boundaries can strip the setup that makes paired numbers
    # interpretable (the audit found a 22% vs 42% comparison whose meaning
    # lives in the surrounding sentences). Serving that fragment as evidence
    # invites the debaters to MISREAD a grounded number — an interpretation
    # hallucination the review layer would then have to catch downstream.
    # Cheaper to prepend one labeled context sentence here than to litigate a
    # misreading there.
    meta = result["metadata"]
    sentences = split_sentences(result["text"])
    # Fire only when the FIRST sentence contains a number (digit or %); prose
    # openings need no rescue.
    if not sentences or not re.search(r"[0-9%]", sentences[0]):
        return result
    # core_start 0 (or absent) means the chunk BEGINS its section/risk block —
    # nothing precedes it, so there is nothing to restore.
    if not meta.get("core_start"):
        return result
    try:
        got = coll.get(
            where={"$and": [{"form": meta["form"]}, {"section": meta["section"]},
                            {"url": meta["url"]}]},
            include=["documents", "metadatas"],
        )
    except Exception as e:
        print(f"RAG: number-context lookup skipped ({e})")
        return result
    # The predecessor is the chunk with the LARGEST core_start strictly before
    # ours — compared inside the same risk block only, because sentence
    # positions restart at 0 for every titled risk block chunk_sections sees.
    predecessor = None
    for doc, m in zip(got["documents"] or [], got["metadatas"] or []):
        if m.get("risk_title", "") != meta.get("risk_title", ""):
            continue
        if "core_start" not in m or m["core_start"] >= meta["core_start"]:
            continue
        if predecessor is None or m["core_start"] > predecessor[1]["core_start"]:
            predecessor = (doc, m)
    if predecessor is None:
        return result
    # Windows OVERLAP by design (stride < core + context), so the predecessor's
    # literal last sentence is often already visible inside our window. Prepend
    # the last predecessor sentence we do NOT already show; if every sentence
    # is visible, nothing was stripped and there is nothing to restore.
    for sentence in reversed(split_sentences(predecessor[0])):
        if sentence not in result["text"]:
            out = dict(result)
            # Inline label: visibly OUR addition, not part of the retrieved chunk.
            out["text"] = f"[context] {sentence} {result['text']}"
            return out
    return result


def semantic_block(coll, title, rerank_query, sub_queries, fence, k, cap) -> list:
    # One retrieved theme -> formatted lines, exceptions contained.
    # WHY the "thin" branch: when every candidate lands under SIM_FLOOR,
    # quoting the least-bad noise would put confident-looking garbage in front
    # of the debaters. An honest one-line report is more useful AND cheaper —
    # it costs ~20 tokens instead of ~850 on every round.
    try:
        results, best = retrieve_theme(coll, rerank_query, sub_queries, fence, k)
        if results:
            # Truncation guard for context-dependent numbers (display-time
            # only) — see add_number_context. Deterministic blocks never pass
            # through here, so Outlook/caption blocks are never decorated.
            results = [add_number_context(coll, r) for r in results]
            return format_theme_block(title, results, cap)
        if best is not None:
            return [f"-- {title} --",
                    f"[theme thin: best match {best:.2f} is below the {SIM_FLOOR} floor — omitted rather than quoting noise]"]
        return format_theme_block(title, [], cap)
    except Exception as e:
        return [f"-- {title} --", f"[theme unavailable: {e}]"]


def deterministic_block(coll, title, section_name, cap) -> list:
    # One labeled block -> formatted lines, fetched by metadata EQUALITY.
    try:
        got = coll.get(where={"section": section_name}, include=["documents", "metadatas"])
        results = [{"text": d, "metadata": m}
                   for d, m in zip(got["documents"] or [], got["metadatas"] or [])]
        if not results:
            return [f"-- {title} --",
                    f"[not extracted: no '{section_name}' block in the indexed filings]"]
        return format_theme_block(title, results, cap)
    except Exception as e:
        return [f"-- {title} --", f"[theme unavailable: {e}]"]


def get_filings_evidence(ticker: str) -> str:
    # Builds the [FILINGS] evidence section: index the ticker's latest filings,
    # then assemble two RETRIEVED themes and two DETERMINISTIC blocks under
    # per-theme and global token caps. Appended to market_snapshot's packet by
    # research_node.
    try:
        coll = build_filing_index(ticker)
    except Exception as e:
        # WHY fail soft: filings are ONE evidence layer — a dead EDGAR must not
        # kill the debate, which still has the full market_snapshot packet.
        return f"\n\n[FILINGS]\n[filings unavailable: {e}]"

    lines = ["[FILINGS]"]
    # Header: every source document's date AND age in months. WHY: the debaters
    # must know how stale each source is — a 10-K risk factor from 11 months ago
    # and a 3-week-old press release deserve different weight.
    try:
        metas = coll.get(include=["metadatas"])["metadatas"] or []
        seen = {}
        for m in metas:
            label = "8-K press release" if m["form"] == "8-K" else m["form"]
            seen[(label, m["filing_date"])] = True
        if seen:
            lines.append("Sources: " + " | ".join(
                f"{label} filed {d} ({months_old(d)} months old)"
                for (label, d) in sorted(seen)))
        else:
            lines.append("[no filing documents indexed]")
    except Exception as e:
        lines.append(f"[source header unavailable: {e}]")

    # WHY a helper per block (each with its own try/except): one broken block
    # costs exactly one block — the other three still make it in. Packet order
    # is interleaved by hand so related blocks sit together: risk QUOTES, then
    # the caption list that summarizes ALL the risks; results quotes, then the
    # deterministic outlook.
    blocks = [
        deterministic_block(coll, *DETERMINISTIC_BLOCKS[2]),  # Business overview — first: what the company sells
        semantic_block(coll, *THEMES[0]),            # Key risks (retrieved)
        deterministic_block(coll, *DETERMINISTIC_BLOCKS[0]),  # Risk factor titles
        semantic_block(coll, *THEMES[1]),            # Results & drivers (retrieved)
        deterministic_block(coll, *DETERMINISTIC_BLOCKS[1]),  # Guidance & outlook
        semantic_block(coll, *THEMES[2]),            # Guidance, commitments & obligations (retrieved)
        deterministic_block(coll, *DETERMINISTIC_BLOCKS[3]),  # Guarantees & commitments — dollar figures
        deterministic_block(coll, *DETERMINISTIC_BLOCKS[4]),  # 8-K highlights
        deterministic_block(coll, *DETERMINISTIC_BLOCKS[5]),  # Customer concentration (dated)
    ]

    blocks = enforce_global_cap(blocks, GLOBAL_TOKEN_CAP * CHARS_PER_TOKEN)
    for b in blocks:
        lines.append("")  # blank line between themes for readability
        lines.extend(b)
    return "\n\n" + "\n".join(lines)


# ---------------- AUDIT PASS (run #2): corroborate news-sourced claims against the filings ----------------
# WHY: the judge flagged "221% AI revenue growth" and "$29B maximum guarantee
# exposure" as untrusted NEWS — both sit in the filings (8-K press release,
# 10-Q note). Before a flagged number is discarded, look for it VERBATIM in
# the indexed filings; a hit promotes it to filing-backed evidence.

def number_variants(num: str) -> list:
    # PURE. Spellings a filing may use for a packet-style number.
    # "221%" -> ["221%", "221 %", "221 percent"]; "$29B" -> ["$29 billion", "$29.0 billion", "29 billion", "$29B"]
    raw = num.strip()
    out = [raw]
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s?%", raw)
    if m:
        n = m.group(1)
        out += [f"{n} %", f"{n} percent", f"{n}percent"]
    m = re.fullmatch(r"\$?\s?(\d[\d,]*(?:\.\d+)?)\s?([TBMKtbmk])\b", raw)
    if m:
        n, unit = m.group(1), m.group(2).lower()
        word = {"t": "trillion", "b": "billion", "m": "million", "k": "thousand"}[unit]
        base = n.rstrip("0").rstrip(".") if "." in n else n
        out += [f"${base} {word}", f"${n} {word}", f"{base} {word}", f"$ {base} {word}",
                f"${base}.0 {word}" if "." not in base else f"${base} {word}"]
    seen, uniq = set(), []
    for v in out:
        if v not in seen:
            seen.add(v); uniq.append(v)
    return uniq


def corroborate_claims(ticker: str, claims: list, coll=None, max_hits: int = 2) -> list:
    """For each claim, find its numbers verbatim in the ticker's indexed
    filings. Returns [{"claim","number","match","text","metadata"}]. coll= is
    the test seam (anything with .get(where_document=...)); production uses
    the session's cached collection."""
    if coll is None:
        coll = build_filing_index(ticker)
    hits = []
    for claim in claims:
        for num in extract_numeric_claims(str(claim)):
            found = None
            for variant in number_variants(num):
                try:
                    got = coll.get(where_document={"$contains": variant},
                                   include=["documents", "metadatas"], limit=max_hits)
                except Exception as e:
                    print(f"CORROBORATE: lookup failed for {variant!r} ({e})")
                    continue
                docs = got.get("documents") or []
                if docs:
                    found = {"claim": str(claim), "number": num, "match": variant,
                             "text": docs[0], "metadata": (got.get("metadatas") or [{}])[0]}
                    break
            if found:
                hits.append(found)
    return hits


def format_corroboration_addendum(hits: list) -> str:
    # The packet addendum the governor traces against: one quoted passage per
    # corroborated number, with its filing source tag, in the same shape as
    # the [FILINGS] section. Appended to the evidence by the claim review node.
    if not hits:
        return ""
    lines = ["[FILINGS ADDENDUM — numbers from flagged claims found verbatim in the indexed filings during claim review]"]
    seen = set()
    for h in hits:
        key = (h["number"], h["metadata"].get("url"))
        if key in seen:
            continue
        seen.add(key)
        m = h["metadata"]
        lines.append(f"- {h['number']} (from claim: {truncate_to_budget(h['claim'], 160)})")
        lines.append(f'- "{truncate_to_budget(h["text"], 600)}"')
        lines.append(f"  [source: EDGAR/{m.get('form', '?')} {m.get('section', '?')}, filed {m.get('filing_date', '?')}, {m.get('url', '')}]")
    return "\n".join(lines)
