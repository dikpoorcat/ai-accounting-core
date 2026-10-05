"""Real dashboard pages over one layered synthetic book, with payload/read probes."""

from __future__ import annotations

import hashlib
import itertools
import json
import sqlite3
import weakref
from contextlib import contextmanager
from types import MappingProxyType, SimpleNamespace

import pytest
from entity_fixture import seed_registration_entities
from payroll_plan_fixture import confirm_wage_inputs
from test_integrity_content import damage
from test_payroll import contribution_policy, income_tax_policy, opening, payroll, profile

import ai_accounting.kernel.dashboard as dashboard_module
from ai_accounting.kernel.asset_batches import AssetBatches
from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_pages import seal_page
from ai_accounting.kernel.dashboard_reads import Journal
from ai_accounting.kernel.domains.opening import CATEGORIES
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.permissions import create_private_file
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.read_indexes import sync_close, sync_job
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.types import YearMonth, canonical


@pytest.fixture(scope="module")
def layered_book(tmp_path_factory):
    path = tmp_path_factory.mktemp("dashboard-bounded") / "company.sqlite"
    engine = Engine(Store.create(path, production_bundle(), "bounded", "911100000000000001", "db"))
    proof = engine.register_evidence(
        b"Synthetic layered dashboard read fixture", "text/plain", "proof", request_id="proof"
    )["digest"]
    requests = itertools.count()
    facts, calculations = {}, {}

    def save(kind, subject, data, revision=0):
        seed_registration_entities(engine, kind, data)
        result = engine.save_fact(
            kind,
            subject,
            data,
            evidence=(proof,),
            expected_revision=revision,
            request_id=f"save-{next(requests)}",
        )
        facts[subject] = result["fact_id"]
        return result

    def publish(*subjects):
        preview = engine.preview(list(subjects))
        result = engine.confirm(
            list(subjects),
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id=f"publish-{next(requests)}",
        )
        calculations.update(
            {item["subject_id"]: item["calculation_id"] for item in result["results"]}
        )

    legacy = [
        ("opening_cash", "legacy-cash", {"cash_account_id": "cash", "balance_fen": 20000}),
        (
            "opening_payroll_payable",
            "legacy-wage",
            {
                "employee_id": "legacy-employee",
                "recipient_id": "legacy-employee",
                "payroll_period": "2023-12",
                "component": "net",
                "outstanding_fen": 100,
            },
        ),
        (
            "opening_asset",
            "legacy-asset",
            {
                "asset_id": "legacy-asset",
                "asset_type": "fixed",
                "cost_fen": 1000,
                "accumulated_fen": 0,
                "in_use_date": "2023-12-01",
                "useful_life_months": 120,
                "completed_months": 0,
                "residual_fen": 0,
                "benefit_area": "administration",
                "rounding_policy": "floor_final_remainder",
            },
        ),
        (
            "opening_equity",
            "legacy-equity",
            {
                "equity_kind": "paid_in_capital",
                "balance_fen": 20900,
                "holder_or_basis_id": "owner",
            },
        ),
    ]
    counts = dict.fromkeys(CATEGORIES.values(), 0)
    for kind, subject, fields in legacy:
        save(kind, subject, {"period": "2024-01", "package_id": "opening", **fields})
        counts[CATEGORIES[kind]] += 1
    save(
        "opening_package",
        "opening",
        {
            "period": "2024-01",
            "package_id": "opening",
            "counts": counts,
            "members": [{"kind": kind, "subject_id": subject} for kind, subject, _ in legacy],
            "completeness_confirmed": True,
        },
    )
    publish("opening", *(subject for _, subject, _ in legacy))
    batches = AssetBatches(engine)
    batch_options = {"evidence": (proof,), "expected_revision": 0}
    preview = batches.prepare_consumption_month("2024-01", **batch_options)
    legacy_charge = next(
        item["subject_id"]
        for item in preview["fact_changes"]
        if item["kind"] == "asset_consumption"
    )
    result = batches.confirm_consumption_month(
        "2024-01",
        **batch_options,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="consume-legacy-assets",
    )
    calculations.update({item["subject_id"]: item["calculation_id"] for item in result["results"]})
    save(
        "cash_payment",
        "legacy-payment",
        {
            "period": "2024-01",
            "actual_date": "2024-01-15",
            "cash_account_id": "cash",
            "counterparty_id": "legacy-employee",
            "direction": "outflow",
            "amount_fen": 40,
            "allocations": [
                {
                    "source_kind": "opening_payroll_payable",
                    "source_id": "legacy-wage",
                    "obligation": "primary",
                    "amount_fen": 40,
                }
            ],
        },
    )
    publish("legacy-payment")
    funding_subjects = set()
    for index in range(48):
        month = str(YearMonth.from_ordinal(YearMonth("2024-02").ordinal + index % 23))
        subject = f"history-funding-{index:03}"
        save(
            "cash_funding",
            subject,
            {
                "period": month,
                "actual_date": month + "-01",
                "cash_account_id": "cash",
                "owner_id": "owner",
                "amount_fen": 100 + index,
                "funding_kind": "capital",
            },
        )
        funding_subjects.add(subject)
    publish(*sorted(funding_subjects))
    for fact, subject in (
        (contribution_policy(), "contributions"),
        (income_tax_policy(), "income-tax"),
    ):
        save(fact.kind, subject, fact.model_dump(mode="json"))
    for index in range(4):
        employee = f"employee-{index}"
        for fact, subject in (
            (profile(employee_id=employee), f"profile-{index}"),
            (opening(employee_id=employee), f"tax-opening-{index}"),
            (payroll(employee_id=employee, profile_id=f"profile-{index}"), f"wage-{index}"),
        ):
            save(fact.kind, subject, fact.model_dump(mode="json"))
        save(
            "asset",
            f"asset-{index}",
            {
                "period": "2026-01",
                "asset_id": f"asset-{index}",
                "asset_type": "fixed",
                "supplier_id": f"supplier-{index}",
                "acquisition_date": "2026-01-02",
                "cost_fen": 1000,
                "acquisition_basis": "direct_purchase",
            },
        )
    for index in range(4):
        confirm_wage_inputs(
            engine, f"wage-{index}", evidence=(proof,), request_id=f"confirm-wage-{index}"
        )
    publish(*(f"wage-{index}" for index in range(4)), *(f"asset-{index}" for index in range(4)))
    for revision in range(1, 4):
        save(
            "asset",
            "asset-0",
            {
                "period": "2026-01",
                "asset_id": "asset-0",
                "asset_type": "fixed",
                "supplier_id": "supplier-0",
                "acquisition_date": "2026-01-02",
                "cost_fen": 1000,
                "acquisition_basis": "direct_purchase",
            },
            revision=revision,
        )
    publish("asset-0")
    payload = canonical(
        {
            "plan": {
                "report_fact_ids": [facts["asset-0"]],
                "source_closes": [],
                "period": {"quarter_start": "2026-01-01", "quarter_end": "2026-03-31"},
            }
        }
    )
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        for index in range(3):
            connection.execute(
                "INSERT INTO jobs(id,kind,payload,status) VALUES(?,'report_export',?,'pending')",
                (f"job-{index}", payload),
            )
            sync_job(connection, f"job-{index}")
        connection.commit()
    return {
        "engine": engine,
        "path": path,
        "facts": facts,
        "calculations": calculations,
        "funding_subjects": funding_subjects,
        "legacy_charge": legacy_charge,
    }


