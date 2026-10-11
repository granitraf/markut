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

    # run #28: the builder reads the filer-aware filing set and gathers its
    # chunks by role; the seams moved, the guarded behaviour did not
    from markut.evidence import sections
    monkeypatch.setattr(sections, "filing_set", lambda t: {
        "10-K": {"form": "10-K", "url": "https://sec.test/000-fake-accession-1/f10k.htm", "filing_date": "2026-01-01"},
        "10-Q": None, "8-K": None, "monthly": [], "reasons": {}})
    monkeypatch.setattr(rag, "filing_chunks", lambda fs, extra=None: [
        {"text": "Export risk hurts. Supply risk hurts.", "metadata": {"form": "10-K", "section": "Item 1A"}},
        {"text": "Revenue grew strongly. Margins expanded again this year.", "metadata": {"form": "10-K", "section": "Item 7"}}])
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
