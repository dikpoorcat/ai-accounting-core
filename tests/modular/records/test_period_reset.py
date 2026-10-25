"""Month cancellation clears selected issues while preserving notes, uses and versions."""

from uuid import uuid4

import pytest
from test_documents import _prepare, _publish

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.records import _sources
from ai_accounting_modular.records.public import RecordsChange, RecordsContext


def store(case, text, period=None, *, file=False):
    storage, (company, _), records = case
    value = {"kind": "owner_statement", "text": text}
    if file:
        relative = "originals/unassigned/" + text + ".txt"
        path = company.company_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        value = {"kind": "file", "relative_path": relative}
    if period is not None:
        value["archive_month"] = period
    with storage.read_unit(company.company_id) as read:
        source = records.prepare_source(value, read)
    with storage.write_unit(company.company_id) as unit:
        records.apply_current(
            records.plan_change(
                RecordsChange(sources=(source,)), RecordsContext(unit, str(uuid4()))
            ),
            unit,
        )
    return source


def issue(scope, source_id, period=None):
    value = {
        "issue_id": str(uuid4()),
        "scope": scope,
        "evidence_ids": [source_id],
        "missing_fields": [],
        "question": "由AI核对对应资料。",
        "assignee": "ai",
        "action": "professional_check",
    }
    if period is not None:
        value["period"] = period
    return value


def save_work(case, *, notes=(), issues=(), scope=None):
    storage, (company, _), records = case
    with storage.write_unit(company.company_id) as unit:
        change = RecordsChange(
            notes=tuple(notes),
            unresolved=tuple(issues) if scope is not None else None,
            unresolved_scope=scope,
            expected_scope_head=0 if scope is not None else None,
        )
        records.apply_current(
            records.plan_change(
                change,
                RecordsContext(
                    unit,
                    str(uuid4()),
                    {
                        note["scope"]: note["expected_basis_head"]
                        for note in notes
                        if note["scope"] != "company"
                    },
                ),
            ),
            unit,
        )


def test_period_sources_is_complete_and_does_not_observe_files_or_other_month_bodies(
    records_case, monkeypatch
):
    storage, (company, _), records = records_case
    current = store(records_case, "当月原件", "2026-01", file=True)
    statement = store(records_case, "当月现实说明", "2026-01")
    store(records_case, "其他月份", "2026-02")
    store(records_case, "长期未归月")
    (company.company_root / current["relative_path"]).unlink()

    def no_files(*args, **kwargs):
        raise AssertionError("月份来源查询不应检查原件或其他月份正文。")

    monkeypatch.setattr(_sources, "source_status", no_files)
    monkeypatch.setattr(_sources, "check_source", no_files)
    with storage.read_unit(company.company_id) as read:
        repository = read.repository("records")
        monkeypatch.setattr(repository, "all_sources", no_files)
        monkeypatch.setattr(repository, "sources", no_files)
        result = records.period_sources("2026-01", read)
        assert result == tuple(sorted((current, statement), key=lambda value: value["source_id"]))
        assert records.period_sources("2026-03", read) == ()


def test_clear_only_removes_target_month_issues_and_preserves_all_notes(records_case):
    storage, (company, _), records = records_case
    target = store(records_case, "当月待核实来源", "2026-01")
    preserved = store(records_case, "长期或其他月依据", "2026-01")
    noted = store(records_case, "当月说明单独引用的依据", "2026-01")
    jan = "period:2026-01"
    feb = "period:2026-02"
    notes = (
        {
            "scope": "company",
            "expected_seq": 0,
            "text": "长期经营说明",
            "evidence_ids": [preserved["source_id"]],
        },
        {
            "scope": jan,
            "expected_seq": 0,
            "expected_basis_head": "jan-basis",
            "text": "一月原办理结论",
            "evidence_ids": [noted["source_id"]],
        },
        {
            "scope": feb,
            "expected_seq": 0,
            "expected_basis_head": "feb-basis",
            "text": "二月正确说明",
            "evidence_ids": [preserved["source_id"]],
        },
    )
    jan_unperioded = issue(jan, target["source_id"])
    feb_in_jan = issue(jan, preserved["source_id"], "2026-02")
    save_work(records_case, notes=notes, issues=(jan_unperioded, feb_in_jan), scope=jan)
    jan_custom = issue("custom:review", target["source_id"], "2026-01")
    unperioded_custom = issue("custom:review", preserved["source_id"])
    feb_custom = issue("custom:review", preserved["source_id"], "2026-02")
    save_work(
        records_case, issues=(jan_custom, unperioded_custom, feb_custom), scope="custom:review"
    )
    jan_company = issue("company", target["source_id"], "2026-01")
    company_longterm = issue("company", preserved["source_id"])
    save_work(records_case, issues=(jan_company, company_longterm), scope="company")
    assert _publish(records_case, _prepare(records_case, [], []))["state"] == "ready"
    with storage.read_unit(company.company_id) as read:
        before_notes = {value["scope"]: value for value in records.current_notes(None, read)}
        before_targets = {
            row["shard_key"]: row["wanted_head"] for row in read.repository("records").targets()
        }
        before_reset = read.head
        assert set(
            records.source_discard_protection(
                (target["source_id"], preserved["source_id"], noted["source_id"]),
                read,
                retained_source_ids=(),
                protected_paths=(),
            )
        ) == {target["source_id"], preserved["source_id"], noted["source_id"]}
    with storage.write_unit(company.company_id) as unit:
        assert records.clear_period_issues("2026-01", str(uuid4()), unit) == (
            "company",
            "custom:review",
            jan,
        )
    with storage.read_unit(company.company_id) as read:
        after = {value["scope"]: value for value in records.current_notes(None, read)}
        assert after == before_notes
        assert records.current_issues(jan, read) == {
            "scope": jan,
            "head": 2,
            "issues": [feb_in_jan],
        }
        assert records.current_issues("company", read) == {
            "scope": "company",
            "head": 2,
            "issues": [company_longterm],
        }
        assert records.current_issues("custom:review", read) == {
            "scope": "custom:review",
            "head": 2,
            "issues": sorted((unperioded_custom, feb_custom), key=lambda value: value["issue_id"]),
        }
        assert records.source_discard_protection(
            (target["source_id"], preserved["source_id"], noted["source_id"]),
            read,
            retained_source_ids=(),
            protected_paths=(),
        ) == tuple(sorted((preserved["source_id"], noted["source_id"])))
        after_targets = {
            row["shard_key"]: row["wanted_head"] for row in read.repository("records").targets()
        }
        assert after_targets == before_targets | dict.fromkeys(
            ("sources", "work"), before_reset + 1
        )
        before_retry = read.head
    with storage.write_unit(company.company_id) as unit:
        assert records.clear_period_issues("2026-01", str(uuid4()), unit) == ()
    with storage.read_unit(company.company_id) as read:
        assert read.head == before_retry
        assert records.current_notes((jan,), read)[0]["seq"] == 1


