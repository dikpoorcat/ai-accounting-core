"""One financial selection resolves each precise source and Target only once."""

import json
from copy import deepcopy
from types import SimpleNamespace
from uuid import NAMESPACE_DNS, uuid5

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.ledger import _financial_selection as financial
from ai_accounting_modular.ledger import _planning
from ai_accounting_modular.ledger._financial_cells import Issues
from ai_accounting_modular.ledger._financial_reads import _sources


def _head(index):
    return {
        "business_id": str(uuid5(NAMESPACE_DNS, f"financial-cache-business-{index}")),
        "result_id": str(uuid5(NAMESPACE_DNS, f"financial-cache-result-{index}")),
        "result_slot": "main",
        "generation": 1,
        "business_period": "2026-01",
        "posting_period": "2026-01",
        "state": "open",
        "baseline_result_id": None,
        "calculation_key": "expense/1",
        "calculation_revision": "1",
        "basis_digest": "a" * 64,
        "target_digest": "b" * 64,
        "target_kind": "operations.expense",
        "business_kind": "operations.expense",
        "target_json": json.dumps(
            {
                "kind": "operations.expense",
                "result_slot": "main",
                "values": {
                    "amount_fen": str(index + 1),
                    "expense_or_income_class": "administration",
                    "report_detail_state": "known",
                },
            }
        ),
    }


def _selection(heads):
    repo = SimpleNamespace(
        financial_boundaries=lambda **kwargs: (),
        financial_heads=lambda *args, **kwargs: heads,
        financial_lines=lambda ids: [
            {
                "voucher_id": identity,
                "line_no": 1,
                "account_code": "5602",
                "debit_fen": 1,
                "credit_fen": 0,
            }
            for identity in ids
        ],
    )
    return financial.Selection(repo, "current", "2026-01", "2026-01", Issues())


def test_hundred_heads_three_source_passes_verify_once_and_decode_target_once(monkeypatch):
    heads = [_head(index) for index in range(100)]
    expected = {head["result_id"]: financial.stored_source_reference(head) for head in heads}
    source, decode = financial.stored_source_reference, json.loads
    validate, digest = _planning.validate_contract, _planning.source_digest
    verified, decoded, journals, schemas, digests = [], [], [], [], []
    targets = {head["target_json"] for head in heads}

    def actual_source(head):
        verified.append((head["result_id"], head["generation"]))
        return source(head)

    def actual_decode(raw, *args, **kwargs):
        if raw in targets:
            decoded.append(raw)
        return decode(raw, *args, **kwargs)

    def actual_validate(schema, definition, value):
        schemas.append(definition)
        return validate(schema, definition, value)

    def actual_digest(*args):
        digests.append(args)
        return digest(*args)

    def actual_journals(repo, requested, *, headers_only):
        assert headers_only
        journals.append(tuple(head["result_id"] for head in requested))
        return {head["result_id"]: {"voucher_id": head["result_id"]} for head in requested}

    monkeypatch.setattr(financial, "stored_source_reference", actual_source)
    monkeypatch.setattr(financial.json, "loads", actual_decode)
    monkeypatch.setattr(financial, "target_vouchers", actual_journals)
    monkeypatch.setattr(_planning, "validate_contract", actual_validate)
    monkeypatch.setattr(_planning, "source_digest", actual_digest)
    selection = _selection(heads)
    selected = list(selection.stock("2026-01").values())
    first = selection.lines(selected)
    assert selection.lines(selected) == first
    actual = _sources(selection)
    assert {ref["result_id"]: ref for ref in actual} == expected
    assert len(verified) == 100 and len(set(verified)) == 100
    assert schemas == ["ResultSourceRef"] * 100 and len(digests) == 100
    assert len(journals) == 1
    assert selection.flow("2026-01", "2026-01") == dict.fromkeys(expected, 1)
    for head in selected:
        assert selection.target(head) == decode(head["target_json"])
        assert selection.target(head)["kind"] == "operations.expense"
    assert len(decoded) == 100
    # Source dictionaries leave the private selection and must not share writable state.
    actual[0]["business_id"] = "changed by a consumer"
    assert {ref["result_id"]: ref for ref in _sources(selection)} == expected


def test_new_generation_and_frozen_baseline_keep_distinct_exact_sources_and_targets():
    old, new = _head(1), _head(2)
    old["state"] = "frozen"
    new["baseline_result_id"] = old["result_id"]
    selection = _selection((old, new))
    assert selection.source(old) != selection.source(new)
    assert selection.target(old)["values"]["amount_fen"] == "2"
    assert selection.target(new)["values"]["amount_fen"] == "3"
    changed = deepcopy(new)
    changed.update(generation=2, target_json=new["target_json"].replace('"3"', '"4"'))
    assert selection.source(changed)["result_generation"] == 2
    assert selection.target(changed)["values"]["amount_fen"] == "4"
    assert selection.source(new)["result_generation"] == 1
    assert selection.target(new)["values"]["amount_fen"] == "3"


