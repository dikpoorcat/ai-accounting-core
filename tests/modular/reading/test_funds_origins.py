"""Funds meanings use exact frozen fragments; complex origins still verify complete bodies."""

import sqlite3
from contextlib import closing
from dataclasses import replace

import pytest

from ai_accounting_modular.contracts.public import (
    ContractError,
    PageQuery,
    canonical_json,
    loads_json,
)
from ai_accounting_modular.domains.financing import _read_funds
from ai_accounting_modular.ledger.public import AdoptedContent, AdoptedOriginProjection
from ai_accounting_modular.reading import _funds, _funds_sources
from ai_accounting_modular.storage.public import module_schema
from tests.modular.acceptance.performance.ledger_variant import MODES, install_ledger_implementation
from tests.modular.application._working_case import case as working_case
from tests.modular.domains.financing._support import CASH, COUNTERPARTY, payment, six

from .test_funds import apply, capture, funding, initialize, page, selection


@pytest.fixture(params=MODES)
def book(tmp_path, request):
    with install_ledger_implementation(request.param):
        fixture = working_case.__wrapped__(tmp_path)
        case = next(fixture)
        try:
            yield case
        finally:
            fixture.close()


def mixed(book, *, account=None):
    initialize(book)
    *_, purchase, _ = six()
    purchase["data"]["purchase_price_fen"] = "300"
    expense = book.expense("700")
    expense["data"]["creditor_id"] = COUNTERPARTY
    paid = payment(purchase, 1000, slot="primary", party=COUNTERPARTY)
    paid["data"]["allocations"].append(
        {"source": {"business_id": expense["business_id"], "slot": "primary"}, "amount_fen": "700"}
    )
    paid["data"]["allocations"][0]["amount_fen"] = "300"
    funded = funding("2000")
    if account is not None:
        funded["data"]["funds_account_id"] = paid["data"]["funds_account_id"] = account
    apply(book, funded, expense, purchase, paid)
    return expense, purchase, paid


def test_mixed_funds_fragment_matches_full_model_and_never_loads_expense_body(book, monkeypatch):
    expense, purchase, paid = mixed(book)
    ledger, reader = book.kernel.ledger, book.kernel.reading
    with book.kernel.storage.read_unit(book.company) as read:
        with monkeypatch.context() as patch:
            patch.setattr(ledger, "get_obligation_origins", ledger.get_adopted_contents)
            expected = _funds.prepare(reader, selection(book), read)
        observed, fragments = [], []
        repository = read.repository("ledger")
        fetch, origins = repository.results, ledger.get_obligation_origins

        def bodies(ids):
            rows = fetch(ids)
            observed.extend(row["business_id"] for row in rows)
            return rows

        def projected(refs, read):
            result = origins(refs, read)
            fragments.extend(result)
            return result

        monkeypatch.setattr(repository, "results", bodies)
        monkeypatch.setattr(ledger, "get_obligation_origins", projected)
        actual = _funds.prepare(reader, selection(book), read)
    assert actual == expected
    assert expense["business_id"] not in observed
    by_business = {item.reference["business_id"]: item for item in fragments}
    assert isinstance(by_business[expense["business_id"]], AdoptedOriginProjection)
    assert all(
        isinstance(by_business[item["business_id"]], AdoptedContent) for item in (purchase, paid)
    )


