"""Finite origin guarantees remain separate from complete Body verification."""

import json
from uuid import uuid4

import pytest
from test_adoption import BUSINESS, COMPANY, SUPPLIER, adopt_declared, expense
from test_adoption import store as store
from test_financial_core import ORIGIN, origin, save
from test_resource_indexes import split_resources
from test_snapshot import _tamper

from ai_accounting_modular.contracts.public import ContractError, PageQuery
from ai_accounting_modular.ledger import _read_origin_headers, _read_origins
from ai_accounting_modular.ledger.public import (
    AdoptedContent,
    AdoptedOriginProjection,
    ObligationDirectoryQuery,
    create_ledger,
)


def query(**fields):
    return ObligationDirectoryQuery("2026-01", "2026-01", True, **fields)


def locator(store, ledger, business=BUSINESS, month="2026-01"):
    with store.read_unit(COMPANY) as read:
        scope = {
            "company_id": COMPANY,
            "period_from": "2026-01",
            "period_to": month,
            "view": "current",
        }
        page = ledger.query_adopted_directory(
            scope, (), PageQuery(), read, business_ids=(business,)
        )
        return page.items[0].reference


def test_capture_binds_501_resources_and_origin_to_one_checked_source(store, monkeypatch):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [split_resources(501)[0]], unit)
    observed, validate = [], _read_origin_headers.validate_contract_items

    def checked(schema, definition, values, **fields):
        observed.append((definition, len(values)))
        return validate(schema, definition, values, **fields)

    monkeypatch.setattr(_read_origin_headers, "validate_contract_items", checked)
    with store.read_unit(COMPANY) as read:
        repo = read.repository("ledger")
        fetch, calls = repo.read_origin_rows, []

        def origins(ids, selection):
            calls.append(ids)
            return fetch(ids, selection)

        monkeypatch.setattr(repo, "read_origin_rows", origins)
        monkeypatch.setattr(repo, "results", lambda *a: pytest.fail("简单投影读取了完整正文"))
        actual = ledger.capture_obligation_directory(query(), read)
    assert len(calls) == 1 and len(calls[0]) == 1
    assert observed.count(("ResultRef", 1)) == observed.count(("ResultSourceRef", 1)) == 1
    assert len(actual.selected) == 501 and len(actual.origins) == len(actual.sources) == 1
    body = next(iter(actual.origins.values()))
    assert isinstance(body, AdoptedOriginProjection)
    assert body.source is actual.sources[0].source
    assert body.reference is actual.sources[0].reference
    assert body.target_fields == {"kind": "operations.expense"}
    assert body.basis_fields == {
        "kind": "operations.expense",
        "adopted_quantities": [],
        "input": {
            "creditor_id": SUPPLIER,
            "creditor_kind": "supplier",
            "expense_class": "administration",
        },
    }


def test_exact_origins_preserve_frozen_and_current_and_can_locate_effective_owner(store):
    ledger = create_ledger()
    save(store, ledger, [expense()], freeze="2026-01")
    january = locator(store, ledger)
    revised = expense(seq=1)
    revised["plans"][0]["obligations"][0]["allowed_settlements"] = ["money"]
    save(store, ledger, [revised], correction="2026-02")
    february = locator(store, ledger, month="2026-02")
    with store.read_unit(COMPANY) as read:
        headers = ledger.get_effective_voucher_directory([february], read)
        older = ledger.get_obligation_origins([headers[0].result], read)[0]
        actual = ledger.get_obligation_origins([january, february], read)
        all_bodies = ledger.get_adopted_contents([january, february], read)
    assert older.reference == january
    assert actual[0].source != actual[1].source
    for projected, full in zip(actual, all_bodies, strict=True):
        assert projected.source == full.source
        assert projected.reference == full.reference
        assert projected.target_fields["kind"] == full.target["kind"]
        assert projected.basis_fields["input"] == {
            key: full.basis["input"][key] for key in projected.basis_fields["input"]
        }


