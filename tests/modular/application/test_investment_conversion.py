"""The predecessor's real synthetic backup survives explicit contract conversion."""

import hashlib
import json
import shutil
import sqlite3
import zipfile
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import (
    ContractError,
    canonical_json,
    validate_contract,
    validate_conversion_envelope,
)
from ai_accounting_modular.host.public import create_kernel, create_offline_application
from ai_accounting_modular.records.public import (
    open_investment_conversion_bundle,
    open_standard_bundle,
)
from ai_accounting_modular.storage.public import OwnerProfile, create_catalog, open_database_unit
from tools.upgrade_business_descriptions import convert_structure

FIXTURE = Path(__file__).with_name("fixtures") / "investment-pre-separation.finance-company.zip"


def _predecessor(tmp_path, fixture=FIXTURE):
    app, root = create_offline_application(), tmp_path / "original"
    with fixture.open("rb") as stream, app.backup_verifier.extract(stream) as extracted:
        # Prepare the current SQL structure before exercising investment conversion.
        manifest = extracted.manifest
        contract_path = extracted.company_root / "contracts/company.json"
        source_contract = json.loads(contract_path.read_bytes())
        contract, _ = convert_structure(
            extracted.company_root / "runtime/company.sqlite3", source_contract
        )
        contract_path.write_text(canonical_json(contract) + "\n", encoding="utf-8")
        for item in manifest["files"]:
            path = extracted.company_root / item["path"]
            item.update(
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(), byte_size=path.stat().st_size
            )
        manifest["structure_fingerprint"] = contract["fingerprint"]
        app.backup_verifier.verify_extracted(
            extracted.company_root, manifest, investment_conversion=True
        )
        with zipfile.ZipFile(tmp_path / "current-structure.finance-company.zip", "w") as current:
            current.writestr("manifest.json", canonical_json(manifest))
            for item in manifest["files"]:
                current.write(extracted.company_root / item["path"], item["path"])
        shutil.copytree(extracted.company_root, root)
    bundle = open_investment_conversion_bundle(root / "standard/current.json")
    terms = next(fact for fact in bundle.facts if fact["kind"] == "investment.terms")
    basis = {
        "business_id": str(uuid4()),
        "kind": "taxes.investment_basis",
        "period": "2026-08",
        "evidence_ids": terms["evidence_ids"],
        "data": {
            "terms_id": terms["business_id"],
            "tax_facts": deepcopy(terms["data"]["tax_facts"]),
            "applicable_from": "2026-07-01",
        },
    }
    return app, root, bundle, terms, basis


@pytest.fixture
def predecessor(tmp_path):
    return _predecessor(tmp_path)


def _read(app, root):
    return open_database_unit(
        root / "runtime/company.sqlite3", repositories=app.repositories, company_root=root
    )


def _digest_files(root):
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file() and not path.name.endswith(("-wal", "-shm"))
    }


def _close_records(root):
    with sqlite3.connect(root / "runtime/company.sqlite3") as sql:
        return sql.execute("SELECT * FROM period_close ORDER BY period").fetchall()


