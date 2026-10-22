"""Exact raw checks never stand in for parsing, current state or owner validation."""

import sys
from collections import OrderedDict

import pytest

from ai_accounting_modular.contracts.public import (
    ContractError,
    ReadInputs,
    canonical_json,
    content_digest,
    loads_json,
)
from ai_accounting_modular.reading import _brief_open_index as owner
from ai_accounting_modular.reading import _component_view as views


@pytest.fixture
def packet(monkeypatch):
    monkeypatch.setattr(views, "_summary_checks", OrderedDict())
    model = owner.complete(
        {"_inputs": ReadInputs().wire(), "_cooperation_inputs": ReadInputs().wire()}, (), 7
    )
    return (
        {
            "scope_key": "scope-a",
            "component_key": owner.COMPONENT,
            "format_version": owner.FORMAT_VERSION,
            "ready_head": 7,
            "content_digest": model.content_digest,
        },
        {
            "scope_key": "scope-a",
            "component_key": owner.COMPONENT,
            "head": 7,
            "payload_json": model.payload_json,
        },
    )


def read(packet):
    state, row = packet
    return views.summary(state["scope_key"], owner.COMPONENT, state, row)


def count_checks(monkeypatch):
    calls = []
    validate = owner.validate

    def checked(value):
        calls.append(value)
        return validate(value)

    monkeypatch.setattr(owner, "validate", checked)
    return calls


def replace_raw(packet, value):
    state, row = packet
    value["_semantic_digest"] = owner.semantic(value)
    state["content_digest"] = content_digest(value)
    row["payload_json"] = canonical_json(value)


def test_each_parse_is_fresh_and_noncanonical_whitespace_uses_canonical_digest(packet, monkeypatch):
    calls = count_checks(monkeypatch)
    first, epoch = read(packet)
    first["_inputs"].clear()
    second, _ = read(packet)
    assert second["_inputs"] == ReadInputs().wire()
    assert epoch == 7 and len(calls) == 1
    packet[1]["payload_json"] = "\n " + packet[1]["payload_json"]
    assert read(packet) == (second, epoch)
    assert len(calls) == 2


@pytest.mark.parametrize("field", ["_inputs", "_cooperation_inputs"])
def test_both_declared_input_sets_are_checked_before_remembering_raw(packet, field):
    read(packet)
    value = loads_json(packet[1]["payload_json"])
    del value[field]
    replace_raw(packet, value)
    with pytest.raises(ContractError) as error:
        read(packet)
    assert error.value.code == "storage_corrupt"


@pytest.mark.parametrize(
    "where,field,value",
    [
        (0, "content_digest", "0" * 64),
        (0, "format_version", owner.FORMAT_VERSION - 1),
        (1, "head", 8),
        (1, "scope_key", "scope-b"),
        (1, "component_key", "brief_activity"),
    ],
)
def test_exact_raw_does_not_prove_its_actual_header(packet, where, field, value):
    read(packet)
    packet[where][field] = value
    with pytest.raises(ContractError) as error:
        read(packet)
    assert error.value.code == "storage_corrupt"


def test_cache_is_bounded_by_raw_bytes_without_imposing_a_payload_limit(packet, monkeypatch):
    monkeypatch.setattr(views, "_SUMMARY_BYTES", 1)
    calls = count_checks(monkeypatch)
    assert read(packet) == read(packet)
    assert len(calls) == 2 and not views._summary_checks


def test_only_one_raw_per_scope_component_and_a_bounded_number_of_scopes(packet, monkeypatch):
    calls = count_checks(monkeypatch)
    original = packet[1]["payload_json"]
    read(packet)
    packet[1]["payload_json"] = "\n" + original
    read(packet)
    packet[1]["payload_json"] = original
    read(packet)
    assert len(calls) == 3
    monkeypatch.setattr(views, "_SUMMARY_LIMIT", 1)
    packet[0]["scope_key"] = packet[1]["scope_key"] = "scope-b"
    read(packet)
    packet[0]["scope_key"] = packet[1]["scope_key"] = "scope-a"
    read(packet)
    assert len(calls) == 5


def test_candidates_still_validate_and_do_not_replace_the_persisted_base(packet, monkeypatch):
    calls = count_checks(monkeypatch)
    value, _ = read(packet)
    candidate = owner.seal(dict(value, title="尚未发布"), (), 7)
    assert owner.validate(loads_json(candidate.payload_json)) == 7
    assert read(packet) == (value, 7)
    assert len(calls) == 2
    invalid = loads_json(candidate.payload_json)
    invalid["_item_epoch"] = True
    with pytest.raises(ContractError):
        owner.validate(invalid)


def test_a_cache_hit_cannot_bypass_the_current_json_integer_parser_limit(packet):
    previous = sys.get_int_max_str_digits()
    try:
        sys.set_int_max_str_digits(0)
        value = loads_json(packet[1]["payload_json"])
        value["large_integer"] = int("9" * 700)
        replace_raw(packet, value)
        read(packet)
        sys.set_int_max_str_digits(640)
        with pytest.raises(ContractError) as error:
            read(packet)
        assert error.value.code == "storage_corrupt"
    finally:
        sys.set_int_max_str_digits(previous)
