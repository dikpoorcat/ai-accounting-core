"""The existing resource tree carries only the selected classification inputs."""

from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
from types import SimpleNamespace

import _financial_snapshot_review_support as support
import pytest

from ai_accounting_modular.contracts.public import MAX_FEN, ContractError, loads_json
from ai_accounting_modular.ledger._financial import normalize
from ai_accounting_modular.ledger._financial_calculation import calculate_snapshot, summary_values
from ai_accounting_modular.ledger._financial_cells import Issues
from ai_accounting_modular.ledger._financial_projection_bodies import (
    resource_projection,
    source_atom,
    validate_body,
    validate_resource_projection,
)
from ai_accounting_modular.ledger._financial_projection_indexes import BodyDirectory
from ai_accounting_modular.ledger._financial_projection_model import Model
from ai_accounting_modular.ledger._financial_selection import Selection
from ai_accounting_modular.ledger._financial_tree import Entry
from ai_accounting_modular.ledger._financial_tree_store import TreeStore
from tests.modular.acceptance.performance.ledger_variant import MODES, repository_factory
from tests.modular.ledger.test_financial_projection_model import Blocks, scope


@pytest.fixture(params=MODES)
def book(request, tmp_path, monkeypatch):
    factory = repository_factory(request.param)
    monkeypatch.setattr(support, "ledger_repository", lambda: factory)
    return support.assemble(tmp_path)


def payroll(*, social="0", housing="0"):
    employed = support.fact(
        "resource-index-employment",
        "people.employment",
        {
            "employee_id": support.PERSON,
            "effective_from": "2026-01",
            "social_insurance_participating": False,
            "housing_fund_participating": False,
            "contribution_shortfall": "reject",
        },
    )
    wage = support.fact(
        "resource-index-wage",
        "people.remuneration",
        {
            "type": "wage",
            "person_id": support.PERSON,
            "gross_fen": "1000",
            "expense_area": "administration",
            "employment_id": employed["business_id"],
            "tax_basis": {
                "tax_period": "2026-01",
                "tax_fen": "0",
                "basis_type": "final_statement",
                "source": {"source_id": support.SOURCE},
            },
        },
    )
    if social == housing == "0":
        return employed, wage
    employed["data"].update(
        social_insurance_participating=True,
        housing_fund_participating=True,
        social_insurance_base_fen="1000",
        housing_fund_base_fen="1000",
    )
    actual = support.fact(
        "resource-index-contributions",
        "people.contribution_actual",
        {
            "employee_id": support.PERSON,
            "items": [
                {
                    "code": code,
                    "base_kind": base,
                    "state": "declared",
                    "employee_fen": amount,
                    "employer_fen": "0",
                }
                for code, base, amount in (
                    ("social", "social_insurance", social),
                    ("housing", "housing_fund", housing),
                )
            ],
        },
    )
    wage["data"].update(
        contribution_basis="actual_required", contribution_actual_id=actual["business_id"]
    )
    return employed, actual, wage


def prepare(book, blocks, requests, dirty=()):
    with book.app.storage.read_unit(book.company) as read:
        model = Model(read, blocks)
        previous = model.advance(requests, dirty)
        cold = Model(read)
        cold.advance(requests, ())
        assert model.header == cold.header
        for selected in requests:
            assert model.descriptor(selected) == cold.descriptor(selected)
        plan = model.plan(previous)
        blocks.publish(plan)
        return plan


def requests(book, read, month="2026-01", *, view="as_posted"):
    selected = scope(book, month, view)
    return tuple(
        book.app.ledger.financial_projection_request(kind, selected, read)
        for kind in ("summary_values", "snapshot")
    )


def check_original(book, read, blocks, selected):
    normalized = normalize(selected.scope, read)
    original = Selection(
        read.repository("ledger"),
        normalized["view"],
        normalized["period_to"],
        read.start_period,
        Issues(),
    )
    actual = book.app.ledger.read_financial_projection(selected, read, blocks)
    if selected.kind == "snapshot":
        expected = calculate_snapshot(normalized, read, original)
        assert {key: value for key, value in actual.value.items() if key != "selection"} == expected
        assert actual.value == book.app.ledger.financial_snapshot(selected.scope, read)
    else:
        assert actual.value.values == summary_values(normalized, original)
        assert list(actual.value.issues) == original.issues.values
        full = book.app.ledger.financial_summary(selected.scope, read)
        assert full.version == actual.version and full.scope == actual.value.scope
        public_values = {
            key: value for key, value in asdict(full).items() if key in actual.value.values
        }
        assert public_values == actual.value.values
        assert full.sources == tuple(
            sorted(
                (original.source(original.heads[key]) for key in tuple(original.used)),
                key=lambda value: (value["business_id"], value["result_slot"], value["result_id"]),
            )
        )
    return actual


