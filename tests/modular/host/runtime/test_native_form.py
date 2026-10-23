"""Real Tk interactions with synthetic secrets and an isolated clipboard boundary."""

import gc
from threading import Event
from time import monotonic
from unittest.mock import Mock

import pytest

from ai_accounting_modular.contracts.public import ContractError

from .conftest import PASSWORD

RECOVERY_CODE = "synthetic-recovery-code-not-for-real-use"


@pytest.fixture
def form_factory(monkeypatch):
    tk = pytest.importorskip("tkinter", reason="Real native form checks require Tk")
    from ai_accounting_modular.host.runtime._native_form import SecurityForm

    forms, callback_errors = [], []

    def create(kind="login", submit=lambda _: {}, cancel=lambda: None, *, status=None):
        try:
            root = tk.Tk()
        except tk.TclError as error:
            pytest.skip(f"Real Tk requires an available desktop: {error}")
        root.withdraw()
        monkeypatch.setattr(root, "clipboard_clear", lambda: None)
        monkeypatch.setattr(root, "clipboard_append", lambda _: None)
        form = SecurityForm(root, kind, "合成负责人", submit, cancel, status=status)
        # Observe focus requests without activating a window or capturing the
        # owner's keyboard. The widgets and their Tcl commands remain real.
        for widget in (*form.entries.values(), form.copy_button, form.submit_button):
            monkeypatch.setattr(widget, "focus_set", Mock(wraps=widget.focus_set))
        root.report_callback_exception = lambda error_type, *_: callback_errors.append(
            error_type.__name__
        )
        forms.append(form)
        root.update()
        return form

    yield create
    try:
        for form in forms:
            form.destroy()
    finally:
        # Monkeypatch retains bound Tk methods. Release them and the last loop
        # variable before collecting, while still on Tk's creating thread.
        monkeypatch.undo()
        forms.clear()
        form = None
        gc.collect()
    assert not callback_errors, "A real Tk callback raised unexpectedly"


@pytest.fixture
def release_worker():
    release = Event()
    yield release
    release.set()


def wait_for(form, condition):
    """Pump Tk while waiting for a worker; the timeout only bounds a broken test."""
    deadline, pause = monotonic() + 5, Event()
    while not condition():
        assert monotonic() < deadline, "The synthetic Tk interaction did not settle"
        assert not form.finished, "The form closed before the expected result"
        form.root.update()
        pause.wait(0.005)


def key(form, widget, sequence="<Return>"):
    # Invoke Tk's registered binding with its normal event substitutions. Hidden
    # Tk cannot receive physical keys; mapping it would steal the owner's focus.
    binding = form.root.bind(sequence)
    assert binding
    command = binding.split('"', 2)[1].split()[0].removeprefix("[")
    event_values = {"%W": str(widget), "%T": "2", "%K": sequence[1:-1], "%E": "1"}
    form.root.tk.call(command, *(event_values.get(field, "0") for field in form.root._subst_format))
    if not form.finished:
        form.root.update()


def fill(form, **values):
    for name, value in values.items():
        form.entries[name].insert(0, value)


@pytest.mark.parametrize(
    ("kind", "fields"),
    [
        ("login", ("password",)),
        ("change_password", ("password", "new_password", "repeat_password")),
        ("recover", ("recovery_code", "new_password", "repeat_password")),
        ("replace_recovery_code", ()),
        ("bootstrap_owner", ("display_name", "new_password", "repeat_password")),
    ],
)
def test_fields_mask_secrets_and_choose_first_action_for_focus(form_factory, kind, fields):
    form = form_factory(kind)
    assert tuple(form.entries) == fields
    for name, entry in form.entries.items():
        assert bool(entry.cget("show")) == (name != "display_name")
    expected_focus = next(iter(form.entries.values()), form.submit_button)
    form.focus_input()
    expected_focus.focus_set.assert_called_once_with()
    assert str(form.submit_button.cget("text")) == "确认"
    assert str(form.cancel_button.cget("text")) == "取消"
    assert not form.recovery_row.winfo_ismapped()


