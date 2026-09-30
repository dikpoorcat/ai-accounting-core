import json
import sqlite3
import uuid

import pytest

import ai_accounting.kernel.duplicates as duplicate_module
from ai_accounting.kernel.contracts import FactVersion, KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.duplicates import (
    DuplicateCandidates,
    DuplicateReview,
    Duplicates,
    ReviewBasis,
    SourceLocation,
    verify_duplicate_checks,
)
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.entities import Entities
from ai_accounting.kernel.materials import Materials
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.types import YearMonth, canonical, digest


def test_material_location_merge_keeps_resolution_identity_and_original_order(monkeypatch):
    base = {
        "source_id": "source",
        "source_fact_id": "source-fact",
        "evidence_digest": "a" * 64,
        "location": "sheet-1!row-2",
    }
    actual = [
        {"fact_id": "business", "resolution_fact_id": "resolution-b", **base},
        {"fact_id": "business", "resolution_fact_id": "resolution-a", **base},
    ]
    stored = [base, base | {"location": "sheet-1!row-3"}, base]

    class Connection:
        def execute(self, sql, _parameters):
            assert "fact_material_resolution_v2_links" in sql
            return actual

    monkeypatch.setattr(
        duplicate_module,
        "_source_locations_from_checks",
        lambda _connection, _ids, *, current_sources: {"business": stored},
    )
    result = duplicate_module._material_locations(Connection(), {"business"})["business"]
    old_known = {
        canonical({key: value for key, value in row.items() if key != "fact_id"}) for row in actual
    }
    old_result = [
        {key: value for key, value in row.items() if key != "fact_id"} for row in actual
    ] + [item for item in stored if canonical(item) not in old_known]
    assert result == old_result
    assert [item.get("resolution_fact_id") for item in result] == [
        "resolution-b",
        "resolution-a",
        None,
        None,
        None,
    ]


@pytest.fixture
def company(tmp_path):
    store = Store.create(
        tmp_path / "company.sqlite",
        production_bundle(),
        "company-a",
        "91310000123456789A",
        "database-a",
    )
    engine = Engine(store)
    counterparty = Entities(engine).register_entity(
        "organization",
        {"display_name": "供应商甲"},
        source="合成测试资料",
        request_id="counterparty",
    )["entity_id"]
    return engine, counterparty


def evidence(engine, name):
    return engine.register_evidence(
        f"{name}\n业务行 2\n业务行 3".encode(),
        "text/plain",
        name,
        request_id="evidence-" + name,
    )["digest"]


def expense(engine, counterparty, *, period="2026-01", amount=10000):
    return engine.store.registry.models["expense"].model_validate(
        {
            "period": period,
            "counterparty_id": counterparty,
            "amount_fen": amount,
            "expense_class": "administration",
            "creditor_kind": "supplier",
        }
    )


def material_source(engine, proof, *, subject="source", purpose="business"):
    return engine.save_fact(
        "material_source_v2",
        subject,
        {
            "period": "2026-01",
            "evidence_digest": proof,
            "category": "transactions",
            "purpose": purpose,
            "supporting_purpose": "共同政策说明" if purpose == "supporting" else None,
            "specification": {
                "format": "text",
                "all_pages_reviewed": True,
                "passages": [
                    {"location": "sheet-1!row-2", "page": 1, "excerpt": "业务行 2"},
                    {"location": "sheet-1!row-3", "page": 1, "excerpt": "业务行 3"},
                ],
            },
        },
        evidence=(proof,),
        expected_revision=0,
        request_id="material-" + subject,
    )


def write_fact(store, connection, subject_id, fact, evidence_digests, revision=1):
    version = FactVersion(
        uuid.uuid4().hex,
        subject_id,
        revision,
        fact,
        tuple(sorted(evidence_digests)),
    )
    store.write_fact(connection, version, digest(fact.model_dump(mode="json")))
    return version


def fill_missing_test_checks(store, connection):
    """Complete low-level synthetic facts before exercising formal read invariants."""
    duplicates = DuplicateCandidates(store)
    rows = list(connection.execute(
        "SELECT f.id FROM fact_current c JOIN fact_revision f ON f.id=c.fact_id "
        "JOIN subject s ON s.id=f.subject_id "
        "WHERE s.kind IN(SELECT value FROM json_each(?)) "
        "AND NOT EXISTS(SELECT 1 FROM business_duplicate_check d "
        "WHERE d.result_fact_id=f.id) ORDER BY f.period,f.subject_id",
        (canonical(sorted(duplicate_module.ELIGIBLE_KINDS)),),
    ))
    for row in rows:
        version = store.fact(connection, row["id"])
        prepared = duplicates.prepare(
            connection,
            subject_id=version.subject_id,
            revision=version.revision,
            fact=version.fact,
            evidence=version.evidence,
        )
        # A low-level fixture has no registration-time candidate snapshot.
        # Its empty review is synthetic; current unresolved still recomputes
        # strong pairs independently from authoritative facts and materials.
        prepared["strong_candidates"] = []
        prepared["candidate_digest"] = duplicate_module._candidate_digest(
            prepared["proposed"], prepared["source_locations"], []
        )
        duplicates.record_check(
            connection, prepared=prepared, result_fact_id=version.id, review=None
        )


def owner_review(prepared, proof, action="create_separate", candidate_subject_id=None):
    return DuplicateReview(
        candidate_digest=prepared["candidate_digest"],
        action=action,
        candidate_subject_id=candidate_subject_id,
        explanation="已核对原始资料并确认处置",
        review_basis=(ReviewBasis(kind="owner_confirmation", evidence_digest=proof),),
    )


def test_complete_signature_without_shared_business_evidence_stays_weak(company):
    engine, party = company
    first_proof, second_proof = evidence(engine, "first"), evidence(engine, "second")
    fact = expense(engine, party)
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        write_fact(engine.store, connection, "expense-a", fact, (first_proof,))
        prepared = DuplicateCandidates(engine.store).prepare(
            connection,
            subject_id="expense-b",
            revision=1,
            fact=fact,
            evidence=(second_proof,),
        )
        assert prepared["status"] == "clear"
        assert prepared["strong_candidates"] == []
        assert prepared["weak_candidates"][0]["signals"][0]["code"] == ("same_complete_signature")


