"""Joint evidence pools retain original members without inventing row classifications."""

import json
from io import BytesIO

import pytest
from openpyxl import Workbook
from test_deletion_boundaries import book as book
from test_local_service import service as service
from test_materials import Company, codes, csv_spec

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.materials import MaterialGroupResolution, Materials
from ai_accounting.kernel.periods import Periods

SPEC = {
    "format": "csv",
    "columns": [{"column": "A", "role": "context"}, {"column": "B", "role": "amount"}],
}


@pytest.fixture
def company(tmp_path):
    return Company(tmp_path)


def pool(company, *, extra=False):
    source, proof = company.source(
        b"name,amount\na,10\nb,20\n" + (b"unlisted,40\n" if extra else b""), spec=SPEC
    )
    links = [
        company.expense("cost-feb", 1200, "2026-02"),
        company.expense("cost-mar", 1800, "2026-03"),
    ]
    data = {
        "period": "2026-01",
        "source_id": "source",
        "source_fact_id": source["fact_id"],
        "members": [
            {"location": "CSV!B2", "amount_fen": 1000},
            {"location": "CSV!B3", "amount_fen": 2000},
        ],
        "group_amount_fen": 3000,
        "links": links,
        "joint_basis_confirmed": True,
        "basis_evidence_digest": company.proof,
        "basis_location": "核准完整组",
        "reason": "原两项共同形成二月1200分和三月1800分，无逐行分类或单项期间确认。",
    }
    return source, proof, data


def resolve(company, data, proof, *, subject="pool", revision=0, request_id=None):
    return company.materials.resolve_group(
        subject,
        data,
        evidence=(proof, company.proof),
        expected_revision=revision,
        request_id=request_id or company.request(),
    )


def withdraw(company, subject):
    preview = company.engine.preview_delete(subject, recording_error_evidence=company.proof)
    return company.engine.delete(
        subject,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        recording_error_evidence=company.proof,
        request_id=company.request(),
    )


def test_joint_pool_covers_both_months_without_individual_period_and_is_idempotent(
    company, monkeypatch
):
    _, proof, data = pool(company)
    saved = resolve(company, data, proof, request_id="one-group")
    assert resolve(company, data, proof, request_id="one-group") == saved
    for period in ("2026-02", "2026-03"):
        result = company.materials.check(period)
        assert result["status"] == "complete", result["issues"]
        assert result["group_versions"] == [saved["fact_id"]]
        assert saved["fact_id"] in result["fact_ids"]
        assert len(result["coverage"]) == 2
        for item in result["coverage"]:
            assert item["recognition_period"] is None
            assert item["joint_periods"] == ["2026-02", "2026-03"]
            assert item["group_ids"] == ["pool"]
    from ai_accounting.kernel import materials

    monkeypatch.setattr(
        materials, "inspect_bytes", lambda *_: pytest.fail("historical BLOB parsed")
    )
    assert company.materials.check("2030-09")["status"] == "complete"


def test_omitted_original_member_keeps_full_file_and_month_unresolved(company):
    _, proof, data = pool(company, extra=True)
    resolve(company, data, proof)
    assert "material_period_unknown" in codes(company.materials.check("2026-02"))
    assert "material_item_unresolved" in codes(company.materials.check("2026-03"))
    assert "material_period_unknown" in codes(company.materials.check("2030-09"))


@pytest.mark.parametrize("change", ("duplicate", "amount", "control", "offset_direction"))
def test_invalid_members_or_amounts_cannot_be_registered(company, change):
    _, proof, data = pool(company)
    if change == "duplicate":
        data["members"][1]["location"] = "CSV!B2"
    elif change == "amount":
        data["members"][0]["amount_fen"] = 1100
        data["members"][1]["amount_fen"] = 1900
    elif change == "control":
        data["members"][1]["location"] = "CSV!B999"
    else:
        data["members"][0]["amount_fen"] = -1000
        data["members"][1]["amount_fen"] = 4000
    with pytest.raises(KernelError):
        resolve(company, data, proof)
    with company.engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT 1 FROM subject WHERE id='pool'").fetchone() is None


