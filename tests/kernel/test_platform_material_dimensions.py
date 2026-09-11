"""Original signed bank and platform rows cover two sides of one conserved transfer."""

import pytest
from test_banking import entry, match, opening, reconciliation, statement
from test_deletion_boundaries import book as book
from test_materials import Company, codes

from ai_accounting.kernel.contracts import KernelError, Read
from ai_accounting.kernel.materials import Materials
from ai_accounting.kernel.periods import Periods


def save(company, kind, subject, data, revision=0):
    evidence = tuple(
        dict.fromkeys((company.proof, data.get("source_evidence_digest", company.proof)))
    )
    return company.engine.save_fact(
        kind,
        subject,
        data,
        evidence=evidence,
        expected_revision=revision,
        request_id=company.request(),
    )


def publish(company, *subjects, correction_period=None):
    preview = company.engine.preview(list(subjects), correction_period=correction_period)
    return company.engine.confirm(
        list(subjects),
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        correction_period=correction_period,
        request_id=company.request(),
    )


def setup_transfer(company, direction="bank_to_platform", *, separate_bank_columns=False):
    bank_sign = 1 if direction == "platform_to_bank" else -1
    bank_options = {}
    bank_content = f"name,amount,period\nbank,{bank_sign * 10},2026-01\n".encode()
    if separate_bank_columns:
        bank_content = (
            f"name,income,outgoing,period\nbank,{10 if bank_sign > 0 else 0},"
            f"{10 if bank_sign < 0 else 0},2026-01\n"
        ).encode()
        bank_options["spec"] = {
            "format": "csv",
            "columns": [
                {"column": "A", "role": "context"},
                {"column": "B", "role": "amount", "funds_direction": "inflow"},
                {"column": "C", "role": "amount", "funds_direction": "outflow"},
                {"column": "D", "role": "recognition_period"},
            ],
        }
    bank_source, _ = company.source(
        bank_content,
        subject="bank-original",
        category="bank",
        **bank_options,
    )
    platform_source, proof = company.source(
        f"name,amount,period\nplatform,{-bank_sign * 10},2026-01\n".encode(),
        subject="platform-original",
        category="bank",
    )
    save(
        company,
        "platform_movement",
        "movement",
        {
            "period": "2026-01",
            "platform_account_id": "platform",
            "actual_date": "2026-01-02",
            "amount_fen": 1000,
            "direction": "outflow" if bank_sign > 0 else "inflow",
            "source_evidence_digest": proof,
            "source_location": "CSV!B2",
            "transaction_reference": "actual-reference",
        },
    )
    saved = save(
        company,
        "bank_platform_transfer",
        "transfer",
        {
            "period": "2026-01",
            "actual_date": "2026-01-02",
            "direction": direction,
            "bank_account_id": "bank",
            "platform_account_id": "platform",
            "amount_fen": 1000,
            "movement_ids": ["movement"],
        },
    )
    results = publish(company, "movement", "transfer")["results"]
    result = next(r for r in results if r["subject_id"] == "transfer")
    link = {
        "subject_id": "transfer",
        "fact_kind": "bank_platform_transfer",
        "fact_id": saved["fact_id"],
        "calculation_id": result["calculation_id"],
        "recognition_period": "2026-01",
    }
    return bank_source, platform_source, link, bank_sign


def resolve_sides(company, bank, platform, link, bank_sign):
    company.resolve(
        bank,
        "CSV!B2",
        [{**link, "amount_fen": bank_sign * 1000, "amount_field": "fact.amount_fen"}],
        subject="bank-link",
    )
    company.resolve(
        platform,
        "CSV!B2",
        [{**link, "amount_fen": -bank_sign * 1000, "amount_field": "result.platform_amount_fen"}],
        subject="platform-link",
    )


