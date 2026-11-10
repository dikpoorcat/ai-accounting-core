"""One adopted result owns a complete, independently balanced journal group."""

from copy import deepcopy

import pytest
from test_adoption import BUSINESS, COMPANY, SUPPLIER, adopt_declared, expense, result, withdrawal
from test_adoption import store as store
from test_closing import close
from test_financial_core import origin, report, save, value

from ai_accounting_modular.contracts.public import MAX_FEN, ContractError
from ai_accounting_modular.ledger._financial_calculation import calculate_snapshot
from ai_accounting_modular.ledger._financial_cells import Issues
from ai_accounting_modular.ledger._financial_projection_bodies import capture_bodies
from ai_accounting_modular.ledger._financial_selection import Selection
from ai_accounting_modular.ledger._repository import LedgerRepository
from ai_accounting_modular.ledger.public import VoucherLocator, create_ledger
from tests.modular.application._working_case import case as case
from tests.modular.domains.people.test_people import case as wage_case
from tests.modular.domains.people.test_people import no_contributions
from tests.modular.domains.people.test_read_remuneration import add


def multiple(*, deduction=300, seq=0):
    change = expense("1000", seq=seq)
    plan = change["plans"][0]
    accrual = plan["journals"][0]
    accrual.update(journal_key="accrual", summary="合成计提凭证")
    if deduction:
        plan["journals"].append(
            {
                "journal_key": "deductions",
                "summary": "合成代扣凭证",
                "lines": [
                    {
                        "line_no": index,
                        "account_code": code,
                        "debit_fen": str(deduction if index == 1 else 0),
                        "credit_fen": str(deduction if index == 2 else 0),
                        "party_id": SUPPLIER,
                        "summary": "合成个人代扣",
                    }
                    for index, code in ((1, "2202"), (2, "224102"))
                ],
            }
        )
    plan["obligations"][0]["amount_fen"] = str(1000 - deduction)
    return change


def keyed(view):
    return {(row["journal_key"], row["role"]): row for row in view["vouchers"]}


def test_open_group_preserves_each_number_and_does_not_reuse_a_retired_identity(store):
    ledger = create_ledger()
    save(store, ledger, [multiple()])
    first = result(store)
    assert len(first["vouchers"]) == 2
    assert {row["lines"][0]["line_no"] for row in first["vouchers"]} == {1}
    assert first["vouchers"][0]["summary"] != first["vouchers"][0]["lines"][0]["summary"]
    save(store, ledger, [multiple(deduction=200, seq=1)])
    second = result(store)
    for key, row in keyed(first).items():
        assert (keyed(second)[key]["voucher_id"], keyed(second)[key]["number"]) == (
            row["voucher_id"],
            row["number"],
        )
    reordered = multiple(deduction=200, seq=2)
    reordered["plans"][0]["journals"].reverse()
    save(store, ledger, [reordered])
    assert result(store)["reference"] == second["reference"]
    save(store, ledger, [multiple(deduction=0, seq=3)])
    assert set(keyed(result(store))) == {("accrual", "original")}
    save(store, ledger, [multiple(seq=4)])
    final = result(store)
    assert (
        keyed(final)["accrual", "original"]["voucher_id"]
        == keyed(first)["accrual", "original"]["voucher_id"]
    )
    restored = keyed(final)["deductions", "original"]
    retired = keyed(first)["deductions", "original"]
    assert restored["voucher_id"] != retired["voucher_id"]
    assert restored["number"] > retired["number"]
    with store.read_unit(COMPANY) as read:
        assert ledger.get_result_refs([BUSINESS], read) == (final["reference"],)
        assert not {"voucher_id", "voucher_number"} & final["reference"].keys()
        (headers,) = ledger.get_effective_voucher_directory([final["reference"]], read)
        assert tuple(row.journal_key for row in headers) == ("accrual", "deductions")
        located = ledger.get_vouchers(
            tuple(
                VoucherLocator(row.voucher_id, row.result["result_id"], row.result["generation"])
                for row in headers
            ),
            read,
        )
        assert tuple(row["journal_key"] for row in located) == ("accrual", "deductions")
        assert ledger.verify_snapshot(read).voucher_count == 2


