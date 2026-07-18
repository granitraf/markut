"""Frozen-specimen acceptance for the output governor. The deterministic
pin (3 CITED / 2 FLAGGED) runs offline and free; the full governed revision
is @live (one paid call): pytest -m live tests/test_review_acceptance.py"""
import os

import pytest

from markut.guardrails.review import review_node
from markut.guardrails.tracer import (extract_numeric_claims, trace_claim,
    find_advice_language, has_disclaimer)

_FIX = os.path.join(os.path.dirname(__file__), "fixtures")
SPECIMEN_VERDICT = open(os.path.join(_FIX, "specimen_verdict.txt")).read()
SPECIMEN_EVIDENCE = open(os.path.join(_FIX, "specimen_evidence.txt")).read()


def test_specimen_deterministic_pin_offline():
    # the acceptance cell's pinned reading of the frozen matched pair
    claims = extract_numeric_claims(SPECIMEN_VERDICT)
    status = {c: trace_claim(c, SPECIMEN_EVIDENCE) for c in claims}
    assert status["$4.74 trillion"] == "CITED"
    assert status["15.32x"] == "CITED"
    assert status["$12.76"] == "CITED"
    assert status["20-50%"] == "FLAGGED"
    assert status["30-40%"] == "FLAGGED"
    assert set(find_advice_language(SPECIMEN_VERDICT)) == {
        "investors should", "position siz", "prudent approach"}
    assert not has_disclaimer(SPECIMEN_VERDICT)


@pytest.mark.live
def test_specimen_governed_revision_live():
    out = review_node({"verdict": SPECIMEN_VERDICT, "evidence": SPECIMEN_EVIDENCE})
    report = out["review_report"]
    v = out["verdict"]
    # guaranteed invariants (the model's resolutions may vary run to run):
    assert report["revision_used"] is True
    assert has_disclaimer(v)                       # deterministic stapler net
    for cited in ("15.32x", "$12.76"):             # cited numbers never annotated
        assert f"{cited} [UNGROUNDED" not in v
    assert report["final_status"] in ("revised", "annotated")
    assert isinstance(report["resolutions"], list)