def test_shared_business_source_requires_review_and_review_is_immutable(company):
    engine, party = company
    proof = evidence(engine, "invoice")
    fact = expense(engine, party)
    duplicates = DuplicateCandidates(engine.store)
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        first = write_fact(engine.store, connection, "expense-a", fact, (proof,))
        prepared = duplicates.prepare(
            connection,
            subject_id="expense-b",
            revision=1,
            fact=fact,
            evidence=(proof,),
        )
        assert prepared["status"] == "review_required"
        assert prepared["strong_candidates"][0]["fact_id"] == first.id
        with pytest.raises(KernelError) as required:
            duplicates.require_review(prepared, None)
        assert required.value.code == "duplicate_review_required"

        second = write_fact(engine.store, connection, "expense-b", fact, (proof,))
        check = duplicates.record_check(
            connection,
            prepared=prepared,
            result_fact_id=second.id,
            review=owner_review(prepared, proof),
        )
        fill_missing_test_checks(engine.store, connection)
        assert duplicates.unresolved(connection) == []
        verify_duplicate_checks(connection)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE business_duplicate_check SET explanation='changed' WHERE id=?",
                (check["check_id"],),
            )


def test_supporting_source_does_not_promote_equal_business_to_strong(company):
    engine, party = company
    proof = evidence(engine, "general-support")
    material_source(engine, proof, subject="support-source", purpose="supporting")
    fact = expense(engine, party)
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        write_fact(engine.store, connection, "expense-a", fact, (proof,))
        prepared = DuplicateCandidates(engine.store).prepare(
            connection,
            subject_id="expense-b",
            revision=1,
            fact=fact,
            evidence=(proof,),
        )
        assert prepared["status"] == "clear"
        assert prepared["strong_candidates"] == []
        assert {item["code"] for item in prepared["weak_candidates"][0]["signals"]} == {
            "same_complete_signature",
            "shared_evidence",
        }
        second = write_fact(engine.store, connection, "expense-b", fact, (proof,))
        check = DuplicateCandidates(engine.store).record_check(
            connection, prepared=prepared, result_fact_id=second.id, review=None
        )
        saved = connection.execute(
            "SELECT manifest FROM business_duplicate_check WHERE id=?", (check["check_id"],)
        ).fetchone()[0]
        assert "weak_candidates" not in json.loads(saved)
        fill_missing_test_checks(engine.store, connection)
        verify_duplicate_checks(connection)


def test_distinct_locations_keep_same_document_lines_separate(company):
    engine, party = company
    proof = evidence(engine, "multi-line-invoice")
    source = material_source(engine, proof)
    fact = expense(engine, party)
    duplicates = DuplicateCandidates(engine.store)
    first_location = SourceLocation(
        source_id="source",
        source_fact_id=source["fact_id"],
        evidence_digest=proof,
        location="sheet-1!row-2",
    )
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        first = write_fact(engine.store, connection, "expense-a", fact, (proof,))
        initial = duplicates.prepare(
            connection,
            subject_id="expense-a",
            revision=1,
            fact=fact,
            evidence=(proof,),
            source_locations=(first_location,),
        )
        duplicates.record_check(connection, prepared=initial, result_fact_id=first.id, review=None)
        different_row = duplicates.prepare(
            connection,
            subject_id="expense-b",
            revision=1,
            fact=fact,
            evidence=(proof,),
            source_locations=(first_location.model_copy(update={"location": "sheet-1!row-3"}),),
        )
        assert different_row["strong_candidates"] == []
        assert different_row["weak_candidates"][0]["signals"][1]["distinct_locations_proven"]
        same_row = duplicates.prepare(
            connection,
            subject_id="expense-c",
            revision=1,
            fact=fact,
            evidence=(proof,),
            source_locations=(first_location,),
        )
        assert same_row["strong_candidates"][0]["signals"][0]["code"] == (
            "same_exact_material_location"
        )


def test_v1_check_reader_keeps_nonempty_source_location_when_current_parser_changes(
    company, monkeypatch
):
    import ai_accounting.kernel.materials as current_materials
    from ai_accounting.kernel.duplicate_checks_v1 import verify_duplicate_checks as verify_v1

    engine, party = company
    proof = evidence(engine, "v1-source-position")
    source = material_source(engine, proof)
    location = SourceLocation(
        source_id="source",
        source_fact_id=source["fact_id"],
        evidence_digest=proof,
        location="sheet-1!row-2",
    )
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        version = write_fact(
            engine.store, connection, "v1-location-expense", expense(engine, party), (proof,)
        )
        duplicates = DuplicateCandidates(engine.store)
        prepared = duplicates.prepare(
            connection,
            subject_id=version.subject_id,
            revision=version.revision,
            fact=version.fact,
            evidence=version.evidence,
            source_locations=(location,),
        )
        duplicates.record_check(
            connection, prepared=prepared, result_fact_id=version.id, review=None
        )
        monkeypatch.setattr(
            current_materials,
            "inspect_bytes",
            lambda *_args: (_ for _ in ()).throw(AssertionError("current parser called")),
        )
        verify_v1(connection)


def test_same_verified_location_uses_role_object_and_amount_not_secondary_class(company):
    engine, party = company
    proof = evidence(engine, "classification-correction")
    source = material_source(engine, proof)
    first_fact = expense(engine, party)
    changed_class = engine.store.registry.models["expense"].model_validate(
        {
            **first_fact.model_dump(mode="json"),
            "expense_class": "sales",
        }
    )
    location = SourceLocation(
        source_id="source",
        source_fact_id=source["fact_id"],
        evidence_digest=proof,
        location="sheet-1!row-2",
    )
    duplicates = DuplicateCandidates(engine.store)
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        first = write_fact(engine.store, connection, "expense-a", first_fact, (proof,))
        initial = duplicates.prepare(
            connection,
            subject_id="expense-a",
            revision=1,
            fact=first_fact,
            evidence=(proof,),
            source_locations=(location,),
        )
        duplicates.record_check(connection, prepared=initial, result_fact_id=first.id, review=None)
        prepared = duplicates.prepare(
            connection,
            subject_id="expense-b",
            revision=1,
            fact=changed_class,
            evidence=(proof,),
            source_locations=(location,),
        )
        assert prepared["status"] == "review_required"
        assert prepared["strong_candidates"][0]["signals"] == [
            {
                "code": "same_exact_material_location",
                "matched_fields": ["duplicate_role", "business_objects", "amounts"],
                "source_locations": [{"evidence_digest": proof, "location": "sheet-1!row-2"}],
            }
        ]


