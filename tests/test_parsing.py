"""Migrated from the notebook offline suite: parse_judge_json (3 cases) and
the revision-path parse_review_json shape tests rev-a..rev-d (4 cases).
Fixtures are byte-identical to the notebook's."""
import pytest

from markut.guardrails.parsing import parse_judge_json, parse_review_json


# --- parse_judge_json --------------------------------------------------------

def test_clean_json_parses_with_verdict_and_converged():
    clean = ('{"bull_strongest": "a", "bear_strongest": "b", "reasoning": "c", '
             '"verdict": "hold", "converged": true}')
    d = parse_judge_json(clean)
    assert d["verdict"] == "hold" and d["converged"] is True


def test_fence_wrapped_json_parses_after_stripping_fences():
    fenced = '```json\n{"verdict": "sell", "converged": false}\n```'
    d = parse_judge_json(fenced)
    assert d["verdict"] == "sell" and d["converged"] is False


def test_non_json_prose_raises_for_retry_path():
    prose = "After careful thought, I believe the debate has converged. CONVERGED: YES"
    with pytest.raises(Exception):
        parse_judge_json(prose)


# --- parse_review_json: tolerant on shape, strict on substance ---------------

def test_rev_a_dict_shaped_resolutions_converted_to_canonical_list():
    out = parse_review_json('{"verdict": "v", "resolutions": '
                            '{"20-50%": {"resolution": "DERIVED", "anchors": ["$244.85"]}}}')
    assert out["resolutions"] == [{"resolution": "DERIVED", "anchors": ["$244.85"],
                                   "claim": "20-50%"}]


def test_rev_b_list_shaped_resolutions_accepted_unchanged():
    out = parse_review_json('{"verdict": "v", "resolutions": '
                            '[{"claim": "30-40%", "resolution": "labeled", "anchors": []}]}')
    assert out["resolutions"] == [{"claim": "30-40%", "resolution": "LABELED", "anchors": []}]


def test_rev_c_string_resolutions_rejected_with_specific_message():
    with pytest.raises(ValueError, match="JSON array"):
        parse_review_json('{"verdict": "v", "resolutions": "none"}')


def test_rev_d_derived_with_empty_anchors_rejected():
    with pytest.raises(ValueError, match="no anchors"):
        parse_review_json('{"verdict": "v", "resolutions": '
                          '[{"claim": "20-50%", "resolution": "DERIVED", "anchors": []}]}')