def test_zero_controlled_definitions_remain_but_ready_consumers_do_not_hydrate_full_bodies(
    book, monkeypatch
):
    book.submit(support.opening(cash="5000"), *payroll())
    blocks = Blocks()
    with book.app.storage.read_unit(book.company) as read:
        selected = requests(book, read)
    prepare(book, blocks, selected)
    with book.app.storage.read_unit(book.company) as read:
        expected = [check_original(book, read, blocks, value) for value in selected]

        def no_bodies(directory, identities):
            assert not tuple(identities), "ready zero rows hydrated unrelated body fields"
            return ()

        monkeypatch.setattr(BodyDirectory, "get_many", no_bodies)
        model = Model(read, blocks)
        role = model.stock_role("as_posted", "2026-01")[1]
        heads = role.resource_heads()
        assert len(heads) == 1
        resources = next(iter(heads.values()))["resources"]
        assert {row["account_code"] for row in resources["obligation"]} == {"224102", "224103"}
        assert len(resources["obligation"]) == 2
        assert all(row["amount_fen"] == 0 for row in resources["obligation"])
        assert resources["settlement"] == resources["obligation_reclassification"] == []
        for value, original in zip(selected, expected, strict=True):
            assert book.app.ledger.read_financial_projection(value, read, blocks) == original


@pytest.mark.parametrize("view", ["current", "as_posted"])
def test_nonzero_loans_still_load_the_actual_source_and_adopted_terms_only_when_needed(
    book, monkeypatch, view
):
    contract = support.loan_contract("resource-index-loan", "2028-01-01")
    loan = support.drawdown("resource-index-drawdown", contract, "300")
    paid = support.payment(
        "resource-index-repayment", loan, "100", slot="principal", on="2026-02-01"
    )
    book.submit(support.opening(cash="5000"), *payroll(), contract, loan, paid)
    blocks = Blocks()
    with book.app.storage.read_unit(book.company) as read:
        selected = requests(book, read, "2026-02", view=view)
    prepare(book, blocks, selected)
    with book.app.storage.read_unit(book.company) as read:
        expected = [check_original(book, read, blocks, value) for value in selected]
        assert expected[1].value["balance_sheet"]["42"]["ending"]["amount_fen"] == "200"
        body_reads, basis_reads = [], []
        original_bodies, original_results = (
            BodyDirectory.get_many,
            read.repository("ledger").results,
        )

        def observe_bodies(directory, identities):
            values = original_bodies(directory, identities)
            body_reads.extend(value["source"] for value in values)
            return values

        def observe_basis(identities):
            values = original_results(identities)
            basis_reads.extend(value["business_id"] for value in values)
            return values

        monkeypatch.setattr(BodyDirectory, "get_many", observe_bodies)
        monkeypatch.setattr(read.repository("ledger"), "results", observe_basis)
        for value, original in zip(selected, expected, strict=True):
            assert book.app.ledger.read_financial_projection(value, read, blocks) == original
        assert {value["business_id"] for value in body_reads} == {loan["business_id"]}
        assert set(basis_reads) == {loan["business_id"]}