@pytest.fixture
def read_probe(layered_book, monkeypatch):
    engine = layered_book["engine"]
    snapshots, transactions = [], []
    original_snapshot, original_connection = dashboard_module._Snapshot, engine.store.connection
    original_release = original_snapshot.release
    records_by_id = {}
    original_loads = json.loads
    with original_connection(read_only=True) as connection:
        unrelated_outcomes = {
            row[0]
            for row in connection.execute(
                "SELECT outcome FROM calculation "
                "WHERE subject_id IN (SELECT value FROM json_each(?))",
                (json.dumps(sorted(layered_book["funding_subjects"])),),
            )
        }
    decoded_unrelated = []

    def loads(value, *args, **kwargs):
        if isinstance(value, str) and value in unrelated_outcomes:
            decoded_unrelated.append(value)
        return original_loads(value, *args, **kwargs)

    def snapshot(*args):
        result = original_snapshot(*args)
        record = SimpleNamespace(source_metadata={}, loaded_subjects={})
        snapshots.append(record)
        records_by_id[id(result)] = record
        return result

    def release(captured):
        reads = captured.reads
        record = records_by_id.pop(id(captured))
        record.source_metadata = MappingProxyType(dict(captured.source_metadata))
        record.loaded_subjects = MappingProxyType(
            {
                "calculations": frozenset(
                    row["subject_id"] for row in reads._calculations.values()
                ),
                "facts": frozenset(version.subject_id for version in reads._fact_versions.values()),
                "metadata": frozenset(row["subject_id"] for row in reads._metadata.values()),
            }
        )
        original_release(captured)

    @contextmanager
    def connection(*, read_only=False):
        with original_connection(read_only=read_only) as opened:
            statements = []
            if read_only:
                transactions.append(statements)
                opened.set_trace_callback(statements.append)
            yield opened

    monkeypatch.setattr(dashboard_module, "_Snapshot", snapshot)
    monkeypatch.setattr(original_snapshot, "release", release)
    monkeypatch.setattr(engine.store, "connection", connection)
    monkeypatch.setattr(json, "loads", loads)
    return snapshots, transactions, decoded_unrelated


