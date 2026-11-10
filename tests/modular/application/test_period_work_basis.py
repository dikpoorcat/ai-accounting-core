"""Closing adopts explicit reality coverage without inventing approval or accounting facts."""

from copy import deepcopy
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError, loads_json
from ai_accounting_modular.ledger._closing import _basis

from ._working_case import case as case
from .conftest import source


def coverage(case, last="2026-01"):
    evidence = source(f"合成老板陈述：2026年1月至{last}的业务资料已全部提供。")
    result = case.app.accounting_apply(case.request(sources=[evidence]))
    assert result["completion"] == "complete", result
    return {
        "coverage": [
            {
                "period_from": "2026-01",
                "period_to": last,
                "sources": [{"source_id": evidence["source_id"]}],
            }
        ]
    }


def check(case, basis=None, period="2026-01", version=None):
    request = {"company_id": case.company, "action": "check", "period": period}
    if basis is not None:
        request["basis"] = basis
    if version is not None:
        request["expected_material_version"] = version
    return case.app.accounting_period(request)


def adopted(case, basis, period="2026-01"):
    candidate = check(case, basis, period)
    return check(case, basis, period, candidate["material_version"])


def close_request(case, checked, basis):
    return {
        "company_id": case.company,
        "action": "close",
        "actual_range": checked["actual_range"],
        "basis": basis,
        "expected_material_version": checked["material_version"],
        "expected_head": checked["head"],
    }


def pending(case, *, period="2026-01", before=None, action="external_filing", missing=()):
    return {
        "issue_id": str(uuid4()),
        "scope": "synthetic:work",
        "evidence_ids": [case.source_id],
        "missing_fields": list(missing),
        "question": "合成实际未完成事项。",
        "assignee": "owner",
        "action": action,
        "required_before_close": before,
        **({"period": period} if period else {}),
    }


def save_issues(case, issues):
    result = case.app.accounting_apply(
        case.request(
            unresolved=issues,
            unresolved_scope="synthetic:work",
            expected_scope_head=0,
        )
    )
    assert result["completion"] == "complete", result


def test_technical_readiness_does_not_claim_reality_coverage_or_adopt_unknown_material_version(
    case,
):
    case.seed(expense=False)
    uncovered = check(case, period="2026-03")
    assert uncovered["actual_range"] == {"period_from": "2026-01", "period_to": "2026-03"}
    assert uncovered["previous_closed_through"] is None
    assert uncovered["accounting_ready"] and not uncovered["ready"]
    assert {item["code"] for item in uncovered["work_issues"]} == {
        "materials_coverage_missing",
        "material_review_required",
    }
    no_claim = check(case, period="2026-03", version=uncovered["material_version"])
    assert not no_claim["ready"]
    basis = coverage(case, "2026-03")
    candidate = check(case, basis, "2026-03")
    assert candidate["accounting_ready"] and not candidate["ready"]
    checked = check(case, basis, "2026-03", candidate["material_version"])
    assert checked["ready"] and checked["head"] == candidate["head"]


def test_coverage_can_combine_sources_but_cannot_skip_a_month(case):
    case.seed(expense=False)
    basis = coverage(case, "2026-03")
    january = {**basis["coverage"][0], "period_to": "2026-01"}
    march = {**basis["coverage"][0], "period_from": "2026-03"}
    basis["coverage"] = [january, march]
    checked = adopted(case, basis, "2026-03")
    assert checked["accounting_ready"] and not checked["ready"]
    assert [item["code"] for item in checked["work_issues"]] == ["materials_coverage_missing"]
    march["period_from"] = "2026-02"
    assert adopted(case, basis, "2026-03")["ready"]


