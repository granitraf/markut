"""Migrated from the notebook offline suite: validate_ticker (4 cases)."""
import pytest

from markut.guardrails.validate import validate_ticker


@pytest.mark.parametrize("raw, expected", [("nvda ", "NVDA"), ("BRK.B", "BRK.B")])
def test_valid_tickers_accepted_and_normalized(raw, expected):
    assert validate_ticker(raw) == expected


@pytest.mark.parametrize("raw", ["TOOLONG", "'; drop everything"])
def test_malformed_tickers_rejected(raw):
    with pytest.raises(ValueError):
        validate_ticker(raw)
