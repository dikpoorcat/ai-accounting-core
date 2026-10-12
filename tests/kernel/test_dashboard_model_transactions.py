"""Read models preserve native pages and participate in the actual write boundary."""

import copy
import sqlite3
from contextlib import contextmanager

import pytest
from entity_fixture import save_entity_display_profile
from test_banking import book as book
from test_banking import entry, funding, opening, statement
from test_dashboard_funds_alignment import _publish_filter_funding
from test_dashboard_open_groups import expenses
from test_payroll_corrections import actual as actual_contributions
from test_payroll_corrections import company as company_fixture
from test_reimbursement_assets import accepted_batch, batch_card

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_model_runtime import (
    INTERNAL_REQUEST_PREFIX,
    _producing,
    production_reads,
)
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.entities import Entities
from ai_accounting.kernel.maintenance import Maintenance
from ai_accounting.kernel.response_contracts import validate_response
from ai_accounting.kernel.storage import Store, _active_fact_reads
from ai_accounting.kernel.stored_json import loads_unique
from ai_accounting.kernel.types import YearMonth

company = company_fixture


@contextmanager
def raw_read():
    token = _producing.set(True)
    try:
        yield
    finally:
        _producing.reset(token)


def comparable(payload):
    value = copy.deepcopy(payload)
    if value.get("scope") == "overview" and value.get("data"):
        value["data"].pop("generated_at")
    return value


def state(engine):
    with engine.store.connection(read_only=True) as connection:
        return tuple(connection.iterdump())


def current_model(engine):
    with engine.store.connection(read_only=True) as connection:
        row = connection.execute(
            "SELECT c.id,c.descriptor FROM dashboard_read_model_current x "
            "JOIN dashboard_read_model_commit c ON c.id=x.commit_id WHERE x.period='2026-09'",
        ).fetchone()
        return bytes(row[0]), loads_unique(row[1])


def receipt_counts(engine):
    with engine.store.connection(read_only=True) as connection:
        return tuple(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                     for table in ("audit", "request", "dashboard_read_model_commit"))


@contextmanager
def source_body_work(monkeypatch):
    """Observe actual decodes, including any hidden recursive verifier work."""
    from ai_accounting.kernel import integrity

    work = {"outcomes": [], "facts": [], "lineages": []}
    original_outcome = integrity._outcome_with_lines
    original_facts = Store.fact_data_many
    original_lineage = integrity._source_set

    def outcome(row):
        work["outcomes"].append(row["id"])
        return original_outcome(row)

    def facts(store, connection, identifiers):
        work["facts"].extend(identifiers)
        return original_facts(store, connection, identifiers)

    def lineage(connection, identifiers):
        result = original_lineage(connection, identifiers)
        work["lineages"].append(result)
        return result

    with monkeypatch.context() as scope:
        scope.setattr(integrity, "_outcome_with_lines", outcome)
        scope.setattr(Store, "fact_data_many", facts)
        scope.setattr(integrity, "_source_set", lineage)
        yield work


@pytest.fixture
def model_book(book):
    engine = book[0]
    _publish_filter_funding(engine, ["bank-a"] * 23)
    Maintenance(engine).rebuild_dashboard_read_model("2026-09", request_id="bootstrap-model")
    return book


@pytest.mark.parametrize("page", ("brief", "funds", "employees", "assets"))
@pytest.mark.parametrize("limit", (1, 3, 20, 500))
def test_default_pages_equal_raw_algorithms_and_keep_runtime_contract(model_book, page, limit):
    dashboard = Dashboard(model_book[0])
    options = {"limit": limit}
    if page == "funds":
        options["movement_account_selection"] = "first"
    operation = getattr(dashboard, page)
    with raw_read():
        expected = operation("2026-09", **options)
    actual = operation("2026-09", **options)
    validate_response("dashboard_" + page, actual)
    assert comparable(actual) == comparable(expected)


