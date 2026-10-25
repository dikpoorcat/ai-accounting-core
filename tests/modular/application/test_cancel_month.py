"""Whole-month cancellation owns one atomic exit and resumable original cleanup."""

from pathlib import Path
from uuid import uuid4

import pytest

from ai_accounting_modular.application import _documents
from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.host.public import create_kernel
from ai_accounting_modular.records.public import open_standard_bundle

from ._working_case import case as case
from .conftest import source as evidence
from .test_withdrawal_restart import _seed_payroll


def _root(case):
    with case.kernel.storage.read_unit(case.company) as read:
        return case.kernel.storage.company_metadata(read).company_root


def _original(case, name, period="2026-01"):
    root = _root(case)
    incoming = root / "originals/pending" / (name + ".txt")
    incoming.parent.mkdir(parents=True, exist_ok=True)
    incoming.write_text("合成原件：" + name, encoding="utf-8")
    result = case.app.accounting_sources(
        {
            "company_id": case.company,
            "action": "import",
            "sources": [
                {
                    "kind": "file",
                    "relative_path": incoming.relative_to(root).as_posix(),
                    "archive_month": period,
                }
            ],
        }
    )
    return result["sources"][0]


def _inspect(case, period="2026-01"):
    return case.app.accounting_cancel_month(
        {"company_id": case.company, "period": period, "action": "inspect"}
    )


def _request(case, checked):
    return {
        "company_id": case.company,
        "period": checked["period"],
        "action": "cancel",
        "command_id": str(uuid4()),
        "expected_head": checked["head"],
    }


def _sources(case, *ids):
    return case.app.accounting_sources(
        {"company_id": case.company, "action": "get", "source_ids": list(ids)}
    )


def _forbid_protection_reads(case, patch):
    def unexpected(*args, **kwargs):
        raise AssertionError("a rejected old request must not expand source or task protection")

    patch.setattr(case.kernel.records, "source_discard_protection", unexpected)
    patch.setattr(case.kernel.ledger, "referenced_source_ids", unexpected)
    patch.setattr(case.kernel.files, "retained_input_plans", unexpected)


def _issue(scope, source, period):
    return {
        "issue_id": str(uuid4()),
        "scope": scope,
        "period": period,
        "evidence_ids": [source],
        "missing_fields": [],
        "question": "合成待核对事项：" + period,
        "assignee": "ai",
        "action": "professional_check",
    }


def test_cancel_month_exits_all_families_and_resets_only_that_months_work(case):
    original, _, _ = _seed_payroll(case)
    january, february = _original(case, "一月费用"), _original(case, "二月费用", "2026-02")
    expense, later = case.expense(), case.expense("20000", "2026-02")
    expense["evidence_ids"] = [january["source_id"]]
    later["evidence_ids"] = [february["source_id"]]
    assert case.app.accounting_apply(case.request(expense, later))["completion"] == "complete"
    company_note = {
        "scope": "company",
        "expected_seq": 0,
        "text": "合成公司的长期真实经营说明应保留。",
        "evidence_ids": [case.source_id],
    }
    month_note = {
        "scope": "period:2026-01",
        "expected_seq": 0,
        "text": "旧的一月完成结论，不是重做本月的事实依据。",
        "evidence_ids": [january["source_id"]],
        "expected_basis_head": case.note_head(),
    }
    scope = "custom:monthly-work"
    first = _issue(scope, january["source_id"], "2026-01")
    second = _issue(scope, february["source_id"], "2026-02")
    saved = case.app.accounting_apply(
        case.request(
            notes=[company_note, month_note],
            unresolved=[first, second],
            unresolved_scope=scope,
            expected_scope_head=0,
        )
    )
    assert saved["completion"] == "complete", saved
    before = case.context(note_scopes=["company"], issue_scopes=[])
    checked = _inspect(case)
    assert checked["state"] == "inspected"
    assert checked["discarded_source_ids"] == []
    assert checked["file_cleanup"]["state"] == "not_started"
    expected = {fact["business_id"] for fact in original} | {expense["business_id"]}
    assert expected <= set(checked["business_ids"])
    assert later["business_id"] not in checked["business_ids"]
    result = case.app.accounting_cancel_month(_request(case, checked))
    assert result["state"] == "cancelled" and result["documents"] == "ready"
    assert january["source_id"] in result["discarded_source_ids"]
    assert case.source_id in result["retained_source_ids"]
    assert not (_root(case) / january["relative_path"]).exists()
    assert (_root(case) / february["relative_path"]).is_file()
    for business in case.get(*checked["business_ids"])["businesses"]:
        assert business["state"]["state"] == "withdrawn"
        assert "fact" not in business and business["results"] == []
    assert case.get(later["business_id"])["businesses"][0]["fact"] == later
    after = case.context(note_scopes=["company", "period:2026-01"], issue_scopes=[scope])
    assert after["database_id"] == before["database_id"]
    assert after["company_id"] == before["company_id"]
    assert next(note for note in after["notes"] if note["scope"] == "company") == before["notes"][0]
    restarted = next(note for note in after["notes"] if note["scope"] == "period:2026-01")
    assert restarted["state"] == "current" and restarted["evidence_ids"] == []
    assert "重新" in restarted["text"] and "第1" in restarted["text"]
    assert month_note["text"] not in restarted["text"]
    assert after["issues"] == [second]
    bundle = open_standard_bundle(_root(case) / "standard/current.json")
    assert {fact["business_id"] for fact in bundle.facts} == {later["business_id"]}
    assert january["source_id"] not in {source["source_id"] for source in bundle.sources}