@pytest.mark.parametrize("damage", ["source", "generation", "period", "kind", "missing"])
def test_funds_fragment_must_match_full_selected_source_and_locator(book, monkeypatch, damage):
    expense, *_ = mixed(book)
    ledger, reader = book.kernel.ledger, book.kernel.reading
    with book.kernel.storage.read_unit(book.company) as read:
        activity = book.kernel.money.account_activity(selection(book), read)
        origins = ledger.get_obligation_origins

        def conflict(refs, read):
            result = list(origins(refs, read))
            index = next(
                i
                for i, item in enumerate(result)
                if item.reference["business_id"] == expense["business_id"]
            )
            item = result[index]
            if damage == "missing":
                result.pop(index)
            elif damage == "kind":
                result[index] = replace(item, target_fields={"kind": "operations.service_sale"})
            elif damage == "source":
                result[index] = replace(item, source=item.source | {"content_digest": "0" * 64})
            else:
                field, value = (
                    ("generation", 999)
                    if damage == "generation"
                    else ("business_period", "2026-02")
                )
                result[index] = replace(item, reference=item.reference | {field: value})
            return tuple(result)

        monkeypatch.setattr(ledger, "get_obligation_origins", conflict)
        with pytest.raises(ContractError) as error:
            _funds_sources.load(reader, activity, selection(book), read)
    assert error.value.code == "storage_corrupt"


def test_same_amount_new_expense_class_changes_payment_purpose(book):
    expense, _, paid = mixed(book)
    capture(book)
    previous = next(
        item
        for item in page(book)["data"]["collections"]["movements"]["items"]
        if item["subject_id"] == paid["business_id"]
    )
    expense["data"]["expense_class"] = "sales"
    changed = book.app.accounting_apply(
        book.request(mutations=[{"op": "replace", "expected_seq": 1, "fact": expense}])
    )
    assert changed["completion"] == "complete", changed
    capture(book)
    current = next(
        item
        for item in page(book)["data"]["collections"]["movements"]["items"]
        if item["subject_id"] == paid["business_id"]
    )
    assert previous["amount_fen"] == current["amount_fen"] == "1000"
    assert "管理费用" in previous["list_summary"]
    assert "销售费用" in current["list_summary"] and "管理费用" not in current["list_summary"]


def test_closed_payment_keeps_old_projected_purpose_after_current_expense_changes(book):
    expense, _, paid = mixed(book, account=CASH)
    capture(book)
    previous = page(book)["data"]
    assert any(
        item["subject_id"] == paid["business_id"] and "管理费用" in item["list_summary"]
        for item in previous["collections"]["movements"]["items"]
    )
    checked = book.check()
    assert checked["ready"], checked
    assert book.close(checked)["state"] == "closed"
    expense["data"]["expense_class"] = "sales"
    changed = book.app.accounting_apply(
        book.request(
            correction_period="2026-02",
            mutations=[{"op": "replace", "expected_seq": 1, "fact": expense}],
        )
    )
    assert changed["completion"] == "complete", changed
    capture(book)
    assert page(book)["data"] == previous


def test_complex_funds_origin_still_rejects_unreturned_basis_damage(book):
    initialize(book)
    (funded,) = apply(book, funding("100"))
    with book.kernel.storage.read_unit(book.company) as read:
        activity = book.kernel.money.account_activity(selection(book), read)
        sources = _funds_sources.load(book.kernel.reading, activity, selection(book), read)
        content = next(iter(sources.contents.values()))
        assert isinstance(content, AdoptedContent)
        path = book.kernel.storage.company_metadata(read).database_path
        row = read.repository("ledger").results([content.reference["result_id"]])[0]
        basis = loads_json(row["basis_json"])
        basis["policies"] = "damaged-unselected-field"
    triggers = [item for item in module_schema("ledger").objects if item.type == "trigger"]
    with closing(sqlite3.connect(path)) as connection:
        for trigger in triggers:
            connection.execute(f'DROP TRIGGER "{trigger.name}"')
        connection.execute(
            "UPDATE adopted_result SET basis_json=? WHERE result_id=?",
            (canonical_json(basis), content.reference["result_id"]),
        )
        for trigger in triggers:
            connection.execute(trigger.sql)
        connection.commit()
    assert funded["business_id"] == content.reference["business_id"]
    with book.kernel.storage.read_unit(book.company) as read:
        with pytest.raises(ContractError) as error:
            _funds_sources.load(book.kernel.reading, activity, selection(book), read)
    assert error.value.code == "storage_corrupt"