@pytest.mark.parametrize("route", ["button", "escape", "titlebar", "return", "keypad_enter"])
def test_cancel_routes_do_not_submit_and_cancel_only_once(form_factory, route):
    submitted, cancelled, started = [], [], Event()

    def cancel():
        cancelled.append(True)
        started.set()

    form = form_factory(
        submit=lambda values: submitted.append(dict(values)),
        cancel=cancel,
    )
    fill(form, password=PASSWORD)
    if route == "button":
        form.cancel_button.invoke()
    elif route == "escape":
        key(form, form.entries["password"], "<Escape>")
    elif route == "titlebar":
        form.root.tk.call(form.root.protocol("WM_DELETE_WINDOW"))
    else:
        key(form, form.cancel_button, "<KP_Enter>" if route == "keypad_enter" else "<Return>")
    assert started.wait(2)
    form.close()
    wait_for(form, lambda: form.finished)
    form.close()
    assert submitted == []
    assert cancelled == [True]


@pytest.mark.parametrize("sequence", ["<Return>", "<KP_Enter>"])
def test_repeated_keys_submit_once_keep_tk_responsive_and_do_not_acknowledge_on_transition(
    form_factory, release_worker, sequence
):
    submitted, started = [], Event()

    def submit(values):
        submitted.append(None if values is None else dict(values))
        started.set()
        release_worker.wait()
        return {"recovery_code": RECOVERY_CODE}

    form = form_factory("change_password", submit)
    values = {"password": PASSWORD, "new_password": PASSWORD, "repeat_password": PASSWORD}
    fill(form, **values)
    key(form, form.entries["password"], sequence)
    assert started.wait(2)
    for _ in range(4):
        key(form, form.entries["password"], sequence)
        form.submit_button.invoke()
    assert submitted == [values]
    assert form.busy
    assert "正在处理" in form.message.cget("text")
    assert all(entry.get() == "" for entry in form.entries.values())
    for widget in (*form.entries.values(), form.submit_button, form.cancel_button):
        assert widget.instate(["disabled"])
    heartbeat = Event()
    form.root.after_idle(heartbeat.set)
    form.root.update()
    assert heartbeat.is_set()

    release_worker.set()
    wait_for(form, lambda: form.recovery_displayed)
    form.copy_button.focus_set.assert_called_once_with()
    # Continued Enter now activates Copy, not the newly displayed save action.
    for _ in range(3):
        key(form, form.copy_button, sequence)
    key(form, form.code, sequence)
    key(form, form.entries["password"], sequence)
    assert submitted == [values]
    assert form.code.get() == RECOVERY_CODE
    assert not form.finished


@pytest.mark.parametrize("failure", ["contract", "unexpected"])
def test_failed_input_can_retry_without_losing_name_or_retaining_passwords(form_factory, failure):
    submitted = []

    def submit(values):
        submitted.append(dict(values))
        if len(submitted) == 1:
            if failure == "contract":
                raise ContractError("synthetic_invalid", "合成校验失败，请重新输入。")
            raise RuntimeError("synthetic-secret-that-must-not-reach-the-form")
        return {"recovery_code": RECOVERY_CODE}

    form = form_factory("bootstrap_owner", submit)
    values = {"display_name": "合成新负责人", "new_password": PASSWORD, "repeat_password": PASSWORD}
    fill(form, **values)
    form.submit_button.invoke()
    wait_for(form, lambda: len(submitted) == 1 and not form.busy)
    assert not form.finished
    message = form.message.cget("text")
    expected_error = "重新输入" if failure == "contract" else "检查后台连接后重试"
    assert expected_error in message
    assert "synthetic-secret" not in message
    assert form.entries["display_name"].get() == values["display_name"]
    form.entries["display_name"].focus_set.assert_called_once_with()
    for name, entry in form.entries.items():
        assert not entry.instate(["disabled"])
        if name != "display_name":
            assert entry.get() == ""
    fill(form, new_password=PASSWORD, repeat_password=PASSWORD)
    form.submit_button.invoke()
    wait_for(form, lambda: form.recovery_displayed)
    assert submitted == [values, values]
    assert not form.finished


