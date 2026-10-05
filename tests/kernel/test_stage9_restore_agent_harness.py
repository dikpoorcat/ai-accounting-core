"""Small test-tool guards; actual AI restore remains an independent acceptance step."""

import copy

import pytest
import stage9_restore_agent_harness as restore


def test_restore_target_is_absent_and_scoped(tmp_path, monkeypatch):
    monkeypatch.setattr(restore, "REPOSITORY", tmp_path)
    (tmp_path / ".tmp").mkdir()
    target = tmp_path / ".tmp/stage9-final-draft-agent-restore-test"
    assert restore.check_target(target, resume=False) == target
    target.mkdir()
    with pytest.raises(ValueError, match="absent"):
        restore.check_target(target, resume=False)
    with pytest.raises(ValueError, match="prepared"):
        restore.check_target(target, resume=True)
    with pytest.raises(ValueError, match="isolated"):
        restore.check_target(tmp_path / "real-data", resume=False)


@pytest.mark.parametrize("field", ["id", "database_id", "taxpayer_id"])
def test_restore_keeps_source_business_identity(field):
    source = [{"id": "original-company", "database_id": "original-db", "taxpayer_id": "A"}]
    restore.assert_restored_identities(source, [])
    restore.assert_restored_identities(source, source)
    changed = copy.deepcopy(source)
    changed[0][field] = "another"
    with pytest.raises(ValueError, match="source identities"):
        restore.assert_restored_identities(source, changed)


@pytest.mark.parametrize("field", ["root", "package", "manifest_sha256", "build_id",
                                  "database_formats", "owner_login"])
def test_resume_requires_original_target_binding(tmp_path, field):
    package = tmp_path / "package"
    record = {"format": "stage9-isolated-restore-target/1", "root": str(tmp_path / "data"),
              "package": str(package), "manifest_sha256": "manifest", "build_id": "build",
              "database_formats": {"kind": "draft"}, "owner_login": restore.LOGIN}
    options = dict(directory=tmp_path, package=package, manifest_sha="manifest",
                   build_id="build", formats={"kind": "draft"})
    restore.check_resume_record(record, **options)
    record[field] = "changed"
    with pytest.raises(ValueError, match="binding"):
        restore.check_resume_record(record, **options)


def test_restore_reuses_reviewed_error_and_receipt_preservation_helpers():
    relay = restore.relay_module()
    assert relay.process_mcp_requests.__name__ == "process_mcp_requests"
    assert relay.mcp_result_value.__name__ == "mcp_result_value"