@pytest.mark.parametrize(
    "archive_month,changed", [("2026-01", True), (None, True), ("2026-11", False)]
)
def test_unbooked_source_changes_only_the_selected_material_collection(
    case, archive_month, changed
):
    case.seed(expense=False)
    basis = coverage(case)
    checked = adopted(case, basis)
    evidence = source("合成后来收到的独立原始资料，尚未登记业务。")
    if archive_month is None:
        evidence.pop("archive_month")
    else:
        evidence["archive_month"] = archive_month
    assert case.app.accounting_apply(case.request(sources=[evidence]))["completion"] == "complete"
    if changed:
        with pytest.raises(ContractError) as error:
            case.app.accounting_period(close_request(case, checked, basis))
        assert error.value.code == "snapshot_changed"
    else:
        assert adopted(case, basis)["head"] == checked["head"]
        assert case.app.accounting_period(close_request(case, checked, basis))["state"] == "closed"


def test_quarter_filing_is_selected_by_close_node_and_does_not_block_earlier_months(case):
    case.seed(expense=False)
    quarterly = pending(case, period="2026-10", before="2026-09")
    ordinary_debt = pending(case, period="2026-08", action="external_payment")
    unscoped = pending(case, period=None, action="professional_check")
    save_issues(case, [quarterly, ordinary_debt, unscoped])
    basis = coverage(case, "2026-09")
    august = adopted(case, basis, "2026-08")
    assert august["ready"]
    september = adopted(case, basis, "2026-09")
    assert september["accounting_ready"] and not september["ready"]
    assert {item["issue_id"] for item in september["unresolved"]} == {
        quarterly["issue_id"],
        ordinary_debt["issue_id"],
    }
    assert [item["code"] for item in september["work_issues"]] == ["external_completion_pending"]


def test_explicit_early_close_cannot_exempt_required_accounting_facts(case):
    case.seed(expense=False)
    external = pending(case, before="2026-01", missing=["/data/amount_fen"])
    save_issues(case, [external])
    basis = coverage(case)
    consent = source("合成老板明确选择：本次一月可以先关账，外部申报继续提醒。")
    assert case.app.accounting_apply(case.request(sources=[consent]))["completion"] == "complete"
    basis["early_close"] = {
        "pending_issue_ids": [external["issue_id"]],
        "sources": [{"source_id": consent["source_id"]}],
    }
    checked = adopted(case, basis)
    assert not checked["accounting_ready"] and not checked["ready"]
    assert not checked["work_issues"]
    assert [item["code"] for item in checked["blocking_issues"]] == ["unresolved_accounting_fact"]
    with pytest.raises(ContractError) as error:
        case.app.accounting_period(close_request(case, checked, basis))
    assert error.value.code == "needs_information"


def test_closing_scope_is_explicit_and_repeat_uses_saved_basis_after_current_sources_change(case):
    case.seed(expense=False)
    basis = coverage(case, "2026-03")
    checked = adopted(case, basis, "2026-03")
    request = close_request(case, checked, basis)
    narrow = deepcopy(request)
    narrow["actual_range"]["period_from"] = "2026-03"
    with pytest.raises(ContractError) as error:
        case.app.accounting_period(narrow)
    assert error.value.code == "conflict"
    closed = case.app.accounting_period(request)
    assert closed["ready"] and closed["head"] == checked["head"]
    evidence = source("合成关闭后才收到的一月资料。")
    assert case.app.accounting_apply(case.request(sources=[evidence]))["completion"] == "complete"
    repeated = case.app.accounting_period(request)
    assert repeated["head"] == closed["head"] and repeated["state"] == "closed"
    covering = check(case, period="2026-02")
    assert covering["actual_range"] == checked["actual_range"]
    assert covering["previous_closed_through"] is None
    with case.kernel.storage.read_unit(case.company) as read:
        row = read.repository("ledger").close_record("2026-03")
        saved = loads_json(row["basis_json"])["work_basis"]
        assert saved["public_head"] == checked["head"]
        assert saved["material_version"] == checked["material_version"]
        assert saved["source_ids"] == [basis["coverage"][0]["sources"][0]["source_id"]]
        assert row["result_head"] != saved["public_head"]
        assert read.repository("records").uses("period_close", "2026-03") == tuple(
            saved["source_ids"]
        )
        case.app.backup_verifier.verify_snapshot(read)