def test_identity_reassignment_inherits_registered_location_for_new_candidate(company):
    engine, first_party = company
    second_party = Entities(engine).register_entity(
        "organization", {"display_name": "供应商乙"},
        source="合成测试资料", request_id="second-counterparty",
    )["entity_id"]
    third_party = Entities(engine).register_entity(
        "organization", {"display_name": "供应商丙"},
        source="合成测试资料", request_id="third-counterparty",
    )["entity_id"]
    proof = evidence(engine, "identity-location")
    source = material_source(engine, proof)
    location = SourceLocation(
        source_id="source", source_fact_id=source["fact_id"],
        evidence_digest=proof, location="sheet-1!row-2",
    )
    original = expense(engine, first_party)
    reassigned = original.model_copy(update={"counterparty_id": second_party})
    final = original.model_copy(update={"counterparty_id": third_party})
    proposed = final.model_copy(update={"expense_class": "sales"})
    duplicates = DuplicateCandidates(engine.store)
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        first = write_fact(engine.store, connection, "expense-a", original, ())
        initial = duplicates.prepare(
            connection, subject_id="expense-a", revision=1, fact=original,
            evidence=(), source_locations=(location,),
        )
        duplicates.record_check(connection, prepared=initial, result_fact_id=first.id, review=None)
        after = write_fact(engine.store, connection, "expense-a", reassigned, (), revision=2)
        connection.execute(
            "INSERT INTO identity_correction VALUES(?,?,?)", ("correction", "{}", b"0" * 32)
        )
        connection.execute(
            "INSERT INTO identity_correction_item VALUES(?,?,?,?,?,?,?,?)",
            ("item", "correction", "expense-a", "reassign", first.id, after.id, None, None),
        )
        latest = write_fact(engine.store, connection, "expense-a", final, (), revision=3)
        connection.execute(
            "INSERT INTO identity_correction VALUES(?,?,?)", ("correction-2", "{}", b"1" * 32)
        )
        connection.execute(
            "INSERT INTO identity_correction_item VALUES(?,?,?,?,?,?,?,?)",
            ("item-2", "correction-2", "expense-a", "reassign", after.id, latest.id, None, None),
        )
        prepared = duplicates.prepare(
            connection, subject_id="expense-b", revision=1, fact=proposed,
            evidence=(), source_locations=(location,),
        )
        assert prepared["status"] == "review_required"
        assert prepared["strong_candidates"][0]["subject_id"] == "expense-a"
        assert prepared["strong_candidates"][0]["fact_id"] == latest.id
        assert prepared["strong_candidates"][0]["signals"][0]["code"] == (
            "same_exact_material_location"
        )


def test_sql_location_lineage_stops_at_checked_reassignment(company):
    engine, party = company
    proof = evidence(engine, "checked-reassignment-location")
    source = material_source(engine, proof)
    location = SourceLocation(
        source_id="source", source_fact_id=source["fact_id"],
        evidence_digest=proof, location="sheet-1!row-2",
    )
    fact = expense(engine, party)
    duplicates = DuplicateCandidates(engine.store)
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        first = write_fact(engine.store, connection, "expense-a", fact, ())
        checked = duplicates.prepare(
            connection, subject_id="expense-a", revision=1, fact=fact,
            evidence=(), source_locations=(location,),
        )
        duplicates.record_check(connection, prepared=checked, result_fact_id=first.id, review=None)
        second = write_fact(engine.store, connection, "expense-a", fact, (), revision=2)
        connection.execute(
            "INSERT INTO identity_correction VALUES(?,?,?)",
            ("checked-lineage-1", "{}", b"1" * 32),
        )
        connection.execute(
            "INSERT INTO identity_correction_item VALUES(?,?,?,?,?,?,?,?)",
            ("checked-item-1", "checked-lineage-1", "expense-a", "reassign",
             first.id, second.id, None, None),
        )
        empty_check = duplicates.prepare(
            connection, subject_id="expense-a", revision=2, fact=fact,
            evidence=(), source_locations=(),
        )
        duplicates.record_check(
            connection, prepared=empty_check, result_fact_id=second.id, review=None,
        )
        third = write_fact(engine.store, connection, "expense-a", fact, (), revision=3)
        connection.execute(
            "INSERT INTO identity_correction VALUES(?,?,?)",
            ("checked-lineage-2", "{}", b"2" * 32),
        )
        connection.execute(
            "INSERT INTO identity_correction_item VALUES(?,?,?,?,?,?,?,?)",
            ("checked-item-2", "checked-lineage-2", "expense-a", "reassign",
             second.id, third.id, None, None),
        )
        other = write_fact(engine.store, connection, "expense-b", fact, ())
        other_check = duplicates.prepare(
            connection, subject_id="expense-b", revision=1, fact=fact,
            evidence=(), source_locations=(location,),
        )
        duplicates.record_check(
            connection, prepared=other_check, result_fact_id=other.id, review=None,
        )
        assert duplicate_module._strong_location_pair_fact_ids(
            connection, [{"id": third.id}, {"id": other.id}],
        ) == set()


def test_source_location_must_exist_in_current_material_inspection(company):
    engine, party = company
    proof = evidence(engine, "forged-location")
    source = material_source(engine, proof)
    with engine.store.connection() as connection:
        with pytest.raises(KernelError) as invalid:
            DuplicateCandidates(engine.store).prepare(
                connection,
                subject_id="expense-a",
                revision=1,
                fact=expense(engine, party),
                evidence=(proof,),
                source_locations=(
                    SourceLocation(
                        source_id="source",
                        source_fact_id=source["fact_id"],
                        evidence_digest=proof,
                        location="sheet-1!row-999",
                    ),
                ),
            )
    assert invalid.value.code == "duplicate_source_location_invalid"


@pytest.mark.parametrize("location", ["sheet-1!row-2", "sheet-1!row-999"])
def test_distinct_location_review_cannot_use_same_or_invented_position(company, location):
    engine, party = company
    proof = evidence(engine, "invalid-distinct-review-" + location[-1])
    source = material_source(engine, proof)
    fact = expense(engine, party)
    source_location = SourceLocation(
        source_id="source",
        source_fact_id=source["fact_id"],
        evidence_digest=proof,
        location="sheet-1!row-2",
    )
    duplicates = DuplicateCandidates(engine.store)
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        first = write_fact(engine.store, connection, "expense-a", fact, (proof,))
        clear = duplicates.prepare(
            connection,
            subject_id="expense-a",
            revision=1,
            fact=fact,
            evidence=(proof,),
            source_locations=(source_location,),
        )
        duplicates.record_check(connection, prepared=clear, result_fact_id=first.id, review=None)
        prepared = duplicates.prepare(
            connection,
            subject_id="expense-b",
            revision=1,
            fact=fact,
            evidence=(proof,),
            source_locations=(source_location,),
        )
        second = write_fact(engine.store, connection, "expense-b", fact, (proof,))
        review = DuplicateReview(
            candidate_digest=prepared["candidate_digest"],
            action="create_separate",
            explanation="声称来自不同业务行",
            review_basis=(
                ReviewBasis(
                    kind="distinct_material_location",
                    evidence_digest=proof,
                    source_id="source",
                    source_fact_id=source["fact_id"],
                    location=location,
                ),
            ),
        )
        with pytest.raises(KernelError) as invalid:
            duplicates.record_check(
                connection,
                prepared=prepared,
                result_fact_id=second.id,
                review=review,
            )
    assert invalid.value.code in {
        "duplicate_source_location_invalid",
        "duplicate_separation_basis_invalid",
    }