def test_first_movement_selection_keeps_explicit_statement_account_filter(book):
    engine, save, publish, _ = book
    opening(save, publish)
    funding(save, publish, amount=7)
    statement(save, publish, [entry("filter-statement", amount=7)])
    Maintenance(engine).rebuild_dashboard_read_model("2026-09", request_id="bootstrap-model")
    dashboard = Dashboard(engine)
    unfiltered = dashboard.funds("2026-09", movement_account_selection="first")
    assert unfiltered["data"]["collections"]["statements"]["page"]["filtered_count"] == 1
    options = {
        "movement_account_selection": "first",
        "statement_account_id": "not-this-company",
    }
    with raw_read():
        expected = dashboard.funds("2026-09", **options)
    actual = dashboard.funds("2026-09", **options)
    validate_response("dashboard_funds", actual)
    assert actual == expected
    statements = actual["data"]["collections"]["statements"]
    assert statements["items"] == []
    assert statements["page"]["total_count"] == 1
    assert statements["page"]["filtered_count"] == 0
    assert actual["data"]["collections"]["movements"]["items"] == (
        unfiltered["data"]["collections"]["movements"]["items"]
    )


@pytest.mark.parametrize("section", ("activity", "vouchers", "open_items"))
def test_brief_opaque_continuations_have_no_gaps_duplicates_or_changed_money(model_book, section):
    dashboard = Dashboard(model_book[0])
    with raw_read():
        expected = dashboard.brief("2026-09", section=section, limit=500)
    complete = expected["data"]["collections"][section]["items"]
    returned, cursor = [], None
    version = expected["snapshot_version"]
    while True:
        actual = dashboard.brief("2026-09", section=section, limit=3, cursor=cursor,
                                 expected_version=version)
        validate_response("dashboard_brief", actual)
        collection = actual["data"]["collections"][section]
        returned.extend(collection["items"])
        if not collection["page"]["has_more"]:
            break
        following = collection["page"]["next_cursor"]
        assert type(following) is str and following != cursor
        cursor = following
    assert returned == complete


def test_custom_overview_preserves_open_group_loaded_counts(book):
    for index in range(3):
        expenses(book, 2, party=f"supplier-{index}", prefix=f"expense-{index}")
    engine = book[0]
    Maintenance(engine).rebuild_dashboard_read_model("2026-09", request_id="bootstrap-model")
    dashboard = Dashboard(engine)
    with raw_read():
        expected = dashboard.brief("2026-09", limit=1)
    actual = dashboard.brief("2026-09", limit=1)
    assert comparable(actual) == comparable(expected)


def test_nonempty_employee_page_keeps_payroll_money_and_source_validation(company):
    company.publish("january", "february")
    save_entity_display_profile(company.engine, {
        "kind": "employee", "entity_id": "employee",
        "workforce_event": {"action": "hire", "effective_date": "2026-01-01"},
        "source": "synthetic employment confirmation",
    }, expected_revision=0, request_id=company.request())
    Maintenance(company.engine).rebuild_dashboard_read_model(
        "2026-02", request_id="bootstrap-model",
    )
    dashboard = Dashboard(company.engine)
    with raw_read():
        expected = dashboard.employees("2026-02")
    actual = dashboard.employees("2026-02")
    assert actual["data"]["collections"]["employees"]["items"]
    validate_response("dashboard_employees", actual)
    assert actual == expected


def test_nonempty_asset_page_keeps_batch_cards_money_and_source_validation(book):
    engine, save, publish, _ = book
    save("reimbursed_asset_batch", "batch", accepted_batch())
    save("reimbursed_asset", "computer", batch_card())
    save("reimbursed_asset", "chair", batch_card(30000, asset_id="chair"))
    publish("batch", "computer", "chair")
    Maintenance(engine).rebuild_dashboard_read_model("2026-02", request_id="bootstrap-model")
    dashboard = Dashboard(engine)
    with raw_read():
        expected = dashboard.assets("2026-02")
    actual = dashboard.assets("2026-02")
    items = actual["data"]["collections"]["assets"]["items"]
    assert len(items) == 2
    assert sum(item["cost_fen"] for item in items) == 150000
    validate_response("dashboard_assets", actual)
    assert actual == expected


