"""Synthetic exact-scope native approvals; no real owner, credential store or company."""

import json
import sqlite3
import sys
import uuid
from contextlib import closing
from types import SimpleNamespace

import pytest
from test_close_range import ready
from test_identity import PASSWORD, Clock, catalogue

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.periods import Periods
from ai_accounting.kernel.runtime import connect
from ai_accounting.kernel.security import IdentityError, initialize_company
from ai_accounting.kernel.security.batches import (
    CloseBatchHost,
    consume_batch_approval,
    issue_batch_approval,
    read_batch_approval,
)
from ai_accounting.kernel.security.credentials import InMemoryCredentialStore
from ai_accounting.kernel.security.native import (
    NativeRequest,
    NativeSecurityController,
    WindowRecord,
)
from ai_accounting.kernel.service import LocalService
from ai_accounting.kernel.types import YearMonth


def target(company="one", **changes):
    return {
        "company_id": company,
        "database_id": "database-" + company,
        "from_period": "2026-01",
        "through_period": "2026-07",
        "calculation_hash": "a" * 64,
        "epochs": {"accounting": 2, "material": 3, "management": 4},
        **changes,
    }


@pytest.fixture
def owner(tmp_path):
    clock = Clock()
    security = catalogue(tmp_path / "catalog.sqlite", clock=clock)
    security.provision("owner", PASSWORD)
    login = security.login("owner", PASSWORD)
    return security, login, clock


def issue(owner, *, targets=None, batch_id=None):
    service, login, _ = owner
    return issue_batch_approval(
        service,
        token=login.session_token,
        password=PASSWORD,
        batch_id=batch_id or uuid.uuid4().hex,
        targets=targets or [target(), target("two")],
    )


def company(path, name="one"):
    connection = connect(path)
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "CREATE TABLE identity(id INTEGER PRIMARY KEY,company_id TEXT,database_id TEXT) STRICT"
    )
    connection.execute("INSERT INTO identity VALUES(1,?,?)", (name, "database-" + name))
    initialize_company(connection)
    connection.commit()
    return connection


def consume(connection, grant, owner, *, company_name="one", **changes):
    service, login, _ = owner
    values = target(company_name)
    values["preview_digest"] = values.pop("calculation_hash")
    return consume_batch_approval(
        connection,
        grant["batch_id"],
        service=service,
        authority=login.authority,
        **(values | changes),
    )


def test_one_password_one_catalogue_commit_two_local_consumptions(owner, tmp_path, monkeypatch):
    service, _, _ = owner
    checked = []
    original = service._password_check

    def password_check(*args, **kwargs):
        checked.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "_password_check", password_check)
    grant = issue(owner)
    assert checked == [True]
    assert issue(owner, batch_id=grant["batch_id"]) == grant
    assert checked == [True]  # Durable recovery never issues a fresh or extended grant.
    for name in ("one", "two"):
        with closing(company(tmp_path / f"{name}.sqlite", name)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            proof = consume(connection, grant, owner, company_name=name)
            assert proof["method"] == "local_password_batch_reauthentication"
            assert proof["through_period"] == "2026-07"
            connection.commit()
            connection.execute("BEGIN IMMEDIATE")
            with pytest.raises(IdentityError, match="APPROVAL_INVALID"):
                consume(connection, grant, owner, company_name=name)
            connection.rollback()
            for sql in (
                "UPDATE security_close_batch_receipt SET consumed_at=0",
                "DELETE FROM security_close_batch_receipt",
            ):
                with pytest.raises(sqlite3.IntegrityError):
                    connection.execute(sql)
    with closing(connect(service.path, read_only=True)) as connection:
        assert connection.execute("SELECT count(*) FROM security_close_batch").fetchone()[0] == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"company_id": "other"},
        {"database_id": "other"},
        {"from_period": "2025-12"},
        {"through_period": "2026-08"},
        {"preview_digest": "b" * 64},
        {"epochs": {"accounting": 3, "material": 3, "management": 4}},
        {"epochs": {"accounting": 2, "material": 4, "management": 4}},
    ],
)
def test_scope_cannot_expand_or_reuse_stale_preview(owner, tmp_path, changes):
    grant = issue(owner)
    with closing(company(tmp_path / "company.sqlite")) as connection:
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(IdentityError):
            consume(connection, grant, owner, **changes)
        assert (
            connection.execute("SELECT count(*) FROM security_close_batch_receipt").fetchone()[0]
            == 0
        )