def test_cancel_month_uses_one_atomic_unit_beyond_the_ai_wire_business_limit(case):
    case.seed(expense=False)
    source = evidence("合成原件：一月501项独立维修费用，每项1元，均未支付。")
    facts = [case.expense("100") for _ in range(501)]
    for fact in facts:
        fact["evidence_ids"] = [source["source_id"]]
    for offset in range(0, len(facts), 500):
        request = case.request(*facts[offset : offset + 500])
        if offset == 0:
            request["sources"] = [source]
        saved = case.app.accounting_apply(request)
        assert saved["completion"] == "complete", saved
    checked = _inspect(case)
    assert len(checked["business_ids"]) == 502
    with case.kernel.storage.read_unit(case.company) as read:
        head = read.head
    result = case.app.accounting_cancel_month(_request(case, checked))
    assert result["state"] == "cancelled" and result["documents"] == "ready"
    with case.kernel.storage.read_unit(case.company) as read:
        assert read.head == head + 1
    for offset in range(0, len(facts), 500):
        observed = case.get(*(fact["business_id"] for fact in facts[offset : offset + 500]))
        assert all(business["state"]["state"] == "withdrawn" for business in observed["businesses"])


def test_cancel_month_without_books_removes_only_that_months_unused_originals(case):
    january, february = _original(case, "仅原件"), _original(case, "后月原件", "2026-02")
    checked = _inspect(case)
    assert checked["business_ids"] == []
    assert checked["source_ids"] == [january["source_id"]]
    result = case.app.accounting_cancel_month(_request(case, checked))
    assert result["discarded_source_ids"] == [january["source_id"]]
    assert result["file_cleanup"] == {"state": "complete", "issues": []}
    assert not (_root(case) / january["relative_path"]).exists()
    assert (_root(case) / february["relative_path"]).is_file()
    assert _sources(case, january["source_id"])["missing_source_ids"] == [january["source_id"]]


def test_source_only_month_replaces_its_note_once_and_releases_originals(case):
    source = _original(case, "旧月度说明引用的唯一原件")
    scope = "period:2026-01"
    note = {
        "scope": scope,
        "expected_seq": 0,
        "expected_basis_head": case.note_head(),
        "text": "旧资料已经收到，尚未开始正式记账。",
        "evidence_ids": [source["source_id"]],
    }
    assert case.app.accounting_apply(case.request(notes=[note]))["completion"] == "complete"
    before = case.context(note_scopes=[scope], issue_scopes=[])["notes"][0]
    checked = _inspect(case)
    assert checked["business_ids"] == []
    assert checked["source_ids"] == [source["source_id"]]

    result = case.app.accounting_cancel_month(_request(case, checked))

    assert result["state"] == "cancelled" and result["documents"] == "ready"
    assert result["discarded_source_ids"] == [source["source_id"]]
    assert result["retained_source_ids"] == []
    assert result["file_cleanup"] == {"state": "complete", "issues": []}
    current = case.context(note_scopes=[scope], issue_scopes=[])["notes"][0]
    assert current["seq"] == before["seq"] + 1
    assert current["state"] == "current" and current["evidence_ids"] == []
    assert "重新" in current["text"] and "第1" in current["text"]
    assert note["text"] not in current["text"]
    assert _sources(case, source["source_id"])["missing_source_ids"] == [source["source_id"]]
    assert not (_root(case) / source["relative_path"]).exists()
    bundle = open_standard_bundle(_root(case) / "standard/current.json")
    assert bundle.facts == () and bundle.sources == ()
    assert bundle.notes == ({"scope": scope, "text": current["text"], "evidence_ids": []},)