def test_materialized_defaults_do_not_rebuild_raw_page_algorithms(model_book, monkeypatch):
    import ai_accounting.kernel.dashboard as dashboard_module
    import ai_accounting.kernel.dashboard_brief_groups as activity_module
    import ai_accounting.kernel.dashboard_open_groups as open_module

    def unexpected(*_args, **_kwargs):
        raise AssertionError("a certified default page rebuilt an unrelated full raw projection")

    for name in ("_brief_amounts", "_funds", "_employees", "_assets"):
        monkeypatch.setattr(dashboard_module, name, unexpected)
    monkeypatch.setattr(activity_module, "activity_groups", unexpected)
    monkeypatch.setattr(open_module, "_record_data", unexpected)
    dashboard = Dashboard(model_book[0])
    for page in ("brief", "funds", "employees", "assets"):
        options = {"movement_account_selection": "first"} if page == "funds" else {}
        actual = getattr(dashboard, page)("2026-09", **options)
        validate_response("dashboard_" + page, actual)


def test_default_returned_source_work_does_not_expand_with_unrelated_prior_history(
    model_book, monkeypatch,
):
    engine = model_book[0]
    dashboard = Dashboard(engine)

    def read_hits():
        with source_body_work(monkeypatch) as work:
            response = dashboard.funds("2026-09", movement_account_selection="first")
        returned = response["data"]["collections"]["movements"]["items"]
        expected = {item["id"].split(":", 2)[1] for item in returned}
        with engine.store.connection(read_only=True) as connection:
            expected_facts = {connection.execute(
                "SELECT fact_id FROM calculation WHERE id=?", (ident,),
            ).fetchone()[0] for ident in expected}
        assert len(returned) == 20
        assert set(work["outcomes"]) == expected
        assert len(work["outcomes"]) == 20
        assert set(work["facts"]) == expected_facts
        assert len(work["facts"]) == 20
        assert work["lineages"] == []
        return response, work

    before, before_hits = read_hits()
    _publish_filter_funding(engine, ["bank-a"] * 3, period="2026-08")
    after, after_hits = read_hits()
    assert before_hits == after_hits
    assert (before["data"]["collections"]["movements"]["items"]
            == after["data"]["collections"]["movements"]["items"])
    assert after["data"]["total_fen"] == before["data"]["total_fen"] + 3


def test_cumulative_employee_reads_only_displayed_month_bodies_and_full_verifier_checks_history(
    company, monkeypatch,
):
    company.publish("january", "february")
    save_entity_display_profile(company.engine, {
        "kind": "employee", "entity_id": "employee",
        "workforce_event": {"action": "hire", "effective_date": "2026-01-01"},
        "source": "synthetic employment confirmation",
    }, expected_revision=0, request_id=company.request())
    Maintenance(company.engine).rebuild_dashboard_read_model(
        "2026-02", request_id="bootstrap-model",
    )
    with company.engine.store.connection(read_only=True) as connection:
        rows = {row["subject_id"]: dict(row) for row in connection.execute(
            "SELECT c.id,c.fact_id,c.subject_id FROM calculation_current h "
            "JOIN calculation c ON c.id=h.calculation_id "
            "WHERE c.subject_id IN ('january','february')",
        )}
    dashboard = Dashboard(company.engine)
    with source_body_work(monkeypatch) as work:
        expected = dashboard.employees("2026-02")
    assert expected["data"]["collections"]["employees"]["items"]
    assert work["outcomes"] == [rows["february"]["id"]]
    assert work["facts"] == [rows["february"]["fact_id"]]
    assert work["lineages"] == []

    # Corruption in an unseen ancestor cannot be hidden from the independent
    # complete verifier, but is outside this authenticated current-month hit.
    with company.engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        trigger = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name='immutable_calculation_UPDATE'",
        ).fetchone()[0]
        connection.execute("DROP TRIGGER immutable_calculation_UPDATE")
        connection.execute("UPDATE calculation SET digest=zeroblob(32) WHERE id=?",
                           (rows["january"]["id"],))
        connection.execute(trigger)
        connection.commit()
    assert dashboard.employees("2026-02") == expected
    with pytest.raises(KernelError) as error:
        Maintenance(company.engine).verify_integrity()
    assert error.value.code == "content_integrity_failed"
    assert error.value.details["record_id"] == rows["january"]["id"]


