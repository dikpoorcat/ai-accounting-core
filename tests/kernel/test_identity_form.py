"""Hidden real-Tk checks for first-owner input and retry usability."""
from types import SimpleNamespace

import pytest

from ai_accounting.kernel.security.native import NativeRequest
from ai_accounting.kernel.security.window import SecurityForm


@pytest.fixture
def form_factory():
    import tkinter as tk

    forms = []

    def create(login_name=None):
        root = tk.Tk()
        root.withdraw()
        operations = SimpleNamespace(operation_committed=False, login_completed=False)
        launcher = SimpleNamespace(operations=operations, update=lambda *args, **kwargs: None)
        request = NativeRequest(kind="bootstrap_owner", login_name=login_name)
        record = SimpleNamespace(request=request, request_id="synthetic", target_id="synthetic")
        form = SecurityForm(
            root, launcher, record, {"company_name": "合成测试目录", "login_name": login_name}
        )
        forms.append(form)
        return form

    yield create
    for form in forms:
        form.destroy()


def test_bootstrap_has_editable_plain_login_name_and_masked_passwords(form_factory):
    form = form_factory()
    assert form.entries["login_name"].get() == ""
    assert form.entries["login_name"].cget("show") == ""
    assert str(form.entries["login_name"].cget("state")) == "normal"
    for field in ("new_password", "repeat_password"):
        assert form.entries[field].cget("show") == "●"
    form.entries["login_name"].insert(0, "owner.chosen")
    assert form.entries["login_name"].get() == "owner.chosen"


def test_bootstrap_submission_uses_edited_name_and_retry_keeps_it(form_factory):
    form = form_factory("suggested.owner")
    form.entries["login_name"].delete(0, "end")
    form.entries["login_name"].insert(0, "chosen.owner")
    for field in ("new_password", "repeat_password"):
        form.entries[field].insert(0, "synthetic-new-password")
    from threading import Event

    completed, captured = Event(), {}

    def execute(request, target, **values):
        captured.update(values)
        completed.set()
        return None

    form.operations.execute = execute
    form.submit()
    assert completed.wait(2)
    assert captured["login_name"] == "chosen.owner"
    assert captured["new_password"].get_secret_value() == "synthetic-new-password"
    assert form.entries["login_name"].get() == "chosen.owner"
    assert form.entries["new_password"].get() == ""
    form.results.get_nowait()
    form.results.put((None, "IDENTITY_LOGIN_NAME_INVALID"))
    form.poll()
    assert "3—100" in form.message.get()
    assert form.entries["login_name"].get() == "chosen.owner"
    assert str(form.entries["login_name"].cget("state")) == "normal"