@pytest.mark.parametrize("endpoint", ["brief", "funds", "employees", "assets"])
def test_dashboard_does_not_decode_unrelated_funding_history(layered_book, read_probe, endpoint):
    response = getattr(Dashboard(layered_book["engine"]), endpoint)("2026-01", limit=1)
    snapshots, transactions, decoded_unrelated = read_probe
    loaded_subjects = snapshots[-1].loaded_subjects
    excluded = layered_book["funding_subjects"]
    assert any(loaded_subjects.values())
    assert all(not excluded.intersection(subjects) for subjects in loaded_subjects.values())
    assert decoded_unrelated == []
    assert response["data"] is not None
    assert len(transactions) == 1
    assert sum(statement == "BEGIN" for statement in transactions[0]) == 1


@pytest.mark.parametrize(
    ("endpoint", "section", "identity", "count"),
    [
        ("employees", "employees", "employee_id", 5),
        ("assets", "assets", "asset_id", 5),
    ],
)
def test_entity_page_counts_and_next_page_do_not_expand_other_source_rows(
    layered_book, read_probe, endpoint, section, identity, count
):
    dashboard = Dashboard(layered_book["engine"])
    first = getattr(dashboard, endpoint)("2026-01", section=section, limit=1)
    collection = first["data"]["collections"][section]
    assert collection["page"]["total_count"] == collection["page"]["filtered_count"] == count
    items = collection["items"]
    assert len(items) == 1
    assert collection["page"]["returned_count"] == 1
    assert collection["page"]["has_more"]
    second = getattr(dashboard, endpoint)(
        "2026-01",
        section=section,
        limit=1,
        cursor=collection["page"]["next_cursor"],
        expected_version=first["snapshot_version"],
    )
    next_items = second["data"]["collections"][section]["items"]
    assert next_items[0][identity] != items[0][identity]
    first_snapshot = read_probe[0][0]
    source_ids = {key[1] for key in first_snapshot.source_metadata if key[0] == "fact"}
    offpage = {
        layered_book["facts"][f"wage-{i}" if endpoint == "employees" else f"asset-{i}"]
        for i in range(1, 4)
    }
    assert not source_ids.intersection(offpage)
    offpage_subjects = {
        f"wage-{i}" if endpoint == "employees" else f"asset-{i}" for i in range(1, 4)
    }
    assert not first_snapshot.loaded_subjects["calculations"].intersection(offpage_subjects)