def test_material_source_revision_expires_prior_separation_without_rewriting_history(company):
    engine, party = company
    proof = evidence(engine, "versioned-material")
    source = material_source(engine, proof)
    fact = expense(engine, party)
    location = SourceLocation(
        source_id="source",
        source_fact_id=source["fact_id"],
        evidence_digest=proof,
        location="sheet-1!row-2",
    )
    duplicates = DuplicateCandidates(engine.store)
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        first = write_fact(engine.store, connection, "expense-a", fact, (proof,))
        clear = duplicates.prepare(
            connection,
            subject_id="expense-a",
            revision=1,
            fact=fact,
            evidence=(proof,),
            source_locations=(location,),
        )
        duplicates.record_check(connection, prepared=clear, result_fact_id=first.id, review=None)
        second = write_fact(engine.store, connection, "expense-b", fact, (proof,))
        prepared = duplicates.prepare(
            connection,
            subject_id="expense-b",
            revision=1,
            fact=fact,
            evidence=(proof,),
            source_locations=(location,),
        )
        duplicates.record_check(
            connection,
            prepared=prepared,
            result_fact_id=second.id,
            review=owner_review(prepared, proof),
        )
        connection.commit()
    Materials(engine).receive(
        "source",
        {
            "period": "2026-01",
            "evidence_digest": proof,
            "category": "assets",
            "purpose": "business",
            "specification": {
                "format": "text",
                "all_pages_reviewed": True,
                "passages": [
                    {"location": "sheet-1!row-2", "page": 1, "excerpt": "业务行 2"},
                    {"location": "sheet-1!row-3", "page": 1, "excerpt": "业务行 3"},
                ],
            },
        },
        evidence=(proof,),
        expected_revision=1,
        request_id="material-source-v2",
    )
    with engine.store.connection(read_only=True) as connection:
        verify_duplicate_checks(connection)
        unresolved = duplicates.unresolved(connection)
    assert len(unresolved) == 1
    assert {unresolved[0][key] for key in ("subject_id", "candidate_subject_id")} == {
        "expense-a",
        "expense-b",
    }


def test_record_digest_rejects_tampered_disposition_on_normal_and_full_reads(company):
    engine, party = company
    proof = evidence(engine, "tampered-disposition")
    saved = engine.save_fact(
        "expense",
        "expense-a",
        expense(engine, party).model_dump(mode="json"),
        evidence=(proof,),
        expected_revision=0,
        request_id="save-tamper-target",
    )
    with engine.store.connection() as connection:
        connection.execute("DROP TRIGGER business_duplicate_check_no_update")
        connection.execute(
            "UPDATE business_duplicate_check SET explanation='篡改后的处置' WHERE result_fact_id=?",
            (saved["fact_id"],),
        )
        with pytest.raises(KernelError) as normal:
            DuplicateCandidates(engine.store).business_detail(connection, "expense-a")
        assert normal.value.code == "duplicate_review_corrupt"
        with pytest.raises(KernelError) as complete:
            verify_duplicate_checks(connection)
        assert complete.value.code == "duplicate_review_corrupt"


@pytest.mark.parametrize(
    ("column", "invalid"),
    [
        ("contract", "ai-accounting-kernel/2/business-duplicate-corrupt"),
        ("contract_version", 3),
    ],
)
def test_check_reader_rejects_tampered_contract_on_current_and_v1_reads(company, column, invalid):
    from ai_accounting.kernel.duplicate_checks_v1 import verify_duplicate_checks as verify_v1

    engine, party = company
    proof = evidence(engine, f"tampered-{column}")
    saved = engine.save_fact(
        "expense",
        "expense-a",
        expense(engine, party).model_dump(mode="json"),
        evidence=(proof,),
        expected_revision=0,
        request_id=f"save-tampered-{column}",
    )
    with engine.store.connection() as connection:
        connection.execute("DROP TRIGGER business_duplicate_check_no_update")
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            f"UPDATE business_duplicate_check SET {column}=? WHERE result_fact_id=?",
            (invalid, saved["fact_id"]),
        )
        connection.execute("PRAGMA ignore_check_constraints=OFF")
        with pytest.raises(KernelError) as normal:
            DuplicateCandidates(engine.store).business_detail(connection, "expense-a")
        with pytest.raises(KernelError) as complete:
            verify_duplicate_checks(connection)
        with pytest.raises(KernelError) as historical:
            verify_v1(connection)
    assert {normal.value.code, complete.value.code, historical.value.code} == {
        "duplicate_review_corrupt"
    }


def test_missing_current_check_rejected_by_close_and_full_integrity(company):
    engine, party = company
    proof = evidence(engine, "missing-current-check")
    saved = engine.save_fact(
        "expense", "expense-a", expense(engine, party).model_dump(mode="json"),
        evidence=(proof,), expected_revision=0, request_id="save-missing-current-check",
    )
    with engine.store.connection() as connection:
        connection.execute("DROP TRIGGER business_duplicate_check_no_delete")
        connection.execute(
            "DELETE FROM business_duplicate_check WHERE result_fact_id=?", (saved["fact_id"],)
        )
        with pytest.raises(KernelError) as normal:
            DuplicateCandidates(engine.store).close_readiness(connection, "2026-01")
        with pytest.raises(KernelError) as complete:
            verify_duplicate_checks(connection)
    assert normal.value.code == complete.value.code == "duplicate_review_corrupt"


def test_unpaired_checks_are_not_decoded_during_close(company, monkeypatch):
    engine, party = company
    first, second = evidence(engine, "unpaired-first"), evidence(engine, "unpaired-second")
    for index, proof in enumerate((first, second)):
        engine.save_fact(
            "expense", f"expense-{index}",
            expense(engine, party, amount=10000 + index).model_dump(mode="json"),
            evidence=(proof,), expected_revision=0, request_id=f"save-unpaired-{index}",
        )
    with engine.store.connection(read_only=True) as connection:
        with monkeypatch.context() as patch:
            patch.setattr(
                duplicate_module, "_source_locations_from_checks",
                lambda *_args, **_kwargs: pytest.fail("unpaired check decoded"),
            )
            assert DuplicateCandidates(engine.store).close_readiness(connection, "2026-01") == []
        verify_duplicate_checks(connection)