def test_group_and_regular_allocations_share_members_and_capacity(company):
    source, proof, data = pool(company)
    resolve(company, data, proof)
    with pytest.raises(KernelError) as error:
        resolve(company, data, proof, subject="second-pool")
    assert error.value.code == "material_group_overlap"
    company.resolve(
        source, "CSV!B2", [data["links"][0] | {"amount_fen": 1000}], recognition_period="2026-02"
    )
    assert "material_group_overlap" in codes(company.materials.check("2026-02"))
    withdraw(company, "resolution-CSV!B2")
    other, _ = company.source(b"name,amount,period\na,1,2026-02\n", subject="other")
    company.resolve(
        other,
        "CSV!B2",
        [data["links"][0] | {"amount_fen": 100}],
        subject="other-resolution",
        recognition_period="2026-02",
    )
    assert "material_business_overallocated" in codes(company.materials.check("2026-02"))


def test_regular_resolution_existing_first_rejects_overlapping_pool(company):
    source, proof, data = pool(company)
    company.resolve(
        source, "CSV!B2", [data["links"][0] | {"amount_fen": 1000}], recognition_period="2026-02"
    )
    with pytest.raises(KernelError) as error:
        resolve(company, data, proof)
    assert error.value.code == "material_group_overlap"


def test_withdrawal_and_result_changes_reopen_group_members(company):
    _, proof, data = pool(company)
    resolve(company, data, proof)
    changed = company.expense("cost-feb", 1300, "2026-02", revision=1)
    assert "material_result_stale" in codes(company.materials.check("2026-02"))
    assert "material_result_stale" in codes(company.materials.check("2026-03"))
    repaired = dict(data, links=[changed | {"amount_fen": 1200}, data["links"][1]])
    resolve(company, repaired, proof, revision=1)
    assert company.materials.check("2026-02")["status"] == "complete"
    withdraw(company, "pool")
    assert "material_period_unknown" in codes(company.materials.check("2026-02"))
    assert "material_period_unknown" in codes(company.materials.check("2030-09"))
    resolve(company, repaired, proof, subject="replacement")
    assert company.materials.check("2026-03")["status"] == "complete"


def test_saved_unpublished_result_change_invalidates_all_group_months(company):
    _, proof, data = pool(company)
    resolve(company, data, proof)
    with company.engine.store.connection(read_only=True) as connection:
        old = company.engine.store.current_fact(connection, "cost-feb")
    company.engine.save_fact(
        "expense",
        "cost-feb",
        old.fact.model_dump(mode="json") | {"amount_fen": 1300},
        evidence=(company.proof,),
        expected_revision=1,
        request_id=company.request(),
    )
    for period in ("2026-02", "2026-03"):
        assert "material_result_stale" in codes(company.materials.check(period))


def test_group_late_transaction_failure_rolls_back_members_and_request_and_can_retry(company):
    _, proof, data = pool(company)
    before = company.engine.store.epochs
    with company.engine.store.connection(read_only=True) as connection:
        epochs = before(connection)

    def failed_commit(stage, connection):
        if stage == "commit":
            raise OSError("synthetic transaction commit failure")

    company.engine.fault = failed_commit
    with pytest.raises(OSError):
        resolve(company, data, proof, request_id="failed-group")
    with company.engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT 1 FROM subject WHERE id='pool'").fetchone() is None
        assert (
            connection.execute("SELECT 1 FROM request WHERE id='failed-group'").fetchone() is None
        )
        assert before(connection) == epochs
    company.engine.fault = lambda *_: None
    resolve(company, data, proof, request_id="failed-group")
    assert company.materials.check("2026-02")["status"] == "complete"


def test_public_save_cannot_bypass_group_registration_and_proof_is_required(company):
    _, proof, data = pool(company)
    with pytest.raises(KernelError) as error:
        company.engine.save_fact(
            MaterialGroupResolution.kind,
            "pool",
            data,
            evidence=(proof, company.proof),
            expected_revision=0,
            request_id=company.request(),
        )
    assert error.value.code == "registration_command_required"
    with pytest.raises(KernelError) as error:
        resolve(company, dict(data, joint_basis_confirmed=None), proof)
    assert error.value.code == "material_group_basis_missing"