def test_fully_repaid_loan_keeps_both_definition_and_use_without_loading_an_unused_source(
    book, monkeypatch
):
    contract = support.loan_contract("resource-index-repaid-loan", "2028-01-01")
    loan = support.drawdown("resource-index-repaid-drawdown", contract, "300")
    paid = support.payment("resource-index-all-repaid", loan, "300", slot="principal")
    book.submit(support.opening(cash="5000"), contract, loan, paid)
    blocks = Blocks()
    with book.app.storage.read_unit(book.company) as read:
        selected = requests(book, read)
    prepare(book, blocks, selected)
    with book.app.storage.read_unit(book.company) as read:
        expected = [check_original(book, read, blocks, value) for value in selected]
        assert expected[1].value["balance_sheet"]["42"]["ending"]["amount_fen"] == "0"
        source_businesses = {
            source["business_id"]
            for source in book.app.ledger.financial_summary(selected[0].scope, read).sources
        }
        assert {loan["business_id"], paid["business_id"]} <= source_businesses

        def no_bodies(directory, identities):
            assert not tuple(identities), "zero remaining must not invent an actual Source use"
            return ()

        monkeypatch.setattr(BodyDirectory, "get_many", no_bodies)
        heads = Model(read, blocks).stock_role("as_posted", "2026-01")[1].resource_heads()
        assert {head["business_id"] for head in heads.values()} == {
            loan["business_id"],
            paid["business_id"],
        }
        assert sum(len(head["resources"]["obligation"]) for head in heads.values()) == 1
        assert sum(len(head["resources"]["settlement"]) for head in heads.values()) == 1
        for value, original in zip(selected, expected, strict=True):
            assert book.app.ledger.read_financial_projection(value, read, blocks) == original


def dirty(*facts):
    return tuple(
        SimpleNamespace(business_id=value["business_id"], result_slot="main") for value in facts
    )


def test_equal_amount_new_source_then_open_retirement_changes_the_same_resource_root(book):
    employed, wage = payroll()
    book.submit(support.opening(), employed, wage)
    blocks = Blocks()
    with book.app.storage.read_unit(book.company) as read:
        selected = requests(book, read)
    prepare(book, blocks, selected)
    with book.app.storage.read_unit(book.company) as read:
        before = check_original(book, read, blocks, selected[0])
        original = Model(read, blocks).stock_role("as_posted", "2026-01")[1].wire()
    evidence, text = support.identity("resource-index-new-proof"), "同额工资的正确合成来源。"
    book.submit(
        sources=[
            {
                "source_id": evidence,
                "kind": "owner_statement",
                "text": text,
                "sha256": sha256(text.encode("utf-8")).hexdigest(),
                "archive_month": "2026-01",
            }
        ]
    )
    wage["evidence_ids"] = [evidence]
    book.replace(wage)
    prepare(book, blocks, selected, dirty(wage))
    with book.app.storage.read_unit(book.company) as read:
        after = check_original(book, read, blocks, selected[0])
        changed = Model(read, blocks).stock_role("as_posted", "2026-01")[1].wire()
        assert before.value.values == after.value.values and before.version != after.version
        assert changed["dimensions"] == original["dimensions"]
        assert changed["resources"] != original["resources"]
    book.submit(
        mutations=[
            {
                "op": "withdraw",
                "business_id": wage["business_id"],
                "expected_seq": 2,
            }
        ]
    )
    prepare(book, blocks, selected, dirty(wage))
    with book.app.storage.read_unit(book.company) as read:
        assert Model(read, blocks).stock_role("as_posted", "2026-01")[1].roots["resources"] is None
        for value in selected:
            check_original(book, read, blocks, value)