def test_frozen_group_inherits_whole_then_reverses_whole_even_when_one_key_disappears(store):
    ledger = create_ledger()
    save(store, ledger, [origin("0"), multiple()])
    january = result(store)
    close(store, ledger, "2026-01")
    formal = multiple(seq=1)
    formal["plans"][0]["obligations"][0]["allowed_settlements"] = ["money"]
    save(store, ledger, [formal], correction="2026-02")
    february = result(store)
    assert february["vouchers"] == []
    with store.read_unit(COMPANY) as read:
        (inherited,) = ledger.get_effective_voucher_directory([february["reference"]], read)
        assert {row.voucher_id for row in inherited} == {
            row["voucher_id"] for row in january["vouchers"]
        }
        assert all(
            row.result["result_id"] == january["reference"]["result_id"] for row in inherited
        )
    close(store, ledger, "2026-02")
    changed = multiple(deduction=0, seq=2)
    changed["plans"][0]["obligations"][0]["allowed_settlements"] = ["money"]
    save(store, ledger, [changed], correction="2026-03")
    march = result(store)
    assert set(keyed(march)) == {
        ("accrual", "reversal"),
        ("deductions", "reversal"),
        ("accrual", "replacement"),
    }
    for old in january["vouchers"]:
        reversal = keyed(march)[old["journal_key"], "reversal"]
        assert reversal["reverses_voucher_id"] == old["voucher_id"]
        assert reversal["summary"] == old["summary"]
        assert reversal["lines"] == [
            dict(row, debit_fen=row["credit_fen"], credit_fen=row["debit_fen"])
            for row in old["lines"]
        ]
    assert value(report(store, ledger, "2026-03", view="as_posted"), "profit_statement", 14) == "0"
    cumulative = report(store, ledger, "2026-01", "2026-03", view="as_posted")
    assert value(cumulative, "profit_statement", 14) == "1000"
    assert value(cumulative, "balance_sheet", 47, "ending") == "1000"
    close(store, ledger, "2026-03")
    with store.write_unit(COMPANY) as unit:
        baseline = ledger.get_result_source(BUSINESS, "main", unit)
        adopt_declared(ledger, [withdrawal(baseline)], unit, correction_period="2026-04")
    april = result(store)
    assert set(keyed(april)) == {("accrual", "reversal")}
    assert (
        april["vouchers"][0]["reverses_voucher_id"]
        == keyed(march)["accrual", "replacement"]["voucher_id"]
    )
    with store.read_unit(COMPANY) as read:
        assert ledger.get_effective_voucher_directory([april["reference"]], read) == ((),)
        assert ledger.verify_snapshot(read).voucher_count == 6


def test_multi_journal_financial_projection_uses_distinct_internal_ordinals_once(store):
    ledger = create_ledger()
    save(store, ledger, [origin("0"), multiple()])
    scope = {
        "company_id": COMPANY,
        "period_from": "2026-01",
        "period_to": "2026-01",
        "view": "current",
    }
    with store.read_unit(COMPANY) as read:
        selection = Selection(read.repository("ledger"), "current", "2026-01", "2026-01", Issues())
        bodies = capture_bodies(selection, tuple(selection.stock("2026-01").values()))
        (body,) = bodies.values()
        assert [row[0] for row in body["journal"]] == [1, 2, 3, 4]
        assert body["profit"] == {"14": 1000}
        projected = ledger.financial_snapshot(scope, read)
        classic = calculate_snapshot(scope, read, selection)
        for name in ("balance_sheet", "profit_statement", "cash_flow_statement", "pre_tax"):
            assert projected[name] == classic[name]
        assert value(projected, "profit_statement", 14) == "1000"
        assert value(projected, "balance_sheet", 47, "ending") == "1000"


