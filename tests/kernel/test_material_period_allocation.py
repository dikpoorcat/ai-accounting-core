"""A preserved multi-month original is partitioned before monthly accounting coverage."""

import json

import pytest
from test_materials import Company, codes, csv_spec

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.materials import (
    MaterialPeriodAllocation,
    MaterialSource,
    PeriodAllocationEntry,
    Specification,
)
from ai_accounting.kernel.types import canonical, digest


@pytest.fixture
def company(tmp_path):
    return Company(tmp_path)


def assignment(company, first=2, last=2, period="2026-02", **changes):
    return PeriodAllocationEntry(
        sheet="CSV",
        column="B",
        first_row=first,
        last_row=last,
        recognition_period=period,
        basis="confirmed_period",
        basis_evidence_digest=company.proof,
        basis_location="负责人确认第1段",
        basis_excerpt="费用由公司在二月审批承担",
        **changes,
    )


def allocate(company, entries, source_id="source"):
    preview = company.materials.preview_period_allocation(source_id, entries)
    assert preview["status"] == "ready", preview
    result = company.materials.confirm_period_allocation(
        source_id,
        entries,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        expected_revision=preview["expected_revision"],
        request_id=company.request(),
    )
    return preview, result


def test_multimonth_original_closes_month_without_future_results(company):
    source, evidence = company.source(
        b"name,amount,period\njan,10,2026-01\nfeb,20,2026-02\nmar,30,2026-03\n"
    )
    company.resolve(source, "CSV!B2", [company.expense("jan", 1000)])
    january = company.materials.check("2026-01")
    assert january["status"] == "complete"
    assert january["file_status"] == "needs_information"
    assert january["file_summaries"][0]["unprocessed_count"] == 2
    assert january["coverage_count"] == 1
    assert source["allocation_fact_id"] in january["allocation_versions"]
    assert set(january["allocation_versions"]) <= set(january["fact_ids"])
    assert company.materials.check("2026-02")["coverage"][0]["location"] == "CSV!B3"
    assert company.materials.check("2026-03")["coverage"][0]["location"] == "CSV!B4"
    with company.engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT content FROM evidence WHERE digest=?", (bytes.fromhex(evidence),)
            )
            .fetchone()[0]
            .startswith(b"name")
        )


def test_unknown_period_is_global_and_personal_payment_date_is_not_a_default(company):
    spec = csv_spec()
    spec["columns"][2]["role"] = "context"
    source, _ = company.source(b"name,amount,personal_payment_day\na,10,2026-01-20\n", spec=spec)
    company.resolve(
        source,
        "CSV!B2",
        treatment="no_accounting",
        reason="等待公司审批",
        non_accounting_reason="forecast",
        recognition_period="2026-02",
    )
    for period in ("2025-12", "2026-01", "2026-02", "2027-12"):
        assert "material_period_unknown" in codes(company.materials.check(period))
    preview, result = allocate(company, (assignment(company),))
    assert preview["period_counts"] == {"2026-02": 1}
    assert result["revision"] == 2  # normal revision, never recording-error amendment
    assert company.materials.preview_period_allocation("source")["period_counts"] == {"2026-02": 1}
    assert company.materials.check("2026-01")["status"] == "complete"
    assert "material_period_unknown" not in codes(company.materials.check("2026-02"))


def test_original_period_conflict_and_unregistered_confirmation_cannot_be_published(company):
    company.source(b"name,amount,period\na,10,2026-01\n")
    entries = (assignment(company),)
    preview = company.materials.preview_period_allocation("source", entries)
    assert "material_allocation_period_conflict" in codes(preview)
    with pytest.raises(KernelError) as rejected:
        company.materials.confirm_period_allocation(
            "source",
            entries,
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            expected_revision=1,
            request_id=company.request(),
        )
    assert rejected.value.code == "needs_information"
    missing = entries[0].model_copy(
        update={"recognition_period": "2026-01", "basis_evidence_digest": "a" * 64}
    )
    assert "material_allocation_evidence_missing" in codes(
        company.materials.preview_period_allocation("source", (missing,))
    )


def test_public_wire_assignments_and_idempotency_with_changed_material_epoch(company):
    company.source(b"name,amount,period\na,10,\n")
    entries = [assignment(company).model_dump(mode="json")]
    preview = company.materials.preview_period_allocation("source", entries)
    kwargs = dict(
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        expected_revision=1,
        request_id=company.request(),
    )
    first = company.materials.confirm_period_allocation("source", entries, **kwargs)
    company.evidence(b"later confirmation")
    assert company.materials.confirm_period_allocation("source", entries, **kwargs) == first
    assert first["revision"] == 2


