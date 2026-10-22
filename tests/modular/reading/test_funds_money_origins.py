"""Finite payment origins preserve real allocations, batches and frozen recipients."""

import json
from dataclasses import replace
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError, PageQuery
from ai_accounting_modular.ledger.public import AdoptedOriginProjection
from ai_accounting_modular.money.public import MovementQuery
from ai_accounting_modular.reading import _funds
from tests.modular.domains.financing._support import BANK, CASH, COUNTERPARTY, fact, payment
from tests.modular.money.test_payroll import inputs as payroll_inputs

from .test_funds import apply, capture, check_data, funding, initialize, page, selection
from .test_funds_integrity import original_row, pair
from .test_funds_origins import book as book


def payroll(book):
    book.seed(expense=False)
    call, paid, wage = payroll_inputs(book.company)
    next(obj for obj in call["objects"] if obj["kind"] == "bank_account")["data"].update(
        opened_period="2026-01", zero_at_opening=True
    )
    funded = funding("1000000")
    funded["data"].update(
        owner_id=wage["data"]["person_id"], funds_account_id=paid["data"]["funds_account_id"]
    )
    funded["evidence_ids"] = [book.source_id]
    call["mutations"].append({"op": "create", "expected_seq": 0, "fact": funded})
    result = book.app.accounting_apply(call)
    assert result["completion"] == "complete", json.dumps(result, ensure_ascii=True)
    return funded, paid


def paid_expense(book, *, account=BANK):
    initialize(book)
    expense = book.expense("100")
    funded = funding("1000")
    paid = payment(expense, 100, slot="primary", party=book.party)
    funded["data"]["funds_account_id"] = paid["data"]["funds_account_id"] = account
    funded, expense, paid = apply(book, funded, expense, paid)
    return expense, funded, paid


@pytest.mark.parametrize("kind", ["settlement", "managed_reserve", "payroll_reserve"])
def test_three_real_payment_variants_keep_full_funds_packet_and_bank_batch(book, monkeypatch, kind):
    if kind == "payroll_reserve":
        funded, paid = payroll(book)
    elif kind == "settlement":
        _, funded, paid = paid_expense(book)
        # Two real obligations in one actual bank payment, rather than splitting its bank row.
        extra = book.expense("200")
        extra["data"]["creditor_id"] = COUNTERPARTY
        changed = dict(paid) | {
            "data": dict(paid["data"])
            | {
                "payment_method": "bank_batch",
                "amount_fen": "300",
                "allocations": [
                    *paid["data"]["allocations"],
                    {
                        "source": {"business_id": extra["business_id"], "slot": "primary"},
                        "amount_fen": "200",
                    },
                ],
            }
        }
        changed["data"].pop("counterparty_id")
        extra["evidence_ids"] = [book.source_id]
        changed["evidence_ids"] = [book.source_id]
        result = book.app.accounting_apply(
            book.request(
                mutations=[
                    {"op": "create", "expected_seq": 0, "fact": extra},
                    {"op": "replace", "expected_seq": 1, "fact": changed},
                ]
            )
        )
        assert result["completion"] == "complete", result
        paid = changed
    else:
        initialize(book)
        funded = funding("1000")
        paid = fact(
            "money.movement",
            {
                "type": "managed_reserve",
                "funds_account_id": BANK,
                "direction": "outflow",
                "amount_fen": "100",
            },
            actual="2026-01-16",
        )
        apply(book, funded, paid)
    account = paid["data"]["funds_account_id"]
    apply(
        book,
        *pair(
            account,
            [
                original_row(
                    book,
                    str(uuid4()),
                    funded["data"]["amount_fen"],
                    funded["effective_on"],
                    "inflow",
                ),
                original_row(
                    book, str(uuid4()), paid["data"]["amount_fen"], paid["effective_on"], "outflow"
                ),
            ],
            [funded, paid],
        ),
    )
    ledger, reader = book.kernel.ledger, book.kernel.reading
    with book.kernel.storage.read_unit(book.company) as read:
        with monkeypatch.context() as patch:
            patch.setattr(ledger, "get_obligation_origins", ledger.get_adopted_contents)
            expected = _funds.prepare(reader, selection(book), read)
        repo = read.repository("ledger")
        full, observed = repo.results, []

        def checked(ids):
            rows = full(ids)
            observed.extend(row["business_id"] for row in rows)
            return rows

        monkeypatch.setattr(repo, "results", checked)
        actual = _funds.prepare(reader, selection(book), read)
        assert paid["business_id"] not in observed
        assert actual == expected
    capture(book)
    data = page(book)["data"]
    check_data(data)
    row = next(
        item
        for item in data["collections"]["statements"]["items"]
        if item["direction"] == "outflow"
    )
    if kind == "payroll_reserve":
        assert row["amount_fen"] == row["batch_payment"]["total_fen"] == "1000000"
        assert row["batch_payment"]["items"] == [
            {"party": "合成职工", "amount_fen": "830000"},
            {"party": "工资代发备用金费用", "amount_fen": "170000"},
        ]
    elif kind == "settlement":
        assert row["amount_fen"] == row["batch_payment"]["total_fen"] == "300"
        assert {(item["party"], item["amount_fen"]) for item in row["batch_payment"]["items"]} == {
            ("合成供应商", "100"),
            ("合成对象", "200"),
        }
    else:
        assert row["amount_fen"] == "100" and "batch_payment" not in row