def test_business_and_source_collections_keep_month_and_entity_scope(layered_book, read_probe):
    dashboard = Dashboard(layered_book["engine"])
    businesses = dashboard.brief("2026-01", section="activity", limit=1)
    collection = businesses["data"]["collections"]["activity"]
    assert collection["page"]["total_count"] == 8
    assert len(collection["items"]) == 1
    sources = technical_collection(dashboard, "source_history", subjects={"asset-0"}, limit=1)
    page = sources["data"]["collections"]["source_history"]
    assert page["page"]["total_count"] == 4
    assert len(page["items"]) == 1
    assert page["items"][0]["subject_id"] == "asset-0"
    for snapshot in read_probe[0]:
        assert not layered_book["funding_subjects"].intersection(
            snapshot.loaded_subjects["calculations"]
        )


def technical_collection(
    dashboard, section, *, subjects=None, limit=1, cursor=None, expected_version=None
):
    """Retained private adapter proves collection binding outside owner endpoints."""
    with dashboard._snapshot("2026-01") as snapshot:
        dashboard._check_page_version(snapshot, cursor, expected_version)
        collection = dashboard._business_collection(
            snapshot,
            "ai-business",
            subjects,
            section,
            cursor,
            {},
            limit,
        )
        collection["page"] = seal_page(snapshot, "ai-business", section, collection["page"], {})
        return {
            "snapshot_version": snapshot.snapshot_version,
            "data": {"collections": {section: collection}},
        }


def _copy_book(layered_book, tmp_path):
    path = tmp_path / "copy.sqlite"
    create_private_file(path)
    with (
        layered_book["engine"].store.connection(read_only=True) as source,
        sqlite3.connect(path) as target,
    ):
        source.backup(target)
    return Engine(Store(path, production_bundle(), "bounded", "db"))


@pytest.mark.parametrize("endpoint", ["brief", "funds", "employees", "assets"])
def test_dashboard_page_releases_snapshot_without_cyclic_collection(
    layered_book, monkeypatch, endpoint
):
    snapshots = []
    reads = []
    original_snapshot = dashboard_module._Snapshot.__init__
    original_reads = QueryReads.__init__

    def capture_snapshot(self, *args, **kwargs):
        original_snapshot(self, *args, **kwargs)
        snapshots.append(weakref.ref(self))

    def capture_reads(self, *args, **kwargs):
        original_reads(self, *args, **kwargs)
        reads.append(weakref.ref(self))

    monkeypatch.setattr(dashboard_module._Snapshot, "__init__", capture_snapshot)
    monkeypatch.setattr(QueryReads, "__init__", capture_reads)
    result = getattr(Dashboard(layered_book["engine"]), endpoint)("2026-01", preparation="deferred")

    assert result["data"]
    assert canonical(result)
    assert snapshots and all(reference() is None for reference in snapshots)
    assert reads and all(reference() is None for reference in reads)


def test_dashboard_snapshot_releases_on_failure_and_nested_exit(layered_book):
    dashboard = Dashboard(layered_book["engine"])
    with pytest.raises(RuntimeError, match="synthetic failure"):
        with dashboard._snapshot("2026-01") as failed:
            failed_ref = weakref.ref(failed)
            raise RuntimeError("synthetic failure")
    del failed
    assert failed_ref() is None

    with dashboard._snapshot("2026-01") as outer:
        outer_ref = weakref.ref(outer)
        with dashboard._snapshot("2026-01") as inner:
            inner_ref = weakref.ref(inner)
            assert outer_ref() is outer
        del inner
        assert inner_ref() is None
        assert outer_ref() is outer
    del outer
    assert outer_ref() is None