def test_open_replacement_rejects_old_generation_and_missing_or_duplicate_exact_ids(store):
    ledger = create_ledger()
    save(store, ledger, [expense()])
    old = locator(store, ledger)
    save(store, ledger, [expense("80000", seq=1)])
    current = locator(store, ledger)
    with store.read_unit(COMPANY) as read:
        for requested, code in [
            ([old], "snapshot_changed"),
            ([current, current], "invalid_input"),
            ([current | {"result_id": str(uuid4())}], "not_found"),
        ]:
            with pytest.raises(ContractError) as error:
                ledger.get_obligation_origins(requested, read)
            assert error.value.code == code
        assert ledger.get_obligation_origins([], read) == ()


def test_opening_uses_complete_existing_schema_and_hash_verification(store):
    ledger = create_ledger()
    save(store, ledger, [origin("0")])
    ref = locator(store, ledger, ORIGIN)
    with store.read_unit(COMPANY) as read:
        expected = ledger.get_adopted_contents([ref], read)[0]
        actual = ledger.get_obligation_origins([ref], read)[0]
    assert isinstance(actual, AdoptedContent) and actual == expected
    _tamper(store, "UPDATE adopted_result SET basis_json='{}'")
    with store.read_unit(COMPANY) as read:
        with pytest.raises(ContractError) as error:
            ledger.get_obligation_origins([ref], read)
    assert error.value.code == "storage_corrupt"


@pytest.mark.parametrize("damage", ["missing_required", "wrong_basis_kind", "bad_quantity"])
def test_required_fragment_fields_and_real_quantity_union_are_strict(store, damage):
    ledger = create_ledger()
    save(store, ledger, [expense()])
    ref = locator(store, ledger)

    def mutate(connection):
        body = json.loads(connection.execute("SELECT basis_json FROM adopted_result").fetchone()[0])
        if damage == "missing_required":
            del body["input"]["creditor_kind"]
        elif damage == "wrong_basis_kind":
            body["kind"] = "operations.service_sale"
        else:
            body["adopted_quantities"] = [{"type": "obligation", "party_id": SUPPLIER}]
        connection.execute("UPDATE adopted_result SET basis_json=?", (json.dumps(body),))

    _tamper(store, mutate)
    with store.read_unit(COMPANY) as read:
        with pytest.raises(ContractError) as error:
            ledger.get_obligation_origins([ref], read)
    assert error.value.code == "storage_corrupt"


def test_adopted_participant_is_retained_and_not_shared_between_projection_instances(store):
    ledger = create_ledger()
    change = expense()
    quantity = {
        "type": "obligation",
        "source": {"business_id": BUSINESS, "slot": "primary"},
        "account_code": "2202",
        "direction": "payable",
        "amount_fen": "100000",
        "party_id": SUPPLIER,
    }
    change["plans"][0]["basis"]["adopted_quantities"] = [quantity]
    save(store, ledger, [change])
    ref = locator(store, ledger)
    with store.read_unit(COMPANY) as read:
        first = ledger.get_obligation_origins([ref], read)[0]
        second = ledger.get_obligation_origins([ref], read)[0]
    assert first.basis_fields["adopted_quantities"] == [quantity]
    first.basis_fields["adopted_quantities"][0]["source"]["slot"] = "changed-by-caller"
    assert second.basis_fields["adopted_quantities"] == [quantity]


def test_finite_read_does_not_weaken_the_complete_body_api(store):
    ledger = create_ledger()
    save(store, ledger, [expense()])
    ref = locator(store, ledger)

    def damage(connection):
        body = json.loads(connection.execute("SELECT basis_json FROM adopted_result").fetchone()[0])
        body["policies"] = "damaged-unselected-field"
        connection.execute("UPDATE adopted_result SET basis_json=?", (json.dumps(body),))

    _tamper(store, damage)
    with store.read_unit(COMPANY) as read:
        assert isinstance(ledger.get_obligation_origins([ref], read)[0], AdoptedOriginProjection)
        with pytest.raises(ContractError) as error:
            ledger.get_adopted_contents([ref], read)
    assert error.value.code == "storage_corrupt"