@pytest.mark.parametrize(
    ("source_kind", "applicable_from"),
    [("company", "2026-07-01"), ("backup", "2026-07-01"), ("company", "2026-08-01")],
)
def test_conversion_keeps_original_frozen_and_open_results(
    predecessor, tmp_path, source_kind, applicable_from
):
    app, root, bundle, terms, basis = predecessor
    basis["data"]["applicable_from"] = applicable_from
    ids = [fact["business_id"] for fact in bundle.facts]
    with _read(app, root) as read:
        before = app.ledger.get_results(ids, read)
        heads = read.repository("application").heads(ids)
        old_terms = next(row for row in before if row["target"]["kind"] == "investment.terms")
        purchase = next(row for row in before if row["target"]["kind"] == "investment.acquire")
        assert old_terms["reference"]["state"] == purchase["reference"]["state"] == "frozen"
        assert "tax_event" in purchase["target"]["values"]
        old_pool = next(
            row for row in before if row["reference"]["result_slot"] == "financial_transfer:2026-07"
        )
        assert (
            old_pool["target"]["values"]["positions"][0]["vat_pool_identity"]["legal_form"]
            == "securities_fund"
        )
    source = tmp_path / "current-structure.finance-company.zip" if source_kind == "backup" else root
    source_before = source.read_bytes() if source_kind == "backup" else _digest_files(root)
    closed_before = _close_records(root)
    target = tmp_path / "converted"
    outcome = app.convert_investment_basis(source, target, [basis], source_kind=source_kind)
    assert outcome["state"] == "created"
    assert _close_records(target) == closed_before
    with _read(app, target) as read:
        app.backup_verifier.verify_snapshot(read)
        assert app.ledger.get_results(ids, read) == before
        assert app.ledger.closed_through(read) == "2026-07"
        new_heads = read.repository("application").heads(ids)
        assert {key: row["seq"] for key, row in new_heads.items()} == {
            key: row["seq"] for key, row in heads.items()
        }
        (adopted,) = app.ledger.get_results([basis["business_id"]], read)
        assert (
            adopted["basis"]["source_results"][0]["result_id"]
            == old_terms["reference"]["result_id"]
        )
    assert (
        source.read_bytes() if source_kind == "backup" else _digest_files(root)
    ) == source_before
    facts = open_standard_bundle(target / "standard/current.json").facts
    converted = next(f for f in facts if f["business_id"] == terms["business_id"])
    assert converted["data"]["right_type"] == "fund_units"
    assert "tax_facts" not in converted["data"]

    # Continue through the real application using the original independent tax
    # price. A capitalised acquisition fee must not become a VAT purchase price.
    catalog = tmp_path / "new-catalog.sqlite3"
    create_catalog(catalog, OwnerProfile(str(uuid4()), "合成接续负责人", "synthetic-only"))
    kernel = create_kernel(catalog)
    try:
        kernel.storage.register_company(target)
        original_purchase = next(f for f in bundle.facts if f["kind"] == "investment.acquire")
        sold = {
            "business_id": str(uuid4()),
            "kind": "investment.dispose",
            "period": "2026-08",
            "effective_on": "2026-08-12",
            "evidence_ids": terms["evidence_ids"],
            "data": {
                "terms_id": terms["business_id"],
                "counterparty_id": original_purchase["data"]["counterparty_id"],
                "quantity": "5",
                "gross_proceeds_fen": "601000",
                "disposal_fees_fen": "0",
                "exit_mode": "transfer",
            },
        }
        response = kernel.application.accounting_apply(
            {
                "contract": "ai-accounting-modular/1",
                "company_id": outcome["company_id"],
                "command_id": str(uuid4()),
                "mutations": [{"op": "create", "expected_seq": 0, "fact": sold}],
            }
        )
        assert response["completion"] == "complete", response
        method = next(f for f in bundle.facts if f["kind"] == "taxes.financial_transfer_basis")
        with kernel.storage.read_unit(outcome["company_id"]) as read:
            results = kernel.ledger.get_results([method["business_id"]], read)
            assert (
                next(
                    row
                    for row in results
                    if row["reference"]["result_slot"] == "financial_transfer:2026-07"
                )
                == old_pool
            )
            august = next(
                row["target"]["values"]
                for row in results
                if row["reference"]["result_slot"] == "financial_transfer:2026-08"
            )
            assert august["payable_tax_fen"] == "1000"
            assert august["disposals"][0]["vat_buy_price_fen"] == "500000"
            assert august["disposals"][0]["eit_basis_fen"] == "550000"
            kernel.application.backup_verifier.verify_snapshot(read)
    finally:
        kernel.close()


def test_old_standard_rebuild_stays_open_and_preserves_sources(predecessor, tmp_path):
    app, root, bundle, _, basis = predecessor
    target = tmp_path / "rebuilt"
    outcome = app.convert_investment_basis(
        root / "standard/current.json", target, [basis], source_kind="standard"
    )
    assert outcome["state"] == "created"
    with _read(app, target) as read:
        assert app.ledger.closed_through(read) is None
        app.backup_verifier.verify_snapshot(read)
    assert open_standard_bundle(target / "standard/current.json").sources == bundle.sources


def test_later_range_cannot_hide_earlier_required_tax_event(tmp_path):
    fixture = FIXTURE.with_name("investment-pre-separation-tax-event.finance-company.zip")
    app, root, _, terms, basis = _predecessor(tmp_path, fixture)
    basis["data"]["applicable_from"] = "2026-08-01"
    target, before = tmp_path / "failed", _digest_files(root)
    with pytest.raises(ContractError) as caught:
        app.convert_investment_basis(root, target, [basis], source_kind="company")
    assert caught.value.code == "needs_information"
    assert any(
        terms["business_id"] in issue["message"] and "2026-07-02" in issue["message"]
        for issue in caught.value.fact_issues
    ), caught.value.to_response()
    assert not target.exists()
    assert _digest_files(root) == before
    basis["data"]["applicable_from"] = "2026-07-01"
    assert (
        app.convert_investment_basis(root, target, [basis], source_kind="company")["state"]
        == "created"
    )


