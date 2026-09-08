from __future__ import annotations

import sys
import time
import uuid
from types import SimpleNamespace
from typing import get_args

import pytest
from pydantic import SecretStr

from ai_accounting.identity import IdentityError
from ai_accounting.owner_login_launcher import SecurityWindowRecord
from ai_accounting.owner_security import OwnerSecurityWindowRequest, SecurityKind
from ai_accounting.owner_security_window import SecurityForm

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="native Windows form")


@pytest.fixture
def form():
    import tkinter as tk

    root = tk.Tk()
    root.withdraw()
    forms = []

    def create(kind, error=None):
        request = OwnerSecurityWindowRequest(
            kind=kind,
            org_id=uuid.uuid4() if kind in {"bootstrap_owner", "approve_period_close"} else None,
            period_id=uuid.uuid4() if kind == "approve_period_close" else None,
            calculation_hash="a" * 64 if kind == "approve_period_close" else None,
        )
        records, calls = [], []

        class Operations:
            operation_committed = False
            login_completed = False

            def execute(self, request, target, **values):
                calls.append(set(values))
                if error:
                    raise IdentityError(error)
                self.operation_committed = True
                if request.kind in {"login", "approve_period_close"}:
                    self.login_completed = True
                    return None
                return SecretStr("TEST-RECOVERY-ONLY-IN-FORM")

            def finish_recovery_display(self, request, target, **values):
                calls.append({"acknowledged"})
                if request.kind == "bootstrap_owner":
                    assert values["new_password"] == SecretStr("TEST-PASSWORD")
                    self.login_completed = True

        operations = Operations()
        launcher = SimpleNamespace(
            operations=operations, update=lambda _, **changes: records.append(changes)
        )
        record = SecurityWindowRecord(request_id=uuid.uuid4(), target_id="a" * 64, request=request)
        result = SecurityForm(
            root,
            launcher,
            record,
            {
                "company_name": "隔离表单测试",
                "login_name": "test-owner",
                "period_month": "2026-07",
            },
        )
        forms.append(result)
        return result, records, calls

    yield create
    # Dispose Tcl variables on their owning thread, before another test starts a worker.
    for window in forms:
        window.message = None
    try:
        root.destroy()
    except tk.TclError:
        pass


def settle(form):
    deadline = time.monotonic() + 5
    while form.busy and time.monotonic() < deadline:
        form.root.update()
        time.sleep(0.01)
    assert not form.busy


@pytest.mark.parametrize("kind", get_args(SecurityKind))
def test_all_six_forms_success_and_no_duplicate_submission(form, kind):
    window, records, calls = form(kind)
    for entry in window.entries.values():
        entry.insert(0, "TEST-PASSWORD")
        assert entry.cget("show") == "●"
    if kind == "bootstrap_owner":
        assert set(window.entries) == {"new_password", "repeat_password"}
    window.submit()
    window.submit()
    settle(window)
    assert len(calls) == 1
    if window.recovery_displayed:
        assert window.recovery_label.cget("text") == "TEST-RECOVERY-ONLY-IN-FORM"
        window.submit()
        settle(window)
    assert records[-1]["status"] == "succeeded"
    assert "TEST-PASSWORD" not in repr(records)
    assert "TEST-RECOVERY-ONLY-IN-FORM" not in repr(records)
    assert window.saved_new_password is None


@pytest.mark.parametrize("kind", get_args(SecurityKind))
def test_cancel_does_not_invoke_identity_service(form, kind):
    window, records, calls = form(kind)
    window.cancel()
    assert records[-1]["status"] == "cancelled"
    assert not calls


@pytest.mark.parametrize(
    "code",
    [
        "IDENTITY_AUTHENTICATION_FAILED",
        "IDENTITY_PASSWORD_CONFIRMATION_MISMATCH",
        "IDENTITY_RECOVERY_FAILED",
        "IDENTITY_SESSION_INVALID",
    ],
)
def test_safe_error_leaves_form_retryable(form, code):
    window, records, _ = form("login", error=code)
    window.submit()
    settle(window)
    assert records[-1]["status"] == "waiting_for_user"
    assert code in window.message.get()
    assert not window.finished


def test_close_after_rotation_is_not_reported_as_cancelled_or_success(form):
    window, records, _ = form("replace_recovery_code")
    window.submit()
    settle(window)
    window.cancel()
    assert records[-1]["status"] == "failed"
    assert records[-1]["operation_committed"] is True
    assert records[-1]["error_code"] == "OWNER_SECURITY_RECOVERY_CODE_NOT_ACKNOWLEDGED"


def test_waiting_only_after_form_is_mapped(form):
    window, records, _ = form("login")
    window.root.update()
    assert not records
    window.root.deiconify()
    deadline = time.monotonic() + 3
    while not records and time.monotonic() < deadline:
        window.root.update()
        time.sleep(0.01)
    assert records[-1]["status"] == "waiting_for_user"


def test_uncertain_identity_commit_stops_form_retries(form):
    window, records, calls = form("replace_recovery_code")
    window.operations.operation_committed = None
    window.results.put((None, "OWNER_SECURITY_OPERATION_FAILED"))
    window.poll()
    assert records[-1]["status"] == "failed"
    assert records[-1]["operation_committed"] is None
    assert window.finished
    window.submit()
    assert not calls


def test_state_write_failure_before_submit_never_executes_identity_operation(form):
    window, _, calls = form("login")

    def fail(*args, **kwargs):
        raise IdentityError("OWNER_SECURITY_STATE_ACCESS_DENIED")

    window.launcher.update = fail
    window.submit()
    assert not window.busy
    assert window.finished
    assert not calls
    assert "OWNER_SECURITY_STATE_ACCESS_DENIED" in window.message.get()
