"""Draft forward adjustment preserves source ZIPs and produces current portable snapshots."""

import json
import zipfile
from contextlib import closing
from pathlib import Path

import pytest
from test_development_upgrade import create_old_company, old_business

from ai_accounting.kernel import backup
from ai_accounting.kernel.offline_development_upgrade import upgrade_company
from ai_accounting.kernel.runtime import connect


@pytest.mark.parametrize("after_request", ["after-upgrade", "before-upgrade"])
def test_upgraded_draft_rollover_and_restore_keep_identity_and_frozen_history(
    tmp_path, after_request
):
    path, source, target = create_old_company(tmp_path / "company.sqlite")
    old_business(path, source)
    output = tmp_path / "backups"
    initial = backup.create_portable(path, output, _bundle=source, request_id="before-upgrade")
    original_zip = Path(initial["path"]).read_bytes()
    with closing(connect(path)) as connection:
        upgrade_company(connection, target)
        history = [tuple(row) for row in connection.execute("SELECT * FROM schema_history")]
        adjustments = [
            tuple(row) for row in connection.execute("SELECT * FROM schema_draft_history")
        ]
        closes = [tuple(row) for row in connection.execute("SELECT * FROM period_close")]
        retained = backup._retained_history_digest(connection)
    result = backup.create_portable(
        path,
        output,
        _bundle=target,
        request_id=after_request,
        rollover=True,
    )
    assert result["database_format"] == target.database_format("company")
    assert result["manifest"]["database_format"] == target.database_format("company")
    previous = output / (result["identity"]["taxpayer_id"] + ".previous.finance-company.zip")
    assert previous.read_bytes() == original_zip
    assert backup.verify_portable(previous, _bundle=source)["identity"] == result["identity"]
    with pytest.raises(backup.BackupError) as source_only:
        backup.verify_portable(previous, _bundle=target)
    assert source_only.value.code == "backup_schema_unsupported"
    checked = backup.verify_portable(result["path"], _bundle=target)
    assert checked["verification"]["status"] == "verified"
    restored = tmp_path / "restored.sqlite"
    restored_result = backup.restore_portable(result["path"], restored, _bundle=target)
    assert restored_result["identity"] == result["identity"]
    with closing(connect(restored, read_only=True)) as connection:
        assert [tuple(row) for row in connection.execute("SELECT * FROM schema_history")] == history
        assert [
            tuple(row) for row in connection.execute("SELECT * FROM schema_draft_history")
        ] == adjustments
        assert [tuple(row) for row in connection.execute("SELECT * FROM period_close")] == closes
        assert backup._retained_history_digest(connection) == retained
    replay = backup.create_portable(
        path,
        output,
        _bundle=target,
        request_id=after_request,
        rollover=True,
    )
    assert replay["idempotent_replay"] is True


def test_interrupted_upgraded_rollover_verifies_historical_pending_zip(tmp_path, monkeypatch):
    path, source, target = create_old_company(tmp_path / "company.sqlite")
    old_business(path, source)
    output = tmp_path / "backups"
    initial = backup.create_portable(path, output, _bundle=source, request_id="old")
    original_zip = Path(initial["path"]).read_bytes()
    with closing(connect(path)) as connection:
        upgrade_company(connection, target)
    replace_archive = backup._replace_archive

    def interrupted(source, destination, **kwargs):
        if str(destination).endswith(".previous.finance-company.zip"):
            raise OSError("synthetic interruption after publishing current target")
        return replace_archive(source, destination, **kwargs)

    with monkeypatch.context() as interrupted_publish:
        interrupted_publish.setattr(backup, "_replace_archive", interrupted)
        with pytest.raises(OSError):
            backup.create_portable(path, output, _bundle=target, request_id="new", rollover=True)
    result = backup.create_portable(path, output, _bundle=target, request_id="new", rollover=True)
    assert result["idempotent_replay"] is True
    previous = output / (result["identity"]["taxpayer_id"] + ".previous.finance-company.zip")
    assert previous.read_bytes() == original_zip
    assert backup.verify_portable(previous, _bundle=source)["verification"]["status"] == "verified"
    assert not list(output.glob("*.pending-previous*"))


def test_historical_rollover_cannot_replace_another_company_archive(tmp_path):
    path, source, target = create_old_company(tmp_path / "company.sqlite")
    old_business(path, source)
    other, _, _ = create_old_company(
        tmp_path / "other.sqlite",
        company_id="other-company",
        database_id="other-database",
    )
    output = tmp_path / "backups"
    initial = backup.create_portable(other, output, _bundle=source, request_id="other")
    original_zip = Path(initial["path"]).read_bytes()
    with closing(connect(path)) as connection:
        upgrade_company(connection, target)
    with pytest.raises(backup.BackupError) as rejected:
        backup.create_portable(path, output, _bundle=target, request_id="new", rollover=True)
    assert rejected.value.code == "backup_identity_mismatch"
    assert Path(initial["path"]).read_bytes() == original_zip


@pytest.mark.parametrize(
    "problem,code",
    [
        ("unknown_format", "backup_schema_unsupported"),
        ("database_digest", "backup_content_invalid"),
    ],
)
def test_retained_source_archive_requires_exact_declared_format_and_content(
    tmp_path, problem, code
):
    path, source, target = create_old_company(tmp_path / "company.sqlite")
    output = tmp_path / "backups"
    initial = backup.create_portable(path, output, _bundle=source, request_id="old")
    archive_path = Path(initial["path"])
    with zipfile.ZipFile(archive_path) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(files[backup.MANIFEST_MEMBER])
    if problem == "unknown_format":
        manifest["database_format"]["fingerprint"] = "f" * 64
    else:
        manifest["database_sha256"] = "f" * 64
    files[backup.MANIFEST_MEMBER] = json.dumps(manifest).encode()
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    rejected_zip = archive_path.read_bytes()
    with closing(connect(path)) as connection:
        upgrade_company(connection, target)
    with pytest.raises(backup.BackupError) as rejected:
        backup.create_portable(path, output, _bundle=target, request_id="new", rollover=True)
    assert rejected.value.code == code
    assert archive_path.read_bytes() == rejected_zip