def test_default_employee_list_does_not_build_discarded_payroll_history(layered_book, monkeypatch):
    import ai_accounting.kernel.dashboard as dashboard_module

    original = dashboard_module._people_asset_settlement
    original_selected = dashboard_module.Calculations.selected
    wage_sources = []
    selected_scopes = []

    def counted(snap, calc, **options):
        if calc["kind"] in dashboard_module.PAYROLL_KINDS | {"opening_payroll_payable"}:
            wage_sources.append(calc["subject_id"])
        return original(snap, calc, **options)

    monkeypatch.setattr(dashboard_module, "_people_asset_settlement", counted)

    def scoped(self, *, kinds=None, subjects=None, posting_period=None):
        if kinds and kinds.intersection(
            dashboard_module.PAYROLL_KINDS | {"opening_payroll_payable"}
        ):
            selected_scopes.append(None if subjects is None else set(subjects))
        return original_selected(
            self, kinds=kinds, subjects=subjects, posting_period=posting_period
        )

    monkeypatch.setattr(dashboard_module.Calculations, "selected", scoped)
    dashboard = Dashboard(layered_book["engine"])
    default = dashboard.employees("2026-01", preparation="deferred")
    assert default["data"]["collections"]["employees"]["page"]["total_count"] == 5
    assert "payroll_sources" not in default["data"]["collections"]
    assert wage_sources == []
    assert all(scope is not None for scope in selected_scopes)
    focused = dashboard.employees(
        "2026-01",
        employee_id="employee-0",
        section="employees",
        preparation="deferred",
    )
    focused_items = focused["data"]["collections"]["employees"]["items"]
    assert [item["employee_id"] for item in focused_items] == ["employee-0"]
    assert "payroll_sources" not in focused["data"]["collections"]
    assert wage_sources == []
    assert focused["data"]["employees"] == default["data"]["employees"]
    # Full month-end unpaid totals still prove all representative sources;
    # neither the list nor a precise employee lookup builds history cards.
    assert all(
        scope <= {"legacy-wage", *(f"wage-{index}" for index in range(4))}
        for scope in selected_scopes
    )


def test_employee_list_rejects_damaged_wage_identity_source(layered_book, tmp_path):
    engine = _copy_book(layered_book, tmp_path)
    with engine.store.connection() as connection:
        trigger = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='immutable_fact_payroll_UPDATE'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER immutable_fact_payroll_UPDATE")
        connection.execute(
            "UPDATE fact_payroll SET employee_id='phantom-employee' WHERE revision_id=?",
            (layered_book["facts"]["wage-0"],),
        )
        connection.execute(trigger)
    with pytest.raises(KernelError) as failure:
        Dashboard(engine).employees("2026-01", preparation="deferred")
    assert failure.value.code == "content_integrity_failed"


def test_employee_list_uses_verified_roles_before_loading_wage_scalars(layered_book, monkeypatch):
    import ai_accounting.kernel.entity_references as references

    engine = layered_book["engine"]
    original_scalar = dashboard_module.scalar_facts
    original_roles = references.current_role_matches
    scalar_calls = []

    def measured_scalar(snap, calculations):
        calculations = list(calculations)
        scalar_calls.append({item["fact_id"] for item in calculations})
        return original_scalar(snap, calculations)

    monkeypatch.setattr(dashboard_module, "scalar_facts", measured_scalar)
    dashboard = Dashboard(engine)
    ordinary = dashboard.employees("2026-01", preparation="deferred")
    assert ordinary["data"]["collections"]["employees"]["page"]["total_count"] == 5
    all_wage_facts = {layered_book["facts"][f"wage-{index}"] for index in range(4)}
    all_wage_facts.add(layered_book["facts"]["legacy-wage"])
    assert not any(all_wage_facts <= call for call in scalar_calls), [
        sorted(call) for call in scalar_calls
    ]
    assert all(call <= {layered_book["facts"]["legacy-wage"]} for call in scalar_calls)

    # A registry without the employee role still uses the original source
    # scalar for that fact, after the ordinary role verifier succeeds.
    scalar_calls.clear()

    def roles_without_one(connection, fact_ids, role, *, registry=None):
        result = original_roles(connection, fact_ids, role, registry=registry)
        result.pop(layered_book["facts"]["legacy-wage"], None)
        return result

    monkeypatch.setattr(references, "current_role_matches", roles_without_one)
    fallback = dashboard.employees("2026-01", preparation="deferred")
    assert fallback == ordinary
    assert any(layered_book["facts"]["legacy-wage"] in call for call in scalar_calls)


