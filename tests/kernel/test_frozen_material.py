"""A frozen close may accelerate only unchanged, independently sealed sources."""

import pytest
from stage9_book import MixedBook
from test_integrity_content import damage
from test_materials import Company

from ai_accounting.kernel import frozen_material, materials
from ai_accounting.kernel.close_contract import CLOSE_FORMAT, CLOSE_FORMAT_VERSION
from ai_accounting.kernel.close_storage import write_close
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.integrity import verify_sources
from ai_accounting.kernel.types import YearMonth, canonical, digest


def _company_with_proof(tmp_path, monkeypatch):
    company = Company(tmp_path)
    source, evidence = company.source(b"name,amount,period\na,10,2026-01\n")
    source = {**source, "evidence_digest": evidence}
    link = company.expense("expense", 1000)
    company.resolve(source, "CSV!B2", [link])
    with company.engine.store.connection(read_only=True) as connection:
        proof = materials.check_completeness(
            connection, YearMonth("2026-01").ordinal, company.engine.store.registry
        )
    assert proof["status"] == proof["file_status"] == "complete"
    return company, source, proof, link


def test_source_evidence_batch_checks_shared_bytes_once_and_keeps_bindings(tmp_path, monkeypatch):
    company = Company(tmp_path)
    raw = b"name,amount,period\nshared-original,10,2026-01\n"
    first, proof = company.source(raw, subject="first-source")
    second, same_proof = company.source(raw, subject="second-source")
    assert proof == same_proof
    original = frozen_material.hashlib.sha256
    checked = []

    def counted(value=b"", **kwargs):
        if value == raw:
            checked.append(value)
        return original(value, **kwargs)

    monkeypatch.setattr(frozen_material.hashlib, "sha256", counted)
    with company.engine.store.connection(read_only=True) as connection:
        result = frozen_material._sealed_source_evidence(
            connection,
            {
                "first-source": first["fact_id"],
                "second-source": second["fact_id"],
                "wrong-owner": first["fact_id"],
            },
        )
    assert result == {"first-source", "second-source"}
    assert checked == [raw]
    damage(
        company.engine,
        "evidence",
        "UPDATE evidence SET content=? WHERE digest=?",
        (b"changed", bytes.fromhex(proof)),
    )
    with company.engine.store.connection(read_only=True) as connection:
        with pytest.raises(KernelError) as error:
            frozen_material._sealed_source_evidence(connection, {"first-source": first["fact_id"]})
    assert error.value.code == "content_integrity_failed"


def _save_frozen_proof(company, proof, *, period="2026-01", anchor=True):
    ordinal = YearMonth(period).ordinal
    with company.engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        previous = connection.execute(
            "SELECT period,digest FROM period_close ORDER BY period DESC LIMIT 1"
        ).fetchone()
        epochs = company.engine.store.epochs(connection)
        zero = 0
        owner_review = {
            "presentation_contract": "ai-accounting-kernel/2/close-review/1",
            "period": period,
            "accounting_summary": {
                "voucher_count": 0,
                "line_count": 0,
                "total_debit_fen": zero,
                "total_credit_fen": zero,
                "month_revenue_fen": zero,
                "month_expense_fen": zero,
                "month_result_fen": zero,
                "ending_assets_fen": None,
                "ending_liabilities_fen": None,
                "ending_equity_fen": None,
                "funds_total_fen": None,
                "bank_fen": None,
                "cash_fen": None,
                "payment_platform_fen": None,
                "actual_receipts_fen": zero,
                "actual_payments_fen": zero,
                "internal_transfer_fen": zero,
                "voucher_balanced": True,
                "financial_position_balanced": None,
                "financial_position_complete": False,
            },
            "business_summary": [],
            "material_summary": [],
            "adopted_basis_summary": {
                "policy_count": 0,
                "payroll_confirmation_count": 0,
                "evidence_count": 0,
                "summary": "合成资料证明隔离测试",
            },
            "owner_confirmation": {
                "source_type": "evidence",
                "id": company.proof,
                "revision": None,
                "digest": company.proof,
                "name": None,
                "media_type": None,
            },
            "followup_summary": {
                "close_issue_count": 0,
                "settlement_issue_count": 0,
                "external_issue_count": 0,
                "file_issue_count": 0,
                "followup_count": 0,
            },
            "collections": [],
        }
        manifest = {
            "format": CLOSE_FORMAT,
            "format_version": CLOSE_FORMAT_VERSION,
            "period": period,
            "company_id": company.engine.store.company_id,
            "database_id": company.engine.store.database_id,
            "previous_close_period": (
                str(YearMonth.from_ordinal(previous["period"])) if previous else None
            ),
            "previous_close_digest": previous["digest"].hex() if previous else None,
            "publication_sequence": connection.execute(
                "SELECT coalesce(max(sequence),0) FROM calculation_publication"
            ).fetchone()[0],
            "adopted_results": [],
            "vouchers": [],
            "opening_calculation_id": None,
            "asset_batch_adoptions": [],
            "asset_card_adoptions": [],
            "inventories": {},
            "owner_confirmation": company.proof,
            "readiness": {},
            "management_snapshot": {"entity_profiles": [], "employee_entities": []},
            "material_coverage": {key: value for key, value in proof.items() if key != "issues"},
            "trial_balance": [],
            "report_classification": {},
            "read_version": {**epochs, "read_repair_revision": 0},
            "approval": None,
            "owner_review": owner_review,
        }
        close_digest = write_close(connection, ordinal, manifest)
        connection.execute(
            "INSERT INTO read_index_source(source_kind,source_id,source_digest) VALUES(?,?,?)",
            ("close", str(ordinal), close_digest),
        )
        if anchor:
            connection.execute(
                "INSERT INTO material_close_rule(period,rule_digest) VALUES(?,?)",
                (ordinal, frozen_material.MATERIAL_COVERAGE_RULE_DIGEST),
            )
        connection.commit()


