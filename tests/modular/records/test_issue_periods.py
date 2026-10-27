"""Issue notifications follow changed current bodies, independently of their container keys."""

from uuid import uuid4

import pytest
from test_documents import _prepare, _publish

from ai_accounting_modular.contracts.public import ContractError, content_digest
from ai_accounting_modular.records.public import RecordsChange, RecordsContext


def issue(scope, period=None, **values):
    result = {
        "issue_id": str(uuid4()),
        "scope": scope,
        "evidence_ids": [],
        "missing_fields": [],
        "question": "请核对当前事实。",
        "assignee": "ai",
        "action": "professional_check",
        **values,
    }
    if period is not None:
        result["period"] = period
    return result


def save(case, scope, issues, head):
    storage, (company, _), records = case
    with storage.write_unit(company.company_id) as unit:
        plan = records.plan_change(
            RecordsChange(
                unresolved=tuple(issues), unresolved_scope=scope, expected_scope_head=head
            ),
            RecordsContext(unit, str(uuid4())),
        )
        records.apply_current(plan, unit)
        return plan.issue_periods


def test_creation_and_identical_or_reordered_retries_report_only_real_changes(records_case):
    scope = "arbitrary container with no month meaning"
    january, february, general = (issue(scope, "2026-01"), issue(scope, "2026-02"), issue(scope))
    values = (january, february, general)
    assert save(records_case, scope, values, 0) == (None, "2026-01", "2026-02")
    assert save(records_case, scope, tuple(reversed(values)), 0) == ()
    assert save(records_case, scope, values, 1) == ()
    storage, (company, _), records = records_case
    with storage.read_unit(company.company_id) as read:
        current = records.current_issues(scope, read)
        assert current["head"] == 1 and len(current["issues"]) == 3


def test_unchanged_retries_do_not_load_old_issue_bodies(records_case, monkeypatch):
    storage, (company, _), records = records_case
    scope = "unchanged review queue"
    values = (issue(scope, "2026-01"), issue(scope))
    save(records_case, scope, values, 0)
    with storage.write_unit(company.company_id) as unit:
        repository = unit.repository("records")

        def no_body_read(*args):
            pytest.fail("An unchanged retry must not load the saved issue bodies.")

        monkeypatch.setattr(repository, "issues", no_body_read)
        for expected in (0, 1):
            plan = records.plan_change(
                RecordsChange(
                    unresolved=tuple(reversed(values)),
                    unresolved_scope=scope,
                    expected_scope_head=expected,
                ),
                RecordsContext(unit, str(uuid4())),
            )
            assert plan.issue_periods == () and plan.changed_scopes == ()
            assert records.apply_current(plan, unit) is None


@pytest.mark.parametrize("damage", ["missing", "extra"])
def test_changed_plan_requires_the_complete_old_issue_scope(records_case, damage):
    storage, (company, _), records = records_case
    scope = "mixed review queue"
    retained, removed = issue(scope, "2026-01"), issue(scope, "2026-02")
    save(records_case, scope, (retained, removed), 0)
    with storage.write_unit(company.company_id) as unit:
        repository = unit.repository("records")
        session = repository._session
        if damage == "missing":
            session.execute("DELETE FROM current_issue WHERE issue_id=?", (removed["issue_id"],))
        else:
            extra = removed | {"issue_id": str(uuid4())}
            row = dict(
                session.fetchone(
                    "SELECT * FROM current_issue WHERE issue_id=?", (removed["issue_id"],)
                )
            )
            row.update(issue_id=extra["issue_id"], content_digest=content_digest(extra))
            session.execute(
                "INSERT INTO current_issue("
                + ",".join(row)
                + ") VALUES("
                + ",".join("?" for _ in row)
                + ")",
                tuple(row.values()),
            )
        # Every remaining/extra row is valid on its own; only the complete scope is broken.
        assert len(repository.issues(scope)) == (1 if damage == "missing" else 3)
        before = tuple(
            tuple(tuple(row) for row in session.fetchall("SELECT * FROM " + table + " ORDER BY 1"))
            for table in ("current_issue", "current_issue_scope")
        )
        with pytest.raises(ContractError) as current_error:
            records.current_issues(scope, unit)
        assert current_error.value.code == "storage_corrupt"
        with pytest.raises(ContractError) as plan_error:
            records.plan_change(
                RecordsChange(
                    unresolved=(retained | {"question": "已核实，当前问题需要更正。"},),
                    unresolved_scope=scope,
                    expected_scope_head=1,
                ),
                RecordsContext(unit, str(uuid4())),
            )
        assert plan_error.value.code == "storage_corrupt"
        assert before == tuple(
            tuple(tuple(row) for row in session.fetchall("SELECT * FROM " + table + " ORDER BY 1"))
            for table in ("current_issue", "current_issue_scope")
        )