@pytest.mark.parametrize(
    "statement",
    (
        "DELETE FROM entity_reference_current WHERE fact_id=? AND role='employee'",
        "UPDATE entity_reference_current SET source_digest=zeroblob(32) "
        "WHERE fact_id=? AND role='employee'",
    ),
)
def test_employee_list_rejects_damaged_wage_role_instead_of_scalar_fallback(
    layered_book, tmp_path, statement
):
    engine = _copy_book(layered_book, tmp_path)
    fact_id = layered_book["facts"]["wage-0"]
    damage(
        engine,
        "entity_reference_current",
        statement,
        (fact_id,),
    )
    with pytest.raises(KernelError) as failure:
        Dashboard(engine).employees("2026-01", preparation="deferred")
    assert failure.value.code == "entity_reference_corrupt"


def test_asset_list_bounds_selected_batch_members(layered_book, monkeypatch):
    original = BusinessQueries._selected_asset_members
    scopes = []

    def counted(
        self,
        connection,
        period,
        *,
        kinds=None,
        subjects=None,
        asset_ids=None,
        current_heads=False,
    ):
        scopes.append((kinds, subjects, asset_ids))
        return original(
            self,
            connection,
            period,
            kinds=kinds,
            subjects=subjects,
            asset_ids=asset_ids,
            current_heads=current_heads,
        )

    monkeypatch.setattr(BusinessQueries, "_selected_asset_members", counted)
    result = Dashboard(layered_book["engine"]).assets("2026-01", preparation="deferred")
    assert result["data"]["collections"]["assets"]["items"]
    assert not any(
        kinds is None and subjects is None and asset_ids is None
        for kinds, subjects, asset_ids in scopes
    )
    assert not any(kinds and "asset_consumption" in kinds for kinds, _, _ in scopes)


def test_asset_page_batches_selected_card_facts(layered_book, monkeypatch):
    selected = {layered_book["facts"][f"asset-{index}"] for index in range(4)}
    original = QueryReads.facts
    requested = []

    def counted(self, identifiers):
        identifiers = set(identifiers)
        if identifiers & selected:
            requested.append(identifiers & selected)
        return original(self, identifiers)

    monkeypatch.setattr(QueryReads, "facts", counted)
    result = Dashboard(layered_book["engine"]).assets(
        "2026-01", section="assets", limit=4, preparation="deferred"
    )
    assert result["data"]["collections"]["assets"]["page"]["returned_count"] == 4
    assert selected in requested


def test_asset_page_stops_reference_only_source_voucher_reads(layered_book, monkeypatch):
    original = Journal.__iter__
    source_scopes = []

    def counted(self):
        if self.subjects is not None:
            source_scopes.append(frozenset(self.subjects))
        yield from original(self)

    monkeypatch.setattr(Journal, "__iter__", counted)
    result = Dashboard(layered_book["engine"]).assets(
        "2026-01", section="assets", limit=4, preparation="deferred"
    )
    assert result["data"]["collections"]["assets"]["page"]["returned_count"] == 4
    assert source_scopes == []
    assert {item["asset_id"] for item in result["data"]["collections"]["assets"]["items"]} == {
        f"asset-{index}" for index in range(4)
    }