@pytest.mark.parametrize("count", [1, 2])
def test_selected_single_or_batch_withdrawal_still_keeps_originals(case, count):
    case.seed(expense=False)
    source = _original(case, "单笔或批量撤回保留原件")
    facts = [case.expense("100") for _ in range(count)]
    for fact in facts:
        fact["evidence_ids"] = [source["source_id"]]
    assert case.app.accounting_apply(case.request(*facts))["completion"] == "complete"
    request = case.request()
    request["mutations"] = [
        {"op": "withdraw", "expected_seq": 1, "business_id": fact["business_id"]} for fact in facts
    ]
    assert case.app.accounting_apply(request)["completion"] == "complete"
    assert (_root(case) / source["relative_path"]).is_file()
    assert _sources(case, source["source_id"])["missing_source_ids"] == []


def test_month_cancellation_keeps_a_source_referenced_by_a_later_active_business(case):
    case.seed(expense=False)
    original = _original(case, "跨月公用原件")
    january, february = case.expense(), case.expense("20000", "2026-02")
    for fact in (january, february):
        fact["evidence_ids"] = [original["source_id"]]
    assert case.app.accounting_apply(case.request(january, february))["completion"] == "complete"
    result = case.app.accounting_cancel_month(_request(case, _inspect(case)))
    assert original["source_id"] in result["retained_source_ids"]
    assert original["source_id"] not in result["discarded_source_ids"]
    assert (_root(case) / original["relative_path"]).is_file()
    assert case.get(january["business_id"])["businesses"][0]["state"]["state"] == "withdrawn"
    assert case.get(february["business_id"])["businesses"][0]["fact"] == february


def test_month_cancellation_reports_originals_still_needed_by_a_pending_backup(case):
    case.seed()
    source = _original(case, "待完成备份仍需原件")
    with case.kernel.storage.read_unit(case.company) as read:
        database = read.database_id
    task = case.app.accounting_backup(
        {"company_id": case.company, "expected_database_id": database}
    )
    assert task["state"] == "pending"
    result = case.app.accounting_cancel_month(_request(case, _inspect(case)))
    assert result["state"] == "cancelled"
    assert source["source_id"] in result["retained_source_ids"]
    assert (_root(case) / source["relative_path"]).is_file()
    assert _sources(case, source["source_id"])["missing_source_ids"] == []


def test_open_month_retains_an_original_used_only_by_a_frozen_result(case):
    expense = case.seed()
    source = _original(case, "冻结历史使用的原件", "2026-02")
    expense["evidence_ids"] = [source["source_id"]]
    replace = case.request(expense)
    replace["mutations"][0].update(op="replace", expected_seq=1)
    assert case.app.accounting_apply(replace)["completion"] == "complete"
    checked = case.check()
    assert checked["ready"] and case.close(checked)["state"] == "closed"
    frozen = case.get(expense["business_id"])["businesses"][0]["results"][0]
    corrected = evidence("合成三月核实：一月维修费应为900元。")
    corrected["archive_month"] = "2026-03"
    expense["data"]["amount_fen"] = "90000"
    expense["evidence_ids"] = [corrected["source_id"]]
    replacement = case.request(expense, sources=[corrected], correction_period="2026-03")
    replacement["mutations"][0].update(op="replace", expected_seq=2)
    assert case.app.accounting_apply(replacement)["completion"] == "complete"
    result = case.app.accounting_cancel_month(_request(case, _inspect(case, "2026-02")))
    assert source["source_id"] in result["retained_source_ids"]
    assert (_root(case) / source["relative_path"]).is_file()
    exact = case.get(
        expense["business_id"],
        result_id=frozen["reference"]["result_id"],
        generation=frozen["reference"]["generation"],
    )["businesses"][0]["results"][0]
    assert exact == frozen