def test_real_recovery_copy_and_close_preserve_unacknowledged_commit(
    form_factory, monkeypatch, local
):
    runtime, _, old_code = local
    security = runtime.security
    original = runtime.kernel.storage.owner_profile()
    window = security.request({"kind": "recover"})
    request_id = window["request_id"]

    def submit(values):
        return security.native(
            "acknowledge" if values is None else "execute",
            {"request_id": request_id, **(values or {})},
        )

    form = form_factory(
        "recover",
        submit,
        lambda: security.public("cancel", {"request_id": request_id}),
    )
    fill(
        form,
        recovery_code=old_code.get_secret_value(),
        new_password=PASSWORD,
        repeat_password=PASSWORD,
    )
    form.submit_button.invoke()
    wait_for(form, lambda: form.recovery_displayed)
    generated_code = form.code.get()
    assert generated_code and generated_code != old_code.get_secret_value()
    assert str(form.code.cget("state")) == "readonly"
    copied = []

    def clipboard_unavailable(_):
        raise RuntimeError("synthetic clipboard unavailable")

    monkeypatch.setattr(form.root, "clipboard_append", clipboard_unavailable)
    form.copy_button.invoke()
    assert "复制失败" in form.message.cget("text")
    assert form.code.get() == generated_code
    monkeypatch.setattr(form.root, "clipboard_append", copied.append)
    form.copy_button.invoke()
    assert copied == [generated_code]
    assert form.copy_button.cget("text") == "已复制"
    state = security.public("status", {"request_id": request_id})
    assert state["operation_committed"]
    assert not state["recovery_code_acknowledged"]
    assert not form.finished
    assert runtime.kernel.storage.owner_profile().owner_id == original.owner_id

    form.cancel_button.invoke()
    wait_for(form, lambda: form.finished)
    state = security.public("status", {"request_id": request_id})
    assert state["status"] == "failed"
    assert state["operation_committed"]
    assert not state["recovery_code_acknowledged"]
    assert state["error_code"] == "OWNER_SECURITY_RECOVERY_CODE_NOT_ACKNOWLEDGED"


def test_acknowledgement_retry_keeps_code_and_does_not_execute_again(
    form_factory, release_worker, monkeypatch
):
    submitted, acknowledgement_started = [], Event()

    def submit(values):
        submitted.append(None if values is None else dict(values))
        if values is not None:
            return {"recovery_code": RECOVERY_CODE}
        if submitted.count(None) == 1:
            raise ContractError("synthetic_unavailable", "合成确认失败，请重试。")
        acknowledgement_started.set()
        release_worker.wait()
        return {}

    form = form_factory("replace_recovery_code", submit)
    form.submit_button.invoke()
    wait_for(form, lambda: form.recovery_displayed)
    form.submit_button.invoke()
    wait_for(form, lambda: submitted.count(None) == 1 and not form.busy)
    assert form.code.get() == RECOVERY_CODE
    assert "确认失败" in form.message.cget("text")
    assert not form.finished
    copied = []
    monkeypatch.setattr(form.root, "clipboard_append", copied.append)
    form.copy_button.invoke()
    assert copied == [RECOVERY_CODE]

    key(form, form.submit_button)
    assert acknowledgement_started.wait(2)
    for _ in range(3):
        key(form, form.submit_button)
        form.submit_button.invoke()
    assert submitted == [{}, None, None]
    assert form.busy
    release_worker.set()
    wait_for(form, lambda: form.finished)
    assert submitted == [{}, None, None]