def test_anonymous_social_amount_change_and_frozen_payment_retirement_keep_each_cutoff(book):
    employed, actual, wage = payroll(social="100", housing="50")
    paid = support.payment("resource-index-social-paid", wage, "100", slot="employee_social")
    book.submit(support.opening(cash="5000"), employed, actual, wage, paid)
    blocks = Blocks()
    with book.app.storage.read_unit(book.company) as read:
        selected = (
            *requests(book, read),
            *requests(book, read, "2026-02"),
            *requests(book, read, "2026-03"),
        )
    prepare(book, blocks, selected)
    with book.app.storage.read_unit(book.company) as read:
        original_january = Model(read, blocks).stock_role("as_posted", "2026-01")[1].wire()
        report = check_original(book, read, blocks, selected[1]).value
        assert report["balance_sheet"]["39"]["ending"]["amount_fen"] == "50"
    with book.app.storage.write_unit(book.company) as unit:
        checked = book.app.ledger.check_period("2026-01", unit)
        assert checked.ready
        book.app.ledger.close_period("2026-01", checked.head, unit)
    actual["data"]["items"][0]["employee_fen"] = "200"
    book.replace(actual, correction_period="2026-02")
    # The real dependency closure also re-adopts the payment against the new wage Source.
    prepare(book, blocks, selected, dirty(actual, wage, paid))
    with book.app.storage.read_unit(book.company) as read:
        assert Model(read, blocks).stock_role("as_posted", "2026-01")[1].wire() == original_january
        report = check_original(book, read, blocks, selected[3]).value
        assert report["balance_sheet"]["39"]["ending"]["amount_fen"] == "150"
        check_original(book, read, blocks, selected[0])
    with book.app.storage.write_unit(book.company) as unit:
        checked = book.app.ledger.check_period("2026-02", unit)
        assert checked.ready
        book.app.ledger.close_period("2026-02", checked.head, unit)
    book.submit(
        mutations=[{"op": "withdraw", "business_id": paid["business_id"], "expected_seq": 1}],
        correction_period="2026-03",
        explanation="银行确认这笔社保款没有实际发生。",
    )
    prepare(book, blocks, selected, dirty(paid))
    with book.app.storage.read_unit(book.company) as read:
        for index, amount in ((1, "50"), (3, "150"), (5, "250")):
            assert (
                check_original(book, read, blocks, selected[index]).value["balance_sheet"]["39"][
                    "ending"
                ]["amount_fen"]
                == amount
            )


def test_controlled_overpayment_keeps_money_and_the_real_excess_reclassification(book):
    contract = support.loan_contract("resource-index-excess-contract", "2028-01-01")
    loan = support.drawdown("resource-index-excess-loan", contract, "100")
    paid = support.payment("resource-index-excess-paid", loan, "100", slot="principal")
    book.submit(support.opening(cash="5000"), contract, loan, paid)
    blocks = Blocks()
    with book.app.storage.read_unit(book.company) as read:
        selected = requests(book, read)
    prepare(book, blocks, selected)
    loan["data"]["principal_fen"] = "80"
    recovery = support.fact(
        "resource-index-excess-recovery",
        "operations.overpayment",
        {
            "source": {"business_id": loan["business_id"], "slot": "principal"},
            "counterparty_id": support.PARTY_B,
            "amount_fen": "20",
        },
    )
    book.submit(recovery, mutations=[{"op": "replace", "expected_seq": 1, "fact": loan}])
    prepare(book, blocks, selected, dirty(loan, paid, recovery))
    with book.app.storage.read_unit(book.company) as read:
        heads = Model(read, blocks).stock_role("as_posted", "2026-01")[1].resource_heads()
        uses = [row for head in heads.values() for row in head["resources"]["settlement"]]
        reclassified = [
            row
            for head in heads.values()
            for row in head["resources"]["obligation_reclassification"]
        ]
        assert [(row["method"], row["amount_fen"]) for row in uses] == [("money", 100)]
        assert [(row["mode"], row["amount_fen"]) for row in reclassified] == [
            ("reclassify_excess", 20)
        ]
        result = check_original(book, read, blocks, selected[1]).value
        assert result["balance_sheet"]["42"]["ending"]["amount_fen"] == "0"
        assert result["balance_sheet"]["8"]["ending"]["amount_fen"] == "20"
        check_original(book, read, blocks, selected[0])