def test_clear_rollback_restores_issues_and_source_uses(records_case):
    storage, (company, _), records = records_case
    source = store(records_case, "一月依据", "2026-01")
    current = issue("period:2026-01", source["source_id"], "2026-01")
    save_work(records_case, issues=(current,), scope=current["scope"])
    with pytest.raises(RuntimeError):
        with storage.write_unit(company.company_id) as unit:
            records.clear_period_issues("2026-01", str(uuid4()), unit)
            raise RuntimeError("模拟整月撤销后续检查失败")
    with storage.read_unit(company.company_id) as read:
        assert records.current_issues(current["scope"], read)["issues"] == [current]
        assert records.source_discard_protection(
            (source["source_id"],), read, retained_source_ids=(), protected_paths=()
        ) == (source["source_id"],)
        assert records.current_notes((current["scope"],), read) == ()
        with pytest.raises(ContractError, match="正式写事务"):
            records.clear_period_issues("2026-01", str(uuid4()), read)


def test_period_cleanup_selection_resumes_only_that_month_without_deleting_files(records_case):
    storage, (company, _), records = records_case
    january = store(records_case, "一月原件", "2026-01", file=True)
    february = store(records_case, "二月原件", "2026-02", file=True)
    with storage.write_unit(company.company_id) as unit:
        discarded = records.discard_sources(
            (january["source_id"], february["source_id"]),
            unit,
            retained_source_ids=(),
            protected_paths=(),
        )
    expected = tuple(
        value for value in discarded.marker_ids if value.startswith(january["source_id"])
    )
    with storage.read_unit(company.company_id) as read:
        assert records.pending_source_discard_markers("2026-01", read) == expected
        assert records.pending_source_discard_markers("2026-03", read) == ()
    assert (company.company_root / january["relative_path"]).exists()
    assert (company.company_root / february["relative_path"]).exists()
    with storage.write_unit(company.company_id) as unit:
        completed = records.finish_source_discards(unit, marker_ids=expected, protected_paths=())
    assert completed.completed_marker_ids == expected
    assert not (company.company_root / january["relative_path"]).exists()
    assert (company.company_root / february["relative_path"]).exists()
    with storage.read_unit(company.company_id) as read:
        assert records.pending_source_discard_markers("2026-01", read) == ()
        assert len(records.pending_source_discard_markers("2026-02", read)) == 1


def test_note_command_proof_expires_when_replaced_or_invalidated(records_case):
    storage, (company, _), records = records_case
    scope = "period:2026-01"
    first_command, second_command, invalidate_command = (str(uuid4()) for _ in range(3))
    note = {
        "scope": scope,
        "expected_seq": 0,
        "expected_basis_head": "current-basis",
        "text": "当月已撤销，等待重新提供资料。",
        "evidence_ids": [],
    }
    with storage.read_unit(company.company_id) as read:
        assert records.note_written_by(scope, first_command, read) is False
    with storage.write_unit(company.company_id) as unit:
        records.apply_current(
            records.plan_change(
                RecordsChange(notes=(note,)),
                RecordsContext(unit, first_command, {scope: "current-basis"}),
            ),
            unit,
        )
    with storage.read_unit(company.company_id) as read:
        assert records.note_written_by(scope, first_command, read) is True
        assert records.note_written_by(scope, second_command, read) is False
        assert records.note_written_by("period:2026-02", first_command, read) is False
    with storage.write_unit(company.company_id) as unit:
        records.apply_current(
            records.plan_change(
                RecordsChange(notes=(note | {"expected_seq": 1, "text": "已开始重新核对原件。"},)),
                RecordsContext(unit, second_command, {scope: "current-basis"}),
            ),
            unit,
        )
    with storage.read_unit(company.company_id) as read:
        assert records.note_written_by(scope, first_command, read) is False
        assert records.note_written_by(scope, second_command, read) is True
    with storage.write_unit(company.company_id) as unit:
        records.invalidate_notes({scope: "updated-basis"}, invalidate_command, unit)
    with storage.read_unit(company.company_id) as read:
        assert records.note_written_by(scope, second_command, read) is False
        assert records.note_written_by(scope, invalidate_command, read) is False
        with pytest.raises(ContractError):
            records.note_written_by("custom:invalid-note", first_command, read)
        with pytest.raises(ContractError):
            records.note_written_by(scope, "not-a-command-id", read)