def test_added_removed_moved_and_edited_members_exclude_unchanged_scope_members(records_case):
    scope = "period:2030-12"  # The key deliberately disagrees with actual issue periods.
    removed, moved, edited = (
        issue(scope, "2026-01"),
        issue(scope, "2026-02"),
        issue(scope, "2026-03"),
    )
    other, general = issue(scope, "2025-01"), issue(scope)
    save(records_case, scope, (removed, moved, edited, other, general), 0)
    replacement = (
        moved | {"period": "2026-04"},
        edited | {"question": "本期问题的最新正确正文。"},
        issue(scope, "2026-05"),
        other,
        general,
    )
    assert save(records_case, scope, replacement, 1) == (
        "2026-01",
        "2026-02",
        "2026-03",
        "2026-04",
        "2026-05",
    )
    storage, (company, _), records = records_case
    with storage.read_unit(company.company_id) as read:
        assert records.current_issues(scope, read)["issues"] == sorted(
            replacement, key=lambda item: item["issue_id"]
        )


def test_moving_between_dated_and_unperioded_keeps_global_meaning(records_case):
    scope = "company"
    value = issue(scope, "2026-02")
    assert save(records_case, scope, (value,), 0) == ("2026-02",)
    unperioded = {key: item for key, item in value.items() if key != "period"}
    assert save(records_case, scope, (unperioded,), 1) == (None, "2026-02")
    assert save(records_case, scope, (value | {"period": "2026-04"},), 2) == (None, "2026-04")
    assert save(records_case, scope, (), 3) == ("2026-04",)
    assert save(records_case, scope, (), 3) == ()


@pytest.mark.parametrize(
    "replacement",
    [
        {"question": "正文已经改变。"},
        {"assignee": "owner"},
        {"action": "provide_material"},
        {"due_on": "2026-03-15"},
        {"missing_fields": ["/data/amount_fen"]},
        {"business_id": "00000000-0000-0000-0000-000000000001"},
    ],
)
def test_any_current_body_change_notifies_its_actual_period(records_case, replacement):
    scope = "period:2020-01"
    changed, unchanged = issue(scope, "2026-03"), issue(scope, "2026-07")
    save(records_case, scope, (changed, unchanged), 0)
    assert save(records_case, scope, (changed | replacement, unchanged), 1) == ("2026-03",)


def test_new_evidence_in_an_issue_is_a_body_change(records_case):
    storage, (company, _), records = records_case
    scope, value = "review evidence", issue("review evidence", "2026-06")
    save(records_case, scope, (value,), 0)
    with storage.read_unit(company.company_id) as read:
        source = records.prepare_source({"kind": "owner_statement", "text": "最新现实依据"}, read)
    with storage.write_unit(company.company_id) as unit:
        plan = records.plan_change(
            RecordsChange(
                sources=(source,),
                unresolved=(value | {"evidence_ids": [source["source_id"]]},),
                unresolved_scope=scope,
                expected_scope_head=1,
            ),
            RecordsContext(unit, str(uuid4())),
        )
        assert plan.issue_periods == ("2026-06",)
        records.apply_current(plan, unit)


