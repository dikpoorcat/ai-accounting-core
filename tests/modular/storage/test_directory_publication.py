"""A target which wins the publication race survives intact, even when empty."""

import ctypes
import errno
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.storage import _directory
from ai_accounting_modular.storage.public import publish_company_directory


@pytest.fixture
def source(tmp_path):
    staging = tmp_path / "staging"
    (staging / "runtime").mkdir(parents=True)
    (staging / "runtime/company.sqlite3").write_bytes(b"verified company database")
    (staging / "standard").mkdir()
    (staging / "standard/current.json").write_bytes(b"verified final documents")
    return staging


def _contents(root):
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_company_directory_is_published_as_one_move(source, tmp_path):
    target = tmp_path / "published"
    before = _contents(source)
    assert publish_company_directory(source, target) is None
    assert not source.exists()
    assert _contents(target) == before


@pytest.mark.parametrize("kind", ["empty_directory", "populated_directory", "file"])
def test_existing_target_is_never_replaced(source, tmp_path, kind):
    target = tmp_path / "existing"
    if kind == "file":
        target.write_bytes(b"outside file")
    else:
        target.mkdir()
        if kind == "populated_directory":
            (target / "existing.txt").write_bytes(b"outside contents")
    before = target.read_bytes() if target.is_file() else _contents(target)
    staged = _contents(source)
    with pytest.raises(ContractError) as exc:
        publish_company_directory(source, target)
    assert exc.value.code == "target_exists"
    assert (target.read_bytes() if target.is_file() else _contents(target)) == before
    assert _contents(source) == staged


@pytest.mark.parametrize("kind", ["empty_directory", "file"])
def test_target_created_at_the_final_os_call_wins_without_being_overwritten(
    source, tmp_path, monkeypatch, kind
):
    target = tmp_path / "race-winner"
    actual_rename = _directory._rename_no_replace

    def competing_target(staging, destination):
        if kind == "empty_directory":
            destination.mkdir()
        else:
            destination.write_bytes(b"race winner")
        return actual_rename(staging, destination)

    monkeypatch.setattr(_directory, "_rename_no_replace", competing_target)
    before = _contents(source)
    with pytest.raises(ContractError) as exc:
        publish_company_directory(source, target)
    assert exc.value.code == "target_exists"
    if kind == "empty_directory":
        assert target.is_dir() and list(target.iterdir()) == []
    else:
        assert target.read_bytes() == b"race winner"
    assert _contents(source) == before


@pytest.mark.parametrize("target_kind", ["same", "inside", "ancestor"])
def test_same_or_overlapping_directories_are_rejected(source, target_kind):
    target = {"same": source, "inside": source / "new-company", "ancestor": source.parent}[
        target_kind
    ]
    before = _contents(source)
    with pytest.raises(ContractError) as exc:
        publish_company_directory(source, target)
    assert exc.value.code == "invalid_path" and _contents(source) == before


def test_invalid_or_missing_staging_and_target_parent_create_nothing(source, tmp_path):
    for staging, target in (
        (tmp_path / "absent", tmp_path / "target"),
        (source / "runtime/company.sqlite3", tmp_path / "target"),
        (source, tmp_path / "absent-parent/target"),
    ):
        with pytest.raises(ContractError) as exc:
            publish_company_directory(staging, target)
        assert exc.value.code == "invalid_path" and not target.exists()
    assert source.is_dir() and not (tmp_path / "absent-parent").exists()


def _directory_link(link, actual):
    if sys.platform == "win32":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(actual)],
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
    else:
        link.symlink_to(actual, target_is_directory=True)


def test_staging_directory_link_is_rejected_and_external_contents_survive(source, tmp_path):
    linked = tmp_path / "linked-staging"
    _directory_link(linked, source)
    target = tmp_path / "target"
    before = _contents(source)
    with pytest.raises(ContractError) as exc:
        publish_company_directory(linked, target)
    assert exc.value.code == "invalid_path" and not target.exists()
    assert _contents(source) == before