def test_new_asset_id_does_not_hide_same_supplier_amount_and_material_row(company):
    engine, supplier = company
    entities = Entities(engine)
    asset_a = entities.register_entity("asset", {}, source="合成资产清单", request_id="asset-a")[
        "entity_id"
    ]
    asset_b = entities.register_entity("asset", {}, source="合成资产清单", request_id="asset-b")[
        "entity_id"
    ]
    proof = evidence(engine, "asset-invoice")
    source = material_source(engine, proof)
    model = engine.store.registry.models["asset"]
    common = {
        "period": "2026-01",
        "asset_type": "fixed",
        "supplier_id": supplier,
        "acquisition_date": "2026-01-05",
        "cost_fen": 80000,
        "acquisition_basis": "direct_purchase",
    }
    first_fact = model.model_validate({**common, "asset_id": asset_a})
    second_fact = model.model_validate({**common, "asset_id": asset_b})
    location = SourceLocation(
        source_id="source",
        source_fact_id=source["fact_id"],
        evidence_digest=proof,
        location="sheet-1!row-2",
    )
    duplicates = DuplicateCandidates(engine.store)
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        first = write_fact(engine.store, connection, "asset-business-a", first_fact, (proof,))
        initial = duplicates.prepare(
            connection,
            subject_id="asset-business-a",
            revision=1,
            fact=first_fact,
            evidence=(proof,),
            source_locations=(location,),
        )
        duplicates.record_check(connection, prepared=initial, result_fact_id=first.id, review=None)
        prepared = duplicates.prepare(
            connection,
            subject_id="asset-business-b",
            revision=1,
            fact=second_fact,
            evidence=(proof,),
            source_locations=(location,),
        )
    assert prepared["status"] == "review_required"
    assert prepared["strong_candidates"][0]["signals"][0]["code"] == (
        "same_exact_material_location"
    )


def test_late_material_resolution_creates_current_strong_candidate_and_binds_version(
    company, monkeypatch
):
    engine, party = company
    first_proof = evidence(engine, "late-business-a")
    second_proof = evidence(engine, "late-business-b")
    source_proof = evidence(engine, "late-source")
    facts = []
    for subject, period, proof in (
        ("expense-a", "2026-01", first_proof),
        ("expense-b", "2026-02", second_proof),
    ):
        saved = engine.save_fact(
            "expense",
            subject,
            expense(engine, party, period=period).model_dump(mode="json"),
            evidence=(proof,),
            expected_revision=0,
            request_id="save-" + subject,
        )
        plan = engine.preview([subject])
        published = engine.confirm(
            [subject],
            preview_digest=plan["digest"],
            epochs=plan["epochs"],
            request_id="publish-" + subject,
        )
        facts.append((subject, period, saved["fact_id"], published["results"][0]["calculation_id"]))
    source = material_source(engine, source_proof, subject="late-source")
    resolution = {
        "period": "2026-02",
        "source_id": "late-source",
        "source_fact_id": source["fact_id"],
        "location": "sheet-1!row-2",
        "treatment": "recognize",
        "amount_fen": 20000,
        "recognition_period": "2026-02",
        "links": [
            {
                "subject_id": subject,
                "fact_kind": "expense",
                "fact_id": fact_id,
                "calculation_id": calculation_id,
                "amount_field": "fact.amount_fen",
                "amount_fen": 10000,
                "recognition_period": period,
            }
            for subject, period, fact_id, calculation_id in facts
        ],
    }
    first_resolution = Materials(engine).resolve(
        "late-resolution",
        resolution,
        evidence=(source_proof,),
        expected_revision=0,
        request_id="resolve-late-source",
    )
    original_material_locations = duplicate_module._material_locations
    calls = []

    def traced(connection, fact_ids):
        identifiers = tuple(fact_ids)
        calls.append(identifiers)
        return original_material_locations(connection, identifiers)

    monkeypatch.setattr(duplicate_module, "_material_locations", traced)
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        first_issues = DuplicateCandidates(engine.store).unresolved(connection)
    assert len(calls) == 1 and set(calls[0]) == {item[2] for item in facts}
    assert len(first_issues) == 1
    assert first_issues[0]["signals"][0]["code"] == "same_exact_material_location"

    second_resolution = Materials(engine).resolve(
        "late-resolution",
        resolution,
        evidence=(source_proof,),
        expected_revision=1,
        request_id="revise-late-source",
    )
    assert second_resolution["fact_id"] != first_resolution["fact_id"]
    calls.clear()
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        second_issues = DuplicateCandidates(engine.store).unresolved(connection)
    assert second_issues[0]["pair_digest"] != first_issues[0]["pair_digest"]


def test_actual_money_tuple_is_strong_without_shared_evidence(company):
    engine, _ = company
    entities = Entities(engine)
    owner = entities.register_entity("person", {}, source="合成出资资料", request_id="owner")[
        "entity_id"
    ]
    cash = entities.register_entity(
        "fund_account",
        {},
        account_type="cash",
        source="合成现金账户",
        request_id="cash",
    )["entity_id"]
    model = engine.store.registry.models["cash_funding"]
    fact = model.model_validate(
        {
            "period": "2026-01",
            "actual_date": "2026-01-08",
            "cash_account_id": cash,
            "owner_id": owner,
            "amount_fen": 50000,
            "funding_kind": "capital",
        }
    )
    first_proof = evidence(engine, "cash-receipt-a")
    second_proof = evidence(engine, "cash-receipt-b")
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        write_fact(engine.store, connection, "funding-a", fact, (first_proof,))
        prepared = DuplicateCandidates(engine.store).prepare(
            connection,
            subject_id="funding-b",
            revision=1,
            fact=fact,
            evidence=(second_proof,),
        )
    assert prepared["status"] == "review_required"
    assert prepared["strong_candidates"][0]["signals"] == [
        {
            "code": "same_complete_actual_money",
            "matched_fields": [
                "funds_category",
                "funds_account_id",
                "actual_date",
                "direction",
                "amount_fen",
                "business_object",
            ],
        }
    ]


