"""Migrated from the notebook offline suite: the deterministic claim layer.
Blocks: output-guardrail a-e (11 cases), punch-list pl-a/pl-b multiples (6),
pl-d advice-vs-disclaimer (3), lab-d disclaimer variants (3).
Fixtures are byte-identical to the notebook's — including every regression
harvested from live audits."""
from markut.guardrails.tracer import (DISCLAIMER_TEXT, extract_numeric_claims,
                                      find_advice_language, has_disclaimer,
                                      normalize_number, trace_claim)


# --- (a) extraction: the six audit specimens, range kept as ONE claim --------

_FIX = ("The stock trades at $195.55 against a $4.74T market cap and 15.32x "
        "forward sales. Data Center drove $91.0 billion in revenue, up 25.2%, "
        "while bears see 30-40% downside in a correction scenario.")


def test_a_all_six_specimen_claims_extracted():
    claims = extract_numeric_claims(_FIX)
    assert set(claims) >= {"$195.55", "$4.74T", "15.32x", "$91.0 billion",
                           "25.2%", "30-40%"}


def test_a_range_is_one_claim_no_stray_endpoints():
    claims = extract_numeric_claims(_FIX)
    assert "30%" not in claims and "40%" not in claims


# --- (b) normalization equivalences + refuse-to-guess ambiguity --------------

def test_b_scale_suffix_equals_scale_word():
    assert normalize_number("$4.74T") == normalize_number("4.74 trillion")


def test_b_thousands_separator_ignored():
    assert normalize_number("1,234") == normalize_number("1234")


def test_b_bare_fraction_flagged_ambiguous_never_equated_to_percent():
    assert normalize_number("0.52") != normalize_number("52%")
    assert "ambiguous" in normalize_number("0.52")


# --- (c) tracing against a mini evidence fixture -----------------------------

_EVIDENCE = ("Current price: $195.55 [source: yfinance]. DCF fair value implies "
             "+25.2% upside [source: FMP]. Mean analyst target implies +54.2% "
             "upside [source: yfinance].")


def test_c_literal_number_cited():
    assert trace_claim("$195.55", _EVIDENCE) == "CITED"


def test_c_unanchored_range_flagged():
    assert trace_claim("30-40%", _EVIDENCE) == "FLAGGED"


def test_c_derivable_range_still_flagged_by_design():
    # the judge proves derivations; code only certifies literal presence
    assert trace_claim("20-50%", _EVIDENCE) == "FLAGGED"


# --- (d) advice language -----------------------------------------------------

def test_d_advice_phrases_caught():
    found = set(find_advice_language("Investors should size positions; a prudent approach."))
    assert found == {"investors should", "prudent approach"}


def test_d_research_framing_not_flagged():
    assert find_advice_language("The evidence suggests upside.") == []


# --- (e) disclaimer variants -------------------------------------------------

def test_e_disclaimer_variants_recognized_plain_opinion_not():
    assert has_disclaimer("This is investment RESEARCH, not financial advice.")
    assert has_disclaimer("Not investment advice.")
    assert has_disclaimer(DISCLAIMER_TEXT)
    assert not has_disclaimer("We think the stock is attractive.")


# --- punch list pl-a/pl-b: multiple-suffix normalization ---------------------

def test_pl_a_x_suffix_dropped_for_tracing():
    assert normalize_number("16.16x") == normalize_number("16.16")
    assert normalize_number("31.76x") == normalize_number("31.76")


def test_pl_a_regression_scale_equivalence_still_holds():
    assert normalize_number("$4.74T") == normalize_number("4.74 trillion")


def test_pl_a_regression_ambiguous_fraction_still_never_equated():
    assert normalize_number("0.52") != normalize_number("52%")
    assert "ambiguous" in normalize_number("0.52")


def test_pl_b_multiple_cited_against_unitless_evidence():
    assert trace_claim("16.16x", "Forward P/E: 16.16 [source: yfinance/info]") == "CITED"


def test_pl_b_suffixed_evidence_still_anchors():
    assert trace_claim("16.16x", "Forward P/E: 16.16x [source: test]") == "CITED"


# --- punch list pl-d: advice screen vs disclaimer block ----------------------

def test_pl_d_due_diligence_boilerplate_inside_disclaimer_block_exempt():
    assert find_advice_language(
        "The valuation case is balanced. This is investment research, "
        "not financial advice. Investors should conduct their own due "
        "diligence and consult a qualified financial professional.") == []


def test_pl_d_same_phrase_without_disclaimer_context_still_caught():
    assert "investors should" in find_advice_language(
        "Investors should conduct their own due diligence before acting.")


def test_pl_d_advice_before_the_disclaimer_still_caught():
    assert "investors should" in find_advice_language(
        "Investors should size positions. This is not financial advice.")


# --- labeling-fix lab-d: broadened disclaimer variants -----------------------

def test_lab_d_model_own_phrasing_recognized():
    assert has_disclaimer("This report is for research purposes only and does "
                          "not constitute investment advice.")


def test_lab_d_does_not_constitute_financial_advice_recognized():
    assert has_disclaimer("Does Not Constitute Financial Advice.")


def test_lab_d_scattered_words_still_false():
    assert not has_disclaimer("The research team met on Friday to discuss advice columns.")