def test_receipt_rolls_back_with_company_and_management_does_not_change_scope(owner, tmp_path):
    grant = issue(owner)
    with closing(company(tmp_path / "company.sqlite")) as connection:
        with pytest.raises(IdentityError, match="TRANSACTION_REQUIRED"):
            consume(connection, grant, owner)
        connection.execute("BEGIN IMMEDIATE")
        consume(connection, grant, owner)
        connection.rollback()
        connection.execute("BEGIN IMMEDIATE")
        consume(connection, grant, owner, epochs={"accounting": 2, "material": 3, "management": 99})
        connection.commit()


def test_grant_and_all_targets_roll_back_together_and_remain_immutable(owner, monkeypatch):
    service, _, _ = owner
    original = service._audit

    def fail(connection, event, *args, **kwargs):
        if event == "close_batches_approved":
            raise RuntimeError("synthetic interruption after single grant insert")
        return original(connection, event, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(service, "_audit", fail)
        with pytest.raises(RuntimeError):
            issue(owner)
    with service._transaction() as connection:
        assert connection.execute("SELECT count(*) FROM security_close_batch").fetchone()[0] == 0
    issue(owner)
    with service._transaction() as connection:
        for sql in (
            "UPDATE security_close_batch SET expires_at=expires_at+1",
            "DELETE FROM security_close_batch",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(sql)


def test_expiry_revocation_and_other_session_reject_recovery(owner):
    service, login, clock = owner
    grant = issue(owner)
    other = service.login("owner", PASSWORD)
    with pytest.raises(IdentityError, match="APPROVAL_INVALID"):
        read_batch_approval(service, batch_id=grant["batch_id"], authority=other.authority)
    clock.advance(minutes=30)
    with pytest.raises(IdentityError, match="APPROVAL_INVALID"):
        read_batch_approval(service, batch_id=grant["batch_id"], authority=login.authority)
    second = issue(owner)
    service.logout(login.session_token)
    with pytest.raises(IdentityError, match="SESSION_INVALID"):
        read_batch_approval(service, batch_id=second["batch_id"], authority=login.authority)


def test_native_commit_response_loss_recovers_only_for_same_live_session(owner):
    service, login, _ = owner
    store = InMemoryCredentialStore()
    store.save_session_token(login.session_token)

    def issuer(request, token, password, request_id):
        issue_batch_approval(
            service, token=token, password=password, batch_id=request_id, targets=request["batches"]
        )
        raise OSError("synthetic lost native response after commit")

    native = NativeSecurityController(
        service,
        credential_store=store,
        window_opener=lambda request_id: None,
        batch_issuer=issuer,
        inspect_batches=lambda request: {"batches": request["batches"]},
    )
    request = native.request(kind="approve_close_batches", batches=[target(), target("two")])
    request_id = request["request_id"]
    with pytest.raises(IdentityError, match="PRIVATE_CHANNEL"):
        native.dispatch("native_execute", {"request_id": request_id, "password": PASSWORD})
    with pytest.raises(OSError):
        native.dispatch(
            "native_execute", {"request_id": request_id, "password": PASSWORD}, private=True
        )
    status = native.status(request_id)
    assert status["status"] == "succeeded" and len(status["approvals"]) == 2
    assert PASSWORD.get_secret_value() not in json.dumps(status)
    restarted = NativeSecurityController(service, credential_store=store)
    assert restarted.status(request_id) == status
    store.save_session_token(service.login("owner", PASSWORD).session_token)
    with pytest.raises(IdentityError, match="APPROVAL_INVALID"):
        restarted.status(request_id)
    with pytest.raises(IdentityError, match="REQUEST_UNKNOWN"):
        restarted.status(uuid.uuid4().hex)


def test_native_request_rejects_secrets_duplicate_and_open_ended_scopes(owner):
    service, login, _ = owner
    store = InMemoryCredentialStore()
    store.save_session_token(login.session_token)
    native = NativeSecurityController(service, credential_store=store)
    for values in (
        {"batches": [target()], "password": "never-public"},
        {"batches": [target(), target()]},
        {"batches": []},
        {"batches": [target(through_period="2025-12")]},
        {"batches": [target(epochs={"accounting": True, "material": 0, "management": 0})]},
    ):
        with pytest.raises(IdentityError, match="REQUEST_INVALID"):
            native.request(kind="approve_close_batches", **values)


def real_service(tmp_path):
    service = LocalService(tmp_path / "root")
    service.security.provision("owner", PASSWORD)
    login = service.security.login("owner", PASSWORD)
    companies, previews = [], []
    for index in (1, 2):
        item = service.dispatch(
            "create_company",
            {"taxpayer_id": f"91110000000000000{index}", "name": f"合成公司{index}"},
            session_token=login.session_token,
        )
        engine = service.engine(item["id"])
        proof = engine.register_evidence(
            b"explicit synthetic no-business confirmation",
            "text/plain",
            "synthetic",
            request_id="proof",
        )["digest"]
        ready(engine, proof, "2026-06", "2026-07")
        preview = service.dispatch(
            "preview_close_range",
            {
                "company_id": item["id"],
                "from_period": "2026-06",
                "through_period": "2026-07",
                "owner_confirmation": proof,
            },
            session_token=login.session_token,
        )
        companies.append(item)
        previews.append(preview)
    return service, login, companies, previews


def test_real_service_two_companies_one_native_password_partial_progress_and_replay(
    tmp_path, monkeypatch
):
    service, login, companies, previews = real_service(tmp_path)
    host = CloseBatchHost(service)
    store = InMemoryCredentialStore()
    store.save_session_token(login.session_token)
    native = NativeSecurityController(
        service.security,
        credential_store=store,
        window_opener=lambda request_id: None,
        batch_issuer=host.issue,
        inspect_batches=host.inspect,
    )
    targets = [
        {
            "company_id": item["company_id"],
            "database_id": item["database_id"],
            "from_period": item["from_period"],
            "through_period": item["through_period"],
            "calculation_hash": item["digest"],
            "epochs": item["epochs"],
        }
        for item in previews
    ]
    request_id = native.request(kind="approve_close_batches", batches=targets)["request_id"]
    display = native.dispatch("native_inspect", {"request_id": request_id}, private=True)
    assert {item["company_name"] for item in display["facts"]["batches"]} == {
        "合成公司1",
        "合成公司2",
    }
    checked = []
    original = service.security._password_check

    def check(*args, **kwargs):
        checked.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(service.security, "_password_check", check)

    def unexpected_recompute(*args, **kwargs):
        raise AssertionError("the password window must not rescan historical manifests")

    with monkeypatch.context() as patch:
        patch.setattr(Periods, "preview_close_range", unexpected_recompute)
        native.dispatch(
            "native_execute", {"request_id": request_id, "password": PASSWORD}, private=True
        )
    assert checked == [True]
    for index, preview in enumerate(previews):
        args = {
            key: preview[key]
            for key in (
                "company_id",
                "from_period",
                "through_period",
                "owner_confirmation",
                "epochs",
            )
        }
        args |= {
            "preview_digest": preview["digest"],
            "approval_id": request_id,
            "request_id": f"close-{index}",
        }
        # The untouched second company remains open after first-company success.
        with service.engine(companies[index]["id"]).store.connection(read_only=True) as connection:
            assert connection.execute("SELECT count(*) FROM period_close").fetchone()[0] == 0
        if index == 1:
            original_engine = service.engine

            def reject_commit(stage, connection):
                if stage == "commit":
                    connection.set_authorizer(
                        lambda code, arg, *_: (
                            sqlite3.SQLITE_DENY
                            if code == sqlite3.SQLITE_TRANSACTION and arg == "COMMIT"
                            else sqlite3.SQLITE_OK
                        )
                    )

            def broken_engine(company_id, factory=original_engine):
                engine = factory(company_id)
                engine.fault = reject_commit
                return engine

            with monkeypatch.context() as patch:
                patch.setattr(service, "engine", broken_engine)
                with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
                    service.dispatch("close_range", args, session_token=login.session_token)
            with original_engine(companies[index]["id"]).store.connection(
                read_only=True
            ) as connection:
                assert connection.execute("SELECT count(*) FROM period_close").fetchone()[0] == 0
                assert (
                    connection.execute(
                        "SELECT count(*) FROM security_close_batch_receipt"
                    ).fetchone()[0]
                    == 0
                )
                assert not connection.execute(
                    "SELECT 1 FROM request WHERE id=?", (args["request_id"],)
                ).fetchone()
        result = service.dispatch("close_range", args, session_token=login.session_token)
        assert service.dispatch("close_range", args, session_token=login.session_token) == result
        assert len(result["results"]) == 2
        with service.engine(companies[index]["id"]).store.connection(read_only=True) as connection:
            latest_epochs = service.engine(companies[index]["id"]).store.epochs(connection)
            assert (
                connection.execute("SELECT count(*) FROM security_close_batch_receipt").fetchone()[
                    0
                ]
                == 1
            )
            assert (
                connection.execute(
                    "SELECT count(*) FROM period_close WHERE period=?",
                    (YearMonth("2026-08").ordinal,),
                ).fetchone()[0]
                == 0
            )
            for row in connection.execute("SELECT manifest FROM period_close"):
                proof = json.loads(row[0])["password_confirmation"]
                assert (
                    proof["batch_id"] == request_id
                    and proof["company_id"] == companies[index]["id"]
                )
        with pytest.raises(KernelError, match="已经全部关账"):
            service.dispatch(
                "close_range",
                args | {"request_id": f"another-{index}", "epochs": latest_epochs},
                session_token=login.session_token,
            )
    service.close_range_previews.clear()  # Simulate loss of process-local preview cache.
    recovered = host.issue({"batches": targets}, login.session_token, None, request_id)
    assert recovered["batch_id"] == request_id and checked == [True]
    assert native.status(request_id)["approvals"] == recovered["approvals"]
    with pytest.raises(IdentityError, match="CALCULATION_STALE"):
        host.issue({"batches": targets}, login.session_token, PASSWORD, uuid.uuid4().hex)


def test_native_host_rechecks_changed_preview_before_password(tmp_path):
    service, login, companies, previews = real_service(tmp_path)
    preview = previews[0]
    request = {
        "batches": [
            {
                "company_id": preview["company_id"],
                "database_id": preview["database_id"],
                "from_period": preview["from_period"],
                "through_period": preview["through_period"],
                "calculation_hash": preview["digest"],
                "epochs": preview["epochs"],
            }
        ]
    }
    # A new immutable evidence item changes material epoch and must expire the plan.
    service.engine(companies[0]["id"]).register_evidence(
        b"new synthetic evidence", "text/plain", "changed", request_id="more-proof"
    )
    with pytest.raises(IdentityError, match="CALCULATION_STALE"):
        CloseBatchHost(service).issue(request, login.session_token, PASSWORD, uuid.uuid4().hex)
    with service.security._transaction() as connection:
        assert connection.execute("SELECT count(*) FROM security_close_batch").fetchone()[0] == 0


@pytest.mark.skipif(sys.platform != "win32", reason="bundled Windows Tk runtime")
def test_native_form_has_one_password_and_displays_every_exact_target():
    import tkinter as tk

    from ai_accounting.kernel.security.window import SecurityForm

    targets = [target("one"), target("two")]
    request = NativeRequest(kind="approve_close_batches", batches=targets)
    record = WindowRecord(uuid.uuid4().hex, request, 0)
    root = tk.Tk()
    root.withdraw()  # Synthetic widget construction does not open a real owner window.
    try:
        form = SecurityForm(
            root,
            SimpleNamespace(operations=SimpleNamespace()),
            record,
            {
                "company_name": "合成批次",
                "login_name": "synthetic-owner",
                "batches": [
                    item | {"company_name": "合成公司" + item["company_id"]} for item in targets
                ],
            },
        )
        assert set(form.entries) == {"password"}

        def widgets(parent):
            for child in parent.winfo_children():
                yield child
                yield from widgets(child)

        listings = [widget for widget in widgets(root) if isinstance(widget, tk.Text)]
        assert len(listings) == 1 and listings[0].cget("state") == "disabled"
        content = listings[0].get("1.0", "end")
        for item in targets:
            assert item["database_id"] in content
            assert item["calculation_hash"] in content
            assert item["from_period"] in content and item["through_period"] in content
        assert "2026-08" not in content
    finally:
        root.destroy()