def test_late_assignment_into_previously_empty_month_changes_material_epoch(company):
    assert company.materials.check("2026-02")["status"] == "complete"
    company.source(b"name,amount,period\na,10,\n")
    entries = (assignment(company),)
    old = company.materials.preview_period_allocation("source", entries)
    company.evidence(b"another actual basis")
    with pytest.raises(KernelError) as rejected:
        company.materials.confirm_period_allocation(
            "source",
            entries,
            preview_digest=old["digest"],
            epochs=old["epochs"],
            expected_revision=1,
            request_id=company.request(),
        )
    assert rejected.value.code == "preview_expired"
    allocate(company, entries)
    assert company.materials.check("2026-02")["coverage_count"] == 1


def test_receive_source_and_partition_are_one_transaction(company):
    raw = b"name,amount,period\na,10,2026-01\n"
    evidence = company.evidence(raw)
    before = company.materials.check("2026-01")
    company.engine.fault = lambda stage, connection: (
        (_ for _ in ()).throw(RuntimeError("synthetic commit failure"))
        if stage == "commit"
        else None
    )
    with pytest.raises(RuntimeError):
        company.materials.receive(
            "source",
            dict(
                period="2026-01",
                evidence_digest=evidence,
                category="bank",
                purpose="business",
                specification=csv_spec(),
            ),
            evidence=(evidence,),
            expected_revision=0,
            request_id=company.request(),
        )
    company.engine.fault = lambda *_: None
    assert company.materials.check("2026-01")["coverage_digest"] == before["coverage_digest"]
    with company.engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM subject").fetchone()[0] == 0


def test_generic_allocation_registration_is_closed_and_stored_partition_is_rechecked(company):
    source, evidence = company.source(b"name,amount,period\na,10,2026-01\n")
    fake = MaterialPeriodAllocation(
        period="2026-01",
        source_id="source",
        source_fact_id=source["fact_id"],
        entries=(assignment(company),),
    )
    with pytest.raises(KernelError) as rejected:
        company.engine.save_fact(
            fake.kind,
            "invented",
            fake.model_dump(mode="json"),
            evidence=(evidence,),
            expected_revision=0,
            request_id=company.request(),
        )
    assert rejected.value.code == "registration_command_required"
    # A deliberately injected internal bug still cannot establish valid coverage.
    request_hash, operation = company.engine._registration(
        False,
        fake.kind,
        "invented",
        fake.model_dump(mode="json"),
        evidence=(evidence,),
        expected_revision=0,
    )
    company.engine._write(
        company.request(), request_hash, None, ("material",), "synthetic", operation
    )
    assert "material_allocation_source_changed" in codes(company.materials.check("2026-01"))


def test_source_revision_invalidates_old_maps_and_retired_source_does_not_block_future(company):
    source, evidence = company.source(b"name,amount,period\na,10,2026-02\n")
    with company.engine.store.connection(read_only=True) as connection:
        version = company.engine.store.current_fact(connection, "source")
    updated = version.fact.model_dump(mode="json")
    updated["specification"]["columns"][2]["role"] = "context"
    company.engine.save_fact(
        MaterialSource.kind,
        "source",
        updated,
        evidence=(evidence,),
        expected_revision=1,
        request_id=company.request(),
    )
    assert "material_allocation_required" in codes(company.materials.check("2026-03"))
    # The generic withdrawal keeps historical versions but removes its current obligation.
    preview = company.engine.preview_delete("source")
    company.engine.delete(
        "source",
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id=company.request(),
    )
    assert company.materials.check("2026-02")["status"] == "complete"
    assert (
        source["allocation_fact_id"]
        not in company.materials.check("2026-02")["allocation_versions"]
    )


def test_v2_source_dump_hash_and_existing_disposition_remain_readable(company):
    old_spec = {
        "format": "csv",
        "columns": [
            dict(column="A", role="context", label=""),
            dict(column="B", role="amount", label=""),
        ],
        "sheet_columns": {},
        "header_rows": {},
        "total_rows": {},
        "passages": [],
        "all_pages_reviewed": None,
    }
    assert (
        Specification.model_validate_json(json.dumps(old_spec)).model_dump(mode="json") == old_spec
    )
    evidence = company.evidence(b"name,amount\na,10\n")
    old_payload = dict(
        period="2026-01",
        evidence_digest=evidence,
        category="transactions",
        purpose="business",
        supporting_purpose=None,
        specification=old_spec,
    )
    source = company.engine.save_fact(
        MaterialSource.kind,
        "v2-source",
        old_payload,
        evidence=(evidence,),
        expected_revision=0,
        request_id=company.request(),
    )
    company.resolve(source, "CSV!B2", [company.expense("old-expense", 1000)])
    assert "material_allocation_required" in codes(company.materials.check("2026-01"))
    allocate(company, None, "v2-source")
    assert company.materials.check("2026-01")["status"] == "complete"
    with company.engine.store.connection(read_only=True) as connection:
        version = company.engine.store.fact(connection, source["fact_id"])
        assert canonical(version.fact.model_dump(mode="json")) == canonical(old_payload)
        assert connection.execute(
            "SELECT digest FROM fact_revision WHERE id=?", (version.id,)
        ).fetchone()[0] == digest(old_payload)