def test_early_close_preserves_external_work_without_requiring_same_permission_next_month(case):
    case.seed(expense=False)
    external = pending(case, before="2026-01")
    save_issues(case, [external])
    basis = coverage(case, "2026-02")
    assert not adopted(case, basis)["ready"]
    consent = source("合成老板明确选择：一月先关账，尚未完成的申报继续提醒。")
    assert case.app.accounting_apply(case.request(sources=[consent]))["completion"] == "complete"
    basis["early_close"] = {
        "pending_issue_ids": [external["issue_id"]],
        "sources": [{"source_id": consent["source_id"]}],
    }
    checked = adopted(case, basis)
    assert checked["ready"]
    assert case.app.accounting_period(close_request(case, checked, basis))["state"] == "closed"
    current = case.context(note_scopes=[], issue_scopes=["synthetic:work"])
    assert current["issues"] == [external]
    basis.pop("early_close")
    february = adopted(case, basis, "2026-02")
    assert february["ready"]
    assert february["actual_range"] == {"period_from": "2026-02", "period_to": "2026-02"}
    assert february["previous_closed_through"] == "2026-01"


def test_work_note_version_is_adopted_and_its_sources_remain_protected_after_note_changes(case):
    case.seed(expense=False)
    basis = coverage(case)
    evidence = source("合成老板本月安排：资料覆盖范围与现有长期安排一致。")
    note = {
        "scope": "work:2026-01",
        "expected_seq": 0,
        "text": "采用已核实的本月安排。",
        "evidence_ids": [evidence["source_id"]],
    }
    assert (
        case.app.accounting_apply(case.request(sources=[evidence], notes=[note]))["completion"]
        == "complete"
    )
    basis["note_refs"] = [{"scope": note["scope"], "seq": 1}]
    checked = adopted(case, basis)
    note.update(expected_seq=1, text="本月安排更正为当前有效内容。")
    assert case.app.accounting_apply(case.request(notes=[note]))["completion"] == "complete"
    with pytest.raises(ContractError) as error:
        case.app.accounting_period(close_request(case, checked, basis))
    assert error.value.code == "snapshot_changed"
    basis["note_refs"][0]["seq"] = 2
    checked = adopted(case, basis)
    assert case.app.accounting_period(close_request(case, checked, basis))["state"] == "closed"
    note.update(expected_seq=2, text="后续接续安排。", evidence_ids=[])
    assert case.app.accounting_apply(case.request(notes=[note]))["completion"] == "complete"
    with case.kernel.storage.read_unit(case.company) as read:
        retained = case.app.records.source_discard_protection(
            [evidence["source_id"]],
            read,
            retained_source_ids=(),
            protected_paths=(),
        )
        assert retained == (evidence["source_id"],)
        row = read.repository("ledger").close_record("2026-01")
        assert evidence["source_id"] in loads_json(row["basis_json"])["work_basis"]["source_ids"]
        case.app.backup_verifier.verify_snapshot(read)


def test_unknown_or_unrelated_early_close_target_is_not_a_blanket_exception(case):
    case.seed(expense=False)
    external = pending(case, before="2026-01")
    save_issues(case, [external])
    basis = coverage(case)
    basis["early_close"] = {
        "pending_issue_ids": [str(uuid4())],
        "sources": basis["coverage"][0]["sources"],
    }
    checked = adopted(case, basis)
    assert checked["accounting_ready"] and not checked["ready"]
    assert {item["code"] for item in checked["work_issues"]} == {
        "early_close_scope_changed",
        "external_completion_pending",
    }
    with pytest.raises(ContractError) as error:
        case.app.accounting_period(close_request(case, checked, basis))
    assert error.value.code == "dependency_conflict"


@pytest.mark.parametrize("reference", ["note", "source"])
def test_unknown_reference_is_submission_error_not_owner_missing_material(case, reference):
    case.seed(expense=False)
    basis = coverage(case)
    if reference == "note":
        basis["note_refs"] = [{"scope": "work:2026-01", "seq": 1}]
    else:
        basis["additional_sources"] = [{"source_id": str(uuid4())}]
    with pytest.raises(ContractError) as error:
        check(case, basis)
    assert error.value.code == "invalid_input"