def _reusable(company, *, period="2026-02", closed_through="2026-01"):
    with company.engine.store.connection(read_only=True) as connection:
        return frozen_material.verified_frozen_materials(
            connection,
            YearMonth(period).ordinal,
            YearMonth(closed_through).ordinal,
            company.engine.store.registry,
        )


def _compare_page_checks(company, period="2026-02"):
    with company.engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        month = YearMonth(period).ordinal
        full = materials.check_completeness(connection, month, company.engine.store.registry)
        fast = materials.check_completeness(
            connection, month, company.engine.store.registry, _allow_frozen_reuse=True
        )
    assert canonical(fast) == canonical(full)
    assert fast["coverage_digest"] == full["coverage_digest"]
    return full


def test_verified_frozen_rows_keep_unchanged_source_and_exclude_changed_link(tmp_path, monkeypatch):
    company, source, proof, _ = _company_with_proof(tmp_path, monkeypatch)
    _save_frozen_proof(company, proof)
    reused = _reusable(company)
    assert reused.source_ids == {source["subject_id"]}
    assert len(reused.coverage_by_source[source["subject_id"]]) == 1
    assert reused.version_ids_by_kind["resolution"]
    _compare_page_checks(company)

    # The still-current material resolution now cites a superseded business
    # result; the same frozen rows can no longer be used.
    with company.engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        subject = connection.execute(
            "SELECT subject_id FROM fact_material_resolution_v2_links LIMIT 1"
        ).fetchone()[0]
        connection.execute("DELETE FROM calculation_current WHERE subject_id=?", (subject,))
        connection.commit()
    assert _reusable(company).source_ids == frozenset()
    _compare_page_checks(company)


def test_frozen_reuse_rejects_tampered_original_bytes_and_full_source_check_catches_facts(
    tmp_path, monkeypatch
):
    company, source, proof, _ = _company_with_proof(tmp_path, monkeypatch)
    _save_frozen_proof(company, proof)
    damage(
        company.engine,
        "fact_material_resolution_v2_links",
        "UPDATE fact_material_resolution_v2_links SET amount_fen=amount_fen+1",
    )
    resolution_id = next(iter(proof["resolution_versions"]))
    with company.engine.store.connection(read_only=True) as connection:
        with pytest.raises(KernelError) as error:
            verify_sources(company.engine, connection, fact_ids=(resolution_id,))
    assert error.value.code == "content_integrity_failed"

    third = tmp_path / "third"
    third.mkdir()
    company, third_source, proof, _ = _company_with_proof(third, monkeypatch)
    _save_frozen_proof(company, proof)
    damage(
        company.engine,
        "fact_material_source_v2",
        "UPDATE fact_material_source_v2 SET specification=?",
        ('{"format":"csv","columns":[]}',),
    )
    with company.engine.store.connection(read_only=True) as connection:
        with pytest.raises(KernelError) as error:
            verify_sources(company.engine, connection, fact_ids=(third_source["fact_id"],))
    assert error.value.code == "content_integrity_failed"

    # A separate company tests the original-content seal independently.
    second = tmp_path / "second"
    second.mkdir()
    company, source, proof, _ = _company_with_proof(second, monkeypatch)
    _save_frozen_proof(company, proof)
    damage(
        company.engine,
        "evidence",
        "UPDATE evidence SET content=? WHERE digest=?",
        (b"name,amount,period\na,20,2026-01\n", bytes.fromhex(source["evidence_digest"])),
    )
    with pytest.raises(KernelError) as error:
        _reusable(company)
    assert error.value.code == "content_integrity_failed"


