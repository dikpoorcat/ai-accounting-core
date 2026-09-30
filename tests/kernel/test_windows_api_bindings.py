"""Windows API bindings are static; handle security decisions are not."""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

import pytest

from ai_accounting.kernel.permissions import create_private_file, ensure_private_directory
from ai_accounting.kernel.security import windows
from ai_accounting.kernel.security.primitives import IdentityError

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows ACL API")


def test_cached_windows_bindings_still_check_each_open_handle(tmp_path, monkeypatch):
    root = ensure_private_directory(tmp_path / "owned")
    source = create_private_file(root / "state.bin")
    original = windows._assert_private_handle
    checked = []
    lock = Lock()

    def inspect(kernel, advapi, handle, *, directory=False):
        original(kernel, advapi, handle, directory=directory)
        with lock:
            checked.append(handle)

    monkeypatch.setattr(windows, "_assert_private_handle", inspect)
    bound = windows._apis()
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(windows.assert_private_file, [source] * 32))
        pairs = list(executor.map(lambda _: windows._apis(), range(32)))

    assert len(checked) == 32
    assert all(pair is bound for pair in pairs)

    def reject(*args, **kwargs):
        raise IdentityError("OWNER_SECURITY_STATE_ACCESS_DENIED")

    monkeypatch.setattr(windows, "_assert_private_handle", reject)
    with pytest.raises(IdentityError, match="OWNER_SECURITY_STATE_ACCESS_DENIED"):
        windows.assert_private_file(source)


def test_failed_windows_binding_initialization_is_not_cached(monkeypatch):
    windows._apis.cache_clear()
    original = windows.ctypes.WinDLL
    attempts = 0

    def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("synthetic initialization failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(windows.ctypes, "WinDLL", fail_once)
    try:
        with pytest.raises(OSError, match="synthetic initialization failure"):
            windows._apis()
        assert windows._apis.cache_info().currsize == 0
        first = windows._apis()
        assert windows._apis() is first
        assert windows._apis.cache_info().currsize == 1
    finally:
        windows._apis.cache_clear()