@pytest.mark.parametrize("changed", ["range_from", "range_to", "basis", "version", "head"])
def test_closed_retry_cannot_claim_another_scope_basis_or_version(case, changed):
    case.seed(expense=False)
    basis = coverage(case, "2026-03")
    checked = adopted(case, basis, "2026-03")
    request = close_request(case, checked, basis)
    assert case.app.accounting_period(request)["state"] == "closed"
    altered = deepcopy(request)
    if changed == "range_from":
        altered["actual_range"]["period_from"] = "2026-02"
    elif changed == "range_to":
        altered["actual_range"]["period_to"] = "2026-02"
    elif changed == "basis":
        altered["basis"]["coverage"][0]["sources"] = [{"source_id": str(uuid4())}]
    elif changed == "version":
        altered["expected_material_version"] = "0" * 64
    else:
        altered["expected_head"] = "0" * 64
    with pytest.raises(ContractError) as error:
        case.app.accounting_period(altered)
    assert error.value.code == "conflict"
    assert case.app.accounting_period(request)["head"] == checked["head"]


def test_closed_check_does_not_silently_adopt_a_new_basis(case):
    case.seed(expense=False)
    basis = coverage(case)
    checked = adopted(case, basis)
    assert case.app.accounting_period(close_request(case, checked, basis))["state"] == "closed"
    assert check(case)["state"] == "closed"
    assert check(case, basis, version=checked["material_version"])["state"] == "closed"
    basis["additional_sources"] = [{"source_id": str(uuid4())}]
    with pytest.raises(ContractError) as error:
        check(case, basis)
    assert error.value.code == "conflict"


def test_legacy_closed_period_is_readable_without_accepting_a_new_closing_claim(case):
    case.seed(expense=False)
    # The old exact representation did not record a work basis. Recreate it at
    # fixture insertion; never rewrite an existing frozen result or its hash.
    with case.kernel.storage.write_unit(case.company) as unit:
        checked = case.app.ledger.check_period("2026-01", unit)
        repo = unit.repository("ledger")
        frozen, _ = _basis(repo, checked.period_from, checked.period, unit)
        repo.freeze_range(checked.period_from, checked.period)
        repo.save_close(
            checked.period, checked.head, frozen, checked.report, "2026-02-01T00:00:00+00:00"
        )
    saved = check(case)
    assert saved["state"] == "closed" and saved["material_version"] is None
    basis = {
        "coverage": [
            {
                "period_from": "2026-01",
                "period_to": "2026-01",
                "sources": [{"source_id": case.source_id}],
            }
        ]
    }
    with pytest.raises(ContractError) as error:
        check(case, basis)
    assert error.value.code == "invalid_operation"
    request = close_request(case, saved, basis)
    request["expected_material_version"] = "0" * 64
    with pytest.raises(ContractError) as error:
        case.app.accounting_period(request)
    assert error.value.code == "invalid_operation"
    assert check(case)["head"] == saved["head"]


def test_source_protection_failure_rolls_back_the_new_close(case, monkeypatch):
    case.seed(expense=False)
    basis = coverage(case)
    checked = adopted(case, basis)
    original = case.app.records.replace_source_uses

    def failed(owner_kind, *args, **kwargs):
        if owner_kind == "period_close":
            raise ContractError("temporary_failure", "合成关闭来源引用写入失败。")
        return original(owner_kind, *args, **kwargs)

    monkeypatch.setattr(case.app.records, "replace_source_uses", failed)
    with pytest.raises(ContractError) as error:
        case.app.accounting_period(close_request(case, checked, basis))
    assert error.value.code == "temporary_failure"
    with case.kernel.storage.read_unit(case.company) as read:
        assert case.app.ledger.closed_through(read) is None
        assert all(item["state"] == "open" for item in read.repository("ledger").current_heads())