def test_changed_plan_and_apply_read_old_bodies_once_and_replace_only_scoped_issue_uses(
    records_case, monkeypatch
):
    storage, (company, _), records = records_case
    with storage.maintenance_unit(company.company_id) as unit:
        records.ensure_document_targets(unit)
    assert _publish(records_case, _prepare(records_case, [], []))["state"] == "ready"
    with storage.read_unit(company.company_id) as read:
        sources = tuple(
            records.prepare_source({"kind": "owner_statement", "text": text}, read)
            for text in ("旧明确来源", "旧已知事实来源", "继续保留的来源", "新来源")
        )
    old, nested, retained, added = (source["source_id"] for source in sources)
    scope = "a scoped review queue"
    edited = issue(
        scope,
        "2026-01",
        evidence_ids=[old, retained],
        known_fact={
            "kind": "investment.cooperation_contract",
            "data": {"contract_source": {"source_id": nested, "location": "合同第一页"}},
        },
    )
    removed = issue(scope, "2026-02", evidence_ids=[old])
    unchanged = issue(scope, "2026-03", evidence_ids=[retained])
    with storage.write_unit(company.company_id) as unit:
        records.apply_current(
            records.plan_change(
                RecordsChange(
                    sources=sources,
                    unresolved=(edited, removed, unchanged),
                    unresolved_scope=scope,
                    expected_scope_head=0,
                ),
                RecordsContext(unit, str(uuid4())),
            ),
            unit,
        )
        # A different owner kind may share the UUID, and another queue may share the source.
        records.replace_source_uses("object", edited["issue_id"], (nested,), unit)
    other = issue("another queue", "2026-04", evidence_ids=[old])
    save(records_case, other["scope"], (other,), 0)
    with storage.read_unit(company.company_id) as read:
        before_targets = records.document_target_heads(read)
    replacement = {key: value for key, value in edited.items() if key != "known_fact"}
    replacement["evidence_ids"] = sorted((retained, added))
    with storage.write_unit(company.company_id) as unit:
        repository = unit.repository("records")
        load = repository._issues
        loaded = []

        def capture(rows):
            loaded.append(tuple(row["issue_id"] for row in rows))
            return load(rows)

        monkeypatch.setattr(repository, "_issues", capture)
        plan = records.plan_change(
            RecordsChange(
                unresolved=(replacement, unchanged),
                unresolved_scope=scope,
                expected_scope_head=1,
            ),
            RecordsContext(unit, str(uuid4())),
        )
        records.apply_current(plan, unit)
        assert loaded == [
            tuple(sorted(value["issue_id"] for value in (edited, removed, unchanged)))
        ]
        assert plan.issue_periods == ("2026-01", "2026-02")
        assert repository.uses("issue", edited["issue_id"]) == tuple(sorted((retained, added)))
        assert repository.uses("issue", removed["issue_id"]) == ()
        assert repository.uses("issue", unchanged["issue_id"]) == (retained,)
        assert repository.uses("issue", other["issue_id"]) == (old,)
        assert repository.uses("object", edited["issue_id"]) == (nested,)
        assert records.document_target_heads(unit) == before_targets | dict.fromkeys(
            ("work", "sources:" + old, "sources:" + nested, "sources:" + added), unit.next_head
        )
    with storage.read_unit(company.company_id) as read:
        assert records.current_issues(scope, read)["issues"] == sorted(
            (replacement, unchanged), key=lambda value: value["issue_id"]
        )
        assert records.current_issues(other["scope"], read)["issues"] == [other]


def test_other_records_changes_and_empty_replacements_have_no_issue_periods(records_case):
    storage, (company, _), records = records_case
    with storage.write_unit(company.company_id) as unit:
        change = RecordsChange(
            notes=(
                {"scope": "company", "expected_seq": 0, "text": "当前说明", "evidence_ids": []},
            ),
            unresolved=(),
            unresolved_scope="empty container",
            expected_scope_head=0,
        )
        plan = records.plan_change(change, RecordsContext(unit, str(uuid4())))
        assert plan.issue_periods == () and plan.changed_scopes == ("notes",)
        records.apply_current(plan, unit)


def test_clear_dated_work_does_not_include_unchanged_unperioded_or_other_month_work(records_case):
    storage, (company, _), records = records_case
    scope = "a mixed review queue"
    january, february, general = (issue(scope, "2026-01"), issue(scope, "2026-02"), issue(scope))
    save(records_case, scope, (january, february, general), 0)
    with storage.write_unit(company.company_id) as unit:
        assert records.clear_period_issues("2026-01", str(uuid4()), unit) == ("2026-01",)
    with storage.read_unit(company.company_id) as read:
        assert records.current_issues(scope, read)["issues"] == sorted(
            (february, general), key=lambda item: item["issue_id"]
        )


def test_clearing_unperioded_work_keeps_none_instead_of_guessing_the_container_month(records_case):
    storage, (company, _), records = records_case
    scope = "period:2026-01"
    save(records_case, scope, (issue(scope),), 0)
    with storage.write_unit(company.company_id) as unit:
        assert records.clear_period_issues("2026-01", str(uuid4()), unit) == (None,)
    with storage.write_unit(company.company_id) as unit:
        assert records.clear_period_issues("2026-01", str(uuid4()), unit) == ()
