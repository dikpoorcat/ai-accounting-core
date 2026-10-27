"""Input routing selects related targets and atomically maintains their current membership."""

import sqlite3
from dataclasses import fields
from types import SimpleNamespace

import pytest

from ai_accounting_modular.contracts.public import (
    ContractError,
    ReadInputs,
    canonical_json,
    loads_json,
)
from ai_accounting_modular.ledger.public import FormalChange
from ai_accounting_modular.reading import _dependencies, _monthly
from ai_accounting_modular.reading import _subscription_keys as keys
from ai_accounting_modular.reading._subscriptions import CONTROL, NODES, SCOPE, TARGETS, Directory
from ai_accounting_modular.reading._types import PeriodRange, ReadingImpact

from ._support import PARTY, SOURCE, assemble, expense


@pytest.fixture
def repository():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE read_item(scope_key TEXT,collection_key TEXT,item_id TEXT,sort_key TEXT,"
        "business_id TEXT,object_id TEXT,group_key TEXT,category TEXT,payload_json TEXT,"
        "component_key TEXT,head INTEGER,PRIMARY KEY(scope_key,collection_key,item_id))"
    )
    connection.execute(
        "CREATE TABLE read_component(scope_key TEXT,component_key TEXT,head INTEGER,"
        "payload_json TEXT)"
    )
    session = SimpleNamespace(
        execute=connection.execute,
        executemany=connection.executemany,
        fetchone=lambda sql, args=(): connection.execute(sql, args).fetchone(),
        fetchall=lambda sql, args=(): connection.execute(sql, args).fetchall(),
    )
    yield SimpleNamespace(_session=session)
    connection.close()


def scope(month="2026-01", *, page="monthly"):
    return canonical_json({"page": page, "scope": {"period_to": month}})


def publish(repository, selected, month="2026-01", component="funds", **kwargs):
    directory = Directory(repository)
    plan = directory.plan(scope(month), component, inputs=selected, **kwargs)
    assert directory.commit(plan)
    return plan


def change(**kwargs):
    return FormalChange(
        **{
            "business_id": SOURCE,
            "result_slot": "main",
            "business_periods": ("2026-01",),
            "posted_periods": ("2026-01",),
            "result_kinds": ("operations.expense",),
            "account_codes": ("1002",),
            "obligation_categories": ("trade",),
            "cost_categories": ("inventory",),
            "resource_business_ids": (PARTY,),
            "movement_periods": ("2026-01",),
            **kwargs,
        }
    )


def inputs():
    return ReadInputs(
        result_kinds=("operations.expense",),
        business_result_kinds=("operations.expense",),
        posted_result_kinds=("operations.expense",),
        account_codes=("1002",),
        obligation_categories=("trade",),
        cost_categories=("inventory",),
        business_ids=(SOURCE,),
        current_business_ids=(SOURCE,),
        current_fact_ids=(SOURCE,),
        fact_kinds=("operations.expense",),
        object_ids=(PARTY,),
        object_kinds=("organization",),
        note_scopes=("business:" + SOURCE,),
        movements=True,
        all_results=True,
        posted_results=True,
        all_obligations=True,
    )


@pytest.mark.parametrize("field", [field.name for field in fields(ReadInputs)])
def test_every_public_selector_matches_affected_without_loading_unrelated_targets(
    repository, field
):
    selected = ReadInputs(**{field: getattr(inputs(), field)})
    for month in ("2025-12", "2026-01", "2026-02"):
        publish(repository, selected, month)
    impacts = [
        ReadingImpact(formal_changes=(change(),)),
        ReadingImpact(formal_changes=(change(posted_periods=()),)),
        ReadingImpact(formal_changes=(change(posted_periods=("2026-02",)),)),
        ReadingImpact(formal_changes=(change(business_id=PARTY, resource_business_ids=()),)),
        ReadingImpact(formal_changes=(change(result_kinds=("other.kind",)),)),
        ReadingImpact(facts=((SOURCE, "operations.expense", "2026-01"),)),
        ReadingImpact(objects=((PARTY, "organization"),)),
        ReadingImpact(note_scopes=("business:" + SOURCE,)),
        ReadingImpact(),
    ]
    directory = Directory(repository)
    for impact in impacts:
        expected = {
            (scope(month), "funds")
            for month in ("2025-12", "2026-01", "2026-02")
            if _dependencies.affected(selected, month, impact)
        }
        assert set(directory.candidates(impact)) == expected, (field, impact)