def test_closed_payment_recipient_is_its_adopted_party_after_creditor_changes(book, monkeypatch):
    expense, _, paid = paid_expense(book, account=CASH)
    checked = book.check()
    assert checked["ready"], checked
    assert book.close(checked)["state"] == "closed"
    expense["data"]["creditor_id"] = COUNTERPARTY
    paid["data"]["counterparty_id"] = COUNTERPARTY
    result = book.app.accounting_apply(
        book.request(
            correction_period="2026-02",
            mutations=[
                {"op": "replace", "expected_seq": 1, "fact": expense},
                {"op": "replace", "expected_seq": 1, "fact": paid},
            ],
        )
    )
    assert result["completion"] == "complete", json.dumps(result, ensure_ascii=True)
    assert (
        book.get(expense["business_id"])["businesses"][0]["fact"]["data"]["creditor_id"]
        == COUNTERPARTY
    )
    monkeypatch.setattr(
        book.kernel.ledger, "resource_definitions", lambda *a, **k: pytest.fail("current party")
    )
    with book.kernel.storage.read_unit(book.company) as read:
        values = book.kernel.money.query_movements(
            selection(book), MovementQuery(), PageQuery(500), read
        )
    actual = next(
        item for item in values.items if item["reference"]["business_id"] == paid["business_id"]
    )
    assert [(part.party_id, part.amount_fen) for part in actual["allocations"]] == [
        (book.party, 100)
    ]


@pytest.mark.parametrize(
    "damage",
    [
        "header_source",
        "header_ref",
        "header_missing",
        "origin_source",
        "origin_ref",
        "origin_kind",
        "origin_missing",
        "quantity_missing",
        "quantity_duplicate",
        "event_conflict",
    ],
)
def test_money_parts_reject_source_locator_count_and_adopted_quantity_conflicts(
    book, monkeypatch, damage
):
    paid_expense(book)
    ledger = book.kernel.ledger
    if damage.startswith("header"):
        original = ledger.get_adopted_sources

        def sources(refs, read):
            values = list(original(refs, read))
            if damage == "header_missing":
                return ()
            if damage == "header_source":
                values[0] = replace(
                    values[0], source=values[0].source | {"content_digest": "0" * 64}
                )
            else:
                values[0] = replace(values[0], reference=values[0].reference | {"generation": 999})
            return tuple(values)

        monkeypatch.setattr(ledger, "get_adopted_sources", sources)
    elif damage == "event_conflict":
        original = ledger.query_business_settlements

        def events(*args, **kwargs):
            found = original(*args, **kwargs)
            item = found.items[0]
            changed = replace(
                item, use=replace(item.use, source=item.use.source | {"content_digest": "0" * 64})
            )
            return replace(
                found,
                total_count=found.total_count + 1,
                filtered_count=found.filtered_count + 1,
                items=(*found.items, changed),
            )

        monkeypatch.setattr(ledger, "query_business_settlements", events)
    else:
        original = ledger.get_obligation_origins

        def origins(refs, read):
            values = list(original(refs, read))
            if damage == "origin_missing":
                return ()
            item = values[0]
            assert isinstance(item, AdoptedOriginProjection)
            if damage == "origin_source":
                item = replace(item, source=item.source | {"content_digest": "0" * 64})
            elif damage == "origin_ref":
                item = replace(item, reference=item.reference | {"business_period": "2026-02"})
            elif damage == "origin_kind":
                item = replace(item, target_fields={"kind": "operations.expense"})
            else:
                quantities = item.basis_fields["adopted_quantities"]
                item = replace(
                    item,
                    basis_fields=item.basis_fields
                    | {
                        "adopted_quantities": []
                        if damage == "quantity_missing"
                        else [*quantities, *quantities]
                    },
                )
            values[0] = item
            return tuple(values)

        monkeypatch.setattr(ledger, "get_obligation_origins", origins)
    with book.kernel.storage.read_unit(book.company) as read:
        with pytest.raises(ContractError) as caught:
            book.kernel.money.query_movements(
                selection(book), MovementQuery(), PageQuery(500), read
            )
    assert caught.value.code == "storage_corrupt"