def test_phase_turnover_above_int64_preserves_legal_cost_balances_and_resources(case):
    book = case
    book.seed(expense=False)
    wage = wage_case("B17Z")
    wage["data"]["gross_fen"] = str(MAX_FEN)
    wage["data"]["tax_basis"] = deepcopy(wage_case("B17B")["data"]["tax_basis"])
    wage["data"]["tax_basis"]["tax_fen"] = "1"
    add(book, wage, no_contributions())
    saved = book.get(wage["business_id"])["businesses"][0]["results"][0]
    assert {
        journal["journal_key"]: sum(int(row["debit_fen"]) for row in journal["lines"])
        for journal in saved["vouchers"]
    } == {"accrual": MAX_FEN, "deductions": 1}
    with book.kernel.storage.read_unit(book.company) as read:
        ledger = book.kernel.ledger
        for view in ("current", "as_posted"):
            scope = {
                "company_id": book.company,
                "period_from": "2026-01",
                "period_to": "2026-01",
                "view": view,
            }
            snapshot = ledger.financial_snapshot(scope, read)
            summary = ledger.financial_summary(scope, read)
            assert value(snapshot, "profit_statement", 14) == str(MAX_FEN)
            assert value(snapshot, "balance_sheet", 47, "ending") == str(MAX_FEN)
            assert summary.expense == {"state": "known", "amount_fen": str(MAX_FEN)}
            assert summary.net_profit == {"state": "known", "amount_fen": str(-MAX_FEN)}
        positions = ledger.get_obligation_positions(
            "2026-01", read, business_ids=[wage["business_id"]]
        )
        assert {
            item.definition.reference["slot"]: item.amounts.remaining_fen for item in positions
        } == {
            "net_pay": MAX_FEN - 1,
            "tax": 1,
            "employee_social": 0,
            "employee_housing": 0,
            "employer_social": 0,
            "employer_housing": 0,
        }
        assert ledger.verify_snapshot(read).voucher_count == 2


def test_failure_while_writing_second_voucher_rolls_back_the_complete_result(store, monkeypatch):
    ledger = create_ledger()
    reserve, called = LedgerRepository._reserve, []

    def fail_second(repo, period):
        called.append(period)
        if len(called) == 2:
            raise ContractError("conflict", "合成第二张凭证写入失败")
        return reserve(repo, period)

    with monkeypatch.context() as patch:
        patch.setattr(LedgerRepository, "_reserve", fail_second)
        with pytest.raises(ContractError, match="第二张凭证"):
            save(store, ledger, [multiple()])
    with store.read_unit(COMPANY) as read:
        assert ledger.get_results([BUSINESS], read) == ()
        assert ledger.verify_snapshot(read).voucher_count == 0
    save(store, ledger, [multiple()])
    assert [row["number"] for row in result(store)["vouchers"]] == [1, 2]


def test_frozen_key_change_is_a_formal_group_change_even_with_identical_account_totals(store):
    ledger = create_ledger()
    save(store, ledger, [origin("0"), multiple()])
    close(store, ledger, "2026-01")
    changed = multiple(seq=1)
    changed["plans"][0]["journals"][1]["journal_key"] = "withholding"
    save(store, ledger, [changed], correction="2026-02")
    actual = result(store)
    assert set(keyed(actual)) == {
        ("accrual", "reversal"),
        ("deductions", "reversal"),
        ("accrual", "replacement"),
        ("withholding", "replacement"),
    }
    with store.read_unit(COMPANY) as read:
        (effective,) = ledger.get_effective_voucher_directory([actual["reference"]], read)
        assert tuple(row.journal_key for row in effective) == ("accrual", "withholding")
        assert ledger.verify_snapshot(read).voucher_count == 6