def test_batch_candidate_indexes_cover_signature_location_and_actual_money(company, monkeypatch):
    engine, party = company
    first_proof = evidence(engine, "batch-index-first")
    second_proof = evidence(engine, "batch-index-second")
    source = material_source(engine, first_proof, subject="batch-index-source")
    location = SourceLocation(
        source_id="batch-index-source",
        source_fact_id=source["fact_id"],
        evidence_digest=first_proof,
        location="sheet-1!row-2",
    )
    first = expense(engine, party)
    changed = engine.store.registry.models["expense"].model_validate(
        {**first.model_dump(mode="json"), "expense_class": "sales"}
    )
    entities = Entities(engine)
    owner = entities.register_entity(
        "person", {}, source="批量候选出资人", request_id="batch-index-owner"
    )["entity_id"]
    cash = entities.register_entity(
        "fund_account",
        {},
        account_type="cash",
        source="批量候选现金账户",
        request_id="batch-index-cash",
    )["entity_id"]
    money = engine.store.registry.models["cash_funding"].model_validate(
        {
            "period": "2026-01",
            "actual_date": "2026-01-08",
            "cash_account_id": cash,
            "owner_id": owner,
            "amount_fen": 50000,
            "funding_kind": "capital",
        }
    )
    proposals = [
        {
            "subject_id": "expense-a",
            "revision": 1,
            "fact": first,
            "evidence": (first_proof,),
            "source_locations": (location,),
        },
        {
            "subject_id": "expense-b",
            "revision": 1,
            "fact": changed,
            "evidence": (first_proof,),
            "source_locations": (location,),
        },
        {
            "subject_id": "expense-c",
            "revision": 1,
            "fact": first,
            "evidence": (second_proof,),
        },
        {
            "subject_id": "funding-a",
            "revision": 1,
            "fact": money,
            "evidence": (first_proof,),
        },
        {
            "subject_id": "funding-b",
            "revision": 1,
            "fact": money,
            "evidence": (second_proof,),
        },
    ]
    manifest_scans = []
    original_scope = duplicate_module._require_sql_manifest_scope

    def counted_scope(connection, *, predicate="1", parameters=()):
        manifest_scans.append(predicate)
        return original_scope(connection, predicate=predicate, parameters=parameters)

    monkeypatch.setattr(duplicate_module, "_require_sql_manifest_scope", counted_scope)
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        prepared = DuplicateCandidates(engine.store).prepare_batch(connection, proposals)
    assert len(manifest_scans) == 1  # Both location proposals share one SQL manifest guard.
    assert prepared[1]["strong_candidates"][0]["signals"][0]["code"] == (
        "same_exact_material_location"
    )
    assert prepared[2]["weak_candidates"] == []
    assert prepared[4]["strong_candidates"][0]["signals"][0]["code"] == (
        "same_complete_actual_money"
    )


def test_malformed_saved_location_manifest_is_rejected_not_ignored(company):
    engine, party = company
    proof = evidence(engine, "corrupt-check")
    fact = expense(engine, party)
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        first = write_fact(engine.store, connection, "expense-a", fact, (proof,))
        connection.execute(
            "INSERT INTO business_duplicate_check(id,contract,contract_version,"
            "proposed_subject_id,proposed_revision,proposed_digest,candidate_digest,action,"
            "result_fact_id,selected_fact_id,manifest,review_basis,explanation,record_digest) "
            "VALUES('damaged-check',?,2,'expense-a',1,?,?, 'clear',?,NULL,'{}','[]','clear',?)",
            (
                "ai-accounting-kernel/2/business-duplicate",
                digest("proposal"),
                digest("candidate"),
                first.id,
                digest("damaged-record"),
            ),
        )
        with pytest.raises(KernelError) as corrupt:
            DuplicateCandidates(engine.store).prepare(
                connection,
                subject_id="expense-b",
                revision=1,
                fact=fact,
                evidence=(proof,),
            )
        assert DuplicateCandidates(engine.store).close_readiness(connection, "2026-01") == []
        with pytest.raises(KernelError) as complete_corrupt:
            verify_duplicate_checks(connection)
    assert corrupt.value.code == "duplicate_review_corrupt"
    assert complete_corrupt.value.code == "duplicate_review_corrupt"


def test_batch_candidate_can_reuse_newly_written_first_item(company):
    engine, party = company
    proof = evidence(engine, "batch-source")
    fact = expense(engine, party)
    duplicates = DuplicateCandidates(engine.store)
    proposals = [
        {
            "subject_id": subject,
            "revision": 1,
            "fact": fact,
            "evidence": (proof,),
        }
        for subject in ("expense-a", "expense-b")
    ]
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        prepared = duplicates.prepare_batch(connection, proposals)
        assert prepared[0]["status"] == "clear"
        assert prepared[1]["strong_candidates"][0]["fact_id"] is None
        first = write_fact(engine.store, connection, "expense-a", fact, (proof,))
        duplicates.record_check(
            connection, prepared=prepared[0], result_fact_id=first.id, review=None
        )
        reused = duplicates.record_check(
            connection,
            prepared=prepared[1],
            result_fact_id=None,
            review=owner_review(
                prepared[1],
                proof,
                action="reuse_existing",
                candidate_subject_id="expense-a",
            ),
            fact_ids_by_subject={"expense-a": first.id},
        )
        assert reused["selected_fact_id"] == first.id
        assert (
            connection.execute(
                "SELECT count(*) FROM fact_current WHERE subject_id='expense-b'"
            ).fetchone()[0]
            == 0
        )
        verify_duplicate_checks(connection)


def test_batch_candidates_decode_shared_sources_once_without_cross_batch_cache(
    company, monkeypatch
):
    from collections import Counter

    engine, party = company
    proof = evidence(engine, "shared-existing-candidates")
    other_proof = evidence(engine, "different-candidate-scope")
    duplicates = DuplicateCandidates(engine.store)
    proposals = [
        {
            "subject_id": f"proposed-{index}", "revision": 1,
            "fact": expense(engine, party, amount=20000 + index), "evidence": (proof,),
        }
        for index in range(20)
    ]
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        originals = [
            write_fact(engine.store, connection, f"existing-{index}",
                       expense(engine, party, amount=10000 + index), (proof,))
            for index in range(10)
        ]
        fill_missing_test_checks(engine.store, connection)
        expected = [duplicates.prepare(connection, **proposal) for proposal in proposals]
        loaded = Counter()
        original = Store.facts

        def counted(store, connection, identifiers):
            identifiers = tuple(identifiers)
            loaded.update(identifiers)
            return original(store, connection, identifiers)

        monkeypatch.setattr(Store, "facts", counted)
        actual = duplicates.prepare_batch(connection, proposals)
        assert actual == expected  # Weak candidates and all review digests are unchanged.
        assert loaded == {version.id: 1 for version in originals}
        assert duplicates.prepare_batch(connection, proposals) == expected
        assert loaded == {version.id: 2 for version in originals}

        # The shared evidence branch is read once in a batch, while each
        # proposal still checks its own exact digest and excludes itself.
        queries = []

        class TracedConnection:
            def execute(self, sql, parameters=()):
                queries.append(sql)
                return connection.execute(sql, parameters)

        scope_cache = {"candidate_rows": {}}
        traced = TracedConnection()
        scoped_proposals = [
            *proposals[:2],
            {**proposals[0], "evidence": (other_proof,)},
        ]
        for proposal in scoped_proposals:
            rows = duplicates._candidate_rows(
                traced,
                proposal["fact"],
                proposal["subject_id"],
                proposal["evidence"],
                (),
                batch_cache=scope_cache,
            )
            direct = duplicates._candidate_rows(
                connection,
                proposal["fact"],
                proposal["subject_id"],
                proposal["evidence"],
                (),
            )
            assert [tuple(row) for row in rows] == [tuple(row) for row in direct]
        assert len(scope_cache["candidate_rows"]) == 2
        assert len([sql for sql in queries if "fact_evidence e" in sql]) == 2
        assert len(queries) == 5  # two shared branches and three exact digests

        for index in range(12):
            duplicates._candidate_rows(
                traced,
                proposals[0]["fact"],
                proposals[0]["subject_id"],
                (f"{index:064x}",),
                (),
                batch_cache=scope_cache,
            )
            assert len(scope_cache["candidate_rows"]) <= 8

        expected_rows = duplicates._candidate_rows(
            connection,
            proposals[0]["fact"],
            proposals[0]["subject_id"],
            proposals[0]["evidence"],
            (),
        )
        connection.execute("SAVEPOINT without_candidate_index")
        try:
            connection.execute("DROP INDEX fact_evidence_source")
            without_index = duplicates._candidate_rows(
                connection,
                proposals[0]["fact"],
                proposals[0]["subject_id"],
                proposals[0]["evidence"],
                (),
            )
            assert [tuple(row) for row in without_index] == [
                tuple(row) for row in expected_rows
            ]
        finally:
            connection.execute("ROLLBACK TO without_candidate_index")
            connection.execute("RELEASE without_candidate_index")


