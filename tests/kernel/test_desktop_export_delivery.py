"""Verified desktop copies preserve existing files and reuse matching content."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / ".agents/skills/accounting-operator/scripts/copy-export-to-desktop.ps1"
)
POWERSHELL = shutil.which("pwsh")
pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="PowerShell 7 is unavailable")


def deliver(source, desktop, *, checksum=None, name="公司9月代发表.xlsx"):
    return subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-File",
            str(SCRIPT),
            "-SourcePath",
            str(source),
            "-ExpectedSha256",
            checksum or hashlib.sha256(source.read_bytes()).hexdigest(),
            "-FileName",
            name,
            "-DestinationDirectory",
            str(desktop),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def test_copy_reuses_matching_file_and_keeps_changed_versions(tmp_path):
    desktop = tmp_path / "desktop"
    desktop.mkdir()
    source = tmp_path / "verified.xlsx"
    source.write_bytes(b"verified first export")
    copied = deliver(source, desktop)
    assert copied.returncode == 0, copied.stderr
    first = json.loads(copied.stdout)
    assert first["status"] == "copied"
    assert Path(first["file_path"]).read_bytes() == source.read_bytes()
    repeated = deliver(source, desktop)
    assert repeated.returncode == 0, repeated.stderr
    assert json.loads(repeated.stdout) == {**first, "status": "reused"}

    source.write_bytes(b"verified revised export")
    revised = deliver(source, desktop)
    assert revised.returncode == 0, revised.stderr
    second = json.loads(revised.stdout)
    assert second["status"] == "copied"
    assert second["file_path"] != first["file_path"]
    assert Path(first["file_path"]).read_bytes() == b"verified first export"
    assert Path(second["file_path"]).read_bytes() == source.read_bytes()
    repeated_revision = deliver(source, desktop)
    assert repeated_revision.returncode == 0, repeated_revision.stderr
    assert json.loads(repeated_revision.stdout) == {**second, "status": "reused"}
    assert len(list(desktop.iterdir())) == 2


def test_hash_mismatch_and_filename_escape_do_not_copy(tmp_path):
    desktop = tmp_path / "desktop"
    desktop.mkdir()
    source = tmp_path / "verified.xlsx"
    source.write_bytes(b"verified export")
    wrong_hash = deliver(source, desktop, checksum="0" * 64)
    assert wrong_hash.returncode != 0
    assert "DESKTOP_EXPORT_SOURCE_HASH_MISMATCH" in wrong_hash.stderr
    escaped = deliver(source, desktop, name="../outside.xlsx")
    assert escaped.returncode != 0
    assert not list(desktop.iterdir())
    assert not (tmp_path / "outside.xlsx").exists()


def test_existing_directory_is_preserved_and_copy_gets_another_name(tmp_path):
    desktop = tmp_path / "desktop"
    desktop.mkdir()
    occupied = desktop / "公司9月代发表.xlsx"
    occupied.mkdir()
    (occupied / "keep.txt").write_text("keep", encoding="utf-8")
    source = tmp_path / "verified.xlsx"
    source.write_bytes(b"verified export")
    result = deliver(source, desktop)
    assert result.returncode == 0, result.stderr
    delivered = Path(json.loads(result.stdout)["file_path"])
    assert delivered.parent == desktop
    assert delivered.read_bytes() == source.read_bytes()
    assert (occupied / "keep.txt").read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("target_exists", [False, True])
def test_existing_symlink_is_preserved_and_copy_gets_another_name(tmp_path, target_exists):
    desktop = tmp_path / "desktop"
    desktop.mkdir()
    owner_file = tmp_path / "owner-original.xlsx"
    if target_exists:
        owner_file.write_bytes(b"owner original")
    occupied = desktop / "公司9月代发表.xlsx"
    try:
        occupied.symlink_to(owner_file)
    except OSError as exc:
        pytest.skip(f"File symlinks are unavailable: {exc}")
    original_link = occupied.readlink()
    source = tmp_path / "verified.xlsx"
    source.write_bytes(b"verified export")
    result = deliver(source, desktop)
    assert result.returncode == 0, result.stderr
    delivered = Path(json.loads(result.stdout)["file_path"])
    assert delivered != occupied and delivered.read_bytes() == source.read_bytes()
    assert occupied.is_symlink() and occupied.readlink() == original_link
    assert owner_file.exists() == target_exists
    if target_exists:
        assert owner_file.read_bytes() == b"owner original"
    assert set(desktop.iterdir()) == {occupied, delivered}


def test_symlink_appearing_after_the_last_target_check_is_preserved(tmp_path, monkeypatch):
    desktop = tmp_path / "desktop"
    desktop.mkdir()
    source = tmp_path / "verified.xlsx"
    source.write_bytes(b"verified export")
    owner_target = tmp_path / "missing-owner.xlsx"
    waiting_link = tmp_path / "waiting-link"
    try:
        waiting_link.symlink_to(owner_target)
    except OSError as exc:
        pytest.skip(f"File symlinks are unavailable: {exc}")
    original_link = waiting_link.readlink()
    wrapper = tmp_path / "controlled-race.ps1"
    delivery_script = str(SCRIPT).replace("'", "''")
    link_path = str(waiting_link).replace("'", "''")
    wrapper.write_text(
        "param($SourcePath, $ExpectedSha256, $FileName, $DestinationDirectory)\n"
        f"$deliveryScript = '{delivery_script}'\n"
        f"$global:desktopExportTestWaitingLink = '{link_path}'\n"
        "$global:desktopExportTestRacePath = [IO.Path]::Combine($DestinationDirectory, $FileName)\n"
        "$global:desktopExportTestInserted = $false\n"
        "function Get-Item {\n"
        "    [CmdletBinding()]\n"
        "    param([string]$LiteralPath, [switch]$Force)\n"
        "    $found = Microsoft.PowerShell.Management\\Get-Item @PSBoundParameters\n"
        "    if ($LiteralPath -eq $global:desktopExportTestRacePath "
        "-and -not $global:desktopExportTestInserted) {\n"
        "        if ($null -eq $found) {\n"
        "            [IO.File]::Move($global:desktopExportTestWaitingLink, "
        "$global:desktopExportTestRacePath)\n"
        "            $global:desktopExportTestInserted = $true\n"
        "        }\n"
        "    }\n"
        "    return $found\n"
        "}\n"
        "& $deliveryScript -SourcePath $SourcePath -ExpectedSha256 $ExpectedSha256 "
        "-FileName $FileName -DestinationDirectory $DestinationDirectory\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(globals(), "SCRIPT", wrapper)
    result = deliver(source, desktop)
    assert result.returncode == 0, result.stderr
    delivered = Path(json.loads(result.stdout)["file_path"])
    occupied = desktop / "公司9月代发表.xlsx"
    assert occupied.is_symlink() and occupied.readlink() == original_link
    assert not owner_target.exists() and not waiting_link.is_symlink()
    assert delivered != occupied and delivered.read_bytes() == source.read_bytes()
    assert set(desktop.iterdir()) == {occupied, delivered}


def test_concurrent_deliveries_preserve_both_files(tmp_path):
    desktop = tmp_path / "desktop"
    desktop.mkdir()
    sources = [tmp_path / "first.xlsx", tmp_path / "second.xlsx"]
    for index, source in enumerate(sources):
        source.write_bytes(f"verified export {index}".encode())
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda source: deliver(source, desktop), sources))
    assert all(result.returncode == 0 for result in results), [r.stderr for r in results]
    paths = [Path(json.loads(result.stdout)["file_path"]) for result in results]
    assert len(set(paths)) == 2
    assert {path.read_bytes() for path in paths} == {source.read_bytes() for source in sources}


def test_concurrent_same_content_deliveries_reuse_a_complete_large_file(tmp_path):
    desktop = tmp_path / "desktop"
    desktop.mkdir()
    source = tmp_path / "verified.xlsx"
    source.write_bytes(b"verified export\n" * (2 * 1024 * 1024))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: deliver(source, desktop), range(2)))
    assert all(result.returncode == 0 for result in results), [r.stderr for r in results]
    receipts = [json.loads(result.stdout) for result in results]
    assert sorted(receipt["status"] for receipt in receipts) == ["copied", "reused"]
    paths = {Path(receipt["file_path"]) for receipt in receipts}
    assert len(paths) == 1 and list(desktop.iterdir()) == list(paths)
    assert next(iter(paths)).read_bytes() == source.read_bytes()