def test_asset_list_rejects_damaged_selected_card_source(layered_book, tmp_path):
    engine = _copy_book(layered_book, tmp_path)
    with engine.store.connection() as connection:
        trigger = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='immutable_fact_asset_UPDATE'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER immutable_fact_asset_UPDATE")
        connection.execute(
            "UPDATE fact_asset SET asset_id='phantom-asset' WHERE revision_id=?",
            (layered_book["facts"]["asset-0"],),
        )
        connection.execute(trigger)
    with pytest.raises(KernelError) as failure:
        Dashboard(engine).assets("2026-01", asset_id="asset-0", preparation="deferred")
    assert failure.value.code == "content_integrity_failed"


def test_worker_change_rejects_file_cursor_without_changing_business_epochs(layered_book, tmp_path):
    engine = _copy_book(layered_book, tmp_path)
    dashboard = Dashboard(engine)
    first = technical_collection(dashboard, "file_jobs", limit=1)
    collection = first["data"]["collections"]["file_jobs"]
    assert collection["page"]["total_count"] == 3
    with engine.store.connection() as connection:
        connection.execute("UPDATE jobs SET status='running',attempts=1 WHERE id='job-2'")
    with pytest.raises(KernelError, match="筛选|分页") as error:
        technical_collection(
            dashboard,
            "file_jobs",
            limit=1,
            cursor=collection["page"]["next_cursor"],
            expected_version=first["snapshot_version"],
        )
    assert error.value.code == "dashboard_snapshot_changed"
    fresh = technical_collection(dashboard, "file_jobs", limit=1)
    assert fresh["snapshot_version"] == first["snapshot_version"]
    assert (
        fresh["data"]["collections"]["file_jobs"]["page"]["collection_version"]
        != collection["page"]["collection_version"]
    )


def test_worker_write_during_response_keeps_all_file_reads_on_one_snapshot(
    layered_book, tmp_path, monkeypatch
):
    engine = _copy_book(layered_book, tmp_path)
    original = BusinessQueries._file_jobs
    changed = []

    def file_jobs(queries, connection, *args, **kwargs):
        result = original(queries, connection, *args, **kwargs)
        assert connection.in_transaction
        if not changed:
            with engine.store.connection() as writer:
                writer.execute("UPDATE jobs SET status='running',attempts=1 WHERE id='job-2'")
            changed.append(True)
        return result

    monkeypatch.setattr(BusinessQueries, "_file_jobs", file_jobs)
    first = technical_collection(Dashboard(engine), "file_jobs", limit=3)
    jobs = first["data"]["collections"]["file_jobs"]["items"]
    assert next(item for item in jobs if item["job_id"] == "job-2")["status"] == "pending"
    second = technical_collection(Dashboard(engine), "file_jobs", limit=3)
    jobs = second["data"]["collections"]["file_jobs"]["items"]
    assert next(item for item in jobs if item["job_id"] == "job-2")["status"] == "running"


def test_old_close_manifest_without_direct_adoptions_is_rejected(layered_book, tmp_path):
    engine = _copy_book(layered_book, tmp_path)
    subjects = {
        "opening",
        "legacy-wage",
        "legacy-asset",
        layered_book["legacy_charge"],
        "legacy-payment",
    }
    calculations = {layered_book["calculations"][subject] for subject in subjects}
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        vouchers = [
            dict(row)
            for row in connection.execute(
                "SELECT v.id,v.calculation_id FROM voucher_version v WHERE v.calculation_id IN "
                "(SELECT value FROM json_each(?))",
                (json.dumps(sorted(calculations)),),
            )
        ]
        manifest = canonical(
            {"period": "2024-01", "calculations": sorted(calculations), "vouchers": vouchers}
        )
        connection.execute(
            "INSERT INTO period_close(period,manifest,digest) VALUES(?,?,?)",
            (
                YearMonth("2024-01").ordinal,
                manifest,
                hashlib.sha256(manifest.encode()).digest(),
            ),
        )
        with pytest.raises(KernelError) as failure:
            sync_close(connection, YearMonth("2024-01").ordinal)
        assert failure.value.code == "content_integrity_failed"
