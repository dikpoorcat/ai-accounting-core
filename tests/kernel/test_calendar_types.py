"""The typed JSON month boundary accepts exactly the public calendar range."""

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import TypeAdapter, ValidationError

from ai_accounting.kernel.types import YearMonth

MONTH = TypeAdapter(YearMonth)


@given(st.integers(min_value=0, max_value=9999 * 12 - 1))
def test_month_json_and_python_validation_preserve_exact_calendar(ordinal):
    direct = YearMonth.from_ordinal(ordinal)
    for loaded in (MONTH.validate_json(json.dumps(direct)), MONTH.validate_python(str(direct))):
        assert type(loaded) is YearMonth
        assert loaded == YearMonth(direct)
        assert loaded.ordinal == ordinal
        assert MONTH.dump_json(loaded) == json.dumps(direct).encode()


@pytest.mark.parametrize(
    "value",
    [
        "0000-01", "0000-12", "10000-01", "0001-00", "9999-13", "2026-9",
        "２０２６-09", "2026-０９", "2026-09\n", "2026-09\r\n", " 2026-09",
        "2026-09 ", "2026-09-01", "2026-09\x00", "", None, True, 202609, 2026.09,
    ],
)
def test_month_validation_does_not_accept_coercion_or_partial_pattern(value):
    with pytest.raises(ValueError):
        YearMonth(value)
    with pytest.raises(ValidationError):
        MONTH.validate_python(value)
    with pytest.raises(ValidationError):
        MONTH.validate_json(json.dumps(value))


def test_month_typed_boundary_remains_strict_for_bytes():
    with pytest.raises(ValidationError):
        MONTH.validate_python(b"2026-09")
