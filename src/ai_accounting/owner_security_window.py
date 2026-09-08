"""Native local-only form. Never export secrets via stdout, logs, files, or IPC."""

from __future__ import annotations

import queue
import threading
import uuid

from pydantic import SecretStr

from .identity import PASSWORD_MAX_LENGTH, PASSWORD_MIN_LENGTH, IdentityError
from .identity_cli import SafeArgumentParser
from .owner_login_launcher import OwnerSecurityWindowLauncher, assert_interactive_desktop
from .owner_security import WINDOW_TITLES

_MESSAGES = {
    "IDENTITY_PASSWORD_CONFIRMATION_MISMATCH": "两次输入的新密码不一致，请重新输入。",
    "IDENTITY_PASSWORD_POLICY_REJECTED": (
        f"新密码须为 {PASSWORD_MIN_LENGTH}—{PASSWORD_MAX_LENGTH} 个字符，不能包含空字符。"
    ),
    "IDENTITY_AUTHENTICATION_FAILED": "密码不正确、会话已失效或尝试过于频繁，请稍后重试。",
    "IDENTITY_RECOVERY_FAILED": "恢复码无效或尝试过于频繁，请核对后再试。",
    "ACCOUNTING_PERIOD_CALCULATION_STALE": "关账预览已变化，请回到记账助手重新预览。",
    "OWNER_SECURITY_TARGET_MISMATCH": "目标数据库已变化，本次操作已停止。",
    "IDENTITY_CREDENTIAL_STORE_WRITE_FAILED": "本机登录状态保存失败，请重新登录。",
    "IDENTITY_SESSION_INVALID": "当前登录已失效，请关闭此窗口，重新登录后再操作。",
    "IDENTITY_LOCAL_SESSION_REQUIRED": "请先完成本机登录，再请求此操作。",
    "ACCOUNTING_PERIOD_NOT_OPEN": "该月份不可关账，请关闭此窗口并重新查询期间状态。",
    "COMPANY_NOT_ACTIVE": "该公司当前不可操作，请关闭此窗口并核验公司状态。",
}
_LABELS = {
    "password": "当前密码",
    "new_password": "新密码",
    "repeat_password": "再次输入新密码",
    "recovery_code": "恢复码",
}


def safe_error(exc):
    return exc.code if isinstance(exc, IdentityError) else "OWNER_SECURITY_OPERATION_FAILED"