def test_failed_cancel_keeps_recovery_code_visible_and_can_retry(form_factory):
    cancelled = []

    def cancel():
        cancelled.append(True)
        if len(cancelled) == 1:
            raise ContractError("synthetic_unavailable", "合成关闭失败，请重试。")

    form = form_factory("replace_recovery_code", lambda _: {"recovery_code": RECOVERY_CODE}, cancel)
    form.submit_button.invoke()
    wait_for(form, lambda: form.recovery_displayed)
    form.cancel_button.invoke()
    wait_for(form, lambda: len(cancelled) == 1 and not form.busy)
    assert not form.finished
    assert form.code.get() == RECOVERY_CODE
    assert "关闭失败" in form.message.cget("text")
    for button in (form.cancel_button, form.copy_button, form.submit_button):
        assert not button.instate(["disabled"])
    form.cancel_button.invoke()
    wait_for(form, lambda: form.finished)
    assert cancelled == [True, True]


def test_committed_change_with_lost_reply_cannot_execute_again_and_can_close(form_factory):
    submitted, cancelled = [], []

    def submit(values):
        submitted.append(dict(values))
        raise ContractError("service_unavailable", "合成服务回复丢失。")

    form = form_factory(
        "change_password",
        submit,
        lambda: cancelled.append(True),
        status=lambda: {
            "status": "running",
            "operation_committed": True,
            "login_completed": False,
        },
    )
    values = {"password": PASSWORD, "new_password": PASSWORD, "repeat_password": PASSWORD}
    fill(form, **values)
    form.submit_button.invoke()
    wait_for(form, lambda: bool(submitted) and not form.busy)
    assert "已更换" in form.message.cget("text")
    assert "未收到新的恢复码" in form.message.cget("text")
    assert form.submit_button.instate(["disabled"])
    assert all(entry.instate(["disabled"]) for entry in form.entries.values())
    form.submit_button.invoke()
    form.confirm()
    assert submitted == [values]
    assert not form.finished
    form.cancel_button.invoke()
    wait_for(form, lambda: form.finished)
    assert cancelled == [True]


def test_unknown_execute_and_failed_cancel_do_not_restore_execute(form_factory):
    submitted, cancelled = [], []

    def submit(values):
        submitted.append(dict(values))
        raise ConnectionError("synthetic lost execute response")

    def status():
        raise ConnectionError("synthetic lost status response")

    def cancel():
        cancelled.append(True)
        if len(cancelled) == 1:
            raise ContractError("service_unavailable", "合成关闭未完成，请重试。")

    form = form_factory("change_password", submit, cancel, status=status)
    values = {"password": PASSWORD, "new_password": PASSWORD, "repeat_password": PASSWORD}
    fill(form, **values)
    form.submit_button.invoke()
    wait_for(form, lambda: bool(submitted) and not form.busy)
    assert "未能确认办理结果" in form.message.cget("text")
    assert form.submit_button.instate(["disabled"])
    form.cancel_button.invoke()
    wait_for(form, lambda: bool(cancelled) and not form.busy)
    assert "关闭未完成" in form.message.cget("text")
    assert not form.finished
    assert form.submit_button.instate(["disabled"])
    assert all(entry.instate(["disabled"]) for entry in form.entries.values())
    form.submit_button.invoke()
    form.confirm()
    assert submitted == [values]
    form.cancel_button.invoke()
    wait_for(form, lambda: form.finished)
    assert cancelled == [True, True]


def test_status_disconnect_keeps_window_until_explicit_terminal_status(form_factory):
    checks = []

    def status():
        checks.append(True)
        if len(checks) == 1:
            raise ConnectionError("synthetic temporary disconnect")
        return {"status": "waiting_for_user" if len(checks) == 2 else "expired"}

    form = form_factory(status=status)
    wait_for(form, lambda: len(checks) >= 2)
    assert not form.finished
    wait_for(form, lambda: form.finished)
    assert len(checks) == 3