def test_model_bootstrap_accepts_correction_posting_month_without_a_fact_in_that_month(company):
    company.publish("january", "february")
    company.close("2026-01")
    company.save(actual_contributions(), "actual")
    company.publish("actual", posting_period="2026-03")
    month = YearMonth("2026-03").ordinal
    with company.engine.store.connection(read_only=True) as connection:
        assert not connection.execute(
            "SELECT 1 FROM fact_current h JOIN fact_revision f ON f.id=h.fact_id "
            "WHERE f.period=?", (month,),
        ).fetchone()
    result = Maintenance(company.engine).rebuild_dashboard_read_model(
        "2026-03", request_id="bootstrap-posting-model",
    )
    assert result["period"] == "2026-03"
    dashboard = Dashboard(company.engine)
    with raw_read():
        expected = dashboard.brief("2026-03", section="vouchers", limit=500)
    actual = dashboard.brief("2026-03", section="vouchers", limit=500)
    validate_response("dashboard_brief", actual)
    assert actual == expected
    assert len(actual["data"]["collections"]["vouchers"]["items"]) == 2


@pytest.mark.parametrize("lane", ("accounting", "material", "management"))
def test_every_write_lane_updates_scope_without_changing_business_receipt(model_book, lane):
    engine = model_book[0]
    before_id, before = current_model(engine)
    dashboard = Dashboard(engine)
    old = dashboard.brief("2026-09", section="activity", limit=1)
    key = "lane-update-" + lane
    if lane == "accounting":
        funding(model_book[1], model_book[2], subject="extra-funding", amount=9)
        with engine.store.connection(read_only=True) as connection:
            latest = connection.execute(
                "SELECT request_id FROM audit WHERE action='publish' ORDER BY id DESC LIMIT 1",
            ).fetchone()
        # The fixture publishes with its own unique request key.
        key = latest[0]
    elif lane == "material":
        engine.register_evidence(b"synthetic additional material", "text/plain", "fixture",
                                 request_id=key)
    else:
        Entities(engine).update_entity_profile(
            "bank-a", {"display_name": "明确更新后的账户名称"}, source="synthetic owner update",
            expected_revision=1, request_id=key,
        )
    after_id, after = current_model(engine)
    assert after_id != before_id
    assert after["scope"]["epochs"][lane] > before["scope"]["epochs"][lane]
    assert after["parent_commit_id"] == before_id.hex() or lane == "accounting"
    with engine.store.connection(read_only=True) as connection:
        saved = connection.execute("SELECT result FROM request WHERE id=?", (key,)).fetchone()
        audited = connection.execute(
            "SELECT payload FROM audit WHERE request_id=?", (key,),
        ).fetchone()
        result = loads_unique(saved[0])
        payload = loads_unique(audited[0])
        assert payload.get("result", payload) == result
        assert "dashboard_models" not in result
        assert engine.store.epochs(connection) == after["scope"]["epochs"]
    with pytest.raises(KernelError) as error:
        dashboard.brief("2026-09", section="activity", limit=1,
                        cursor=old["data"]["collections"]["activity"]["page"]["next_cursor"],
                        expected_version=old["snapshot_version"])
    assert error.value.code == "dashboard_snapshot_changed"
    with raw_read():
        expected = dashboard.brief("2026-09")
    assert comparable(dashboard.brief("2026-09")) == comparable(expected)


@pytest.mark.parametrize("stage", ("dashboard_materialized", "dashboard_persisted", "commit"))
def test_model_failure_rolls_back_business_evidence_epoch_receipts_and_content(model_book, stage):
    engine = model_book[0]
    before = state(engine)

    def fail(point, _connection):
        if point == stage:
            raise RuntimeError("synthetic interrupted dashboard write")

    failed = Engine(engine.store, fault=fail)
    with pytest.raises(RuntimeError, match="synthetic interrupted dashboard write"):
        failed.register_evidence(b"synthetic rollback material", "text/plain", "fixture",
                                 request_id="retry-model-write")
    assert state(engine) == before
    result = engine.register_evidence(b"synthetic rollback material", "text/plain", "fixture",
                                      request_id="retry-model-write")
    counts = receipt_counts(engine)
    assert engine.register_evidence(
        b"synthetic rollback material", "text/plain", "fixture", request_id="retry-model-write",
    ) == result
    assert receipt_counts(engine) == counts