def test_closed_manifest_freezes_allocation_version_when_future_assignment_changes(company):
    from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods

    source, evidence = company.source(b"name,amount,period\njan,10,2026-01\nfuture,20,\n")
    allocate(company, (assignment(company, 3, 3),))
    company.resolve(source, "CSV!B2", [company.expense("jan", 1000)])
    periods = Periods(company.engine)
    for category in MATERIAL_CATEGORIES:
        evidence_list = [evidence] if category == "transactions" else []
        periods.inventory(
            "2026-01",
            category,
            evidence=evidence_list,
            expected=len(evidence_list),
            no_business=not evidence_list,
            confirmation_evidence=company.proof,
            request_id=company.request(),
        )
    preview = periods.preview_close("2026-01", owner_confirmation=company.proof)
    frozen_ids = preview["manifest"]["material_coverage"]["allocation_versions"]
    assert len(frozen_ids) == 1
    periods.close(
        "2026-01",
        owner_confirmation=company.proof,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id=company.request(),
    )
    frozen = periods.closed_report("2026-01")
    allocate(company, (assignment(company, 3, 3, "2026-03"),))
    assert periods.closed_report("2026-01") == frozen
    assert company.materials.check("2026-02")["coverage_count"] == 0
    assert company.materials.check("2026-03")["coverage_count"] == 1
    assert company.materials.check("2026-03")["allocation_versions"] != frozen_ids


