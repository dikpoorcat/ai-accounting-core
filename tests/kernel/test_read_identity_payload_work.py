"""Immutable read scopes keep identity bytes without scalar deep copies."""

import copy
from dataclasses import asdict, dataclass

import pytest

from ai_accounting.kernel.contracts import Read, read_identity_payload
from ai_accounting.kernel.history_types_v1 import Read as HistoricalRead
from ai_accounting.kernel.types import YearMonth, canonical


@pytest.mark.parametrize("source", ("fact", "calculation"))
@pytest.mark.parametrize("before", (None, "2016-01", "2025-12"))
def test_current_read_keeps_original_calculation_identity_bytes(source, before):
    read = Read(source, "payroll", "person#precise-source", YearMonth(before) if before else None)
    original = asdict(read)
    assert canonical(read_identity_payload(read)) == canonical(original)
    # Returned payload ownership remains independent, even though scalar values
    # are immutable: consumers cannot change the stored scope through this dict.
    returned = read_identity_payload(read)
    returned["kind"] = "different"
    assert read.kind == "payroll"
    assert read_identity_payload(read) == original


@pytest.mark.parametrize("size", (1, 100, 500))
def test_scope_growth_does_not_deep_copy_immutable_months(monkeypatch, size):
    read = Read("calculation", "payroll", "person", YearMonth("2025-12"))
    original_deepcopy = copy.deepcopy
    copies = []

    def tracked(value, *args, **kwargs):
        copies.append(type(value))
        return original_deepcopy(value, *args, **kwargs)

    monkeypatch.setattr(copy, "deepcopy", tracked)
    expected = canonical([asdict(read) for _ in range(size)])
    assert copies, "the original serializer actually copies the YearMonth subclass"
    copies.clear()
    assert canonical([read_identity_payload(read) for _ in range(size)]) == expected
    assert copies == []


def test_fixed_historical_scope_keeps_its_original_serializer():
    from ai_accounting.kernel.history_types_v1 import YearMonth as HistoricalMonth

    read = HistoricalRead("fact", "payroll", "person", HistoricalMonth("2016-01"))
    assert read_identity_payload(read) == asdict(read)


def test_other_read_dataclass_keeps_nested_fields_and_copy_ownership():
    @dataclass(frozen=True)
    class OtherRead(Read):
        metadata: tuple = ("unchanged",)

    read = OtherRead("fact", "payroll", "person")
    assert read_identity_payload(read) == asdict(read)
