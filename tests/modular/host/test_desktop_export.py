"""Desktop copies preserve exact bytes and never replace a user's existing file."""

import ctypes
import io
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.host import _desktop_export as export

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows desktop delivery")


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    redirected = tmp_path / "重定向桌面"
    redirected.mkdir()
    monkeypatch.setattr(export, "desktop_directory", lambda: redirected)
    return redirected


def report(raw=b"verified report bytes", **changes):
    return SimpleNamespace(
        **{
            "stream": io.BytesIO(raw),
            "file_name": "合成公司_913100000000000001_2026年第3季度财务报表.xlsx",
            "sha256": sha256(raw).hexdigest(),
            "byte_size": len(raw),
            "company_id": str(uuid4()),
            "database_id": str(uuid4()),
            "task_id": str(uuid4()),
        }
        | changes
    )


def test_known_folder_uses_current_user_and_redirected_path_without_real_desktop(
    tmp_path, monkeypatch
):
    buffer = ctypes.create_unicode_buffer(str(tmp_path / "云盘" / "桌面"))
    freed = []

    def locate(identifier, flags, token, output):
        assert (
            ctypes.string_at(identifier, 16)
            == UUID("b4bfcc3a-db2c-424c-b029-7fe99a87c641").bytes_le
        )
        assert flags == 0 and token is None
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.cast(
            buffer, ctypes.c_void_p
        )
        return 0

    def free(value):
        freed.append(value.value)

    libraries = {
        "Shell32": SimpleNamespace(SHGetKnownFolderPath=locate),
        "Ole32": SimpleNamespace(CoTaskMemFree=free),
    }
    monkeypatch.setattr(export.ctypes, "WinDLL", lambda name, **_: libraries[name])
    assert export.desktop_directory() == Path(buffer.value)
    assert freed == [ctypes.addressof(buffer)]
    assert not Path(buffer.value).exists()


def test_known_folder_failure_frees_returned_memory_and_never_guesses(tmp_path, monkeypatch):
    buffer = ctypes.create_unicode_buffer(str(tmp_path))
    freed = []

    def locate(identifier, flags, token, output):
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.cast(
            buffer, ctypes.c_void_p
        )
        return -1

    def free(value):
        freed.append(value.value)

    libraries = {
        "Shell32": SimpleNamespace(SHGetKnownFolderPath=locate),
        "Ole32": SimpleNamespace(CoTaskMemFree=free),
    }
    monkeypatch.setattr(export.ctypes, "WinDLL", lambda name, **_: libraries[name])
    with pytest.raises(ContractError, match="桌面目录") as failed:
        export.desktop_directory()
    assert failed.value.code == "desktop_unavailable"
    assert freed == [ctypes.addressof(buffer)]


def test_copy_and_retry_use_redirected_desktop_exact_bytes_and_leave_stream_open(desktop):
    first = report()
    copied = export.save_report(first)
    assert copied == {
        "state": "copied",
        "path": str(desktop / first.file_name),
        "sha256": first.sha256,
        "byte_size": first.byte_size,
    }
    assert Path(copied["path"]).read_bytes() == first.stream.getvalue()
    assert not first.stream.closed
    reused = export.save_report(report(task_id=first.task_id))
    assert reused == copied | {"state": "reused"}
    assert list(desktop.iterdir()) == [desktop / first.file_name]


def test_changed_original_and_another_existing_copy_are_preserved_under_new_name(desktop):
    value = report()
    original = desktop / value.file_name
    original.write_bytes(b"owner's edited report")
    occupied = desktop / f"{original.stem} ({value.task_id}).xlsx"
    occupied.write_bytes(b"another user's file")
    result = export.save_report(value)
    assert result["state"] == "copied"
    assert Path(result["path"]).name == f"{original.stem} ({value.task_id}-2).xlsx"
    assert original.read_bytes() == b"owner's edited report"
    assert occupied.read_bytes() == b"another user's file"
    repeated = export.save_report(report(task_id=value.task_id))
    assert repeated == result | {"state": "reused"}


def test_same_content_concurrent_calls_publish_once_and_reuse_winner(desktop, monkeypatch):
    identity = str(uuid4())
    barrier = Barrier(2)
    original_copy = export._copy

    def together(download, output):
        original_copy(download, output)
        barrier.wait(timeout=10)

    monkeypatch.setattr(export, "_copy", together)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(lambda _: export.save_report(report(task_id=identity)), range(2))
        )
    assert {item["state"] for item in results} == {"copied", "reused"}
    assert results[0]["path"] == results[1]["path"]
    assert len(list(desktop.iterdir())) == 1


@pytest.mark.parametrize("wrong", [{"byte_size": 1}, {"byte_size": 100}, {"sha256": "0" * 64}])
def test_bad_verified_stream_removes_only_its_temporary_file(desktop, wrong):
    unrelated = desktop / ".finance-report-unrelated.tmp"
    unrelated.write_bytes(b"not owned by this request")
    with pytest.raises(ContractError) as failed:
        export.save_report(report(**wrong))
    assert failed.value.code == "report_delivery_invalid"
    assert list(desktop.iterdir()) == [unrelated]
    assert unrelated.read_bytes() == b"not owned by this request"