def test_frozen_reuse_requires_material_rule_anchor(tmp_path, monkeypatch):
    company, source, proof, _ = _company_with_proof(tmp_path, monkeypatch)
    _save_frozen_proof(company, proof, anchor=False)
    assert _reusable(company) is None


def test_unrelated_build_change_keeps_material_rule_but_changed_rule_falls_back(
    tmp_path, monkeypatch
):
    from ai_accounting.kernel import engine

    company, source, proof, _ = _company_with_proof(tmp_path, monkeypatch)
    _save_frozen_proof(company, proof)
    monkeypatch.setattr(engine, "PROGRAM_VERSION", "unrelated-interface-build")
    assert _reusable(company).source_ids == {source["subject_id"]}
    _compare_page_checks(company)

    monkeypatch.setattr(frozen_material, "MATERIAL_COVERAGE_RULE_DIGEST", digest("different-rule"))
    assert _reusable(company) is None
    _compare_page_checks(company)


def test_reused_source_does_not_decode_its_historical_allocation_rows(tmp_path, monkeypatch):
    company, _, proof, _ = _company_with_proof(tmp_path, monkeypatch)
    _save_frozen_proof(company, proof)
    decoded = []
    original = materials.Store.facts

    def observe(reader, connection, identifiers):
        versions = original(reader, connection, identifiers)
        decoded.extend(version.fact.kind for version in versions.values())
        return versions

    monkeypatch.setattr(materials.Store, "facts", observe)
    with company.engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        actual = materials.check_completeness(
            connection,
            YearMonth("2026-02").ordinal,
            company.engine.store.registry,
            _allow_frozen_reuse=True,
        )
    assert actual["allocation_versions"] == proof["allocation_versions"]
    assert "material_period_allocation" not in decoded
    assert "material_resolution_v2" not in decoded


def test_frozen_reuse_invalidates_current_competitor_component(tmp_path, monkeypatch):
    company, source, proof, link = _company_with_proof(tmp_path, monkeypatch)
    competing, _ = company.source(b"name,amount,period\nother,10,2026-01\n", subject="competing")
    company.resolve(competing, "CSV!B2", [link], subject="competing-resolution")
    _save_frozen_proof(company, proof)
    assert source["subject_id"] not in _reusable(company).source_ids
    _compare_page_checks(company)


def test_frozen_reuse_invalidates_new_duplicate_source_link(tmp_path, monkeypatch):
    company, source, proof, _ = _company_with_proof(tmp_path, monkeypatch)
    repeated, _ = company.source(b"name,amount,period\ncopy,10,2026-01\n", subject="repeated")
    company.resolve(
        repeated,
        "CSV!B2",
        (),
        subject="duplicate-resolution",
        treatment="duplicate",
        duplicate_source_id=source["subject_id"],
        duplicate_location="CSV!B2",
        reason="同一来源重复提交",
    )
    _save_frozen_proof(company, proof)
    assert source["subject_id"] not in _reusable(company).source_ids
    _compare_page_checks(company)