def test_actual_v2_sqlite_source_and_resolution_survive_forward_upgrade(tmp_path, monkeypatch):
    from contextlib import closing

    from ai_accounting.kernel.contracts import FactVersion
    from ai_accounting.kernel.engine import Engine
    from ai_accounting.kernel.materials import MaterialResolution, Materials
    from ai_accounting.kernel.runtime import connect
    from ai_accounting.kernel.schema import VERSION
    from ai_accounting.kernel.service import default_registry
    from ai_accounting.kernel.storage import Store
    from ai_accounting.kernel.versions import (
        known_contracts,
        record_version,
        upgrade,
        verify_schema,
    )

    path = tmp_path / "published-v2.sqlite"
    registry = default_registry()
    store = Store(path, registry, "company", "database")
    raw = b"reference,amount\nzero-control,0\n"
    proof = __import__("hashlib").sha256(raw).hexdigest()
    source = MaterialSource.model_validate_json(
        canonical(
            dict(
                period="2026-01",
                evidence_digest=proof,
                category="transactions",
                purpose="business",
                specification={
                    "format": "csv",
                    "columns": [
                        {"column": "A", "role": "context"},
                        {"column": "B", "role": "amount"},
                    ],
                },
            )
        )
    )
    resolution = MaterialResolution(
        period="2026-01",
        source_id="old-source",
        source_fact_id="old-source-v1",
        location="CSV!B2",
        treatment="no_accounting",
        recognition_period="2026-01",
        amount_fen=0,
        reason="明确零金额控制行",
        non_accounting_reason="zero_amount",
    )
    with closing(connect(path)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        for kind in ("table", "index", "trigger"):
            for obj in known_contracts("business")[2]["objects"]:
                if obj["type"] == kind:
                    connection.execute(obj["sql"])
        connection.execute("INSERT INTO identity VALUES(1,'company','taxpayer','database',2)")
        connection.execute("INSERT INTO state VALUES(1,0,0,0,1)")
        connection.execute(
            "INSERT INTO evidence VALUES(?,?,?,?)",
            (bytes.fromhex(proof), raw, "text/csv", "old.csv"),
        )
        for model, subject, fact_id in (
            (source, "old-source", "old-source-v1"),
            (resolution, "old-resolution", "old-resolution-v1"),
        ):
            store.write_fact(
                connection,
                FactVersion(fact_id, subject, 1, model, (proof,)),
                digest(model.model_dump(mode="json")),
            )
        connection.execute("PRAGMA user_version=2")
        record_version(connection, 2)
        connection.commit()
        assert verify_schema(connection, registry=registry, allow_previous=True) == 2
        assert upgrade(connection, registry=registry)
        assert verify_schema(connection, registry=registry) == VERSION
    api = Materials(Engine(store))
    assert "material_allocation_required" in codes(api.check("2026-01"))
    preview = api.preview_period_allocation("old-source")
    assert preview["period_counts"] == {"2026-01": 1}
    registered = api.confirm_period_allocation(
        "old-source",
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        expected_revision=0,
        request_id="establish-old-source-periods",
    )
    result = api.check("2026-01")
    assert result["status"] == "complete"
    assert result["source_versions"] == ["old-source-v1"]
    assert result["resolution_versions"] == ["old-resolution-v1"]
    assert result["allocation_versions"] == [registered["fact_id"]]
    from ai_accounting.kernel import materials

    monkeypatch.setattr(
        materials, "inspect_bytes", lambda *_: pytest.fail("unrelated original parsed")
    )
    assert api.check("2026-02")["status"] == "complete"


def test_withdrawn_source_releases_capacity_and_future_overallocation_keeps_file_pending(company):
    first, _ = company.source(b"name,amount,period\na,10,2026-01\n", "first")
    second, _ = company.source(b"name,amount,period\nb,10,2026-01\n", "second")
    link = company.expense("one-expense", 1000)
    company.resolve(first, "CSV!B2", [link], subject="first-resolution")
    company.resolve(second, "CSV!B2", [link], subject="second-resolution")
    assert company.materials.check("2026-01")["file_status"] == "needs_information"
    preview = company.engine.preview_delete("first")
    company.engine.delete(
        "first",
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id=company.request(),
    )
    assert company.materials.check("2026-01")["status"] == "complete"
    future, _ = company.source(b"name,amount,period\na,20,2026-02\nb,20,2026-02\n", "future")
    feb = company.expense("feb", 2000, "2026-02")
    for row in (2, 3):
        company.resolve(
            future,
            f"CSV!B{row}",
            [feb],
            subject=f"future-resolution-{row}",
            recognition_period="2026-02",
        )
    january = company.materials.check("2026-01")
    assert january["status"] == "complete"
    assert january["file_status"] == "needs_information"
    february = company.materials.check("2026-02")
    assert "material_business_overallocated" in codes(february)
    assert february["file_status"] == "needs_information"


def test_repeated_allocation_ranges_fail_before_expanding_duplicate_diagnostics(company):
    company.source(b"name,amount,period\n" + b"a,1,\n" * 1000)
    entries = (assignment(company, 2, 1001),) * 100
    preview = company.materials.preview_period_allocation("source", entries)
    assert preview["issue_count"] == 1
    assert "material_allocation_overlap" in codes(preview)


def test_empty_business_mapping_cannot_hide_unknown_original_from_other_month(company):
    company.source(b"name,amount,period\n")
    for month in ("2026-01", "2026-02"):
        assert "material_no_business_columns" in codes(company.materials.check(month))


def test_real_service_public_command_keeps_typed_assignment_contract(tmp_path):
    from ai_accounting.kernel.materials import Materials
    from ai_accounting.kernel.service import LocalService

    service = LocalService(tmp_path / "root")
    password = "Synthetic-material-allocation-2026"
    service.security.provision("owner", password)
    token = service.security.login("owner", password).session_token
    company = service.dispatch(
        "create_company",
        {"taxpayer_id": "91310000123456789A", "name": "Synthetic material service"},
        session_token=token,
    )
    engine = service.engine(company["id"])
    evidence = engine.register_evidence(
        b"name,amount,period\na,10,\n", "text/csv", "synthetic.csv", request_id="proof"
    )["digest"]
    Materials(engine).receive(
        "source",
        dict(
            period="2026-01",
            evidence_digest=evidence,
            purpose="business",
            category="transactions",
            specification=csv_spec(),
        ),
        evidence=(evidence,),
        expected_revision=0,
        request_id="source",
    )
    payload = {
        "company_id": company["id"],
        "source_id": "source",
        "assignments": [
            {
                "sheet": "CSV",
                "column": "B",
                "first_row": 2,
                "last_row": 2,
                "recognition_period": "2026-02",
                "basis": "confirmed_period",
                "basis_evidence_digest": evidence,
                "basis_location": "负责人确认",
                "basis_excerpt": "费用于二月承担",
            }
        ],
    }
    preview = service.dispatch("preview_material_allocation", payload, session_token=token)
    result = service.dispatch(
        "confirm_material_allocation",
        payload
        | {
            "preview_digest": preview["digest"],
            "epochs": preview["epochs"],
            "expected_revision": 1,
            "request_id": "allocate",
        },
        session_token=token,
    )
    assert result["revision"] == 2
    malformed = {**payload, "assignments": [{**payload["assignments"][0], "first_row": True}]}
    with pytest.raises(KernelError) as rejected:
        service.dispatch("preview_material_allocation", malformed, session_token=token)
    assert rejected.value.code == "invalid_command"