def test_disk_failure_keeps_original_and_cleans_this_attempt(desktop, monkeypatch):
    existing = desktop / "owner.txt"
    existing.write_text("keep", encoding="utf-8")

    def unavailable(*_):
        raise PermissionError("synthetic rename failure")

    monkeypatch.setattr(export.os, "rename", unavailable)
    with pytest.raises(ContractError) as failed:
        export.save_report(report())
    assert failed.value.code == "desktop_export_unavailable"
    assert list(desktop.iterdir()) == [existing]


def test_final_file_is_checked_again_without_deleting_changed_user_content(desktop, monkeypatch):
    rename = export.os.rename

    def changed_after_publish(source, target):
        rename(source, target)
        target.write_bytes(b"changed immediately after publication")

    monkeypatch.setattr(export.os, "rename", changed_after_publish)
    value = report()
    with pytest.raises(ContractError) as failed:
        export.save_report(value)
    assert failed.value.code == "desktop_file_invalid"
    assert (desktop / value.file_name).read_bytes() == b"changed immediately after publication"
    assert not list(desktop.glob("*.tmp"))


def test_changed_desktop_is_rejected_before_publication(desktop, tmp_path, monkeypatch):
    candidates = export._candidates
    moved = tmp_path / "original-desktop"

    def move_before_publication(root, name, task_id):
        root.rename(moved)
        root.mkdir()
        yield from candidates(root, name, task_id)

    monkeypatch.setattr(export, "_candidates", move_before_publication)
    with pytest.raises(ContractError) as failed:
        export.save_report(report())
    assert failed.value.code == "desktop_changed"
    assert not list(desktop.iterdir())
    assert not list(moved.glob("*.xlsx"))


def test_closed_stream_fails_explicitly_and_cleans_its_temporary(desktop):
    value = report()
    value.stream.close()
    with pytest.raises(ContractError) as failed:
        export.save_report(value)
    assert failed.value.code == "report_delivery_invalid"
    assert not list(desktop.iterdir())


@pytest.mark.parametrize("kind", ["directory", "hardlink", "symlink"])
def test_existing_directory_or_link_is_preserved_and_cannot_be_reused(desktop, tmp_path, kind):
    value = report()
    destination = desktop / value.file_name
    external = tmp_path / "outside.xlsx"
    external.write_bytes(value.stream.getvalue())
    if kind == "directory":
        destination.mkdir()
    elif kind == "hardlink":
        os.link(external, destination)
    else:
        try:
            destination.symlink_to(external)
        except OSError as error:
            pytest.skip(f"symbolic links unavailable: {error}")
    copied = export.save_report(value)
    assert copied["state"] == "copied"
    assert Path(copied["path"]) != destination
    assert external.read_bytes() == value.stream.getvalue()
    assert destination.is_dir() if kind == "directory" else destination.exists()


def test_redirected_desktop_cannot_itself_be_a_directory_link(tmp_path):
    actual, alias = tmp_path / "actual", tmp_path / "desktop-link"
    actual.mkdir()
    try:
        alias.symlink_to(actual, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symbolic links unavailable: {error}")
    with pytest.raises(ContractError) as failed:
        export.save_report(report(), desktop_resolver=lambda: alias)
    assert failed.value.code == "unsafe_path"
    assert not list(actual.iterdir())


def test_cloud_directory_tag_is_not_a_name_surrogate():
    cloud = SimpleNamespace(
        st_mode=stat.S_IFDIR,
        st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
        st_reparse_tag=0x9000001A,
    )
    assert not export._linked(cloud)
    cloud.st_reparse_tag = 0xA0000003
    assert export._linked(cloud)


@pytest.mark.parametrize("name", ["../escape.xlsx", r"..\escape.xlsx", "C:escape.xlsx", "/x.xlsx"])
def test_download_name_cannot_supply_any_save_path(desktop, name):
    with pytest.raises(ContractError) as failed:
        export.save_report(report(file_name=name))
    assert failed.value.code == "unsafe_path"
    assert not list(desktop.iterdir())


def test_illegal_basename_characters_are_replaced(desktop):
    value = report(file_name='合成<公司>"?*_2026年第3季度.xlsx')
    result = export.save_report(value)
    assert Path(result["path"]).name == "合成_公司_____2026年第3季度.xlsx"


@pytest.mark.parametrize(
    "name",
    [
        "CON.xlsx",
        "NUL.xlsx",
        "COM1.xlsx",
        "COM².xlsx",
        "LPT³.xlsx",
        "CON.技术公司_913100000000000001_2026年第3季度财务报表.xlsx",
        " . .NUL.xlsx",
    ],
)
def test_windows_reserved_names_are_rejected_after_sanitizing(desktop, name):
    with pytest.raises(ContractError) as failed:
        export.save_report(report(file_name=name))
    assert failed.value.code == "report_delivery_invalid"
    assert not list(desktop.iterdir())


@pytest.mark.parametrize("company", ["CON", "_CON.技术公司", "_COM².技术公司"])
def test_nonreserved_company_report_names_remain_usable(desktop, company):
    value = report(file_name=f"{company}_913100000000000001_2026年第3季度财务报表.xlsx")
    saved = export.save_report(value)
    assert Path(saved["path"]).name == value.file_name
    assert Path(saved["path"]).read_bytes() == value.stream.getvalue()


def test_other_platform_is_explicitly_unsupported_without_directory_discovery(monkeypatch):
    monkeypatch.setattr(export.sys, "platform", "linux")
    with pytest.raises(ContractError) as failed:
        export.save_report(report(), desktop_resolver=lambda: pytest.fail("must not discover"))
    assert failed.value.code == "unsupported"
