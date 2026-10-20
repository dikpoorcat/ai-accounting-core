"""Formal details distinguish a known zero from missing accounting classification."""

from copy import deepcopy
from sqlite3 import IntegrityError

import pytest
from test_adoption import BUSINESS, COMPANY, SOURCE, adopt_declared, expense
from test_adoption import store as store

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.ledger._report_details import hydrate_report_contributions
from ai_accounting_modular.ledger.public import create_ledger


def detail(*, amount=None, message="资料只确认费用总额，尚未提供开办费用明细"):
    value = {"statement": "profit", "line": 15, "detail_code": "management_startup"}
    if amount is not None:
        value["amount_fen"] = amount
    else:
        value.update(
            state="unestablished",
            issue={
                "code": "missing_expense_detail",
                "business_id": BUSINESS,
                "field": "/data/report_details",
                "meaning": "管理费用开办费细目",
                "message": message,
                "allowed_precision": ["fen", "source"],
                "reusable_sources": [],
                "blocking_scope": "none",
            },
        )
    return value


def change(value, *, seq=0):
    result = expense("1000", seq=seq)
    result["plans"][0]["report_contributions"] = [deepcopy(value)]
    return result


def read_details(context):
    repo = context.repository("ledger")
    heads = repo.current_heads([BUSINESS])
    return heads, hydrate_report_contributions(repo, heads)


@pytest.mark.parametrize(
    "first,second", [(detail(), detail(amount="0")), (detail(amount="0"), detail())]
)
def test_open_replacement_preserves_known_zero_or_unknown_and_never_blocks_profit(
    first, second, store
):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [change(first)], uow)
        heads, values = read_details(uow)
        result_id = heads[0]["result_id"]
        assert values == {result_id: (first,)}
        profit = ledger.pre_tax("2026-01", "2026-01", uow)
        assert profit.fields["profit_before_tax"] == {"state": "known", "amount_fen": "-1000"}
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [change(second, seq=1)], uow)
        heads, values = read_details(uow)
        assert heads[0]["result_id"] == result_id
        assert values == {result_id: (second,)}
        assert ledger.pre_tax("2026-01", "2026-01", uow).fields == profit.fields
        row = uow.repository("ledger").children("result_report_contribution", [result_id])[0]
        assert (row["amount_fen"] == 0) is ("amount_fen" in second)
        assert (row["issue_json"] is None) is ("amount_fen" in second)
    with store.read_unit(COMPANY) as read:
        assert read_details(read)[1] == {result_id: (second,)}


def test_unknown_issue_is_accounting_state_but_repeat_is_idempotent(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [change(detail())], uow)
        assert adopt_declared(ledger, [change(detail(), seq=1)], uow).changed is False
        before = ledger.get_result_source(BUSINESS, "main", uow)
    updated = detail(message="核对后还需补充原件中的开办费分类")
    with store.write_unit(COMPANY) as uow:
        assert adopt_declared(ledger, [change(updated, seq=1)], uow).changed is True
        after = ledger.get_result_source(BUSINESS, "main", uow)
        assert after != before
        heads, values = read_details(uow)
        assert values[heads[0]["result_id"]] == (updated,)


def test_failed_adoption_rolls_back_unknown_detail_and_prior_write(store):
    ledger = create_ledger()
    invalid = change(detail(), seq=1)
    invalid["plans"][0]["lines"][1]["credit_fen"] = "999"
    with pytest.raises(ContractError, match="借贷不平衡"):
        with store.write_unit(COMPANY) as uow:
            adopt_declared(ledger, [change(detail())], uow)
            adopt_declared(ledger, [invalid], uow)
    with store.read_unit(COMPANY) as read:
        assert ledger.get_results([BUSINESS], read) == ()
        assert read_details(read)[1] == {}


def test_frozen_unknown_remains_immutable_and_correction_has_its_own_complete_details(store):
    ledger = create_ledger()
    unknown, known = detail(), detail(amount="1000")
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [change(unknown)], uow)
        repo = uow.repository("ledger")
        original = repo.current_heads([BUSINESS])[0]
        repo.freeze_heads("2026-01")
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [change(known, seq=1)], uow, correction_period="2026-02")
        repo = uow.repository("ledger")
        current = repo.current_heads([BUSINESS])[0]
        assert current["result_id"] != original["result_id"]
        values = hydrate_report_contributions(repo, [original, current])
        assert values == {original["result_id"]: (unknown,), current["result_id"]: (known,)}
        # Changing just the classification produces no artificial money journal.
        assert repo.vouchers([current["result_id"]]) == []
    with pytest.raises(IntegrityError, match="frozen_result"):
        with store.write_unit(COMPANY) as uow:
            uow.repository("ledger")._session.execute(
                "UPDATE result_report_contribution SET issue_json=NULL,amount_fen=0 "
                "WHERE result_id=?",
                (original["result_id"],),
            )
    with store.read_unit(COMPANY) as read:
        repo = read.repository("ledger")
        assert hydrate_report_contributions(repo, [original])[original["result_id"]] == (unknown,)