def test_changed_original_version_never_uses_old_group_to_hide_unknown_rows(company):
    _, proof, data = pool(company)
    resolve(company, data, proof)
    with company.engine.store.connection(read_only=True) as connection:
        old = company.engine.store.current_fact(connection, "source")
    changed = old.fact.model_dump(mode="json")
    changed["specification"]["columns"][0]["label"] = "clarified original column"
    company.materials.receive(
        "source",
        changed,
        evidence=(proof, company.proof),
        expected_revision=1,
        request_id=company.request(),
    )
    assert "material_period_unknown" in codes(company.materials.check("2026-02"))
    assert "material_period_unknown" in codes(company.materials.check("2030-09"))
    with pytest.raises(KernelError) as error:
        resolve(company, data, proof, subject="stale-group")
    assert error.value.code == "material_source_changed"


def test_group_cannot_override_an_original_individual_business_month(company):
    source, proof = company.source()
    link = company.expense("cost", 3000, "2026-02")
    data = dict(
        period="2026-01",
        source_id="source",
        source_fact_id=source["fact_id"],
        members=[
            dict(location="CSV!B2", amount_fen=1000),
            dict(location="CSV!B3", amount_fen=2000),
        ],
        group_amount_fen=3000,
        links=[link],
        joint_basis_confirmed=True,
        basis_evidence_digest=company.proof,
        basis_location="source",
        reason="Confirmed total",
    )
    with pytest.raises(KernelError) as error:
        resolve(company, data, proof)
    assert error.value.code == "material_group_period_already_known"


def test_group_recovers_explicitly_reviewed_amounts_and_all_original_controls(company):
    spec = SPEC | {"total_rows": {"CSV": [4]}}
    source, proof = company.source(b"name,amount\na,10.00000000000001\nb,20\ntotal,30\n", spec=spec)
    link = company.expense("cost", 3000, "2026-02")
    data = dict(
        period="2026-01",
        source_id="source",
        source_fact_id=source["fact_id"],
        members=[
            dict(location="CSV!B2", amount_fen=1000),
            dict(location="CSV!B3", amount_fen=2000),
        ],
        group_amount_fen=3000,
        links=[link],
        joint_basis_confirmed=True,
        basis_evidence_digest=company.proof,
        basis_location="review of displayed money",
        reason="明确核定原显示10.00及20.00，共30.00元。",
    )
    resolve(company, data, proof)
    assert company.materials.check("2026-02")["status"] == "complete"


def test_group_save_rechecks_accounting_epoch_after_read_snapshot(company, monkeypatch):
    _, proof, data = pool(company)
    original = company.engine._write

    def competing_write(key, request_hash, expected, lanes, action, operation, **kwargs):
        if action == "resolve_material_group":
            # A competing legitimate publication lands after detached validation.
            company.expense("independent", 100, "2026-04")
        return original(key, request_hash, expected, lanes, action, operation, **kwargs)

    monkeypatch.setattr(company.engine, "_write", competing_write)
    with pytest.raises(KernelError) as error:
        resolve(company, data, proof)
    assert error.value.code == "preview_expired"
    with company.engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT 1 FROM subject WHERE id='pool'").fetchone() is None


def test_hidden_original_member_is_included_in_collective_coverage(company):
    book = Workbook()
    sheet = book.active
    sheet.title = "Pool"
    sheet.append(["name", "amount"])
    sheet.append(["visible", 10])
    sheet.append(["hidden", 20])
    sheet.row_dimensions[3].hidden = True
    buffer = BytesIO()
    book.save(buffer)
    source, proof = company.source(buffer.getvalue(), spec=SPEC | {"format": "xlsx"})
    one = company.expense("whole", 3000, "2026-02")
    data = dict(
        period="2026-01",
        source_id="source",
        source_fact_id=source["fact_id"],
        members=[
            {"location": "Pool!B2", "amount_fen": 1000},
            {"location": "Pool!B3", "amount_fen": 2000},
        ],
        group_amount_fen=3000,
        links=[one],
        joint_basis_confirmed=True,
        basis_evidence_digest=company.proof,
        basis_location="whole sheet",
        reason="All original rows including hidden",
    )
    resolve(company, data, proof)
    result = company.materials.check("2026-02")
    assert result["status"] == "complete", result["issues"]
    assert {row["location"] for row in result["coverage"]} == {"Pool!B2", "Pool!B3"}