def test_existing_target_link_is_not_followed_or_replaced(source, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_bytes(b"outside content")
    linked = tmp_path / "linked-target"
    _directory_link(linked, outside)
    before = _contents(source)
    with pytest.raises(ContractError) as exc:
        publish_company_directory(source, linked)
    assert exc.value.code == "target_exists"
    assert os.path.lexists(linked) and _contents(source) == before
    assert (outside / "keep.txt").read_bytes() == b"outside content"


def test_dangling_target_link_still_blocks_publication(source, tmp_path):
    outside = tmp_path / "removed-outside"
    outside.mkdir()
    linked = tmp_path / "dangling-target"
    _directory_link(linked, outside)
    outside.rmdir()
    before = _contents(source)
    try:
        with pytest.raises(ContractError) as exc:
            publish_company_directory(source, linked)
        assert exc.value.code == "target_exists"
        assert os.path.lexists(linked) and not linked.exists()
        assert _contents(source) == before and not outside.exists()
    finally:
        # Nonrecursive: unlink the synthetic junction itself on Windows.
        if sys.platform == "win32":
            linked.rmdir()
        else:
            linked.unlink()


def test_cross_filesystem_is_rejected_before_any_os_rename(source, tmp_path, monkeypatch):
    actual_stat = Path.stat
    target = tmp_path / "target"

    def different_device(path, *args, **kwargs):
        result = actual_stat(path, *args, **kwargs)
        if path == source:
            fields = list(result)
            fields[2] += 1
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(Path, "stat", different_device)
    monkeypatch.setattr(
        _directory, "_rename_no_replace", lambda *args: pytest.fail("cross-device rename")
    )
    with pytest.raises(ContractError) as exc:
        publish_company_directory(source, target)
    assert exc.value.code == "invalid_path" and source.is_dir() and not target.exists()


@pytest.mark.parametrize(
    "error,code",
    [
        (errno.EXDEV, "invalid_path"),
        (errno.ENOSYS, "unsupported"),
        (errno.EINVAL, "unsupported"),
        (errno.EOPNOTSUPP, "unsupported"),
        (errno.ENOTEMPTY, "target_exists"),
    ],
)
def test_kernel_rejection_never_falls_back_to_a_replacing_rename(
    source, tmp_path, monkeypatch, error, code
):
    def reject(*args):
        raise OSError(error, "native rename rejection")

    monkeypatch.setattr(_directory, "_rename_no_replace", reject)
    target = tmp_path / "target"
    before = _contents(source)
    with pytest.raises(ContractError) as exc:
        publish_company_directory(source, target)
    assert exc.value.code == code and _contents(source) == before and not target.exists()


def test_unexpected_os_error_preserves_the_owned_staging(source, tmp_path, monkeypatch):
    def denied(*args):
        raise PermissionError(errno.EACCES, "native permission rejection")

    monkeypatch.setattr(_directory, "_rename_no_replace", denied)
    with pytest.raises(PermissionError):
        publish_company_directory(source, tmp_path / "target")
    assert source.is_dir()


def test_unsupported_platform_does_not_try_plain_rename(source, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "unsupported-platform")
    monkeypatch.setattr(os, "rename", lambda *args: pytest.fail("unsafe fallback"))
    with pytest.raises(ContractError) as exc:
        publish_company_directory(source, tmp_path / "target")
    assert exc.value.code == "unsupported" and source.is_dir()


@pytest.mark.parametrize("returned_error", [0, errno.EEXIST, errno.ENOSYS])
def test_linux_abi_uses_renameat2_noreplace_and_preserves_errno(monkeypatch, returned_error):
    calls = []

    def native(*args):
        calls.append(args)
        return -1 if returned_error else 0

    def load(name, *, use_errno):
        assert name is None and use_errno
        return SimpleNamespace(renameat2=native)

    monkeypatch.setattr(ctypes, "CDLL", load)
    monkeypatch.setattr(ctypes, "get_errno", lambda: returned_error)
    source, target = Path("/staging"), Path("/target")
    if returned_error:
        with pytest.raises(OSError) as exc:
            _directory._linux_rename(source, target)
        assert exc.value.errno == returned_error
    else:
        _directory._linux_rename(source, target)
    assert calls == [(-100, os.fsencode(source), -100, os.fsencode(target), 1)]
    assert native.argtypes == [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    assert native.restype is ctypes.c_int


def test_missing_linux_libc_entry_is_explicitly_unsupported(monkeypatch):
    monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace())
    with pytest.raises(ContractError) as exc:
        _directory._linux_rename(Path("/staging"), Path("/target"))
    assert exc.value.code == "unsupported"