@pytest.mark.parametrize("direction", ["bank_to_platform", "platform_to_bank"])
def test_both_original_signed_sides_cover_one_transfer_and_one_voucher(tmp_path, direction):
    company = Company(tmp_path)
    bank, platform, link, sign = setup_transfer(company, direction)
    resolve_sides(company, bank, platform, link, sign)
    result = company.materials.check("2026-01")
    assert result["status"] == "complete", result["issues"]
    with company.engine.store.connection(read_only=True) as connection:
        calc = company.engine.store.select(
            connection, Read("calculation", "bank_platform_transfer", "@transfer")
        )[0]
        assert calc.values["bank_amount_fen"] == calc.values["platform_amount_fen"] == 1000
        assert calc.values["bank_direction"] == ("inflow" if sign > 0 else "outflow")
        assert calc.values["platform_direction"] == ("outflow" if sign > 0 else "inflow")
        assert connection.execute("SELECT COUNT(*) FROM voucher").fetchone()[0] == 1
        assert connection.execute("SELECT SUM(amount) FROM balance").fetchone()[0] == 0
    assert company.engine.overview("2026-01")["cashflow"] == []
    schema = company.engine.store.registry.schemas()["bank_platform_transfer"]
    assert schema["x-material-amount-dimensions"]["platform"]["account_field"] == (
        "result.platform_account_id"
    )


@pytest.mark.parametrize(
    "field", ["fact.amount_fen", "result.amount_fen", "result.bank_amount_fen"]
)
def test_alternative_bank_field_names_share_same_original_capacity(tmp_path, field):
    company = Company(tmp_path)
    bank, platform, link, sign = setup_transfer(company)
    resolve_sides(company, bank, platform, link, sign)
    other, _ = company.source(b"name,amount,period\nother,-10,2026-01\n", subject="other")
    company.resolve(
        other,
        "CSV!B2",
        [{**link, "amount_fen": -1000, "amount_field": field}],
        subject="other-link",
    )
    assert "material_business_overallocated" in codes(company.materials.check("2026-01"))


@pytest.mark.parametrize("direction", ["bank_to_platform", "platform_to_bank"])
@pytest.mark.parametrize("side", ["bank", "platform"])
def test_same_side_cannot_change_field_to_claim_the_opposite_side(tmp_path, direction, side):
    company = Company(tmp_path)
    bank, platform, link, sign = setup_transfer(company, direction)
    source, signed, field = (
        (bank, sign * 1000, "result.platform_amount_fen")
        if side == "bank"
        else (platform, -sign * 1000, "result.amount_fen")
    )
    company.resolve(source, "CSV!B2", [{**link, "amount_fen": signed, "amount_field": field}])
    assert "material_funds_direction_mismatch" in codes(company.materials.check("2026-01"))


def group_data(company, source, link, amounts, field):
    return {
        "period": "2026-01",
        "source_id": source["subject_id"],
        "source_fact_id": source["fact_id"],
        "members": [
            {"location": f"CSV!B{i + 2}", "amount_fen": amount} for i, amount in enumerate(amounts)
        ],
        "group_amount_fen": sum(amounts),
        "links": [{**link, "amount_fen": sum(amounts), "amount_field": field}],
        "joint_basis_confirmed": True,
        "basis_evidence_digest": company.proof,
        "basis_location": "confirmed synthetic group",
        "reason": "Explicit complete source group",
    }


@pytest.mark.parametrize(
    "field,allowed", [("result.bank_amount_fen", True), ("result.platform_amount_fen", False)]
)
def test_group_uses_original_same_direction_members_not_abs_capacity(tmp_path, field, allowed):
    company = Company(tmp_path)
    _, _, link, _ = setup_transfer(company)
    source, proof = company.source(
        b"name,amount,period\na,-4,2026-01\nb,-6,2026-01\n", subject="group-original"
    )
    data = group_data(company, source, link, [-400, -600], field)
    if allowed:
        company.materials.resolve_group(
            "group",
            data,
            evidence=(proof, company.proof),
            expected_revision=0,
            request_id=company.request(),
        )
        assert "material_funds_direction_mismatch" not in codes(company.materials.check("2026-01"))
    else:
        with pytest.raises(KernelError) as error:
            company.materials.resolve_group(
                "group",
                data,
                evidence=(proof, company.proof),
                expected_revision=0,
                request_id=company.request(),
            )
        assert error.value.code == "material_funds_direction_mismatch"
        with company.engine.store.connection(read_only=True) as connection:
            assert connection.execute("SELECT 1 FROM subject WHERE id='group'").fetchone() is None