def test_candidate_expiry_and_close_period_follow_later_business(company):
    engine, party = company
    proof = evidence(engine, "recurring-source")
    first_march = expense(engine, party, period="2026-03")
    march = expense(engine, party, period="2026-03")
    duplicates = DuplicateCandidates(engine.store)
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        first = write_fact(engine.store, connection, "expense-a", first_march, (proof,))
        old = duplicates.prepare(
            connection,
            subject_id="expense-b",
            revision=1,
            fact=march,
            evidence=(proof,),
        )
        write_fact(engine.store, connection, "expense-b", march, (proof,))
        assert duplicates.close_readiness(connection, "2026-02") == []
        fill_missing_test_checks(engine.store, connection)
        march_issues = duplicates.close_readiness(connection, "2026-03")
        assert march_issues[0]["review_period"] == "2026-03"

        connection.execute("DELETE FROM fact_current WHERE subject_id='expense-a'")
        changed = expense(engine, party, period="2026-03", amount=10001)
        write_fact(engine.store, connection, "expense-a", changed, (proof,), revision=2)
        refreshed = duplicates.prepare(
            connection,
            subject_id="expense-b",
            revision=1,
            fact=march,
            evidence=(proof,),
        )
        with pytest.raises(KernelError) as expired:
            duplicates.require_review(refreshed, owner_review(old, proof))
        assert expired.value.code == "duplicate_candidate_expired"
        assert refreshed["strong_candidates"] == []
        assert (
            first.id
            != connection.execute(
                "SELECT fact_id FROM fact_current WHERE subject_id='expense-a'"
            ).fetchone()[0]
        )


def test_close_readiness_ignores_future_and_keeps_open_review_period(
    company, monkeypatch,
):
    engine, party = company
    proof = evidence(engine, "closed-duplicate")
    duplicates = DuplicateCandidates(engine.store)
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        january = expense(engine, party, period="2026-01")
        write_fact(engine.store, connection, "expense-a", january, (proof,))
        write_fact(engine.store, connection, "expense-b", january, (proof,))
        write_fact(
            engine.store,
            connection,
            "expense-future",
            expense(engine, party, period="2026-03"),
            (proof,),
        )
        fill_missing_test_checks(engine.store, connection)
        issues = duplicates.close_readiness(connection, "2026-02")
        monkeypatch.setattr(
            duplicate_module, "_strong_pair_fact_ids",
            lambda _connection, rows, _locations, _registry, _period: {
                row["id"] for row in rows
            },
        )
        exhaustive = duplicates.close_readiness(connection, "2026-02")

        assert issues == exhaustive
        assert len(issues) == 1
        assert issues[0]["review_period"] == "2026-01"
        assert "expense-future" not in {issues[0]["subject_id"], issues[0]["candidate_subject_id"]}


def test_scoped_unresolved_does_not_reprepare_unrelated_reviewed_subjects(company, monkeypatch):
    engine, party = company
    reviewed_proof = evidence(engine, "reviewed-pair")
    target_proof = evidence(engine, "target-pair")
    fact = expense(engine, party)
    duplicates = DuplicateCandidates(engine.store)
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        write_fact(engine.store, connection, "reviewed-a", fact, (reviewed_proof,))
        reviewed = duplicates.prepare(
            connection,
            subject_id="reviewed-b",
            revision=1,
            fact=fact,
            evidence=(reviewed_proof,),
        )
        reviewed_result = write_fact(
            engine.store, connection, "reviewed-b", fact, (reviewed_proof,)
        )
        duplicates.record_check(
            connection,
            prepared=reviewed,
            result_fact_id=reviewed_result.id,
            review=owner_review(reviewed, reviewed_proof),
        )
        write_fact(engine.store, connection, "target-a", fact, (target_proof,))
        target_result = write_fact(engine.store, connection, "target-b", fact, (target_proof,))
        fill_missing_test_checks(engine.store, connection)

        original = duplicates._current_prepared
        prepared_fact_ids = []

        def traced(
            connection,
            fact_id,
            *,
            through_period=None,
            source_locations=None,
            strong_only=False,
            material_cache=None,
            batch_cache=None,
        ):
            prepared_fact_ids.append(fact_id)
            return original(
                connection,
                fact_id,
                through_period=through_period,
                source_locations=source_locations,
                strong_only=strong_only,
                material_cache=material_cache,
                batch_cache=batch_cache,
            )

        monkeypatch.setattr(duplicates, "_current_prepared", traced)
        issues = duplicates.unresolved(connection, subject_ids=("target-b",))

        assert len(issues) == 1
        assert prepared_fact_ids == [target_result.id]


def test_sql_location_pair_preselection_uses_exact_row_and_not_adjacent_row(company):
    engine, party = company
    proof = evidence(engine, "sql-exact-location")
    source = material_source(engine, proof)
    first = expense(engine, party)
    second = engine.store.registry.models["expense"].model_validate(
        {**first.model_dump(mode="json"), "expense_class": "sales"}
    )
    locations = (
        SourceLocation(
            source_id="source",
            source_fact_id=source["fact_id"],
            evidence_digest=proof,
            location="sheet-1!row-2",
        ),
        SourceLocation(
            source_id="source",
            source_fact_id=source["fact_id"],
            evidence_digest=proof,
            location="sheet-1!row-3",
        ),
    )
    duplicates = DuplicateCandidates(engine.store)
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        versions = []
        for index, (fact, location) in enumerate(
            ((first, locations[0]), (second, locations[0]), (second, locations[1]))
        ):
            subject = f"exact-row-{index}"
            prepared = duplicates.prepare(
                connection,
                subject_id=subject,
                revision=1,
                fact=fact,
                evidence=(proof,),
                source_locations=(location,),
            )
            version = write_fact(engine.store, connection, subject, fact, (proof,))
            duplicates.record_check(
                connection,
                prepared=prepared,
                result_fact_id=version.id,
                review=owner_review(prepared, proof) if prepared["strong_candidates"] else None,
            )
            versions.append(version)
        rows = [
            dict(row)
            for row in connection.execute(
                "SELECT f.id,s.kind FROM fact_current c JOIN fact_revision f ON f.id=c.fact_id "
                "JOIN subject s ON s.id=f.subject_id "
                "WHERE f.id IN (SELECT value FROM json_each(?))",
                (json.dumps([item.id for item in versions]),),
            )
        ]
        paired = duplicate_module._strong_location_pair_fact_ids(connection, rows)
    assert paired == {versions[0].id, versions[1].id}


