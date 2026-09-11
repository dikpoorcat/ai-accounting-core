"""Native form. Recovery codes leave it only through an explicit clipboard-copy action."""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import uuid

from pydantic import SecretStr

from .native import WINDOW_TITLES
from .primitives import PASSWORD_MAX_LENGTH, PASSWORD_MIN_LENGTH, IdentityError
from .transport import NativeHttpClient, WindowBridge
from .windows import assert_interactive_desktop

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
    "IDENTITY_CLOSE_APPROVAL_INVALID": "关账批准已过期或与当前范围不一致，请重新预览并确认。",
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
        self.external_close = queue.Queue()
        self.watch_stop = threading.Event()
        if hasattr(launcher, "status"):
            # The watcher must never retain a form or a Tcl interpreter.
            external_close, watch_stop = self.external_close, self.watch_stop

            def watch_status():
                while not watch_stop.is_set():
                    try:
                        if launcher.status()["status"] in {
                            "succeeded",
                            "failed",
                            "cancelled",
                            "expired",
                        }:
                            external_close.put(True)
                            return
                    except Exception:
                        external_close.put(True)
                        return
                    watch_stop.wait(1)

            threading.Thread(target=watch_status, daemon=True).start()
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
        if self.request.kind == "approve_close_batches":
            text += "\n一次密码确认仅批准以下公司及明确范围，各公司分别执行一次。"
        if self.request.kind in {"change_password", "recover"}:
            text += "\n完成后旧会话和旧恢复码失效，需要重新登录。"
        if self.request.kind in {"bootstrap_owner", "change_password", "recover"}:
            text += f"\n新密码长度：{PASSWORD_MIN_LENGTH}—{PASSWORD_MAX_LENGTH} 个字符。"
        if self.request.kind == "replace_recovery_code":
            text += "\n确认后旧恢复码立即失效，请保存新恢复码。"
        ttk.Label(frame, text=text, wraplength=460).grid(sticky="w", pady=(0, 14))
        if self.request.kind == "approve_close_batches":
            batch_frame = ttk.Frame(frame)
            batch_frame.grid(sticky="ew", pady=(0, 12))
            listing = tk.Text(
                batch_frame,
                height=min(16, 4 * len(facts["batches"])),
                width=72,
                wrap="word",
                font=("Microsoft YaHei UI", 10),
            )
            scroll = ttk.Scrollbar(batch_frame, orient="vertical", command=listing.yview)
            listing.configure(yscrollcommand=scroll.set)
            listing.grid(row=0, column=0, sticky="nsew")
            scroll.grid(row=0, column=1, sticky="ns")
            listing.insert(
                "1.0",
                "\n\n".join(
                    f"公司：{item['company_name']}\n"
                    f"期间：{item['from_period']} 至 {item['through_period']}（含起止月）\n"
                    f"目标库：{item['database_id']}\n预览指纹：{item['calculation_hash']}"
                    for item in facts["batches"]
                ),
            )
            listing.configure(state="disabled")
        self.entries = {}
        if self.request.kind in {
            "login",
            "approve_period_close",
            "approve_close_batches",
            "change_password",
        }:
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
        recovery_row = ttk.Frame(frame)
        recovery_row.grid(sticky="ew")
        self.recovery_label = ttk.Label(recovery_row, text="", wraplength=340)
        self.recovery_label.grid(row=0, column=0, sticky="w")
        self.copy_button = ttk.Button(
            recovery_row, text="复制恢复码", command=self.copy_recovery_code
        )
        self.copy_button.grid(row=0, column=1, padx=(12, 0), sticky="e")
        self.copy_button.grid_remove()
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

    def copy_recovery_code(self):
        if self.busy or not self.recovery_displayed:
            return
        recovery_code = self.recovery_label.cget("text")
        if not recovery_code:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(recovery_code)
        except Exception:
            self.copy_button.configure(text="复制恢复码")
            self.message.set(
                "复制失败，请重试或手动保存恢复码。\nOWNER_SECURITY_CLIPBOARD_UNAVAILABLE"
            )
            return
        self.copy_button.configure(text="已复制")
        if self.message.get().endswith("OWNER_SECURITY_CLIPBOARD_UNAVAILABLE"):
            self.message.set(
                "恢复码已复制，请粘贴到安全位置，再点击“我已保存恢复码”。请勿粘贴到聊天中。"
            )
        # Copying is not acknowledgement that the owner has securely saved the code.

    def submit(self):
        if self.busy:
            return
        if self.finished:
            self.destroy()
            return
        values = {key: SecretStr(entry.get()) for key, entry in self.entries.items()}
        for entry in self.entries.values():
            entry.delete(0, "end")
            entry.configure(state="disabled")
        if self.request.kind == "bootstrap_owner" and not self.recovery_displayed:
            self.saved_new_password = values.get("new_password")
        finishing = self.recovery_displayed
        self.busy = True
        self.copy_button.configure(state="disabled")
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
            self.copy_button.configure(state="normal")
            return

        operations, request = self.operations, self.request
        target_id, saved_new_password = self.record.target_id, self.saved_new_password
        results = self.results

        def work():
            try:
                if finishing:
                    operations.finish_recovery_display(
                        request,
                        target_id,
                        new_password=saved_new_password,
                    )
                    result = None
                else:
                    result = operations.execute(request, target_id, **values)
                results.put((result, None))
            except Exception as exc:
                results.put((None, safe_error(exc)))
            finally:
                values.clear()

        threading.Thread(target=work, daemon=True).start()

    def poll(self):
        if not self.external_close.empty() and not self.busy:
            self.saved_new_password = None
            self.finished = True
            self.destroy()
            return
        try:
            recovery, error = self.results.get_nowait()
        except queue.Empty:
            self.root.after(100, self.poll)
            return
        self.busy = False
        self.copy_button.configure(state="normal")
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
            self.copy_button.configure(text="复制恢复码")
            self.copy_button.grid()
            self.message.set(
                "请保存下面的恢复码，可点击按钮复制到剪贴板，再粘贴到安全位置。"
                "复制不等于已保存，请勿粘贴到聊天中。"
            )
            self.submit_button.configure(text="我已保存恢复码")
            self.launcher.update(self.record.request_id, status="waiting_for_user", **facts)
        else:
            self.saved_new_password = None
            self.recovery_label.configure(text="")
            self.copy_button.grid_remove()
            self.finished = True
            self.launcher.update(self.record.request_id, status="succeeded", **facts)
            self.destroy()
            return
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
        self.destroy()

    def destroy(self):
        self.finished = True
        self.watch_stop.set()
        self.root.report_callback_exception = lambda *_: None
        self.message = None
        self.root.destroy()


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise IdentityError("IDENTITY_REQUEST_INVALID")


def main():
    bridge = None
    try:
        parser = SafeArgumentParser()
        parser.add_argument("--request-id", type=uuid.UUID, required=True)
        args = parser.parse_args()
        raw = sys.stdin.buffer.read(8193)
        sys.stdin.close()
        if len(raw) > 8192:
            raise IdentityError("OWNER_SECURITY_STATE_INVALID")
        bootstrap = json.loads(raw)
        client = NativeHttpClient(**bootstrap)
        del bootstrap, raw
        bridge = WindowBridge(client, args.request_id.hex)
        record, facts = bridge.inspect()
        assert_interactive_desktop()
        import tkinter as tk

        root = tk.Tk()
        SecurityForm(root, bridge, record, facts)
        root.mainloop()
    except Exception as exc:
        if bridge is not None:
            try:
                bridge.update(bridge.request_id, status="failed", error_code=safe_error(exc))
            except Exception:
                pass  # No exceptions or credential material on inherited stdio.


if __name__ == "__main__":
    main()
