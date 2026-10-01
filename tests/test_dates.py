"""
Pure tests for structured date input.
"""

import pytest

from src.gramps_mcp.dates import normalize_date, normalize_dates


def test_about_estimated_year():
    assert normalize_date({"modifier": "about", "quality": "estimated", "start": "1850"}) == {
        "_class": "Date",
        "modifier": 3,
        "quality": 1,
        "dateval": [0, 0, 1850, False],
        "text": "",
    }


def test_range_with_months_and_days():
    date = normalize_date({"modifier": "range", "start": "1804-03", "end": "1810-12-31"})
    assert date["modifier"] == 4
    assert date["dateval"] == [0, 3, 1804, False, 31, 12, 1810, False]


def test_span_from_dicts_and_ints():
    date = normalize_date({"modifier": "span", "start": {"year": 1861, "month": 4}, "end": 1865})
    assert date["dateval"] == [0, 4, 1861, False, 0, 0, 1865, False]


def test_regression_range_without_end_is_rejected():
    """'between 1804 (estimated)' was stored as a range with no second date."""
    with pytest.raises(ValueError, match="range date needs both"):
        normalize_date({"modifier": "range", "quality": "estimated", "start": "1804"})


def test_raw_range_without_second_date_is_rejected():
    with pytest.raises(ValueError, match="range/span"):
        normalize_date({"modifier": 4, "quality": 1, "dateval": [0, 0, 1804, False]})


def test_end_only_allowed_on_range_or_span():
    with pytest.raises(ValueError, match="only allowed"):
        normalize_date({"modifier": "before", "start": "1850", "end": "1851"})


@pytest.mark.parametrize(
    "value", ["18-50", "1850-13", "1850-02-40", "", {"month": 3}, "1850--01"]
)
def test_malformed_start(value):
    with pytest.raises(ValueError):
        normalize_date({"start": value})


def test_unknown_modifier_and_quality():
    with pytest.raises(ValueError, match="modifier"):
        normalize_date({"modifier": "circa", "start": "1850"})
    with pytest.raises(ValueError, match="quality"):
        normalize_date({"quality": "guess", "start": "1850"})


def test_textonly():
    date = normalize_date({"modifier": "textonly", "text": "spring of the flood"})
    assert date["modifier"] == 6 and date["text"] == "spring of the flood"


def test_sortval_is_never_sent():
    raw = normalize_date({"dateval": [1, 2, 1850, False], "sortval": 123, "year": 1850})
    assert "sortval" not in raw and "year" not in raw


def test_nested_dates_normalized():
    payload = {
        "primary_name": {"date": {"modifier": "about", "start": "1850"}},
        "address_list": [{"date": {"start": "1860-05"}}],
    }
    out = normalize_dates(payload)
    assert out["primary_name"]["date"]["modifier"] == 3
    assert out["address_list"][0]["date"]["dateval"] == [0, 5, 1860, False]