def test_range_outside_month_dependency_rejects_books_work_and_source_cleanup_together(case):
    original, materials, _ = _seed_payroll(case)
    source = _original(case, "撤销失败需完整保留")
    later = case.expense("20000", "2026-02")
    later["evidence_ids"] = [source["source_id"]]
    assert case.app.accounting_apply(case.request(later))["completion"] == "complete"
    # The following-month bank starting point adopts January's actual reconciliation.
    statement = {
        "business_id": str(uuid4()),
        "kind": "money.bank_statement",
        "period": "2026-02",
        "evidence_ids": [source["source_id"]],
        "data": {
            "funds_account_id": materials[3],
            "opening_fen": "100000",
            "closing_fen": "100000",
            "rows": [],
        },
    }
    reconciliation = {
        "business_id": str(uuid4()),
        "kind": "money.reconciliation",
        "period": "2026-02",
        "evidence_ids": [source["source_id"]],
        "data": {"statement_id": statement["business_id"], "matches": []},
    }
    saved = case.app.accounting_apply(case.request(statement, reconciliation))
    assert saved["completion"] == "complete", saved
    ids = [fact["business_id"] for fact in (*original, later, statement, reconciliation)]
    before = case.get(*ids)
    checked = _inspect(case)
    with pytest.raises(ContractError) as error:
        case.app.accounting_cancel_month(_request(case, checked))
    assert error.value.code in {"dependency_conflict", "needs_information"}
    assert case.get(*ids) == before
    assert (_root(case) / source["relative_path"]).is_file()
    assert _sources(case, source["source_id"])["missing_source_ids"] == []


def test_late_transaction_failure_rolls_back_withdrawals_and_original_retirement(case, monkeypatch):
    expense = case.seed()
    source = _original(case, "事务回滚原件")
    checked = _inspect(case)
    before = case.get(expense["business_id"])
    original = case.kernel.records.discard_sources

    def fail_after_discard(*args, **kwargs):
        original(*args, **kwargs)
        raise ContractError("temporary_failure", "合成故障：来源退出后同一事务失败。")

    with monkeypatch.context() as patch:
        patch.setattr(case.kernel.records, "discard_sources", fail_after_discard)
        with pytest.raises(ContractError) as error:
            case.app.accounting_cancel_month(_request(case, checked))
    assert error.value.code == "temporary_failure"
    assert case.get(expense["business_id"]) == before
    assert _sources(case, source["source_id"])["missing_source_ids"] == []
    assert (_root(case) / source["relative_path"]).is_file()


def test_closed_month_rejects_original_cleanup_and_keeps_frozen_results(case):
    expense = case.seed()
    checked = case.check()
    assert checked["ready"] and case.close(checked)["state"] == "closed"
    source = _original(case, "闭期原件")
    before = case.get(expense["business_id"])
    with pytest.raises(ContractError) as error:
        case.app.accounting_cancel_month(
            {
                "company_id": case.company,
                "period": "2026-01",
                "action": "cancel",
                "command_id": str(uuid4()),
                "expected_head": 0,
            }
        )
    assert error.value.code == "closed_period"
    assert case.get(expense["business_id"]) == before
    assert (_root(case) / source["relative_path"]).is_file()


