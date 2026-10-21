"""Actual offline check/run, material copying and final target publication."""

from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.host.public import create_offline_application
from ai_accounting_modular.records.public import open_standard_bundle
from ai_accounting_modular.storage.public import open_database_unit

from ._working_case import case as case


def read_target(app, root):
    return open_database_unit(
        root / "runtime/company.sqlite3", repositories=app.repositories, company_root=root
    )


def source_bundle(case):
    with case.kernel.storage.read_unit(case.company) as read:
        metadata = case.kernel.storage.company_metadata(read)
    return metadata, open_standard_bundle(metadata.company_root / "standard/current.json")


def request(action, mode, source, target):
    return {
        "action": action,
        "mode": mode,
        "source_path": str(source),
        "new_target_directory": str(target),
    }


def test_offline_rebuild_check_does_not_create_target_and_run_delivers_a_new_independent_company(
    case, tmp_path
):
    fact = case.seed()
    metadata, bundle = source_bundle(case)
    offline = create_offline_application()
    assert offline.storage is None
    target = tmp_path / "uncreated-parent" / "rebuilt"
    before = {
        item.relative_path: sha256(
            (bundle.company_root / item.relative_path).read_bytes()
        ).hexdigest()
        for item in bundle.files
    }
    checked = offline.accounting_isolated(request("rebuild", "check", bundle.manifest_path, target))
    assert checked["state"] == "checked", checked
    assert not target.parent.exists()
    assert checked["source_digest"] == sha256(bundle.manifest_path.read_bytes()).hexdigest()
    result = offline.accounting_isolated(request("rebuild", "run", bundle.manifest_path, target))
    assert result["state"] == "created", result
    assert result["company_id"] != metadata.company_id
    assert result["database_id"] != metadata.database_id
    assert before == {
        item.relative_path: sha256(
            (bundle.company_root / item.relative_path).read_bytes()
        ).hexdigest()
        for item in bundle.files
    }
    with read_target(offline, target) as read:
        assert read.company_id == result["company_id"]
        result_view = offline.ledger.get_results([fact["business_id"]], read)[0]
        assert result_view["reference"]["generation"] == 1
        report = offline.ledger.financial_snapshot(
            {
                "company_id": read.company_id,
                "period_from": "2026-01",
                "period_to": "2026-01",
                "view": "current",
            },
            read,
        )
        assert report["pre_tax"]["profit_before_tax"]["amount_fen"] == "-100000"
        assert offline.records.document_status(read)["state"] == "ready"
    assert not list(target.parent.glob(".accounting-build-*"))


def test_real_backup_task_restore_preserves_formal_identities_and_copies_only_manifest_inputs(
    case, tmp_path, monkeypatch
):
    fact = case.seed()
    assert case.close(case.check())["state"] == "closed"
    metadata, _ = source_bundle(case)
    app = case.app
    with case.kernel.storage.read_unit(case.company) as read:
        original = app.ledger.get_results([fact["business_id"]], read)
    pending = app.accounting_backup(
        {"company_id": case.company, "expected_database_id": metadata.database_id}
    )
    duplicate = app.accounting_backup(
        {"company_id": case.company, "expected_database_id": metadata.database_id}
    )
    assert pending["task_id"] == duplicate["task_id"]
    assert pending["state"] == "pending"
    done = case.kernel.files.run(case.company, pending["task_id"])
    assert done["state"] == "succeeded", done
    package = Path(done["path"])
    assert sha256(package.read_bytes()).hexdigest() == done["sha256"]
    offline = create_offline_application()
    opening = offline.backup_verifier.extract

    @contextmanager
    def extra_scratch_file(stream):
        with opening(stream) as verified:
            (verified.company_root / "runtime/unlisted-scratch.tmp").write_bytes(
                b"not an archive input"
            )
            yield verified

    monkeypatch.setattr(offline.backup_verifier, "extract", extra_scratch_file)
    target = tmp_path / "restored"
    checked = offline.accounting_isolated(request("restore", "check", package, target))
    assert checked["state"] == "checked", checked
    assert not target.exists()
    result = offline.accounting_isolated(request("restore", "run", package, target))
    assert result["state"] == "created", result
    assert result["source_digest"] == done["sha256"]
    assert result["company_id"] == metadata.company_id
    assert result["database_id"] == metadata.database_id
    assert not (target / "runtime/unlisted-scratch.tmp").exists()
    with read_target(offline, target) as read:
        assert offline.ledger.get_results([fact["business_id"]], read) == original
        assert offline.ledger.closed_through(read) == "2026-01"
        work = offline.work_scope(read)
        assert work.has_formal_results
        assert work.working_period_to == work.closed_through == "2026-01"
        assert read.repository("files").current() == ()
    assert not list(target.parent.glob(".accounting-build-*"))


def test_final_integrity_failure_leaves_no_delivered_target_or_owned_staging(
    case, tmp_path, monkeypatch
):
    case.seed()
    _, bundle = source_bundle(case)
    offline = create_offline_application()
    verify = offline.backup_verifier.verify_snapshot

    def fail_after_actual_checks(read):
        verify(read)
        raise ContractError("conflict", "最后交付核验失败。")

    monkeypatch.setattr(offline.backup_verifier, "verify_snapshot", fail_after_actual_checks)
    target = tmp_path / "not-delivered"
    result = offline.accounting_isolated(request("rebuild", "run", bundle.manifest_path, target))
    assert result["state"] == "failed"
    assert result["message"] == "最后交付核验失败。"
    assert not target.exists()
    assert not list(target.parent.glob(".accounting-build-*"))


@pytest.mark.parametrize("mode", ["check", "run"])
def test_existing_empty_target_is_preserved_in_both_modes(case, tmp_path, mode):
    case.seed()
    _, bundle = source_bundle(case)
    target = tmp_path / "already-exists"
    target.mkdir()
    offline = create_offline_application()
    result = offline.accounting_isolated(request("rebuild", mode, bundle.manifest_path, target))
    assert result["state"] == "failed"
    assert result["error"] == "target_exists"
    assert target.is_dir() and list(target.iterdir()) == []