@pytest.mark.parametrize(
    "field",
    [
        "business_id",
        "result_slot",
        "calculation_key",
        "calculation_revision",
        "target_digest",
        "basis_digest",
    ],
)
def test_conflicting_same_generation_source_does_not_reuse_a_cached_proof(field):
    head = _head(1)
    selection = _selection((head,))
    original = selection.source(head)
    damaged = dict(head) | {field: "changed"}
    with pytest.raises(ContractError) as caught:
        selection.source(damaged)
    assert caught.value.code == "storage_corrupt"
    assert selection.source(head) == original


def test_changed_or_invalid_target_cannot_borrow_an_existing_decoded_value():
    head = _head(1)
    selection = _selection((head,))
    assert selection.target(head)["values"]["amount_fen"] == "2"
    with pytest.raises(ContractError) as caught:
        selection.target(dict(head) | {"target_json": "invalid JSON"})
    assert caught.value.code == "storage_corrupt"
    with pytest.raises((ContractError, json.JSONDecodeError)):
        selection.target(dict(head) | {"generation": 2, "target_json": "invalid JSON"})
    with pytest.raises(ContractError):
        selection.source(dict(head) | {"generation": 2, "business_id": "invalid UUID"})


@pytest.mark.parametrize("generation", [True, 1.0])
def test_invalid_generation_cannot_hit_an_existing_integer_source(generation):
    head = _head(1)
    selection = _selection((head,))
    expected = selection.source(head)
    with pytest.raises(ContractError):
        selection.source(dict(head) | {"generation": generation})
    assert selection.source(head) == expected


def test_main_profit_rows_are_computed_once_per_precise_head_and_cannot_be_mutated(monkeypatch):
    heads = [_head(index) for index in range(100)]
    calculate, calls = financial.profit_rows, []

    def actual_rows(lines):
        calls.append(lines)
        return calculate(lines)

    monkeypatch.setattr(financial, "profit_rows", actual_rows)
    selection = _selection(heads)
    lines = {
        head["result_id"]: [{"account_code": "5602", "debit_fen": str(index), "credit_fen": "0"}]
        for index, head in enumerate(heads)
    }
    for _ in range(3):
        for index, head in enumerate(heads):
            rows = selection.profit_rows(head, lines[head["result_id"]])
            assert rows == {14: index}
    assert len(calls) == 100
    with pytest.raises(TypeError):
        rows[14] = 999
    head = heads[0]
    changed = dict(head) | {"generation": 2}
    new_lines = [{"account_code": "5602", "debit_fen": "7", "credit_fen": "0"}]
    assert selection.profit_rows(changed, new_lines) == {14: 7}
    assert selection.profit_rows(head, lines[head["result_id"]]) == {14: 0}
    assert len(calls) == 101
    # No process-wide reuse: another read selection sees its own selected journals.
    assert _selection(heads).profit_rows(head, new_lines) == {14: 7}
    assert len(calls) == 102


@pytest.mark.parametrize("generation", [True, 1.0])
def test_profit_cache_does_not_confuse_invalid_generation_with_a_known_integer(generation):
    head = _head(1)
    selection = _selection((head,))
    normal = [{"account_code": "5602", "debit_fen": "1", "credit_fen": "0"}]
    assert selection.profit_rows(head, normal) == {14: 1}
    # Journals are already validated by lines(); the cache itself must not skip a distinct key.
    damaged = [{"account_code": "5602", "debit_fen": "invalid", "credit_fen": "0"}]
    with pytest.raises(ValueError):
        selection.profit_rows(dict(head) | {"generation": generation}, damaged)


def test_metadata_targets_load_once_in_batch_and_decode_only_when_consumed(monkeypatch):
    full = [_head(index) for index in range(3)]
    metadata = [
        {key: value for key, value in head.items() if key != "target_json"} for head in full
    ]
    selection = _selection(metadata)
    requested, decoded = [], []
    decode = json.loads

    def bodies(ids):
        requested.append(tuple(ids))
        return [
            {key: head[key] for key in ("result_id", "generation", "target_json")}
            for head in full
            if head["result_id"] in ids
        ]

    def actual_decode(raw, *args, **kwargs):
        decoded.append(raw)
        return decode(raw, *args, **kwargs)

    selection.repo.financial_targets = bodies
    monkeypatch.setattr(financial.json, "loads", actual_decode)
    selection.load_targets(metadata)
    assert requested == [tuple(head["result_id"] for head in metadata)] and decoded == []
    for head, expected in zip(metadata, full, strict=True):
        assert selection.target(head) == decode(expected["target_json"])
        assert selection.target(head) == decode(expected["target_json"])
    assert len(decoded) == 3
    selection.load_targets(metadata)
    assert len(requested) == 1


@pytest.mark.parametrize("generation", [True, 1.0, 2])
def test_target_batch_cannot_substitute_another_typed_generation(generation):
    head = _head(1)
    metadata = {key: value for key, value in head.items() if key != "target_json"}
    selection = _selection((metadata,))
    selection.repo.financial_targets = lambda ids: [head | {"generation": generation}]
    with pytest.raises(ContractError) as caught:
        selection.load_targets((metadata,))
    assert caught.value.code == "storage_corrupt"