def test_batch_scoped_preselection_keeps_external_candidate(company, monkeypatch):
    engine, party = company
    proof = evidence(engine, "batch-scoped-duplicate")
    other = evidence(engine, "batch-scoped-unrelated")
    duplicates = DuplicateCandidates(engine.store)
    targets = {f"target-{index}" for index in range(10)}
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        duplicate_fact = expense(engine, party)
        write_fact(engine.store, connection, "outside-target", duplicate_fact, (proof,))
        for index in range(10):
            write_fact(
                engine.store, connection, f"target-{index}",
                duplicate_fact if index == 0 else expense(engine, party, amount=1000 + index),
                (proof,) if index == 0 else (other,),
            )
        fill_missing_test_checks(engine.store, connection)
        optimized = duplicates.unresolved(connection, subject_ids=targets)
        monkeypatch.setattr(
            duplicate_module, "_strong_pair_fact_ids",
            lambda _connection, rows, _locations, _registry, _period: {
                row["id"] for row in rows
            },
        )
        exhaustive = duplicates.unresolved(connection, subject_ids=targets)
    assert optimized == exhaustive
    assert any(
        {item["subject_id"], item["candidate_subject_id"]}
        == {"target-0", "outside-target"}
        for item in optimized
    )


def test_strong_pair_discovery_does_not_scan_all_historical_revisions(company):
    engine, party = company
    proof = evidence(engine, "current-pair-discovery")
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        first = write_fact(engine.store, connection, "first", expense(engine, party), (proof,))
        second = write_fact(engine.store, connection, "second", expense(engine, party), (proof,))
        other = write_fact(
            engine.store, connection, "revised", expense(engine, party, amount=1), (proof,)
        )

        def measured():
            steps = 0

            def progress():
                nonlocal steps
                steps += 100
                return 0

            rows = [{"id": item.id} for item in (first, second, other)]
            connection.set_progress_handler(progress, 100)
            try:
                result = duplicate_module._strong_pair_fact_ids(
                    connection, rows, set(), engine.store.registry, YearMonth("2026-01").ordinal
                )
            finally:
                connection.set_progress_handler(None, 0)
            return result, steps

        before, initial_steps = measured()
        for revision in range(2, 502):
            other = write_fact(
                engine.store, connection, "revised",
                expense(engine, party, amount=revision), (proof,), revision=revision,
            )
        after, revised_steps = measured()
        assert before == after == {first.id, second.id}
        # Only three current businesses are relevant. Historical revisions must
        # not multiply every domain's candidate scan through fact_period.
        assert revised_steps < initial_steps + 5000


def test_public_prepare_save_and_reuse_repeat_authoritative_check(company):
    engine, party = company
    proof = evidence(engine, "public-registration")
    data = expense(engine, party).model_dump(mode="json")
    first = engine.save_fact(
        "expense",
        "expense-a",
        data,
        evidence=(proof,),
        expected_revision=0,
        request_id="save-a",
    )
    prepared = Duplicates(engine).prepare_fact_registration(
        "expense",
        "expense-b",
        data,
        evidence=(proof,),
        expected_revision=0,
    )
    assert prepared["schema_version"] == 1
    assert prepared["status"] == "review_required"
    with pytest.raises(KernelError) as blocked:
        engine.save_fact(
            "expense",
            "expense-b",
            data,
            evidence=(proof,),
            expected_revision=0,
            request_id="blocked-b",
        )
    assert blocked.value.code == "duplicate_review_required"
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT 1 FROM subject WHERE id='expense-b'").fetchone() is None

    reused = engine.save_fact(
        "expense",
        "expense-b",
        data,
        evidence=(proof,),
        expected_revision=0,
        request_id="reuse-b",
        review=owner_review(
            prepared,
            proof,
            action="reuse_existing",
            candidate_subject_id="expense-a",
        ).model_dump(mode="json"),
    )
    assert reused["status"] == "reused"
    assert reused["fact_id"] == first["fact_id"]
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT 1 FROM subject WHERE id='expense-b'").fetchone() is None


def test_business_status_exposes_review_and_exact_registered_entity(company):
    engine, party = company
    proof = evidence(engine, "business-status")
    saved = engine.save_fact(
        "expense",
        "expense-status",
        expense(engine, party).model_dump(mode="json"),
        evidence=(proof,),
        expected_revision=0,
        request_id="save-status",
    )
    response = Dashboard(engine).business_status("2026-01", "expense-status")
    assert response["schema_version"] == 5
    checks = response["data"]["duplicate_checks"]
    assert checks["status"] == "clear"
    assert checks["strong_candidates"] == checks["weak_candidates"] == []
    assert checks["check_count"] == 1 and not checks["checks_truncated"]
    assert checks["checks"][0]["result_fact_id"] == saved["fact_id"]
    assert response["data"]["identity_corrections"] == []
    assert response["data"]["entity_references"] == [
        {
            "fact_id": saved["fact_id"],
            "path": "counterparty_id",
            "recorded_entity_id": party,
            "current_entity_id": party,
            "role": "counterparty",
        }
    ]


def test_public_batch_duplicate_rolls_back_then_accepts_bound_separation(company):
    engine, party = company
    proof = evidence(engine, "batch-public")
    data = expense(engine, party).model_dump(mode="json")
    records = [
        {
            "kind": "expense",
            "subject_id": subject,
            "data": data,
            "evidence": (proof,),
            "expected_revision": 0,
        }
        for subject in ("batch-a", "batch-b")
    ]
    with pytest.raises(KernelError) as blocked:
        engine.save_facts(records, request_id="batch-blocked")
    assert blocked.value.code == "duplicate_review_required"
    candidate = blocked.value.details["candidate_preview"]
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM subject WHERE id IN('batch-a','batch-b')"
            ).fetchone()[0]
            == 0
        )

    reviewed = [
        records[0],
        records[1]
        | {
            "review": owner_review(candidate, proof).model_dump(mode="json"),
        },
    ]
    result = engine.save_facts(reviewed, request_id="batch-reviewed")
    assert [item["status"] for item in result["results"]] == ["confirmed", "confirmed"]
    with engine.store.connection(read_only=True) as connection:
        assert DuplicateCandidates(engine.store).unresolved(connection) == []
        verify_duplicate_checks(connection)