def test_hydration_is_one_batch_and_does_not_collapse_unknown_to_zero(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [change(detail())], uow)
    with store.read_unit(COMPANY) as read:
        repo = read.repository("ledger")
        heads = repo.current_heads([BUSINESS])
        calls = []

        class Batch:
            def children(self, table, ids):
                calls.append((table, ids))
                return repo.children(table, ids)

        assert hydrate_report_contributions(Batch(), []) == {}
        assert calls == []
        values = hydrate_report_contributions(Batch(), heads)
        assert len(calls) == 1
        assert "amount_fen" not in values[heads[0]["result_id"]][0]


@pytest.mark.parametrize("amount,issue", [(None, None), (0, "{}")])
def test_corrupt_hydration_never_infers_zero(amount, issue):
    class CorruptRows:
        def children(self, table, ids):
            return [
                {
                    "result_id": "chosen",
                    "statement": "profit",
                    "line": 15,
                    "detail_code": "management_startup",
                    "amount_fen": amount,
                    "issue_json": issue,
                }
            ]

    with pytest.raises(ContractError, match="已知金额或未知问题") as rejected:
        hydrate_report_contributions(CorruptRows(), [{"result_id": "chosen"}])
    assert rejected.value.code == "storage_corrupt"


@pytest.mark.parametrize("correction_period", [None, "2026-02"])
def test_source_locator_refinement_in_unknown_detail_does_not_create_frozen_correction(
    store, correction_period
):
    ledger = create_ledger()
    before, after = detail(), detail()
    first_locator = {"source_id": SOURCE, "location": "费用资料中的分类说明"}
    refined_locator = {"source_id": SOURCE, "location": "费用资料第 2 页第 3 行的分类说明"}
    before["issue"]["reusable_sources"] = [first_locator]
    after["issue"]["reusable_sources"] = [refined_locator]
    initial = change(before)
    initial["plans"][0]["basis"]["source_locations"] = [first_locator]
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [initial], uow)
        repo = uow.repository("ledger")
        original = repo.current_heads([BUSINESS])[0]
        repo.freeze_heads("2026-01")
    refined = change(after, seq=1)
    refined["plans"][0]["basis"]["source_locations"] = [refined_locator]
    with store.write_unit(COMPANY) as uow:
        result = adopt_declared(ledger, [refined], uow, correction_period=correction_period)
        assert result.changed is False
        current = uow.repository("ledger").current_heads([BUSINESS])[0]
        assert current["result_id"] == original["result_id"]
        assert current["generation"] == original["generation"]
        assert hydrate_report_contributions(uow.repository("ledger"), [current]) == {
            current["result_id"]: (before,)
        }


def test_open_source_hint_refinement_updates_the_current_complete_detail(store):
    ledger = create_ledger()
    before, after = detail(), detail()
    before["issue"]["reusable_sources"] = [{"source_id": SOURCE, "location": "费用分类资料"}]
    after["issue"]["reusable_sources"] = [{"source_id": SOURCE, "location": "费用分类资料第 2 页"}]
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [change(before)], uow)
        original = uow.repository("ledger").current_heads([BUSINESS])[0]
    with store.write_unit(COMPANY) as uow:
        assert adopt_declared(ledger, [change(after, seq=1)], uow).changed is True
        current = uow.repository("ledger").current_heads([BUSINESS])[0]
        assert current["result_id"] == original["result_id"]
        assert hydrate_report_contributions(uow.repository("ledger"), [current]) == {
            current["result_id"]: (after,)
        }


def test_frozen_unknown_reason_change_still_requires_a_real_correction_and_preserves_original(
    store,
):
    ledger = create_ledger()
    before = detail(message="资料只明确总费用，未明确开办费分类")
    after = detail(message="复核发现所附费用分组相互冲突，开办费金额仍未知")
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [change(before)], uow)
        repo = uow.repository("ledger")
        original = repo.current_heads([BUSINESS])[0]
        repo.freeze_heads("2026-01")
    with pytest.raises(ContractError) as missing:
        with store.write_unit(COMPANY) as uow:
            adopt_declared(ledger, [change(after, seq=1)], uow)
    assert missing.value.code == "closed_period_required"
    with store.write_unit(COMPANY) as uow:
        assert (
            adopt_declared(ledger, [change(after, seq=1)], uow, correction_period="2026-02").changed
            is True
        )
        repo = uow.repository("ledger")
        current = repo.current_heads([BUSINESS])[0]
        assert current["result_id"] != original["result_id"]
        assert hydrate_report_contributions(repo, [original, current]) == {
            original["result_id"]: (before,),
            current["result_id"]: (after,),
        }
