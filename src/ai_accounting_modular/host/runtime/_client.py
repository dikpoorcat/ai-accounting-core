"""Thin, catalog-verified local clients; a lost reply never triggers business replay."""

from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.storage.public import Storage

from . import _state


def request(metadata, path, payload, *, timeout=60):
    connection = http.client.HTTPConnection("127.0.0.1", metadata["port"], timeout=timeout)
    try:
        connection.request(
            "POST",
            path,
            json.dumps(payload, ensure_ascii=False).encode(),
            {
                "Content-Type": "application/json",
                "X-Modular-Capability": metadata["capability"],
            },
        )
        response = connection.getresponse()
        raw = response.read()
        value = json.loads(raw)
        if response.status != 200:
            if isinstance(value, dict) and value.get("status") == "rejected":
                raise ContractError(
                    value["code"],
                    value["message"],
                    fact_issues=value.get("fact_issues", ()),
                    diagnostic_id=value.get("diagnostic_id"),
                )
            raise ContractError("service_unavailable", "本机服务暂时无法连接。")
        if (
            not isinstance(value, dict)
            or value.get("protocol") != _state.PROTOCOL
            or value.get("catalog_id") != metadata["catalog_id"]
            or "result" not in value
        ):
            raise ContractError("service_identity_mismatch", "本机服务与所选目录不一致。")
        return value["result"]
    except ContractError:
        raise
    except (OSError, http.client.HTTPException, ValueError):
        raise ContractError(
            "service_unavailable", "本机连接中断；办理结果请按业务身份查询，不要盲目重放。"
        ) from None
    finally:
        connection.close()


def discover(catalog_path):
    catalog = _state.catalog_path(Path(catalog_path))
    identity = Storage(catalog).catalog_identity()
    metadata = _state.read(catalog)
    if metadata["catalog_id"] != identity:
        raise ContractError("service_identity_mismatch", "连接资料不属于所选目录。")
    try:
        request(metadata, "/private/info", {}, timeout=2)
    except ContractError:
        # A former port may now belong to another service; this is not an owner login failure.
        raise ContractError("service_unavailable", "所选目录的本机后台地址当前不可用。") from None
    return metadata


def ensure_service(catalog_path, *, timeout=20):
    catalog = _state.catalog_path(Path(catalog_path))
    # Startup never creates a catalog, invents an owner, changes schemas or touches other roots.
    Storage(catalog)
    deadline, spawned = time.monotonic() + timeout, False
    while time.monotonic() < deadline:
        try:
            return discover(catalog)
        except FileNotFoundError:
            pass
        except ContractError as error:
            if error.code != "service_unavailable":
                raise
        if not spawned:
            try:
                with _state.instance_lock(catalog):
                    pass
            except ContractError as error:
                if error.code != "busy":
                    raise
            else:
                # Release the probe lock before spawning; the resident holds its lifetime lock.
                subprocess.Popen(
                    [
                        sys.executable,
                        "-I",
                        "-X",
                        "utf8",
                        "-m",
                        "ai_accounting_modular.host.runtime.cli",
                        "--catalog",
                        str(catalog),
                        "daemon",
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    start_new_session=os.name != "nt",
                    close_fds=True,
                )
                spawned = True
        time.sleep(0.1)
    raise ContractError("service_start_failed", "新体系后台未能启动，请显式运行 daemon 核查。")


class Client:
    def __init__(self, catalog_path, *, autostart=True):
        self.catalog_path = Path(catalog_path).absolute()
        self.metadata = (
            ensure_service(self.catalog_path) if autostart else discover(self.catalog_path)
        )

    def call(self, path, payload):
        # Refresh the proven endpoint before sending; never retry a possibly executed POST.
        self.metadata = discover(self.catalog_path)
        return request(self.metadata, path, payload)

    def dispatch(self, command, payload):
        return self.call("/private/command", {"command": command, "payload": payload})

    def security(self, operation, payload):
        return self.call("/private/security", {"operation": operation, "payload": payload})

    def browser_url(self):
        return self.call("/private/browser-ticket", {})

    def contracts(self):
        return self.call("/private/contracts", {})

    def create_company(self, payload):
        return self.call("/private/company-create", payload)

    def register_company(self, payload):
        return self.call("/private/company-register", payload)

    def info(self):
        return self.call("/private/info", {})

    def stop(self):
        return self.call("/private/stop", {})
