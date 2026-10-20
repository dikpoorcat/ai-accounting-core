"""Shared live input pins; persistent task plans are their restart authority."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from weakref import WeakValueDictionary

from ai_accounting_modular.contracts.public import ContractError

_guard = RLock()
_companies: WeakValueDictionary[Path, CompanyRuntime] = WeakValueDictionary()


@dataclass
class CompanyRuntime:
    users: int = 0
    workers: int = 0
    leases: dict = field(default_factory=dict)
    guard: RLock = field(default_factory=RLock)

    def release(self, task_id):
        with self.guard:
            value = self.leases.pop(task_id, None)
        if value is not None:
            value[1].__exit__(None, None, None)

    def capture_pinned(self, capture_id):
        with self.guard:
            return capture_id is not None and any(
                item[0] == capture_id for item in self.leases.values()
            )


def attach(root: Path) -> CompanyRuntime:
    with _guard:
        runtime = _companies.get(root)
        if runtime is None:
            runtime = CompanyRuntime()
            _companies[root] = runtime
        runtime.users += 1
        return runtime


def detach(runtime: CompanyRuntime):
    with _guard:
        if runtime.users == 1 and runtime.workers:
            raise ContractError("busy", "仍有文件生产者执行，请先结束后台执行再关闭。")
        runtime.users -= 1
        if not runtime.users:
            for task_id in tuple(runtime.leases):
                runtime.release(task_id)