def test_dependency_scan_returns_sources_instead_of_every_material_link(tmp_path):
    company = Company(tmp_path)
    raw = b"name,amount,period\n" + b"part,1,2026-01\n" * 12
    source, _ = company.source(raw)
    link = company.expense("whole-expense", 1200) | {"amount_fen": 100}
    for position in range(2, 14):
        company.resolve(source, f"CSV!B{position}", [link])
    competing, _ = company.source(b"name,amount,period\nother,1,2026-01\n", subject="competing")
    company.resolve(competing, "CSV!B2", [link], subject="competing-resolution")

    class ObservedConnection:
        def __init__(self, connection):
            self.connection = connection
            self.link_rows = []

        def execute(self, sql, arguments=()):
            cursor = self.connection.execute(sql, arguments)
            if sql.startswith("WITH links AS MATERIALIZED"):
                rows = cursor.fetchall()
                self.link_rows.append(len(rows))
                return iter(rows)
            return cursor

    with company.engine.store.connection() as connection:
        connection.execute("BEGIN")
        observed = ObservedConnection(connection)
        current = {
            "source": frozen_material._current_versions(
                connection, "material_source_v2", "fact_material_source_v2"
            )
        }
        edges, stale = frozen_material._source_dependencies(observed, current)
        assert dict(edges) == {
            source["subject_id"]: {competing["subject_id"]},
            competing["subject_id"]: {source["subject_id"]},
        }
        assert not stale
        assert observed.link_rows == [1]

        # Missing current accounting is stale even though SQL comparisons to
        # NULL ordinarily evaluate to unknown rather than true.
        connection.execute("DELETE FROM calculation_current WHERE subject_id=?", ("whole-expense",))
        edges_after, stale = frozen_material._source_dependencies(observed, current)
        assert edges_after == edges
        assert stale == {source["subject_id"], competing["subject_id"]}
        assert observed.link_rows == [1, 3]
        connection.rollback()


def test_frozen_and_current_month_sources_have_exact_page_digest(tmp_path, monkeypatch):
    company, source, proof, _ = _company_with_proof(tmp_path, monkeypatch)
    _save_frozen_proof(company, proof)
    current, _ = company.source(
        b"name,amount,period\nfeb,20,2026-02\n",
        subject="feb-source",
        period="2026-02",
    )
    company.resolve(
        current,
        "CSV!B2",
        [company.expense("feb-expense", 2000, period="2026-02")],
        subject="feb-resolution",
        period="2026-02",
        recognition_period="2026-02",
    )
    assert _reusable(company).source_ids == {source["subject_id"]}
    result = _compare_page_checks(company)
    assert {row["source_id"] for row in result["coverage"]} == {
        source["subject_id"],
        current["subject_id"],
    }


def test_prior_month_resolution_change_falls_back_with_exact_digest(tmp_path, monkeypatch):
    company, source, proof, link = _company_with_proof(tmp_path, monkeypatch)
    _save_frozen_proof(company, proof)
    company.resolve(source, "CSV!B2", [link], revision=1, reason="负责人复核")
    assert source["subject_id"] not in _reusable(company).source_ids
    _compare_page_checks(company)


def test_multi_month_original_with_future_row_uses_full_check(tmp_path, monkeypatch):
    company = Company(tmp_path)
    source, _ = company.source(b"name,amount,period\njan,10,2026-01\nfeb,20,2026-02\n")
    company.resolve(source, "CSV!B2", [company.expense("jan", 1000)])
    company.resolve(
        source,
        "CSV!B3",
        [company.expense("feb", 2000, period="2026-02")],
        subject="feb-resolution",
        period="2026-02",
        recognition_period="2026-02",
    )
    with company.engine.store.connection(read_only=True) as connection:
        proof = materials.check_completeness(
            connection, YearMonth("2026-01").ordinal, company.engine.store.registry
        )
    assert proof["status"] == "complete"
    _save_frozen_proof(company, proof)
    assert source["subject_id"] not in _reusable(company).source_ids
    _compare_page_checks(company)


