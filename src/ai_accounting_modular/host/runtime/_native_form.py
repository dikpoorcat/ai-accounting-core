"""Native identity interactions; workers never access Tk or print submitted secrets."""

from __future__ import annotations

from queue import Empty, Queue
from threading import Event, Thread
from tkinter import END, ttk

from ai_accounting_modular.contracts.public import ContractError

_TITLES = {
    "login": "负责人登录",
    "change_password": "修改负责人密码",
    "recover": "通过恢复码恢复访问",
    "replace_recovery_code": "更换恢复码",
    "bootstrap_owner": "首次设置负责人",
}
_FIELDS = {
    "login": ("password",),
    "change_password": ("password", "new_password", "repeat_password"),
    "recover": ("recovery_code", "new_password", "repeat_password"),
    "replace_recovery_code": (),
    "bootstrap_owner": ("display_name", "new_password", "repeat_password"),
}
_LABELS = {
    "display_name": "负责人姓名",
    "password": "当前密码",
    "new_password": "新密码",
    "repeat_password": "再次输入新密码",
    "recovery_code": "恢复码",
}


def _check_failure(action, error, status):
    message = (
        error.message
        if isinstance(error, ContractError)
        else "本机窗口未能完成操作，请检查后台连接后重试。"
    )
    uncertain = not isinstance(error, ContractError) or error.code in {
        "service_unavailable",
        "service_identity_mismatch",
    }
    if action == "execute" and status is not None and uncertain:
        try:
            state = status()
        except Exception:
            state = {}
        if state.get("login_completed"):
            return state, None
        if state.get("operation_committed"):
            message = (
                "密码或恢复码已更换，但未收到新的恢复码。请关闭窗口，使用当前密码登录后更换恢复码。"
            )
        else:
            message = "未能确认办理结果。请关闭窗口，再重新打开身份操作窗口核验。"
        # A lost response must never turn into a second credential change.
        return None, (message, False)
    return None, (message, True)