def test_postcommit_file_failure_and_restart_resume_only_the_original_cleanup(case, monkeypatch):
    source = _original(case, "暂时锁定原件")
    path = _root(case) / source["relative_path"]
    request = _request(case, _inspect(case))
    unlink = Path.unlink

    def unavailable(self, *args, **kwargs):
        if self == path:
            raise PermissionError("synthetic original is locked")
        return unlink(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", unavailable)
        result = case.app.accounting_cancel_month(request)
    assert result["state"] == "cancelled" and result["file_cleanup"]["state"] == "pending"
    assert result["file_cleanup"]["issues"]
    assert _sources(case, source["source_id"])["missing_source_ids"] == [source["source_id"]]
    assert path.is_file()
    catalog = case.kernel.storage.catalog_path
    case.kernel.close()
    case.kernel = create_kernel(catalog)
    resumed = case.app.accounting_cancel_month(request)
    assert resumed["state"] == "cancelled"
    assert resumed["file_cleanup"] == {"state": "complete", "issues": []}
    assert not path.exists()


@pytest.mark.parametrize("phase", ["marker_selection", "documents"])
@pytest.mark.parametrize("failure", ["contract", "io"])
def test_postcommit_failure_reports_saved_cancellation_and_existing_interfaces_resume(
    case, monkeypatch, phase, failure
):
    expense = case.seed()
    source = _original(case, "提交后接续失败的原件")
    path = _root(case) / source["relative_path"]
    request = _request(case, _inspect(case))

    def unavailable(*args, **kwargs):
        if failure == "contract":
            raise ContractError("temporary_failure", "合成故障：提交后接续暂时失败。")
        raise OSError("synthetic postcommit IO unavailable")

    with monkeypatch.context() as patch:
        if phase == "marker_selection":
            patch.setattr(case.kernel.records, "pending_source_discard_markers", unavailable)
        else:
            patch.setattr(_documents, "finish", unavailable)
        result = case.app.accounting_cancel_month(request)
    assert result["state"] == "cancelled"
    assert source["source_id"] in result["discarded_source_ids"]
    assert _sources(case, source["source_id"])["missing_source_ids"] == [source["source_id"]]
    current = case.get(expense["business_id"])["businesses"][0]
    assert current["state"]["state"] == "withdrawn"
    assert "fact" not in current and current["results"] == []
    if phase == "marker_selection":
        assert result["file_cleanup"]["state"] == "pending" and result["file_cleanup"]["issues"]
        assert path.is_file()
        resumed = case.app.finish_source_discards(case.company)
        assert resumed.pending_marker_ids == () and resumed.errors == ()
    else:
        assert result["documents"] == "pending"
        resumed = case.app.accounting_documents({"company_id": case.company, "action": "finish"})
        assert resumed["state"] == "ready"
    assert not path.exists()
    assert case.get(expense["business_id"])["businesses"][0] == current


def test_background_cleanup_preserves_same_original_reimported_for_new_work(case, monkeypatch):
    case.seed()
    source = _original(case, "重新提供的一月维修费900元原件")
    path = _root(case) / source["relative_path"]
    original_bytes = path.read_bytes()
    request = _request(case, _inspect(case))
    unlink = Path.unlink

    def unavailable(self, *args, **kwargs):
        if self == path:
            raise PermissionError("synthetic original is locked")
        return unlink(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", unavailable)
        cancelled = case.app.accounting_cancel_month(request)
    assert cancelled["state"] == "cancelled"
    assert cancelled["file_cleanup"]["state"] == "pending"
    assert _sources(case, source["source_id"])["missing_source_ids"] == [source["source_id"]]
    imported = case.app.accounting_sources(
        {
            "company_id": case.company,
            "action": "import",
            "sources": [
                {
                    "kind": "file",
                    "relative_path": source["relative_path"],
                    "archive_month": "2026-01",
                }
            ],
        }
    )["sources"][0]
    assert imported["source_id"] == source["source_id"]
    assert imported["relative_path"] == source["relative_path"]
    with pytest.raises(ContractError) as error:
        case.app.accounting_cancel_month(request)
    assert error.value.code == "snapshot_changed"

    # Background uses this pure marker continuation, not a fresh source retirement.
    resumed = case.app.finish_source_discards(case.company)
    assert resumed.completed_marker_ids and resumed.pending_marker_ids == ()
    assert resumed.errors == ()
    assert _sources(case, source["source_id"])["sources"] == [imported]
    assert path.read_bytes() == original_bytes

    new = case.expense("90000")
    new["evidence_ids"] = [source["source_id"]]
    assert case.app.accounting_apply(case.request(new))["completion"] == "complete"
    before = case.get(new["business_id"])
    idle = case.app.finish_source_discards(case.company)
    assert idle.completed_marker_ids == () and idle.pending_marker_ids == ()
    assert case.get(new["business_id"]) == before
    assert _sources(case, source["source_id"])["sources"] == [imported]
    assert path.read_bytes() == original_bytes


@pytest.mark.parametrize("pending_cleanup", [False, True])
def test_stale_cancel_request_cannot_remove_newly_rebooked_business(
    case, monkeypatch, pending_cleanup
):
    case.seed()
    original = _original(case, "旧请求只可接续清理的原件")
    path = _root(case) / original["relative_path"]
    request = _request(case, _inspect(case))
    unlink = Path.unlink

    def unavailable(self, *args, **kwargs):
        if self == path:
            raise PermissionError("synthetic original is locked")
        return unlink(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        if pending_cleanup:
            patch.setattr(Path, "unlink", unavailable)
        first = case.app.accounting_cancel_month(request)
    assert first["state"] == "cancelled"
    assert first["file_cleanup"]["state"] == ("pending" if pending_cleanup else "complete")
    source = evidence("合成重做的新原件：一月正确维修费900元，应付供应商。")
    new = case.expense("90000")
    new["evidence_ids"] = [source["source_id"]]
    assert (
        case.app.accounting_apply(case.request(new, sources=[source]))["completion"] == "complete"
    )
    before = case.get(new["business_id"])
    with monkeypatch.context() as patch:
        _forbid_protection_reads(case, patch)
        with pytest.raises(ContractError) as error:
            case.app.accounting_cancel_month(request)
    assert error.value.code == "snapshot_changed"
    assert case.get(new["business_id"]) == before
    assert _sources(case, source["source_id"])["missing_source_ids"] == []
    if pending_cleanup:
        case.app.finish_source_discards(case.company)
        assert not path.exists()
        assert case.get(new["business_id"]) == before


def test_stale_inspection_cannot_cancel_new_intake_or_new_current_business(case):
    case.seed()
    checked = _inspect(case)
    new = _original(case, "检查后新收到的原件")
    with pytest.raises(ContractError) as error:
        case.app.accounting_cancel_month(_request(case, checked))
    assert error.value.code == "snapshot_changed"
    assert (_root(case) / new["relative_path"]).is_file()


def test_old_inspect_without_a_committed_cancel_cannot_claim_success_after_an_unrelated_change(
    case, monkeypatch
):
    case.seed()
    source = _original(case, "二月尚未撤销的待核对原件", "2026-02")
    scope = "period:2026-02"
    issue = _issue(scope, source["source_id"], "2026-02")
    saved = case.app.accounting_apply(
        case.request(unresolved=[issue], unresolved_scope=scope, expected_scope_head=0)
    )
    assert saved["completion"] == "complete"
    checked = _inspect(case, "2026-02")
    assert checked["business_ids"] == []
    unrelated = case.expense("100", "2026-03")
    assert case.app.accounting_apply(case.request(unrelated))["completion"] == "complete"
    before = case.context(note_scopes=[scope], issue_scopes=[scope])
    with monkeypatch.context() as patch:
        _forbid_protection_reads(case, patch)
        with pytest.raises(ContractError) as error:
            case.app.accounting_cancel_month(_request(case, checked))
    assert error.value.code == "snapshot_changed"
    assert case.context(note_scopes=[scope], issue_scopes=[scope]) == before
    assert (_root(case) / source["relative_path"]).is_file()


def test_old_cancel_cannot_claim_a_new_month_explanation_as_its_own_success(case, monkeypatch):
    case.seed()
    request = _request(case, _inspect(case))
    assert case.app.accounting_cancel_month(request)["state"] == "cancelled"
    scope = "period:2026-01"
    current = case.context(note_scopes=[scope], issue_scopes=[])["notes"][0]
    source = _original(case, "老板重新提供的本月说明原件")
    path = _root(case) / source["relative_path"]
    original_bytes = path.read_bytes()
    new_note = {
        "scope": scope,
        "expected_seq": current["seq"],
        "expected_basis_head": case.note_head(),
        "text": "老板已经开始重新核查本月业务，尚未保存正式账务。",
        "evidence_ids": [source["source_id"]],
    }
    assert case.app.accounting_apply(case.request(notes=[new_note]))["completion"] == "complete"
    before = case.context(note_scopes=[scope], issue_scopes=[])
    with monkeypatch.context() as patch:
        _forbid_protection_reads(case, patch)
        with pytest.raises(ContractError) as error:
            case.app.accounting_cancel_month(request)
    assert error.value.code == "snapshot_changed"
    assert case.context(note_scopes=[scope], issue_scopes=[]) == before
    assert _sources(case, source["source_id"])["sources"] == [source]
    assert path.read_bytes() == original_bytes