def test_eleven_journals_and_twenty_two_correction_vouchers_have_no_arbitrary_capacity_limit(store):
    ledger = create_ledger()
    change = expense("1100")
    plan = change["plans"][0]
    template = plan["journals"][0]
    plan["journals"] = []
    for index in range(11):
        journal = deepcopy(template)
        journal.update(journal_key=f"part_{index:02d}", summary=f"合成独立凭证{index + 1}")
        journal["lines"][0]["debit_fen"] = journal["lines"][1]["credit_fen"] = "100"
        plan["journals"].append(journal)
    save(store, ledger, [origin("0"), change])
    before = result(store)
    assert len(before["vouchers"]) == 11
    close(store, ledger, "2026-01")
    corrected = deepcopy(change)
    corrected["businesses"][0].update(expected_seq=1, op="replace")
    corrected["businesses"][0]["fact"]["data"]["amount_fen"] = "1101"
    revised = corrected["plans"][0]
    revised["target"]["values"]["amount_fen"] = "1101"
    revised["basis"]["input"]["amount_fen"] = "1101"
    revised["obligations"][0]["amount_fen"] = "1101"
    rows = revised["journals"][0]["lines"]
    rows[0]["debit_fen"] = rows[1]["credit_fen"] = "101"
    save(store, ledger, [corrected], correction="2026-02")
    after = result(store)
    assert len(after["vouchers"]) == 22
    assert {
        row["reverses_voucher_id"] for row in after["vouchers"] if row["role"] == "reversal"
    } == {row["voucher_id"] for row in before["vouchers"]}
    assert {row["journal_key"] for row in after["vouchers"] if row["role"] == "replacement"} == {
        row["journal_key"] for row in before["vouchers"]
    }
    with store.read_unit(COMPANY) as read:
        assert ledger.get_results([BUSINESS], read)[0] == after
        (effective,) = ledger.get_effective_voucher_directory([after["reference"]], read)
        assert len(effective) == 11
        assert all(row.role == "replacement" for row in effective)
        assert ledger.verify_snapshot(read).voucher_count == 33


@pytest.mark.parametrize("defect", ["duplicate_key", "empty_journal", "only_group_balanced"])
def test_plan_requires_each_distinct_nonempty_journal_to_balance(store, defect):
    ledger = create_ledger()
    change = multiple()
    first, second = change["plans"][0]["journals"]
    if defect == "duplicate_key":
        second["journal_key"] = first["journal_key"]
    elif defect == "empty_journal":
        second["lines"] = []
    else:
        first["lines"][1]["credit_fen"] = "999"
        second["lines"][1]["credit_fen"] = "301"
    with pytest.raises(ContractError) as error:
        save(store, ledger, [change])
    assert error.value.code == (
        "unbalanced_voucher" if defect == "only_group_balanced" else "invalid_input"
    )
    with store.read_unit(COMPANY) as read:
        assert ledger.verify_snapshot(read).result_count == 0


@pytest.mark.parametrize(
    "damage",
    ["duplicate_key", "bad_key", "empty_header", "missing_reversal", "wrong_reverse_owner"],
)
def test_snapshot_rejects_damaged_multi_journal_semantics_without_recalculation(
    store, monkeypatch, damage
):
    ledger = create_ledger()
    save(store, ledger, [origin("0"), multiple()])
    close(store, ledger, "2026-01")
    save(store, ledger, [multiple(deduction=200, seq=1)], correction="2026-02")
    with store.read_unit(COMPANY) as read:
        repo = read.repository("ledger")
        records = deepcopy(repo.snapshot_records())
        originals = [row for row in records["voucher"] if row["role"] == "original"]
        reversals = [row for row in records["voucher"] if row["role"] == "reversal"]
        if damage == "duplicate_key":
            originals[1]["journal_key"] = originals[0]["journal_key"]
        elif damage == "bad_key":
            originals[0]["journal_key"] = "bad key"
        elif damage == "empty_header":
            originals[0]["summary"] = ""
        elif damage == "wrong_reverse_owner":
            reversals[1]["reverses_voucher_id"] = reversals[0]["reverses_voucher_id"]
        else:
            missing = reversals[1]["voucher_id"]
            records["voucher"] = [row for row in records["voucher"] if row["voucher_id"] != missing]
            records["voucher_line"] = [
                row for row in records["voucher_line"] if row["voucher_id"] != missing
            ]
        monkeypatch.setattr(repo, "snapshot_records", lambda: records)
        with pytest.raises(ContractError) as error:
            ledger.verify_snapshot(read)
        assert error.value.code == "storage_corrupt"