class SecurityForm:
    def __init__(self, root, kind, display_name, submit, cancel, *, status=None):
        self.root, self.submit, self.cancel, self.status = root, submit, cancel, status
        self.busy = self.finished = self.recovery_displayed = False
        self.retry_execute = True
        self.results, self.stop = Queue(), Event()
        root.title("AI 记账 - " + _TITLES[kind])
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.report_callback_exception = lambda *_: self.callback_failed()
        root.bind("<Return>", self.enter)
        root.bind("<KP_Enter>", self.enter)
        root.bind("<Escape>", lambda _: self.close())

        panel = ttk.Frame(root, padding=24)
        panel.grid()
        ttk.Label(panel, text=_TITLES[kind], font=("Microsoft YaHei UI", 15, "bold")).grid(
            sticky="w", pady=(0, 12)
        )
        guidance = [f"负责人：{display_name}"] if display_name else []
        if kind in {"bootstrap_owner", "change_password", "recover"}:
            guidance.append("新密码须为 6 至 128 个字符。")
        if kind in {"change_password", "recover"}:
            guidance.append("完成后旧会话和旧恢复码失效，需要重新登录。")
        if kind == "replace_recovery_code":
            guidance.append("确认后旧恢复码立即失效，请保存新的恢复码。")
        guidance.append("密码只在此本机窗口处理，不会发送到聊天。")
        ttk.Label(panel, text="\n".join(guidance), wraplength=420).grid(sticky="w")
        self.entries = {}
        for name in _FIELDS[kind]:
            ttk.Label(panel, text=_LABELS[name]).grid(sticky="w", pady=(8, 2))
            entry = ttk.Entry(panel, width=46, show="" if name == "display_name" else "●")
            entry.grid(sticky="ew")
            self.entries[name] = entry
        self.message = ttk.Label(panel, text="", wraplength=420)
        self.message.grid(sticky="w", pady=14)
        self.recovery_row = ttk.Frame(panel)
        self.recovery_row.grid(sticky="ew")
        self.code = ttk.Entry(self.recovery_row, width=38, state="readonly")
        self.code.grid(row=0, column=0, sticky="ew")
        self.copy_button = ttk.Button(
            self.recovery_row, text="复制恢复码", command=self.copy_recovery_code
        )
        self.copy_button.grid(row=0, column=1, padx=(12, 0))
        self.recovery_row.grid_remove()
        buttons = ttk.Frame(panel)
        buttons.grid(sticky="e", pady=(12, 0))
        self.cancel_button = ttk.Button(buttons, text="取消", command=self.close)
        self.cancel_button.pack(side="left", padx=8)
        self.submit_button = ttk.Button(
            buttons, text="确认", command=self.confirm, default="active"
        )
        self.submit_button.pack(side="left")
        self.map_id = root.after_idle(self.mapped)
        self.poll_id = root.after(50, self.poll)
        self.recovery_message = "请妥善保存恢复码；丢失密码时，可用它恢复访问。"
        if kind != "bootstrap_owner":
            self.recovery_message = "旧恢复码已失效。请妥善保存新的恢复码。"
        if kind in {"change_password", "recover"}:
            self.recovery_message += "完成后请重新登录。"
        if status is not None:
            results, stop = self.results, self.stop

            def watch():
                while not stop.wait(1):
                    try:
                        terminal = status()["status"] in {
                            "succeeded",
                            "failed",
                            "cancelled",
                            "expired",
                        }
                    except Exception:
                        # Losing the connection does not mean cancellation, especially
                        # while a newly generated recovery code is still on screen.
                        continue
                    if terminal:
                        results.put(("external_close", None, None))
                        return

            Thread(target=watch, daemon=True).start()

    def mapped(self):
        if self.finished:
            return
        if not self.root.winfo_viewable():
            self.map_id = self.root.after(50, self.mapped)
            return
        self.root.lift()
        self.root.focus_force()
        self.focus_input()

    def focus_input(self):
        if self.entries:
            next(iter(self.entries.values())).focus_set()
        else:
            self.submit_button.focus_set()

    def enter(self, event):
        # Enter on Cancel or Copy must activate that button, not the default action.
        if isinstance(event.widget, ttk.Button):
            event.widget.invoke()
        elif not self.recovery_displayed:
            self.confirm()
        return "break"

    def start(self, action, callback, values=None):
        if self.busy or self.finished:
            return
        self.busy = True
        for widget in (
            *self.entries.values(),
            self.submit_button,
            self.cancel_button,
            self.copy_button,
        ):
            widget.configure(state="disabled")
        self.message.configure(text="正在处理，请稍候……")
        results, status = self.results, self.status

        def run():
            try:
                result = callback(values) if action != "cancel" else callback()
                results.put((action, result, None))
            except Exception as error:
                result, failure = _check_failure(action, error, status)
                results.put((action, result, failure))
            finally:
                if values is not None:
                    values.clear()

        Thread(target=run, daemon=True).start()

    def confirm(self):
        if self.busy or self.finished:
            return
        if self.recovery_displayed:
            self.start("acknowledge", self.submit)
            return
        if not self.retry_execute:
            return
        values = {name: entry.get() for name, entry in self.entries.items()}
        for name, entry in self.entries.items():
            if name != "display_name":
                entry.delete(0, END)
        self.start("execute", self.submit, values)

    def close(self):
        if not self.busy and not self.finished:
            for name, entry in self.entries.items():
                if name != "display_name":
                    entry.delete(0, END)
        self.start("cancel", self.cancel)
        return "break"

    def poll(self):
        self.poll_id = self.root.after(50, self.poll)
        try:
            action, result, error = self.results.get_nowait()
        except Empty:
            return
        if action == "external_close":
            if self.busy:
                self.stop.set()
            else:
                self.destroy()
            return
        self.busy = False
        if self.stop.is_set():
            self.destroy()
            return
        if not error and (action == "cancel" or not (result or {}).get("recovery_code")):
            self.destroy()
            return
        self.submit_button.configure(state="normal")
        self.cancel_button.configure(state="normal")
        if error:
            message, retry = error
            if action == "execute":
                self.retry_execute = retry
            self.message.configure(text=message)
            if self.recovery_displayed:
                self.copy_button.configure(state="normal")
                self.submit_button.focus_set()
            elif self.retry_execute:
                for entry in self.entries.values():
                    entry.configure(state="normal")
                self.focus_input()
            else:
                self.submit_button.configure(state="disabled")
                self.cancel_button.configure(text="关闭")
                self.cancel_button.focus_set()
            return
        self.recovery_displayed = True
        self.code.configure(state="normal")
        self.code.insert(0, result["recovery_code"])
        self.code.configure(state="readonly")
        self.recovery_row.grid()
        self.copy_button.configure(state="normal")
        self.submit_button.configure(text="我已保存恢复码")
        self.cancel_button.configure(text="关闭")
        self.message.configure(text="操作已完成。" + self.recovery_message)
        self.copy_button.focus_set()

    def copy_recovery_code(self):
        if self.busy or not self.recovery_displayed:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.code.get())
        except Exception:
            self.message.configure(text="复制失败，请重试或手动保存恢复码。")
            return
        self.copy_button.configure(text="已复制")

    def callback_failed(self):
        self.message.configure(text="窗口发生错误，请关闭窗口后重新打开。")

    def destroy(self):
        if not self.finished:
            self.finished = True
            self.stop.set()
            self.root.after_cancel(self.map_id)
            self.root.after_cancel(self.poll_id)
            self.root.report_callback_exception = lambda *_: None
            self.root.destroy()