def test_projection_specific_period_and_work_subscriptions(repository):
    for component in ("brief_context", "brief_open_items", "brief_activity", "financial"):
        for month in ("2025-12", "2026-01", "2026-02"):
            publish(repository, ReadInputs(), month, component)
    directory = Directory(repository)
    assert set(directory.candidates(ReadingImpact(working_period_changed=True))) == {
        (scope(month), "brief_open_items") for month in ("2025-12", "2026-01", "2026-02")
    }
    assert set(directory.candidates(ReadingImpact(issue_periods=(None,)))) == {
        (scope(month), "brief_context") for month in ("2025-12", "2026-01", "2026-02")
    }
    assert directory.candidates(ReadingImpact(note_scopes=("period:2026-01",))) == (
        (scope(), "brief_context"),
    )
    assert set(
        directory.candidates(ReadingImpact(closure_ranges=(PeriodRange("2026-01", "2026-01"),)))
    ) == (
        {(scope(month), "brief_open_items") for month in ("2025-12", "2026-01", "2026-02")}
        | {
            (scope(month), component)
            for month in ("2026-01", "2026-02")
            for component in ("financial", "brief_activity")
        }
    )


@pytest.mark.parametrize(
    ("periods", "months"),
    [
        ((), ()),
        (("2026-01",), ("2026-01", "2026-02")),
        (("2026-02",), ("2026-02",)),
        (("2026-03",), ()),
        (("2026-02", "2026-01"), ("2026-01", "2026-02")),
        ((None,), ("2025-12", "2026-01", "2026-02")),
        (("2026-02", None), ("2025-12", "2026-01", "2026-02")),
    ],
)
def test_issue_periods_route_only_contexts_which_include_the_changed_issue(
    repository, periods, months
):
    for component in ("brief_context", "funds"):
        for month in ("2025-12", "2026-01", "2026-02"):
            publish(repository, ReadInputs(), month, component)
    impact = ReadingImpact(issue_periods=periods)
    expected = {(scope(month), "brief_context") for month in months}
    assert set(Directory(repository).candidates(impact)) == expected
    assert {
        (scope(month), "brief_context")
        for month in ("2025-12", "2026-01", "2026-02")
        if _monthly.affected({"period_to": month}, "brief_context", impact, ReadInputs())
    } == expected


@pytest.mark.parametrize("period", ["period:2026-01", "2026-13", "", 202601, False])
def test_issue_impact_requires_an_actual_month_or_unperioded_marker(period):
    with pytest.raises(ContractError):
        ReadingImpact(issue_periods=(period,))


def test_later_adoption_retires_activity_description_without_general_note_invalidation(repository):
    selected = ReadInputs(note_scopes=("business:" + SOURCE,))
    publish(repository, selected, component="brief_activity")
    publish(repository, selected, component="funds")
    impact = ReadingImpact(formal_changes=(change(posted_periods=("2026-02",)),))
    assert Directory(repository).candidates(impact) == ((scope(), "brief_activity"),)


def test_plan_is_read_only_and_deltas_replace_current_membership(repository):
    directory = Directory(repository)
    plan = directory.plan(scope(), "funds", inputs=ReadInputs(object_ids=(PARTY,)))
    assert plan.expanded
    assert repository._session.fetchone("SELECT count(*) FROM read_item")[0] == 0
    assert directory.commit(plan)
    assert not directory.plan(scope(), "funds", inputs=ReadInputs(object_ids=(PARTY,))).expanded
    delta = directory.plan(
        scope(),
        "funds",
        adds=ReadInputs(current_fact_ids=(SOURCE,)),
        removes=ReadInputs(object_ids=(PARTY,)),
    )
    assert delta.expanded
    assert directory.commit(delta)
    assert not directory.candidates(ReadingImpact(objects=((PARTY, "organization"),)))
    assert directory.candidates(ReadingImpact(facts=((SOURCE, "operations.expense", None),))) == (
        (scope(), "funds"),
    )
    removed = directory.plan(scope(), "funds", removes=ReadInputs(current_fact_ids=(SOURCE,)))
    assert not removed.expanded
    assert directory.commit(removed)
    assert not directory.candidates(ReadingImpact(facts=((SOURCE, "operations.expense", None),)))


def test_delta_of_one_target_rebases_on_current_root_without_invalidating_other_targets(repository):
    publish(repository, ReadInputs(object_ids=(PARTY,)))
    publish(repository, ReadInputs(current_fact_ids=(SOURCE,)), "2026-02")
    directory = Directory(repository)
    plan = directory.plan(scope(), "funds", adds=ReadInputs(note_scopes=("business:" + SOURCE,)))
    assert directory.commit(plan)
    assert directory.candidates(ReadingImpact(facts=((SOURCE, "operations.expense", None),))) == (
        (scope("2026-02"), "funds"),
    )


