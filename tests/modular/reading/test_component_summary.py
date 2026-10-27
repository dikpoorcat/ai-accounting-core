"""Exact raw checks never stand in for parsing, current state or owner validation."""

import sys

import pytest

from ai_accounting_modular.contracts.public import (
    ContractError,
    canonical_json,
    content_digest,
    loads_json,
)
from ai_accounting_modular.reading import _brief_open_index as owner
from ai_accounting_modular.reading import _brief_open_tree as blocks
from ai_accounting_modular.reading import _component_view as views


@pytest.fixture
def packet():
    model = owner.seal(
        owner._summary(
            {"open_items": {"categories": []}},
            dict.fromkeys(blocks.ROLES),
        ),
        (),
        7,
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
    first["open_items"]["categories"].append("caller mutation")
    second, _ = read(packet)
    assert second["open_items"]["categories"] == []
    assert "_inputs" not in second and "_cooperation_inputs" not in second
    assert epoch == 7 and len(calls) == 2
    packet[1]["payload_json"] = "\n " + packet[1]["payload_json"]
    assert read(packet) == (second, epoch)
    assert len(calls) == 3


@pytest.mark.parametrize("field,value", [("_roots", {}), ("_item_epoch", True)])
def test_owner_roots_and_epoch_are_checked_on_every_body_read(packet, field, value):
    read(packet)
    payload = loads_json(packet[1]["payload_json"])
    payload[field] = value
    replace_raw(packet, payload)
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


def test_candidates_still_validate_and_do_not_replace_the_persisted_base(packet, monkeypatch):
    calls = count_checks(monkeypatch)
    value, _ = read(packet)
    candidate = owner.seal(dict(value, title="尚未发布"), (), 7)
    assert owner.validate(loads_json(candidate.payload_json)) == 7
    assert read(packet) == (value, 7)
    assert len(calls) == 3
    invalid = loads_json(candidate.payload_json)
    invalid["_item_epoch"] = True
    with pytest.raises(ContractError):
        owner.validate(invalid)


def test_each_read_uses_the_current_json_integer_parser_limit(packet):
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