@pytest.mark.parametrize("bad", [None, {}, "not facts", [], [None]])
def test_invalid_plan_keeps_input_and_target_absent(predecessor, tmp_path, bad):
    app, root, _, _, _ = predecessor
    target, before = tmp_path / "failed", _digest_files(root)
    with pytest.raises(ContractError):
        app.convert_investment_basis(root, target, bad, source_kind="company")
    assert not target.exists()
    assert _digest_files(root) == before


def test_failure_during_basis_adoption_can_retry_without_partial_publication(predecessor, tmp_path):
    app, root, _, _, basis = predecessor
    second = deepcopy(basis)
    second["business_id"] = str(uuid4())
    second["data"]["applicable_from"] = "2026-07-02"
    target, before = tmp_path / "failed", _digest_files(root)
    with pytest.raises(ContractError, match="重叠"):
        app.convert_investment_basis(root, target, [basis, second], source_kind="company")
    assert not target.exists()
    assert _digest_files(root) == before
    assert (
        app.convert_investment_basis(root, target, [basis], source_kind="company")["state"]
        == "created"
    )


def test_corrupt_old_current_digest_is_not_normalized_away(predecessor, tmp_path):
    app, root, _, terms, basis = predecessor
    with sqlite3.connect(root / "runtime/company.sqlite3") as sql:
        sql.execute(
            "UPDATE business_head SET current_digest=? WHERE business_id=?",
            ("0" * 64, terms["business_id"]),
        )
    before, target = _digest_files(root), tmp_path / "failed"
    with pytest.raises(ContractError):
        app.convert_investment_basis(root, target, [basis], source_kind="company")
    assert not target.exists()
    assert _digest_files(root) == before


def test_changed_old_archive_bytes_fail_before_conversion(predecessor, tmp_path):
    app, _, _, _, basis = predecessor
    archive = tmp_path / "corrupt.zip"
    with zipfile.ZipFile(FIXTURE) as original, zipfile.ZipFile(archive, "w") as broken:
        for info in original.infolist():
            raw = original.read(info.filename)
            if info.filename == "standard/current.json":
                raw += b" "
            broken.writestr(info, raw)
    with pytest.raises(ContractError):
        app.convert_investment_basis(archive, tmp_path / "failed", [basis], source_kind="backup")
    assert not (tmp_path / "failed").exists()


def test_standard_input_cannot_silently_select_another_manifest(predecessor, tmp_path):
    app, root, _, _, basis = predecessor
    unrelated = root / "standard/not-the-selected-manifest.json"
    unrelated.write_text("{}")
    with pytest.raises(ContractError, match="current.json"):
        app.convert_investment_basis(unrelated, tmp_path / "no", [basis], source_kind="standard")


def test_legacy_issue_proof_keeps_original_body_and_does_not_relax_new_writes(predecessor):
    _, _, _, terms, _ = predecessor
    partial = deepcopy(terms)
    del partial["data"]["product_id"]
    issue = {
        "issue_id": str(uuid4()),
        "scope": "investment",
        "period": "2026-08",
        "evidence_ids": terms["evidence_ids"],
        "missing_fields": ["/data/product_id"],
        "question": "已有旧投资条款资料，尚待核对产品身份。",
        "assignee": "ai",
        "action": "clarify_reality",
        "required_before_close": None,
        "known_fact": partial,
    }
    original = deepcopy(issue)
    validate_conversion_envelope("CurrentIssue", issue)
    assert issue == original
    with pytest.raises(ContractError):
        validate_contract("core.schema.json", "CurrentIssue", issue)
    issue["known_fact"]["data"]["unexpected_old_field"] = "不能在转换时丢弃"
    with pytest.raises(ContractError):
        validate_conversion_envelope("CurrentIssue", issue)


@pytest.mark.parametrize("invalid", [{"scope": ""}, {"head": -1}, {"issues": None}])
def test_legacy_empty_scope_envelope_still_has_full_validation(invalid):
    with pytest.raises(ContractError):
        validate_conversion_envelope(
            "IssueScope", {"scope": "investment", "head": 0, "issues": [], **invalid}
        )