def test_new_month_forks_actual_subscriptions_and_recompiles_period(repository):
    publish(
        repository, ReadInputs(business_ids=(SOURCE,), movements=True), component="brief_open_items"
    )
    directory = Directory(repository)
    plan = directory.plan(
        scope("2026-02"),
        "brief_open_items",
        base_target=(scope(), "brief_open_items"),
        adds=ReadInputs(object_ids=(PARTY,)),
        removes=ReadInputs(movements=True),
    )
    assert plan.expanded and directory.commit(plan)
    changed = ReadingImpact(formal_changes=(change(posted_periods=("2026-02",)),))
    assert directory.candidates(changed) == ((scope("2026-02"), "brief_open_items"),)
    assert directory.candidates(ReadingImpact(objects=((PARTY, "organization"),))) == (
        (scope("2026-02"), "brief_open_items"),
    )


def test_missing_control_or_selected_node_cannot_become_an_empty_match(repository):
    publish(repository, ReadInputs(object_ids=(PARTY,)))
    rows = repository._session.fetchall("SELECT * FROM read_item")
    repository._session.execute("DELETE FROM read_item WHERE collection_key=?", (CONTROL,))
    with pytest.raises(ContractError, match="订阅") as error:
        Directory(repository)
    assert error.value.code == "storage_corrupt"
    names = rows[0].keys()
    repository._session.executemany(
        "INSERT OR REPLACE INTO read_item("
        + ",".join(names)
        + ") VALUES("
        + ",".join("?" for _ in names)
        + ")",
        (tuple(row) for row in rows),
    )
    root = Directory(repository).root
    repository._session.execute(
        "DELETE FROM read_item WHERE collection_key=? AND item_id=?", (NODES, root.digest)
    )
    with pytest.raises(ContractError) as error:
        Directory(repository)
    assert error.value.code == "storage_corrupt"


def test_reset_has_no_inherited_subscription_state(repository):
    publish(repository, inputs())
    repository._session.execute("DELETE FROM read_item")
    directory = Directory(repository)
    assert not directory.known(scope(), "funds")
    assert not directory.candidates(ReadingImpact(objects=((PARTY, "organization"),)))
    plan = directory.plan(scope(), "funds", inputs=ReadInputs())
    assert directory.commit(plan)


def test_selecting_a_missing_input_uses_no_unrelated_target_rows(repository):
    for index in range(100):
        publish(
            repository, ReadInputs(note_scopes=(f"business:other-{index}",)), f"{2000 + index}-01"
        )
    session = repository._session
    fetchone, observed = session.fetchone, []

    def selected(sql, args=()):
        observed.append((sql, args))
        return fetchone(sql, args)

    session.fetchone = selected
    assert Directory(repository).candidates(ReadingImpact(note_scopes=("business:missing",))) == ()
    assert not any("_subscription_targets" in args for _, args in observed)


def test_all_keys_are_individually_addressable_and_escaped_identifiers_do_not_overlap(repository):
    selected = ReadInputs(note_scopes=("a", "a\0b", "a\0"))
    publish(repository, selected)
    assert len(keys.selectors(selected)) == 3
    for value in selected.note_scopes:
        assert Directory(repository).candidates(ReadingImpact(note_scopes=(value,))) == (
            (scope(), "funds"),
        )
    assert not Directory(repository).candidates(ReadingImpact(note_scopes=("a\0c",)))


def test_stale_writer_plan_does_not_overwrite_current_subscriptions(repository):
    first, second = Directory(repository), Directory(repository)
    plan = first.plan(scope(), "funds", inputs=ReadInputs(object_ids=(PARTY,)))
    other = second.plan(scope("2026-02"), "funds", inputs=ReadInputs(current_fact_ids=(SOURCE,)))
    assert second.commit(other)
    assert not first.commit(plan)


def test_notification_coalesces_overlapping_ranges_of_the_same_input():
    impact = ReadingImpact(
        formal_changes=tuple(change(posted_periods=(f"{2020 + index}-01",)) for index in range(20))
    )
    queries = keys.queries(impact)
    assert [value for value in queries if value[0] == "all_results"] == [
        ("all_results", "", "2020-01", None, None)
    ]