@pytest.mark.parametrize(
    "field,value",
    [
        ("creditor_kind", None),
        ("creditor_kind", True),
        ("creditor_kind", 1.0),
        ("creditor_kind", "unknown"),
        ("creditor_id", "bad-id"),
        ("cooperation_id", None),
        ("expense_class", ["administration"]),
    ],
)
def test_actual_sql_projection_preserves_null_bool_float_and_rejects_bad_selected_fields(
    store, field, value
):
    ledger = create_ledger()
    save(store, ledger, [expense()])
    ref = locator(store, ledger)

    def damage(connection):
        body = json.loads(connection.execute("SELECT basis_json FROM adopted_result").fetchone()[0])
        body["input"][field] = value
        connection.execute("UPDATE adopted_result SET basis_json=?", (json.dumps(body),))

    _tamper(store, damage)
    with store.read_unit(COMPANY) as read:
        with pytest.raises(ContractError) as error:
            ledger.get_obligation_origins([ref], read)
    assert error.value.code == "storage_corrupt"


def test_same_read_requeries_after_a_write_and_never_reuses_a_prior_acceptance(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
        first = ledger.capture_obligation_directory(query(), unit)
        changed = expense("80000", seq=1)
        plan = changed["plans"][0]
        plan["basis"]["input"]["expense_class"] = "sales"
        plan["basis"]["input"]["report_details"] = [{"code": "sales_other", "amount_fen": "80000"}]
        plan["target"]["values"]["expense_or_income_class"] = "sales"
        plan["lines"][0]["account_code"] = "5601"
        adopt_declared(ledger, [changed], unit)
        second = ledger.capture_obligation_directory(query(), unit)
    assert (
        next(iter(first.origins.values())).basis_fields["input"]["expense_class"]
        == "administration"
    )
    assert next(iter(second.origins.values())).basis_fields["input"]["expense_class"] == "sales"
    assert first.sources[0].source != second.sources[0].source


@pytest.mark.parametrize(
    "kind,data",
    [
        (
            "operations.expense",
            {
                "creditor_id": SUPPLIER,
                "creditor_kind": "supplier",
                "expense_class": "administration",
            },
        ),
        ("operations.service_sale", {"customer_id": SUPPLIER}),
        ("operations.sale_return", {"sale_id": BUSINESS, "customer_id": SUPPLIER}),
        ("operations.expense_recovery", {"expense_id": BUSINESS, "counterparty_id": SUPPLIER}),
        ("operations.pass_through", {"payer_id": SUPPLIER, "beneficiary_id": BUSINESS}),
    ],
)
def test_all_finite_plans_share_original_named_schema_fields_without_mutable_aliases(kind, data):
    plan = _read_origins._SELECTIONS[kind]
    quantity = {
        "type": "obligation",
        "source": {"business_id": BUSINESS, "slot": "primary"},
        "account_code": "2202",
        "direction": "payable",
        "amount_fen": "100000",
        "party_id": SUPPLIER,
    }
    tokens = [json.dumps(kind), json.dumps(kind), json.dumps([quantity])]
    tokens.extend(json.dumps(data[field]) if field in data else None for field in plan.fields)
    row, checked = {"projection_json": json.dumps(tokens)}, {}
    target, basis = _read_origins._fragment(row, plan, checked)
    assert target == {"kind": kind} and basis == {
        "kind": kind,
        "input": data,
        "adopted_quantities": [quantity],
    }
    basis["input"].clear()
    basis["adopted_quantities"][0]["source"]["slot"] = "caller-mutated"
    again = _read_origins._fragment(row, plan, checked)[1]
    assert again["input"] == data
    assert again["adopted_quantities"] == [quantity]
