"""Batch scalar checks retain the original thin slices, first errors and tax SHA."""

from copy import deepcopy
from types import MappingProxyType

import _financial_snapshot_review_support as support
import pytest

from ai_accounting_modular.contracts.public import (
    ContractError,
    checked_fen,
    content_digest,
    validate_contract,
)
from ai_accounting_modular.ledger import _financial_projection_indexes as indexes
from ai_accounting_modular.ledger._financial_calculation import calculate_snapshot
from ai_accounting_modular.ledger._financial_cells import Issues
from ai_accounting_modular.ledger._financial_projection_requests import request
from ai_accounting_modular.ledger._financial_selection import Selection
from ai_accounting_modular.ledger._profit import pretax_basis_digest
from ai_accounting_modular.storage.public import Entry
from tests.modular.acceptance.performance.ledger_variant import MODES, repository_factory
from tests.modular.ledger.test_financial_projection_model import Blocks
from tests.modular.ledger.test_financial_projection_ranges import close, corrected

ID = "10000000-0000-4000-8000-000000000001"
MIN, MAX = -(2**63), 2**63 - 1
ORIGINAL_DIGESTS = {
    ("current", False): "a0feb87f884e9b0f81e90d5f526b234886ba3db287dd572723d07eecbe1a4340",
    ("as_posted", False): "c3c52da4921d6b37a823b52005b5cb686a15368d0d355b276acacd3b18d25cf9",
    ("current", True): "36e3a9e697764322bc3cf776a5cdd49b61e9f02988ed9a74156d6f242acfb71d",
    ("as_posted", True): "7f4ce0c9cd5745fbd288fe7377b6b6441fe833f05c21883eb7013393023e7e54",
}


def entry(raw=None, *, sign=1, number=0):
    return Entry(str(number), ["a" * 64, sign, raw])


def raw(*, identity=ID, period="2026-01", values=(0, 0, -100)):
    return [identity, "main", period, *values]


def original_slice(entry):
    # Frozen pre-change implementation; independent of the new batch extractor.
    value = entry.value
    if (
        type(value) is not list
        or len(value) != 3
        or type(value[0]) is not str
        or type(value[1]) is not int
        or value[1] == 0
    ):
        raise ContractError("storage_corrupt", "财务发生角色的真实采用系数损坏。")
    selected = value[2]
    if selected is None:
        return None
    if (
        type(selected) is not list
        or len(selected) != 6
        or type(selected[1]) is not str
        or not selected[1]
        or any(type(amount) is not int for amount in selected[3:])
    ):
        raise ContractError("storage_corrupt", "财务税前依据的真实薄切片损坏。")
    validate_contract("core.schema.json", "Id", selected[0])
    validate_contract("core.schema.json", "Month", selected[2])
    for amount in selected[3:]:
        checked_fen(amount)
    return {
        "business_id": selected[0],
        "result_slot": selected[1],
        "period": selected[2],
        "sign": value[1],
        "values": selected[3:],
    }


def original_digest(first, last, start, slices, prior, source, kind, *, view):
    values = [
        {key: value for key, value in item.items() if key != "sign" or view != "current"}
        for item in slices
    ]
    payload = {
        "period_from": first,
        "period_to": last,
        "results": sorted(values, key=lambda item: (item["business_id"], item["result_slot"])),
        "prior": {
            "kind": kind,
            "values": list(prior) if prior is not None else None,
            "source": [source["business_id"], source["result_slot"]] if source else None,
        },
        "start_period": start if first < start else None,
    }
    if view != "current":
        payload["view"] = view
    return content_digest(payload)


def test_valid_slices_use_two_complete_scalar_batches_and_return_detached_values(monkeypatch):
    entries = (
        entry(),
        entry(raw(values=(MIN, MAX, 0))),
        entry(raw(), sign=-1, number=1),
        entry(raw(), sign=2, number=2),
    )
    expected = [value for item in entries if (value := original_slice(item)) is not None]
    checked, batches = indexes.validate_contract_items, []

    def batch(schema, definition, values):
        batches.append((schema, definition, list(values)))
        return checked(schema, definition, values)

    monkeypatch.setattr(indexes, "validate_contract_items", batch)
    monkeypatch.setattr(
        indexes,
        "validate_contract",
        lambda *args: pytest.fail("successful batch replayed diagnostics"),
    )
    actual = indexes.pretax_slices(iter(entries))
    assert actual == expected
    assert batches == [
        ("core.schema.json", "Id", [ID, ID, ID]),
        ("core.schema.json", "Month", ["2026-01", "2026-01", "2026-01"]),
    ]
    actual[0]["values"][0] = 12
    assert entries[1].value[2][3] == MIN


def error_of(function, values):
    with pytest.raises((ContractError, ValueError, TypeError)) as caught:
        function(values)
    error = caught.value
    return type(error), getattr(error, "code", None), str(error)


