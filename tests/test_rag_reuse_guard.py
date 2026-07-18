"""pl-e reuse-guard regression, migrated with module-attribute patching
(the notebook swapped shared globals; the package patches rag's namespace).
Stub bodies are verbatim from the notebook suite."""
import markut.evidence.rag as rag


def test_second_build_reuses_session_collection(monkeypatch):
    counts = {"encode": 0, "create": 0}

    class _PlVec:
        def tolist(self):
            return [0.0]

    class _PlEmbedder:
        def encode(self, texts, show_progress_bar=False):
            counts["encode"] += 1
            return [_PlVec() for _ in texts]

    class _PlColl:
        def __init__(self):
            self._n = 0
        def add(self, ids, embeddings, documents, metadatas):
            self._n = len(ids)
        def count(self):
            return self._n

    class _PlChroma:
        def delete_collection(self, name):
            raise ValueError("no such collection")
        def create_collection(self, name, metadata=None):
            counts["create"] += 1
            return _PlColl()

    monkeypatch.setattr(rag, "ticker_to_cik", lambda s: "0000000000")
    monkeypatch.setattr(rag, "get_filing_index", lambda cik: {})
    monkeypatch.setattr(rag, "find_latest_filings",
                        lambda recent, cik, forms, max_per_form=10: {
                            "10-K": [{"url": "https://sec.test/000-fake-accession-1/f10k.htm",
                                      "filing_date": "2026-01-01"}],
                            "10-Q": [], "8-K": []})
    monkeypatch.setattr(rag, "fetch_filing_html", lambda url: "<html></html>")
    monkeypatch.setattr(rag, "extract_10k_sections", lambda html: {
        "Item 1A": "Export risk hurts. Supply risk hurts. Demand risk hurts.",
        "Item 7": "Revenue grew strongly. Margins expanded again this year."})
    monkeypatch.setattr(rag, "extract_risk_titles", lambda html, sec: [])
    monkeypatch.setattr(rag, "extract_8k_press_release", lambda docs: {
        "text": "[press release unavailable: none]", "filing_date": "", "url": ""})
    monkeypatch.setattr(rag, "get_embedder", lambda: _PlEmbedder())
    monkeypatch.setattr(rag, "CHROMA", _PlChroma())
    rag._FILING_INDEX_CACHE.pop("FAKETK", None)
    try:
        c1 = rag.build_filing_index("FAKETK")
        c2 = rag.build_filing_index("FAKETK")
        assert c2 is c1, "second build must reuse the session collection"
        assert counts == {"encode": 1, "create": 1}, counts
    finally:
        rag._FILING_INDEX_CACHE.pop("FAKETK", None)  # never leak the fake
