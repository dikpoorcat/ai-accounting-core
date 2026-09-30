"""Resident catalog handle keeps SQLite warm without retaining a read snapshot."""

from __future__ import annotations

import os
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
from pydantic import SecretStr

from ai_accounting.kernel.catalog import Catalog
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.permissions import ensure_private_file
from ai_accounting.kernel.security import IdentityError
from ai_accounting.kernel.service import LocalService


def test_catalog_observer_only_for_resident_and_closes(tmp_path):
    ordinary = LocalService(tmp_path / "ordinary")
    assert ordinary._catalog_observer is None
    ordinary.close()

    resident = LocalService(tmp_path / "resident", enable_read_pool=True)
    observer = resident._catalog_observer_connection
    assert observer is not None
    assert observer.execute("PRAGMA query_only").fetchone()[0] == 1
    assert not observer.in_transaction
    resident.close()
    assert resident._catalog_observer_connection is None
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        observer.execute("SELECT 1")


def test_catalog_observer_can_close_from_another_thread(tmp_path):
    service = LocalService(tmp_path / "resident", enable_read_pool=True)
    observer = service._catalog_observer_connection
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(service.close).result(timeout=5)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        observer.execute("SELECT 1")


def test_catalog_observer_keeps_no_snapshot_and_allows_catalog_writes(tmp_path):
    service = LocalService(tmp_path / "resident", enable_read_pool=True)
    try:
        observer = service._catalog_observer_connection
        assert not observer.in_transaction
        first = service.catalog.create_company("91310000123456789A", "合成甲")
        assert not observer.in_transaction
        assert (
            observer.execute("SELECT name FROM company WHERE id=?", (first["id"],)).fetchone()[0]
            == "合成甲"
        )
        with service.catalog.connection() as connection:
            connection.execute("UPDATE company SET name=? WHERE id=?", ("合成甲新名", first["id"]))
            assert connection.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()[0] == 0
        assert not observer.in_transaction
        assert service.dashboard_context(first["id"])["company"] == "合成甲新名"
    finally:
        service.close()


def test_catalog_observer_construction_failure_releases_connection(tmp_path, monkeypatch):
    # Catalog initialization performs one read. The following read opens the
    # resident observer; force a transaction to exercise failed construction.
    original = Catalog.connection
    reads = 0
    opened = []

    @contextmanager
    def with_bad_observer(self, *, read_only=False, _cross_thread=False):
        nonlocal reads
        with original(self, read_only=read_only, _cross_thread=_cross_thread) as connection:
            if read_only:
                reads += 1
                if reads == 2:
                    opened.append(connection)
                    connection.execute("BEGIN")
            yield connection

    monkeypatch.setattr(Catalog, "connection", with_bad_observer)
    with pytest.raises(RuntimeError, match="must not hold a transaction"):
        LocalService(tmp_path / "resident", enable_read_pool=True)
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")


def test_context_uses_one_catalog_read_and_rejects_wrong_company(tmp_path, monkeypatch):
    service = LocalService(tmp_path / "resident", enable_read_pool=True)
    try:
        empty = service.dashboard_context()
        assert empty["companies"] == [] and empty["company"] is None
        with pytest.raises(KernelError, match="公司尚未登记"):
            service.dashboard_context("missing")

        first = service.catalog.create_company("91310000123456789A", "合成甲")
        second = service.catalog.create_company("91310000123456789B", "合成乙")
        original = Catalog.connection
        reads = 0

        @contextmanager
        def count_catalog_reads(self, *, read_only=False, _cross_thread=False):
            nonlocal reads
            if read_only:
                reads += 1
            with original(self, read_only=read_only, _cross_thread=_cross_thread) as connection:
                yield connection

        monkeypatch.setattr(Catalog, "connection", count_catalog_reads)
        selected = service.dashboard_context(second["id"])
        assert reads == 1
        assert selected["company"] == "合成乙"
        assert {item["company_id"] for item in selected["companies"]} == {first["id"], second["id"]}
        with pytest.raises(KernelError, match="公司尚未登记"):
            service.dashboard_context("missing")
        assert reads == 2
        with service.catalog.connection() as connection:
            connection.execute(
                "UPDATE company SET taxpayer_id=? WHERE id=?",
                ("91310000123456789C", second["id"]),
            )
        with pytest.raises(KernelError, match="database does not match"):
            service.dashboard_context(second["id"])
        replacement = tmp_path / "wrong-company.sqlite"
        shutil.copy2(service.catalog.bind(first["id"]).path, replacement)
        ensure_private_file(replacement)
        with service.catalog.connection() as connection:
            connection.execute(
                "UPDATE company SET taxpayer_id=?,path=? WHERE id=?",
                (second["taxpayer_id"], str(replacement), second["id"]),
            )
        with pytest.raises(KernelError, match="database does not match"):
            service.dashboard_context(second["id"])
    finally:
        service.close()


def test_resident_observer_does_not_cache_schema_or_authority(tmp_path):
    service = LocalService(tmp_path / "resident", enable_read_pool=True)
    try:
        service.security.provision("owner", SecretStr("Synthetic-owner-password-123"))
        token = service.security.login(
            "owner", SecretStr("Synthetic-owner-password-123")
        ).session_token
        assert service.dispatch("dashboard_context", {}, session_token=token)["companies"] == []
        with sqlite3.connect(service.catalog.path) as connection:
            connection.execute("CREATE TABLE synthetic_drift(x INTEGER)")
        with pytest.raises(Exception, match="schema|结构|contract|fingerprint"):
            service.dispatch("dashboard_context", {}, session_token=token)
        with sqlite3.connect(service.catalog.path) as connection:
            connection.execute("DROP TABLE synthetic_drift")
        service.security.logout(token)
        with pytest.raises(IdentityError):
            service.dispatch("dashboard_context", {}, session_token=token)
    finally:
        service.close()


def test_replaced_catalog_is_not_accepted_through_observer(tmp_path):
    service = LocalService(tmp_path / "resident", enable_read_pool=True)
    replacement = LocalService(tmp_path / "other")
    try:
        password = SecretStr("Synthetic-owner-password-123")
        service.security.provision("owner", password)
        token = service.security.login("owner", password).session_token
        assert service.dispatch("dashboard_context", {}, session_token=token)["companies"] == []
        replacement.close()
        try:
            os.replace(replacement.catalog.path, service.catalog.path)
        except PermissionError:
            # Windows may reject replacement while SQLite owns the old file.
            # Simulate that handle being released and still require the next
            # request to reject the different catalog identity.
            service._catalog_observer.close()
            service._catalog_observer = None
            service._catalog_observer_connection = None
            os.replace(replacement.catalog.path, service.catalog.path)
        with pytest.raises(IdentityError, match="OWNER_SECURITY_TARGET_MISMATCH"):
            service.dispatch("dashboard_context", {}, session_token=token)
    finally:
        service.close()
        replacement.close()