def test_fund_events_reuse_exact_producer_once_and_locate_consumer_by_source(book, monkeypatch):
    _, purchase, paid = mixed(book)
    ledger, owner = book.kernel.ledger, book.kernel.reading.investment
    with book.kernel.storage.read_unit(book.company) as read:
        expected = owner.money_fund_activity(selection(book), read)
        fetch, directory, sources = (
            ledger.get_adopted_contents,
            ledger.query_adopted_directory,
            ledger.get_adopted_sources,
        )
        reads, located = [], []

        def contents(refs, read):
            reads.extend(ref["business_id"] for ref in refs)
            return fetch(refs, read)

        def selected(scope, kinds, page, read, **query):
            assert kinds, "基金消费者必须按精确 SourceRef 定位，不能按业务重选"
            return directory(scope, kinds, page, read, **query)

        def exact(refs, read):
            located.extend(refs)
            return sources(refs, read)

        monkeypatch.setattr(ledger, "get_adopted_contents", contents)
        monkeypatch.setattr(ledger, "query_adopted_directory", selected)
        monkeypatch.setattr(ledger, "get_adopted_sources", exact)
        actual = owner.money_fund_activity(selection(book), read)
        assert actual == expected and reads == [purchase["business_id"]]
        assert {ref["business_id"] for ref in located} == {paid["business_id"]}
        reads.clear()
        assert owner.money_fund_activity(selection(book), read) == expected
        assert reads == [purchase["business_id"]], "正文复用不能跨一次 _events 调用"


@pytest.mark.parametrize("damage", ["digest", "slot", "missing"])
def test_fund_consumer_exact_source_is_not_just_result_id_and_generation(book, monkeypatch, damage):
    mixed(book)
    ledger, owner = book.kernel.ledger, book.kernel.reading.investment
    sources = ledger.get_adopted_sources

    def conflict(refs, read):
        result = sources(refs, read)
        if damage == "missing":
            return ()
        item = result[0]
        source = item.source | {"content_digest": "0" * 64} if damage == "digest" else item.source
        reference = (
            item.reference | {"result_slot": "other"} if damage == "slot" else item.reference
        )
        return (replace(item, source=source, reference=reference),)

    monkeypatch.setattr(ledger, "get_adopted_sources", conflict)
    with book.kernel.storage.read_unit(book.company) as read:
        with pytest.raises(ContractError) as error:
            owner.money_fund_activity(selection(book), read)
    assert error.value.code == "storage_corrupt"


def test_fund_capture_rejects_same_result_with_two_exact_locators(book):
    mixed(book)
    with book.kernel.storage.read_unit(book.company) as read:
        entries = book.kernel.ledger.query_adopted_directory(
            selection(book),
            _read_funds.KINDS,
            PageQuery(),
            read,
        ).items
        ref = entries[0].reference
        cache = {}
        _read_funds._contents(book.kernel.ledger, [ref], read, cache)
        with pytest.raises(ContractError) as error:
            _read_funds._contents(book.kernel.ledger, [ref | {"generation": 999}], read, cache)
    assert error.value.code == "storage_corrupt"


def test_shared_fund_body_still_matches_producer_directory_source(book, monkeypatch):
    mixed(book)
    ledger, owner = book.kernel.ledger, book.kernel.reading.investment
    directory = ledger.query_adopted_directory

    def conflict(*args, **kwargs):
        result = directory(*args, **kwargs)
        return replace(
            result,
            items=tuple(
                replace(item, source=item.source | {"content_digest": "0" * 64})
                for item in result.items
            ),
        )

    monkeypatch.setattr(ledger, "query_adopted_directory", conflict)
    with book.kernel.storage.read_unit(book.company) as read:
        with pytest.raises(ContractError) as error:
            owner.money_fund_activity(selection(book), read)
    assert error.value.code == "storage_corrupt"