def test_three_month_history_and_current_sources_match_full_digest(tmp_path, monkeypatch):
    company, jan, january_proof, jan_link = _company_with_proof(tmp_path, monkeypatch)
    _save_frozen_proof(company, january_proof)
    company.resolve(jan, "CSV!B2", [jan_link], revision=1, reason="二月复核")
    feb, _ = company.source(
        b"name,amount,period\nfeb,20,2026-02\n", subject="feb-source", period="2026-02"
    )
    company.resolve(
        feb,
        "CSV!B2",
        [company.expense("feb-expense", 2000, period="2026-02")],
        subject="feb-resolution",
        period="2026-02",
        recognition_period="2026-02",
    )
    with company.engine.store.connection(read_only=True) as connection:
        february_proof = materials.check_completeness(
            connection, YearMonth("2026-02").ordinal, company.engine.store.registry
        )
    assert february_proof["status"] == "complete"
    _save_frozen_proof(company, february_proof, period="2026-02")
    mar, _ = company.source(
        b"name,amount,period\nmar,30,2026-03\n", subject="mar-source", period="2026-03"
    )
    company.resolve(
        mar,
        "CSV!B2",
        [company.expense("mar-expense", 3000, period="2026-03")],
        subject="mar-resolution",
        period="2026-03",
        recognition_period="2026-03",
    )
    reused = _reusable(company, period="2026-03", closed_through="2026-02")
    assert reused.source_ids == {jan["subject_id"], feb["subject_id"]}
    result = _compare_page_checks(company, "2026-03")
    assert {row["source_id"] for row in result["coverage"]} == {
        jan["subject_id"],
        feb["subject_id"],
        mar["subject_id"],
    }


def test_real_close_preview_rejects_tampered_old_material_after_fast_read(tmp_path):
    book = MixedBook(tmp_path / "stage9-real-close", employees=1, businesses=26)
    book.add_month(0)
    book.add_month(1, close=False)
    with book.engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        month = YearMonth("2016-02").ordinal
        frozen = frozen_material.verified_frozen_materials(
            connection,
            month,
            YearMonth("2016-01").ordinal,
            book.engine.store.registry,
        )
        assert frozen is not None and frozen.source_ids
        full = materials.check_completeness(connection, month, book.engine.store.registry)
        fast = materials.check_completeness(
            connection, month, book.engine.store.registry, _allow_frozen_reuse=True
        )
    assert canonical(fast) == canonical(full)
    assert fast["coverage_digest"] == full["coverage_digest"]
    with book.engine.store.connection(read_only=True) as connection:
        old_resolution = connection.execute(
            "SELECT r.revision_id FROM fact_material_resolution_v2 r "
            "JOIN fact_current c ON c.fact_id=r.revision_id WHERE r.period=? LIMIT 1",
            (YearMonth("2016-01").ordinal,),
        ).fetchone()[0]
    damage(
        book.engine,
        "fact_material_resolution_v2_links",
        "UPDATE fact_material_resolution_v2_links SET amount_fen=amount_fen+1 "
        "WHERE revision_id=? AND item_no=0",
        (old_resolution,),
    )
    with pytest.raises(KernelError) as error:
        book.periods.preview_close(
            "2016-02", owner_confirmation=book.snapshots["2016-02"]["owner_confirmation"]
        )
    assert error.value.code == "content_integrity_failed"


def test_close_reuses_verified_old_material_without_changing_manifest(tmp_path, monkeypatch):
    book = MixedBook(tmp_path / "stage9-close-material-reuse", employees=1, businesses=26)
    book.add_month(0)
    book.add_month(1, close=False)
    with book.engine.store.connection(read_only=True) as connection:
        old_content = {
            row[0]
            for row in connection.execute(
                "SELECT e.content FROM fact_material_source_v2 s "
                "JOIN fact_revision f ON f.id=s.revision_id "
                "JOIN evidence e ON e.digest=unhex(s.evidence_digest) "
                "WHERE f.period=?",
                (YearMonth("2016-01").ordinal,),
            )
        }
    assert old_content
    original_inspect = materials.inspect_bytes
    original_check = book.periods.check_readiness
    mode = ["fast"]
    inspections = {"fast": 0, "full": 0}
    readiness_inspections = {}

    def counted_inspect(raw, specification):
        if raw in old_content:
            inspections[mode[0]] += 1
        return original_inspect(raw, specification)

    def measured_readiness(*args, **kwargs):
        if mode[0] == "full":
            kwargs["_reuse_closed_materials"] = False
        before = inspections[mode[0]]
        result = original_check(*args, **kwargs)
        readiness_inspections[mode[0]] = inspections[mode[0]] - before
        return result

    monkeypatch.setattr(materials, "inspect_bytes", counted_inspect)
    monkeypatch.setattr(book.periods, "check_readiness", measured_readiness)
    owner = book.snapshots["2016-02"]["owner_confirmation"]
    fast = book.periods.preview_close("2016-02", owner_confirmation=owner)
    mode[0] = "full"
    full = book.periods.preview_close("2016-02", owner_confirmation=owner)
    assert canonical(fast["manifest"]) == canonical(full["manifest"])
    assert fast["digest"] == full["digest"]
    assert readiness_inspections["fast"] == 0
    assert readiness_inspections["full"] > 0
    mode[0] = "fast"
    book.periods.close(
        "2016-02", owner_confirmation=owner, preview_digest=full["digest"],
        epochs=full["epochs"], request_id="close-with-verified-material-reuse",
    )


