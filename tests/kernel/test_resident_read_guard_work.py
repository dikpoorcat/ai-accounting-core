"""Real private-file checks around a warm, structurally verified read borrow."""

import ctypes
import os
from contextlib import nullcontext
from pathlib import Path

import pytest

from ai_accounting.kernel import permissions, resident_reads, runtime
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.permissions import PrivatePathError, ensure_private_file
from ai_accounting.kernel.service import LocalService


@pytest.fixture
def warm_store(tmp_path):
    service = LocalService(tmp_path / "synthetic", enable_read_pool=True)
    company = service.catalog.create_company("91310000123456789A", "合成权限测试")
    store = service.engine(company["id"], dashboard_read=True).store
    with store.connection(read_only=True) as connection:
        original = connection
    try:
        yield store, service.read_pool, original
    finally:
        service.close()


def _widen_file_permission(path):
    """Change the real synthetic file's ACL/mode, without replacing the guard."""
    if os.name != "nt":
        path.chmod(0o666)
        return
    from ai_accounting.kernel.security import windows

    kernel, advapi, handle = windows._open_security_handle(path, write=True)
    descriptor, dacl = ctypes.c_void_p(), ctypes.c_void_p()
    present, defaulted = ctypes.c_int(), ctypes.c_int()
    sid = windows.current_windows_sid()
    try:
        assert advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            f"D:P(A;;FA;;;SY)(A;;FA;;;{sid})(A;;FR;;;WD)",
            1, ctypes.byref(descriptor), None,
        )
        assert advapi.GetSecurityDescriptorDacl(
            descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)
        )
        assert present.value and dacl.value
        assert advapi.SetSecurityInfo(handle, 1, 0x80000004, None, None, dacl, None) == 0
    finally:
        if descriptor.value:
            kernel.LocalFree(descriptor)
        kernel.CloseHandle(handle)


def _real_guard_work(monkeypatch):
    work = {"local_path": 0, "private_os": 0, "private_paths": []}
    original_path = runtime.require_local_database

    def local_path(path):
        result = original_path(path)
        work["local_path"] += 1
        return result

    monkeypatch.setattr(runtime, "require_local_database", local_path)
    monkeypatch.setattr(resident_reads, "require_local_database", local_path)
    if os.name == "nt":
        from ai_accounting.kernel.security import windows

        original_handle = windows._assert_private_handle
        original_private_path = windows.assert_private_path

        def private_handle(*args, **kwargs):
            result = original_handle(*args, **kwargs)  # real GetSecurityInfo + ACE validation
            work["private_os"] += 1
            return result

        def private_path(path, **kwargs):
            result = original_private_path(path, **kwargs)
            work["private_paths"].append(Path(path))
            return result

        monkeypatch.setattr(windows, "_assert_private_handle", private_handle)
        monkeypatch.setattr(windows, "assert_private_path", private_path)
    else:
        original_private = permissions._posix_assert

        def private_path(path, **kwargs):
            result = original_private(path, **kwargs)  # real lstat owner + mode validation
            work["private_os"] += 1
            work["private_paths"].append(Path(path))
            return result

        monkeypatch.setattr(permissions, "_posix_assert", private_path)
    return work


@pytest.mark.parametrize("fixed_v1", [False, True])
def test_warm_borrow_merges_only_middle_main_guard(warm_store, monkeypatch, fixed_v1):
    store, pool, original = warm_store
    sidecars = [Path(str(store.path) + suffix) for suffix in ("-wal", "-shm")]
    assert all(path.is_file() for path in sidecars)
    work = _real_guard_work(monkeypatch)

    def read():
        sql = []
        original.set_trace_callback(sql.append)
        scope = historical_content(1) if fixed_v1 else nullcontext()
        with scope, store.connection(read_only=True) as connection:
            assert connection is original
            identity = tuple(connection.execute("SELECT * FROM identity").fetchone())
        return sql, identity

    new_sql, new_identity = read()
    new_work = {
        key: list(value) if isinstance(value, list) else value for key, value in work.items()
    }
    # Exact old warm route: the public validator performs its own real main guard.
    # Both paths execute the same sidecar, SQLite safety, schema and identity checks.
    monkeypatch.setattr(
        resident_reads, "_validate_reused_read_connection_at_private_path",
        runtime.validate_reused_read_connection,
    )
    work.update(local_path=0, private_os=0, private_paths=[])
    old_sql, old_identity = read()
    assert new_identity == old_identity
    assert new_sql == old_sql
    assert any(
        "sqlite_schema" in statement or "sqlite_master" in statement for statement in new_sql
    )
    assert new_work["local_path"] == 2 and work["local_path"] == 3
    assert new_work["private_os"] == 4 and work["private_os"] == 5
    assert new_work["private_paths"].count(store.path) == 2
    assert work["private_paths"].count(store.path) == 3
    assert all(new_work["private_paths"].count(path) == 1 for path in sidecars)
    assert pool._total == 1


@pytest.mark.parametrize("phase", ["before", "after", "sidecar", "standalone"])
def test_real_acl_changes_are_rejected(warm_store, monkeypatch, phase):
    store, pool, original = warm_store
    target = Path(str(store.path) + "-wal") if phase == "sidecar" else store.path
    assert target.is_file()
    if phase == "after":
        validate = store.validate_connection

        def change_after_validation(connection):
            validate(connection)
            _widen_file_permission(target)

        monkeypatch.setattr(store, "validate_connection", change_after_validation)
    else:
        _widen_file_permission(target)
    try:
        with pytest.raises(PrivatePathError):
            if phase == "standalone":
                runtime.validate_reused_read_connection(
                    store.path, original, store.validate_connection
                )
            else:
                with store.connection(read_only=True):
                    pytest.fail("unsafe file must not reach business reads")
        if phase != "standalone":
            assert pool._total == 0
    finally:
        ensure_private_file(target)


def test_unsafe_dbconfig_is_rejected_and_discarded(warm_store):
    import sqlite3

    store, pool, original = warm_store
    original.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, False)
    with pytest.raises(runtime.RuntimeConfigurationError, match="safety settings"):
        with store.connection(read_only=True):
            pytest.fail("unsafe SQLite configuration must not reach business reads")
    assert pool._total == 0


def test_standalone_validator_preserves_actual_structure_validation(warm_store):
    import sqlite3

    store, _, original = warm_store
    with sqlite3.connect(store.path) as writer:
        writer.execute("CREATE TABLE synthetic_guard_drift(id INTEGER)")
    with pytest.raises(KernelError) as failure:
        runtime.validate_reused_read_connection(store.path, original, store.validate_connection)
    assert failure.value.code == "schema_fingerprint_mismatch"
    assert not original.in_transaction


def test_business_authorizer_is_cleared_before_next_validation(warm_store):
    import sqlite3

    store, pool, original = warm_store
    with store.connection(read_only=True) as connection:
        connection.set_authorizer(lambda *args: sqlite3.SQLITE_DENY)
    with store.connection(read_only=True) as connection:
        assert connection is original
        assert connection.execute("SELECT count(*) FROM identity").fetchone()[0] == 1
    assert pool._total == 1