@pytest.mark.parametrize(
    "values",
    [
        [entry(raw(period="2026-13")), entry(raw(identity="bad"), number=1)],
        [entry(raw(values=(MAX + 1, 0, 0))), entry(raw(identity="bad"), number=1)],
        [entry(raw(identity="bad")), entry([ID], number=1)],
        [entry(raw()), entry(raw(), sign=True, number=1)],
        [Entry("bad", ["a" * 64, 1])],
        [Entry("bad", [1, 1, None])],
        [entry({})],
        [entry(raw(), sign=0)],
        [entry(raw(values=(False, 0, 0)))],
        [entry(raw(values=(MIN - 1, 0, 0)))],
        [entry(raw(period="0000-01"))],
        [entry(raw(period=None))],
        [entry(raw(identity=None))],
        [entry([ID, "", "2026-01", 0, 0, 0])],
        [entry([ID, "main", "2026-01", 0, 0])],
    ],
)
def test_malformed_batch_retains_original_per_entry_order_code_and_message(values):
    expected = error_of(lambda entries: [original_slice(item) for item in entries], values)
    assert error_of(indexes.pretax_slices, values) == expected


@pytest.mark.parametrize("view", ("current", "as_posted"))
@pytest.mark.parametrize("prior", (None, (1, 2, -1)))
@pytest.mark.parametrize("mapping", (False, True))
def test_tax_digest_keeps_old_payload_order_boundary_and_borrowed_input(view, prior, mapping):
    # Same business/slot has two genuine legs; preserve the existing stable input order.
    entries = (entry(raw(values=(0, 0, -80))), entry(raw(), sign=-1, number=1))
    slices = sorted(
        indexes.pretax_slices(entries),
        key=lambda item: (item["business_id"], item["result_slot"], item["period"], item["sign"]),
    )
    before = deepcopy(slices)
    selected = [MappingProxyType(item) for item in slices] if mapping else slices
    args = (
        "2026-01",
        "2026-03",
        "2026-02",
        selected,
        prior,
        {"business_id": ID, "result_slot": "main"},
        "year_to_date_fen",
    )
    digest = pretax_basis_digest(*args, view=view)
    assert digest == original_digest(*args, view=view)
    assert digest == ORIGINAL_DIGESTS[view, prior is not None]
    assert slices == before


@pytest.fixture(params=MODES)
def new_book(request, tmp_path, monkeypatch):
    factory = repository_factory(request.param)
    monkeypatch.setattr(support, "ledger_repository", lambda: factory)
    return lambda start="2026-01": support.assemble(tmp_path, start)


@pytest.mark.parametrize("opening_known", (False, True))
def test_real_closed_and_current_quarter_keeps_classic_complete_snapshot(new_book, opening_known):
    book = new_book()
    expense = support.expense("batch-pretax-cost", "100")
    book.submit(*((support.opening(),) if opening_known else ()), expense)
    if opening_known:
        close(book, "2026-01")
        book.replace(corrected(expense, "80"), correction_period="2026-02")
    for view in ("current", "as_posted"):
        scope = {
            "company_id": book.company,
            "period_from": "2026-01",
            "period_to": "2026-03",
            "view": view,
        }
        with book.app.storage.read_unit(book.company) as read:
            selected, blocks = request("snapshot", scope, read), Blocks()
            blocks.publish(book.app.ledger.prepare_financial_projection((selected,), (), (), read))
            actual = book.app.ledger.read_financial_projection(selected, read, blocks).value
            classic = Selection(
                read.repository("ledger"), view, "2026-03", read.start_period, Issues()
            )
            expected = calculate_snapshot(selected.scope, read, classic)
            assert {key: value for key, value in actual.items() if key != "selection"} == expected
            cell = actual["pre_tax"]["profit_before_tax"]
            if opening_known:
                assert cell == {"state": "known", "amount_fen": "-80"}
            else:
                assert cell == {"state": "known", "amount_fen": "-100"}
                assert actual["balance_sheet"]["30"]["ending"]["state"] == "unestablished"


def test_real_cutover_preserves_unknown_annual_pretax_without_zero_default(new_book):
    book = new_book("2026-07")
    book.submit(
        support.opening(month="2026-07", cash="500000", basis="newly_established"),
        support.expense("cutover-pretax-cost", "100", "2026-07"),
    )
    scope = {
        "company_id": book.company,
        "period_from": "2026-07",
        "period_to": "2026-09",
        "view": "as_posted",
    }
    with book.app.storage.read_unit(book.company) as read:
        selected, blocks = request("snapshot", scope, read), Blocks()
        blocks.publish(book.app.ledger.prepare_financial_projection((selected,), (), (), read))
        actual = book.app.ledger.read_financial_projection(selected, read, blocks).value
        classic = Selection(
            read.repository("ledger"), "as_posted", "2026-09", read.start_period, Issues()
        )
        expected = calculate_snapshot(selected.scope, read, classic)
    assert {key: value for key, value in actual.items() if key != "selection"} == expected
    assert actual["pre_tax"]["profit_before_tax"]["state"] == "unestablished"
    assert actual["profit_statement"]["30"]["current"] == {"state": "known", "amount_fen": "-100"}