def test_replacing_current_inputs_releases_unreferenced_nodes(repository):
    for index in range(30):
        publish(repository, ReadInputs(note_scopes=(f"business:current-{index}",)))
    assert repository._session.fetchone("SELECT COUNT(*) FROM read_item")[0] == 6
    publish(repository, ReadInputs())
    assert repository._session.fetchone("SELECT COUNT(*) FROM read_item")[0] == 2


def test_null_reverse_root_with_existing_forward_nodes_is_corruption(repository):
    publish(repository, ReadInputs(object_ids=(PARTY,)))
    row = repository._session.fetchone(
        "SELECT payload_json FROM read_item WHERE collection_key=?", (CONTROL,)
    )
    value = loads_json(row[0])
    value["root"] = None
    repository._session.execute(
        "UPDATE read_item SET payload_json=? WHERE collection_key=?",
        (canonical_json(value), CONTROL),
    )
    with pytest.raises(ContractError) as error:
        Directory(repository)
    assert error.value.code == "storage_corrupt"


def test_selected_reverse_edge_requires_its_real_forward_owner(repository):
    publish(repository, ReadInputs(object_ids=(PARTY,)))
    repository._session.execute("DELETE FROM read_item WHERE collection_key=?", (TARGETS,))
    with pytest.raises(ContractError) as error:
        Directory(repository).candidates(ReadingImpact(objects=((PARTY, "organization"),)))
    assert error.value.code == "storage_corrupt"


@pytest.mark.parametrize("page", ["monthly", "quarterly_report"])
def test_deleted_entire_directory_cannot_leave_a_published_consumer_ready(repository, page):
    publish(repository, ReadInputs(object_ids=(PARTY,)))
    repository._session.execute(
        "INSERT INTO read_component VALUES(?,?,?,?)", (scope(page=page), "financial", 1, "{}")
    )
    repository._session.execute("DELETE FROM read_item WHERE scope_key=?", (SCOPE,))
    with pytest.raises(ContractError) as error:
        Directory(repository)
    assert error.value.code == "storage_corrupt"


def test_first_financial_producer_is_not_a_published_consumer(repository):
    repository._session.execute(
        "INSERT INTO read_component VALUES(?,?,?,?)",
        (canonical_json({"model": "ledger_financial_projection"}), "projection", 1, "{}"),
    )
    assert not Directory(repository).known(scope(), "financial")


def test_synthetic_nonconsumer_does_not_prevent_first_subscription_initialization(repository):
    repository._session.execute(
        "INSERT INTO read_component VALUES(?,?,?,?)", ("synthetic", "guarded", 1, "{}")
    )
    assert not Directory(repository).known(scope(), "financial")


def test_empty_subscription_target_is_a_valid_initialized_directory(repository):
    publish(repository, ReadInputs())
    directory = Directory(repository)
    assert directory.known(scope(), "funds")
    assert directory.root is None
    assert directory.candidates(ReadingImpact(formal_changes=(change(),))) == ()


def test_initialized_directory_does_not_repeat_the_initial_consumer_probe(repository):
    publish(repository, ReadInputs(object_ids=(PARTY,)))
    fetchone = repository._session.fetchone

    def selected(sql, args=()):
        assert "FROM read_component" not in sql
        return fetchone(sql, args)

    repository._session.fetchone = selected
    assert Directory(repository).candidates(ReadingImpact(objects=((PARTY, "organization"),))) == (
        (scope(), "funds"),
    )


def test_real_business_write_recovers_a_deleted_subscription_directory_without_stale_amounts(
    tmp_path,
):
    book = assemble(tmp_path)
    book.submit(expense())
    selected = _monthly.normalize(
        {
            "company_id": book.company,
            "period_from": "2026-01",
            "period_to": "2026-01",
            "view": "as_posted",
        }
    )

    def page():
        return _monthly.deliver(
            book.reading,
            selected,
            ("financial",),
            lambda snapshot, read: snapshot.components["financial"]["financial"],
        )

    assert page().state == "preparing"
    assert book.reading.run_pending(book.company)
    assert page().payload["expense"] == {"state": "known", "amount_fen": "10000"}
    with book.storage.maintenance_unit(book.company) as unit:
        unit.repository("reading")._session.execute(
            "DELETE FROM read_item WHERE scope_key=?", (SCOPE,)
        )
    book.submit(expense(name="second", amount="30000"))
    with book.storage.read_unit(book.company) as read:
        assert read.repository("reading").state(_monthly.key(selected), "financial") is None
    assert page().state == "preparing"
    assert book.reading.run_pending(book.company)
    assert page().payload["expense"] == {"state": "known", "amount_fen": "40000"}