def test_mixed_sign_original_group_cannot_net_into_a_transfer_side(tmp_path):
    company = Company(tmp_path)
    _, _, link, _ = setup_transfer(company)
    source, proof = company.source(
        b"name,amount,period\na,-14,2026-01\nb,4,2026-01\n", subject="group-original"
    )
    data = group_data(company, source, link, [-1400, 400], "result.bank_amount_fen")
    with pytest.raises(KernelError) as error:
        company.materials.resolve_group(
            "group",
            data,
            evidence=(proof, company.proof),
            expected_revision=0,
            request_id=company.request(),
        )
    assert error.value.code == "invalid_fact"


def test_new_original_material_version_does_not_reuse_old_side_mapping(tmp_path):
    company = Company(tmp_path)
    bank, platform, link, sign = setup_transfer(company)
    resolve_sides(company, bank, platform, link, sign)
    with company.engine.store.connection(read_only=True) as connection:
        old = company.engine.store.current_fact(connection, "bank-original")
    proof = company.evidence(b"name,amount,period\ncorrected bank description,-10,2026-01\n")
    with pytest.raises(KernelError) as error:
        company.materials.receive(
            "bank-original",
            old.fact.model_dump(mode="json")
            | {
                "evidence_digest": proof,
            },
            evidence=(proof, company.proof),
            expected_revision=1,
            request_id=company.request(),
        )
    assert error.value.code == "immutable_fact"
    assert company.materials.check("2026-01")["status"] == "complete"
    data = old.fact.model_dump(mode="json")
    data["specification"]["columns"][1]["label"] = "confirmed signed bank side"
    company.materials.receive(
        "bank-original",
        data,
        evidence=(old.fact.evidence_digest, company.proof),
        expected_revision=1,
        request_id=company.request(),
    )
    assert "material_source_changed" in codes(company.materials.check("2026-01"))


def test_closed_sides_remain_frozen_and_new_original_revision_needs_review(book):
    engine, save_fact, publish_fact, close, _, proof = book
    company = object.__new__(Company)
    company.engine, company.materials, company.proof, company.sequence = (
        engine,
        Materials(engine),
        proof,
        1000,
    )
    bank, platform, link, sign = setup_transfer(company, "platform_to_bank")
    resolve_sides(company, bank, platform, link, sign)
    opening(save_fact, publish_fact, bank="bank", month="2026-01")
    statement(
        save_fact,
        publish_fact,
        [entry("transfer", "2026-01-02", 1000)],
        bank="bank",
        month="2026-01",
    )
    reconciliation(
        save_fact,
        publish_fact,
        [match("transfer", "bank_platform_transfer", "transfer")],
        bank="bank",
        month="2026-01",
    )
    close("2026-01")
    frozen = Periods(engine).closed_report("2026-01")
    with engine.store.connection(read_only=True) as connection:
        old = engine.store.current_fact(connection, "transfer")
    with pytest.raises(KernelError) as error:
        save_fact(
            "bank_platform_transfer",
            "transfer",
            old.fact.model_dump(mode="json")
            | {
                "actual_date": "2026-01-03",
            },
            revision=1,
        )
    assert error.value.code == "immutable_fact"
    engine.amend_fact(
        "bank_platform_transfer",
        "transfer",
        old.fact.model_dump(mode="json"),
        evidence=(proof,),
        expected_revision=1,
        recording_error_confirmed=True,
        request_id=company.request(),
    )
    publish(company, "transfer", correction_period="2026-02")
    assert "material_result_stale" in codes(company.materials.check("2026-01"))
    assert Periods(engine).closed_report("2026-01") == frozen