def test_producer_closed_connection_keeps_original_failure_and_rolls_back(model_book, monkeypatch):
    from ai_accounting.kernel import dashboard_model_materializer

    engine = model_book[0]
    before = state(engine)
    original = RuntimeError("synthetic producer closed its connection")

    def fail(_dashboard, _period):
        _active_fact_reads.get().connection.close()
        raise original

    with monkeypatch.context() as scope:
        scope.setattr(dashboard_model_materializer, "build_model", fail)
        with pytest.raises(RuntimeError) as error:
            Entities(engine).update_entity_profile(
                "bank-a", {"display_name": "synthetic producer update"},
                source="synthetic owner update", expected_revision=1,
                request_id="retry-closed-producer",
            )
    assert error.value is original
    assert state(engine) == before
    assert _active_fact_reads.get() is None
    assert not _producing.get()
    result = Entities(engine).update_entity_profile(
        "bank-a", {"display_name": "synthetic producer update"},
        source="synthetic owner update", expected_revision=1,
        request_id="retry-closed-producer",
    )
    assert result["revision"] == 2


def test_explicit_model_bootstrap_replays_without_another_internal_receipt(model_book):
    engine = model_book[0]
    maintenance = Maintenance(engine)
    before = receipt_counts(engine)
    first = maintenance.rebuild_dashboard_read_model("2026-09", request_id="bootstrap-model")
    assert maintenance.rebuild_dashboard_read_model(
        "2026-09", request_id="bootstrap-model",
    ) == first
    assert receipt_counts(engine) == before


def test_internal_receipt_prefix_is_reserved_for_kernel_writes(book):
    engine = book[0]
    before = state(engine)
    with pytest.raises(KernelError) as error:
        engine.register_evidence(b"synthetic rejected material", "text/plain", "fixture",
                                 request_id=INTERNAL_REQUEST_PREFIX + "external")
    assert error.value.code == "invalid_request_id"
    assert state(engine) == before


@pytest.mark.parametrize("operation", (
    lambda connection: connection.execute("UPDATE state SET management=management+1"),
    lambda connection: connection.commit(),
    lambda connection: connection.execute("COMMIT"),
    lambda connection: connection.execute("SAVEPOINT unauthorized"),
    lambda connection: connection.execute("PRAGMA query_only=OFF"),
))
def test_production_view_rejects_mutation_and_restores_its_writer(book, operation):
    engine = book[0]
    with engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        before = engine.store.epochs(connection)
        with production_reads(engine, connection):
            with pytest.raises(sqlite3.DatabaseError):
                operation(connection)
            assert connection.in_transaction
            assert engine.store.epochs(connection) == before
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 0
        assert _active_fact_reads.get() is None
        assert not _producing.get()
        connection.execute("UPDATE state SET management=management+1 WHERE id=1")
        assert engine.store.epochs(connection)["management"] == before["management"] + 1
        connection.rollback()


def test_failed_production_proof_cannot_be_reused_after_writer_changes_its_source(book):
    engine = book[0]
    _publish_filter_funding(engine, ["bank-a"])
    with engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        fact = connection.execute(
            "SELECT fact_id FROM fact_current LIMIT 1",
        ).fetchone()[0]
        with pytest.raises(RuntimeError, match="synthetic read failure"):
            with production_reads(engine, connection) as reads:
                reads.verify_fact_versions((fact,))
                assert reads.fact(fact)["id"] == fact
                raise RuntimeError("synthetic read failure")
        assert _active_fact_reads.get() is None
        assert not _producing.get()
        trigger = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name='immutable_fact_revision_UPDATE'",
        ).fetchone()[0]
        connection.execute("DROP TRIGGER immutable_fact_revision_UPDATE")
        connection.execute("UPDATE fact_revision SET digest=zeroblob(32) WHERE id=?", (fact,))
        connection.execute(trigger)
        with production_reads(engine, connection) as fresh:
            with pytest.raises(KernelError) as error:
                fresh.verify_fact_versions((fact,))
            assert error.value.code == "content_integrity_failed"
        connection.rollback()