def test_page_materials_reuse_verified_close_inside_the_same_snapshot(tmp_path, monkeypatch):
    from ai_accounting.kernel import read_indexes
    from ai_accounting.kernel.query_reads import QueryReads

    book = MixedBook(tmp_path / "stage9-reuse-close", employees=1, businesses=26)
    book.add_month(0)
    book.add_month(1, close=False)
    with QueryReads.snapshot(book.engine) as reads:
        month = YearMonth("2016-02").ordinal
        close = reads.authoritative_close_rows(periods=[YearMonth("2016-01").ordinal])[0]
        original = canonical(reads.close_manifest(close))
        full = materials.check_completeness(reads.connection, month, book.engine.store.registry)

        def already_verified(*_args, **_kwargs):
            raise AssertionError("the same snapshot must not re-read a verified close")

        monkeypatch.setattr(read_indexes, "authoritative_close_rows", already_verified)
        fast = materials.check_completeness(
            reads.connection,
            month,
            book.engine.store.registry,
            _allow_frozen_reuse=True,
            _query_reads=reads,
        )
        assert canonical(full) == canonical(fast)
        assert canonical(reads.close_manifest(close)) == original


def test_frozen_reuse_rejects_changed_calculation_content(tmp_path, monkeypatch):
    company, _, proof, link = _company_with_proof(tmp_path, monkeypatch)
    _save_frozen_proof(company, proof)
    damage(
        company.engine,
        "calculation",
        "UPDATE calculation SET outcome=? WHERE id=?",
        ('{"values":{}}', link["calculation_id"]),
    )
    with company.engine.store.connection(read_only=True) as connection:
        with pytest.raises(KernelError) as error:
            verify_sources(company.engine, connection, calculation_ids=(link["calculation_id"],))
    assert error.value.code == "content_integrity_failed"


def test_frozen_manifest_content_damage_is_rejected(tmp_path, monkeypatch):
    company, _, proof, _ = _company_with_proof(tmp_path, monkeypatch)
    _save_frozen_proof(company, proof)
    damage(
        company.engine,
        "period_close",
        "UPDATE period_close SET manifest=replace(manifest,'2026-01','2026-02')",
    )
    with pytest.raises(KernelError) as error:
        _reusable(company)
    assert error.value.code == "content_integrity_failed"


def test_other_file_status_does_not_invalidate_complete_source(tmp_path, monkeypatch):
    company, source, _, _ = _company_with_proof(tmp_path, monkeypatch)
    future, _ = company.source(
        b"name,amount,period\njan,15,2026-01\nfeb,20,2026-02\n",
        subject="future-row-source",
    )
    company.resolve(
        future,
        "CSV!B2",
        [company.expense("another-jan", 1500)],
        subject="future-jan-resolution",
    )
    with company.engine.store.connection(read_only=True) as connection:
        proof = materials.check_completeness(
            connection, YearMonth("2026-01").ordinal, company.engine.store.registry
        )
    assert proof["status"] == "complete"
    assert proof["file_status"] == "needs_information"
    _save_frozen_proof(company, proof)
    assert _reusable(company).source_ids == {source["subject_id"]}
    _compare_page_checks(company)


def test_legitimate_future_row_diagnostic_does_not_corrupt_close_proof(tmp_path, monkeypatch):
    company = Company(tmp_path)
    source, _ = company.source(b"name,amount,period\njan,10,2026-01\nfeb,20,2026-02\n")
    company.resolve(source, "CSV!B2", [company.expense("jan", 1000)])
    with company.engine.store.connection(read_only=True) as connection:
        proof = materials.check_completeness(
            connection, YearMonth("2026-01").ordinal, company.engine.store.registry
        )
    assert proof["status"] == "complete"
    assert proof["file_status"] == "needs_information"
    _save_frozen_proof(company, proof)
    assert source["subject_id"] not in _reusable(company).source_ids
    _compare_page_checks(company)