@pytest.mark.parametrize("known_period", (False, True))
def test_close_freezes_group_members_and_rebuild_changes_no_accounting(book, known_period):
    engine, save, publish, close, _, proof = book
    api = Materials(engine)
    raw = (
        b"name,amount,period\na,10,2026-01\nb,20,2026-01\n"
        if known_period
        else b"name,amount\na,10\nb,20\n"
    )
    ev = engine.register_evidence(raw, "text/csv", "pool.csv", request_id="pool-evidence")["digest"]
    source = api.receive(
        "pool-source",
        dict(
            period="2026-01",
            evidence_digest=ev,
            category="transactions",
            purpose="business",
            specification=csv_spec() if known_period else SPEC,
        ),
        evidence=(ev, proof),
        expected_revision=0,
        request_id="pool-source",
    )
    links = []
    for index, (month, amount) in enumerate(
        [("2026-01", 1200), ("2026-01" if known_period else "2026-02", 1800)]
    ):
        subject = f"cost-{month}-{index}"
        saved = save(
            "expense",
            subject,
            dict(
                period=month,
                counterparty_id="supplier",
                amount_fen=amount,
                expense_class="administration",
                creditor_kind="supplier",
            ),
        )
        published = publish(subject)["results"][0]
        links.append(
            dict(
                subject_id=subject,
                fact_kind="expense",
                fact_id=saved["fact_id"],
                calculation_id=published["calculation_id"],
                amount_field="fact.amount_fen",
                amount_fen=amount,
                recognition_period=month,
            )
        )
    data = dict(
        period="2026-01",
        source_id="pool-source",
        source_fact_id=source["fact_id"],
        members=[
            dict(location="CSV!B2", amount_fen=1000),
            dict(location="CSV!B3", amount_fen=2000),
        ],
        group_amount_fen=3000,
        links=links,
        joint_basis_confirmed=True,
        basis_evidence_digest=proof,
        basis_location="entire group",
        reason="Approved jointly without row periods",
    )
    group = api.resolve_group(
        "pool", data, evidence=(proof, ev), expected_revision=0, request_id="group"
    )
    close("2026-01")
    before = Periods(engine).closed_report("2026-01")
    assert group["fact_id"] in json.dumps(before)
    close("2026-02")
    overview = engine.overview("2026-02")
    engine.rebuild_projections(request_id="rebuild-group")
    assert engine.overview("2026-02")["accounts"] == overview["accounts"]
    assert Periods(engine).closed_report("2026-01") == before


def test_356_row_social_style_pool_preserves_known_month_and_hidden_members(company):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Contributions"
    sheet.append(["kind", "amount", "period"])
    for _ in range(356):
        sheet.append(["employee or employer contribution", 1, "2026-01"])
    sheet.row_dimensions[10].hidden = True
    buffer = BytesIO()
    workbook.save(buffer)
    workbook.close()
    source, proof = company.source(buffer.getvalue(), spec=csv_spec() | {"format": "xlsx"})
    data = dict(
        period="2026-01",
        source_id="source",
        source_fact_id=source["fact_id"],
        members=[dict(location=f"Contributions!B{row}", amount_fen=100) for row in range(2, 358)],
        group_amount_fen=35600,
        links=[company.expense("one", 17800), company.expense("two", 17800)],
        joint_basis_confirmed=True,
        basis_evidence_digest=company.proof,
        basis_location="full known monthly population",
        reason="同月完整缴费总额对应已确认人员总体，不虚构原无姓名行的单人分摊。",
    )
    saved = resolve(company, data, proof)
    result = company.materials.check("2026-01", limit=500)
    assert result["status"] == "complete", result["issues"]
    assert len(result["coverage"]) == 356
    assert all(item["recognition_period"] == "2026-01" for item in result["coverage"])
    assert all(item["joint_periods"] == ["2026-01"] for item in result["coverage"])
    assert saved["fact_id"] in result["fact_ids"]