class SecurityForm:
    def __init__(self, root, launcher, record, facts):
        import tkinter as tk
        from tkinter import ttk

        self.root, self.launcher, self.record = root, launcher, record
        self.operations = launcher.operations
        self.request = record.request
        self.busy = False
        self.finished = False
        self.recovery_displayed = False
        self.saved_new_password = None
        self.results = queue.Queue()
        root.title(WINDOW_TITLES[self.request.kind])
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.cancel)
        root.report_callback_exception = lambda *_: self.callback_failed()
        frame = ttk.Frame(root, padding=24)
        frame.grid(sticky="nsew")
        ttk.Label(
            frame,
            text=WINDOW_TITLES[self.request.kind].split(" - ")[-1],
            font=("Microsoft YaHei UI", 15, "bold"),
        ).grid(sticky="w", pady=(0, 12))
        text = f"公司：{facts['company_name']}\n负责人：{facts['login_name']}"
        if facts.get("target_label"):
            text += f"\n目标库：{facts['target_label']}"
        if self.request.kind == "approve_period_close":
            text += (
                f"\n关账月份：{facts['period_month']}"
                f"\n预览指纹：{self.request.calculation_hash}"
                "\n本次验证仅授权该月份的当前关账预览，不执行关账。"
            )
        if self.request.kind in {"change_password", "recover"}:
            text += "\n完成后旧会话和旧恢复码失效，需要重新登录。"
        if self.request.kind in {"bootstrap_owner", "change_password", "recover"}:
            text += f"\n新密码长度：{PASSWORD_MIN_LENGTH}—{PASSWORD_MAX_LENGTH} 个字符。"
        if self.request.kind == "replace_recovery_code":
            text += "\n确认后旧恢复码立即失效，请保存新恢复码。"
        ttk.Label(frame, text=text, wraplength=460).grid(sticky="w", pady=(0, 14))
        self.entries = {}
        if self.request.kind in {"login", "approve_period_close", "change_password"}:
            fields = ["password"]
        else:
            fields = []
        if self.request.kind == "recover":
            fields.append("recovery_code")
        if self.request.kind in {"bootstrap_owner", "recover", "change_password"}:
            fields.extend(["new_password", "repeat_password"])
        for field in fields:
            ttk.Label(frame, text=_LABELS[field]).grid(sticky="w", pady=(6, 3))
            entry = ttk.Entry(frame, show="●", width=46)
            entry.grid(sticky="ew")
            self.entries[field] = entry
        self.message = tk.StringVar(value="请在此窗口完成操作，密码不会发送到聊天。")
        ttk.Label(frame, textvariable=self.message, wraplength=460).grid(sticky="w", pady=14)
        self.recovery_label = ttk.Label(frame, text="", wraplength=460)
        self.recovery_label.grid(sticky="w")
        buttons = ttk.Frame(frame)
        buttons.grid(sticky="e", pady=(12, 0))
        self.cancel_button = ttk.Button(buttons, text="取消", command=self.cancel)
        self.cancel_button.pack(side="left", padx=8)
        self.submit_button = ttk.Button(buttons, text="确认", command=self.submit)
        self.submit_button.pack(side="left")
        root.bind("<Return>", lambda _: self.submit())
        root.after(100, self.poll)
        root.after_idle(self.mapped)

    def mapped(self):
        if not self.root.winfo_viewable():
            self.root.after(50, self.mapped)
            return
        if not self.busy and not self.finished:
            self.launcher.update(self.record.request_id, status="waiting_for_user")
        self.root.lift()
        self.root.focus_force()
        if self.entries:
            next(iter(self.entries.values())).focus_set()

    def callback_failed(self):
        self.message.set("窗口发生错误，请关闭窗口并回到记账助手。")
        if not self.busy and not self.finished:
            try:
                self.launcher.update(
                    self.record.request_id,
                    status="failed",
                    error_code="OWNER_SECURITY_WINDOW_UNAVAILABLE",
                )
            finally:
                self.finished = True

    def submit(self):
        if self.busy:
            return
        if self.finished:
            self.root.destroy()
            return
        values = {key: SecretStr(entry.get()) for key, entry in self.entries.items()}
        for entry in self.entries.values():
            entry.delete(0, "end")
            entry.configure(state="disabled")
        if self.request.kind == "bootstrap_owner" and not self.recovery_displayed:
            self.saved_new_password = values.get("new_password")
        finishing = self.recovery_displayed
        self.busy = True
        self.submit_button.configure(state="disabled")
        self.cancel_button.configure(state="disabled")
        self.message.set("正在处理，请稍候……")
        try:
            self.launcher.update(
                self.record.request_id,
                status="running",
                error_code=None,
                operation_committed=True if finishing else None,
                recovery_code_acknowledged=finishing,
            )
        except Exception as exc:
            self.busy = False
            self.finished = True
            self.saved_new_password = None
            values.clear()
            self.message.set("无法记录窗口状态，操作已停止，请关闭窗口。\n" + safe_error(exc))
            self.submit_button.configure(state="normal", text="关闭")
            self.cancel_button.configure(state="normal")
            return

        def work():
            try:
                if finishing:
                    self.operations.finish_recovery_display(
                        self.request,
                        self.record.target_id,
                        new_password=self.saved_new_password,
                    )
                    result = None
                else:
                    result = self.operations.execute(self.request, self.record.target_id, **values)
                self.results.put((result, None))
            except Exception as exc:
                self.results.put((None, safe_error(exc)))
            finally:
                values.clear()

        threading.Thread(target=work, daemon=True).start()

    def poll(self):
        try:
            recovery, error = self.results.get_nowait()
        except queue.Empty:
            self.root.after(100, self.poll)
            return
        self.busy = False
        self.submit_button.configure(state="normal")
        self.cancel_button.configure(state="normal")
        facts = {
            "operation_committed": self.operations.operation_committed,
            "login_completed": self.operations.login_completed,
        }
        if error:
            committed = self.operations.operation_committed is not False
            self.launcher.update(
                self.record.request_id,
                status="failed" if committed else "waiting_for_user",
                error_code=error,
                **facts,
            )
            self.message.set(
                (
                    "身份操作结果不确定，请先核验账号状态。"
                    if self.operations.operation_committed is None
                    else "身份操作已完成，但后续步骤未完成。"
                    if committed
                    else ""
                )
                + _MESSAGES.get(error, "操作未完成，请回到记账助手处理。")
                + f"\n{error}"
            )
            self.saved_new_password = None
            if committed:
                self.finished = True
                self.submit_button.configure(text="关闭")
            else:
                for entry in self.entries.values():
                    entry.configure(state="normal")
        elif recovery is not None:
            self.recovery_displayed = True
            for entry in self.entries.values():
                entry.configure(state="disabled")
            self.recovery_label.configure(text=recovery.get_secret_value())
            self.message.set("请现在保存下面的恢复码。它只在此窗口显示，不会自动复制或保存。")
            self.submit_button.configure(text="我已保存恢复码")
            self.launcher.update(self.record.request_id, status="waiting_for_user", **facts)
        else:
            self.saved_new_password = None
            self.recovery_label.configure(text="")
            self.finished = True
            self.launcher.update(self.record.request_id, status="succeeded", **facts)
            self.message.set(
                "操作完成。"
                + ("请重新登录。" if self.request.kind in {"recover", "change_password"} else "")
            )
            self.submit_button.configure(text="关闭")
            self.root.after(1800, self.root.destroy)
        self.root.after(100, self.poll)

    def cancel(self):
        if self.busy:
            return
        if not self.finished:
            committed = self.operations.operation_committed is not False
            self.launcher.update(
                self.record.request_id,
                status="failed" if committed else "cancelled",
                operation_committed=self.operations.operation_committed,
                login_completed=self.operations.login_completed,
                error_code="OWNER_SECURITY_RECOVERY_CODE_NOT_ACKNOWLEDGED" if committed else None,
            )
        self.saved_new_password = None
        self.root.destroy()


def main():
    parser = SafeArgumentParser()
    parser.add_argument("--request-id", type=uuid.UUID, required=True)
    args = parser.parse_args()
    launcher = OwnerSecurityWindowLauncher()
    try:
        record = launcher.load_for_window(args.request_id)
        facts = launcher.operations.inspect(record.request, record.target_id)
        assert_interactive_desktop()
        import tkinter as tk

        root = tk.Tk()
        SecurityForm(root, launcher, record, facts)
        root.mainloop()
    except Exception as exc:
        try:
            launcher.update(args.request_id, status="failed", error_code=safe_error(exc))
        except Exception:
            pass  # Never send exception details or secrets to inherited stdio.


if __name__ == "__main__":
    main()