def test_resource_projection_and_complete_body_share_one_strict_row_validator(book):
    book.submit(support.opening(), *payroll())
    with book.app.storage.read_unit(book.company) as read:
        selected = requests(book, read)
        model = Model(read)
        model.advance(selected, ())
        role = model.stock_role("as_posted", "2026-01")[1]
        entry = next(role.trees["resources"].entries(role.roots["resources"]))
        valid = entry.value
        body = model.bodies.get_many((source_atom(valid["source"]),))[0]
        assert resource_projection(body) == valid
        blocks = Blocks()
        blocks.publish(model.plan(()))
        node_key = TreeStore.NODES, role.roots["resources"].digest
        original_raw = blocks.values[node_key]
        blocks.values[node_key] = original_raw + " "
        with pytest.raises(ContractError) as error:
            book.app.ledger.read_financial_projection(selected[0], read, blocks)
        assert error.value.code == "storage_corrupt"
        blocks.values[node_key] = original_raw
        invalid_rows = []
        for field, value in (
            ("amount_fen", True),
            ("amount_fen", -1),
            ("amount_fen", MAX_FEN + 1),
            ("account_code", "2202"),
            ("direction", "unknown"),
            ("category", ""),
            ("public_slot", ""),
            ("party_id", support.PARTY_A),
            ("due_on", "2026-01"),
        ):
            resources = deepcopy(valid["resources"])
            resources["obligation"][0][field] = value
            invalid_rows.append(resources)
        invalid_rows.extend(
            (
                {"obligation": []},
                valid["resources"] | {"unexpected": []},
                valid["resources"]
                | {"obligation": list(reversed(valid["resources"]["obligation"]))},
                valid["resources"] | {"obligation": valid["resources"]["obligation"] * 2},
            )
        )
        # Both use-row shapes keep strictly positive amounts, a public source
        # reference, identity and controlled method, independently of definitions.
        for table, identity_field, method_field, method in (
            ("settlement", "settlement_id", "method", "money"),
            ("obligation_reclassification", "relation_id", "mode", "reclassify_excess"),
        ):
            row = {
                identity_field: support.identity(table),
                "source_business_id": support.identity("resource-index-wage"),
                "source_public_slot": "employee_social",
                method_field: method,
                "amount_fen": 1,
            }
            validate_resource_projection(valid | {"resources": valid["resources"] | {table: [row]}})
            for field, value in (
                (identity_field, "bad"),
                ("source_business_id", "bad"),
                ("source_public_slot", ""),
                (method_field, "unsupported"),
                ("amount_fen", 0),
                ("amount_fen", True),
                ("amount_fen", MAX_FEN + 1),
            ):
                invalid_rows.append(valid["resources"] | {table: [row | {field: value}]})
        for resources in invalid_rows:
            for validator, value in (
                (validate_resource_projection, valid | {"resources": resources}),
                (
                    lambda value: validate_body(value, source_atom(body["source"])),
                    body | {"resources": resources},
                ),
            ):
                with pytest.raises(ContractError) as error:
                    validator(value)
                assert error.value.code == "storage_corrupt"
        malformed = [
            source_atom(valid["source"]),  # the former atom-only leaf has no fallback
            {key: value for key, value in valid.items() if key != "resources"},
            valid | {"unexpected": True},
            valid | {"business_period": "2026-02"},  # valid month, wrong selected key
            valid | {"posting_period": "2026-13"},
            valid | {"source": valid["source"] | {"result_generation": True}},
            valid | {"source": valid["source"] | {"basis_digest": "bad"}},
            valid | {"resources": {key: [] for key in valid["resources"]}},
        ]
        for value in malformed:
            role.roots["resources"] = role.trees["resources"].full((Entry(entry.key, value),))
            with pytest.raises(ContractError) as error:
                role.resource_heads()
            assert error.value.code == "storage_corrupt"
        # Returned mutable rows are detached from the strict persisted leaf.
        role.roots["resources"] = role.trees["resources"].full((Entry(entry.key, valid),))
        first = role.resource_heads()
        next(iter(first.values()))["resources"]["obligation"][0]["amount_fen"] = 999
        assert next(iter(role.resource_heads().values()))["resources"] == valid["resources"]


def test_small_actual_resource_tree_plan_has_only_thin_fields(book, record_property):
    book.submit(support.opening(cash="5000"), *payroll())
    blocks = Blocks()
    with book.app.storage.read_unit(book.company) as read:
        selected = requests(book, read)
    plan = prepare(book, blocks, selected)
    nodes = [
        raw
        for collection, _, raw in plan.writes
        if collection == TreeStore.NODES
        and loads_json(raw)["domain"] == "financial-controlled-resources/2"
    ]
    assert len(nodes) == 1
    assert len(loads_json(nodes[0])["entries"]) == 1
    value = loads_json(nodes[0])["entries"][0][1]
    assert set(value) == {"source", "business_period", "posting_period", "resources"}
    record_property("official", False)
    record_property("resource_node_bytes", sum(len(raw.encode("utf-8")) for raw in nodes))
    record_property("plan_rows", len(plan.writes))
    record_property(
        "plan_utf8_bytes",
        len(plan.header_json.encode("utf-8"))
        + sum(len(raw.encode("utf-8")) for _, _, raw in plan.writes),
    )