@pytest.mark.parametrize("known_basis", ("original", "confirmed"))
@pytest.mark.parametrize("cross_month", (False, True))
def test_unknown_member_cannot_weaken_another_members_known_month(
    company, known_basis, cross_month
):
    raw = b"name,amount,period\na,10,2026-01\nb,20,\n"
    source, proof = company.source(
        raw if known_basis == "original" else raw.replace(b"2026-01", b"")
    )
    if known_basis == "confirmed":
        assignments = [
            dict(
                location="CSV!B2",
                recognition_period="2026-01",
                basis="confirmed_period",
                basis_evidence_digest=company.proof,
                basis_location="member a",
                basis_excerpt="a belongs to January",
            )
        ]
        preview = company.materials.preview_period_allocation("source", assignments)
        company.materials.confirm_period_allocation(
            "source",
            assignments,
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            expected_revision=preview["expected_revision"],
            request_id=company.request(),
        )
    data = dict(
        period="2026-01",
        source_id="source",
        source_fact_id=source["fact_id"],
        members=[
            dict(location="CSV!B2", amount_fen=1000),
            dict(location="CSV!B3", amount_fen=2000),
        ],
        group_amount_fen=3000,
        links=[
            company.expense("one", 1200),
            company.expense("two", 1800, "2026-02" if cross_month else "2026-01"),
        ],
        joint_basis_confirmed=True,
        basis_evidence_digest=company.proof,
        basis_location="all members",
        reason="一条已知月份，一条单项月份未确认；已知边界约束共同结果。",
    )
    if cross_month:
        with pytest.raises(KernelError) as error:
            resolve(company, data, proof)
        assert error.value.code == "material_group_period_already_known"
    else:
        resolve(company, data, proof)
        result = company.materials.check("2026-01")
        assert result["status"] == "complete", result["issues"]
        assert [item["recognition_period"] for item in result["coverage"]] == ["2026-01", None]


def test_authenticated_public_command_validates_group_and_blocks_generic_fact_injection(service):
    app, company_id = service
    engine = app.engine(company_id)
    proof = engine.register_evidence(
        b"Approved complete collective pool", "text/plain", "basis", request_id="proof"
    )["digest"]
    raw = engine.register_evidence(
        b"name,amount\na,10\nb,20\n", "text/csv", "original", request_id="original"
    )["digest"]
    source = app.dispatch(
        "receive_material",
        dict(
            company_id=company_id,
            subject_id="source",
            data=dict(
                period="2026-01",
                evidence_digest=raw,
                category="transactions",
                purpose="business",
                specification=SPEC,
            ),
            evidence=[raw, proof],
            expected_revision=0,
            request_id="source",
        ),
    )
    saved = app.dispatch(
        "save_fact",
        dict(
            company_id=company_id,
            kind="expense",
            subject_id="cost",
            data=dict(
                period="2026-02",
                counterparty_id="supplier",
                amount_fen=3000,
                expense_class="administration",
                creditor_kind="supplier",
            ),
            evidence=[proof],
            expected_revision=0,
            request_id="save-cost",
        ),
    )
    plan = app.dispatch("preview", dict(company_id=company_id, subjects=["cost"]))
    published = app.dispatch(
        "confirm",
        dict(
            company_id=company_id,
            subjects=["cost"],
            preview_digest=plan["digest"],
            epochs=plan["epochs"],
            request_id="publish-cost",
        ),
    )["results"][0]
    data = dict(
        period="2026-01",
        source_id="source",
        source_fact_id=source["fact_id"],
        members=[
            dict(location="CSV!B2", amount_fen=1000),
            dict(location="CSV!B3", amount_fen=2000),
        ],
        group_amount_fen=3000,
        links=[
            dict(
                subject_id="cost",
                fact_kind="expense",
                fact_id=saved["fact_id"],
                calculation_id=published["calculation_id"],
                amount_field="fact.amount_fen",
                amount_fen=3000,
                recognition_period="2026-02",
            )
        ],
        joint_basis_confirmed=True,
        basis_evidence_digest=proof,
        basis_location="entire original",
        reason="Approved jointly",
    )
    payload = dict(
        company_id=company_id,
        subject_id="group",
        data=data,
        evidence=[raw, proof],
        expected_revision=0,
        request_id="group",
    )
    with pytest.raises(KernelError):
        app.dispatch("resolve_material_group", payload | {"data": data | {"invented_extra": True}})
    with pytest.raises(KernelError) as error:
        app.dispatch(
            "save_facts",
            dict(
                company_id=company_id,
                facts=[
                    dict(
                        kind=MaterialGroupResolution.kind,
                        subject_id="forbidden",
                        data=data,
                        evidence=[raw, proof],
                        expected_revision=0,
                    )
                ],
                request_id="forbidden",
            ),
        )
    assert (
        error.value.code == "invalid_command"
    )  # The public discriminated schema excludes this kind.
    result = app.dispatch("resolve_material_group", payload)
    assert result["fact_id"]
    assert (
        app.dispatch("material_completeness", dict(company_id=company_id, period="2026-02"))[
            "status"
        ]
        == "complete"
    )
