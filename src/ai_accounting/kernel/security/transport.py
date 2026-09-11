"""Private loopback transport and stdin-only native window bootstrap."""

from __future__ import annotations

import http.client
import json
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

from pydantic import SecretStr

from .native import NativeRequest
from .primitives import IdentityError
from .service import secret_text
from .windows import assert_interactive_desktop


def launch_native_window(request_id, *, port, capability, catalog_instance_id):
    assert_interactive_desktop()
    request_id = uuid.UUID(str(request_id)).hex
    executable = Path(sys.executable).with_name("pythonw.exe")
    if not executable.is_file():
        raise IdentityError("OWNER_SECURITY_WINDOW_UNAVAILABLE")
    process = subprocess.Popen(
        [
            str(executable),
            "-I",
            "-X",
            "utf8",
            "-m",
            "ai_accounting.kernel.security.window",
            "--request-id",
            request_id,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        shell=False,
    )
    try:
        raw = json.dumps(
            {
                "port": port,
                "capability": secret_text(capability),
                "catalog_instance_id": catalog_instance_id,
            }
        ).encode()
        process.stdin.write(raw)
        process.stdin.close()
    except Exception:
        process.terminate()
        raise IdentityError("OWNER_SECURITY_WINDOW_UNAVAILABLE") from None
    return process.pid


class NativeHttpClient:
    def __init__(self, *, port, capability, catalog_instance_id):
        if type(port) is not int or not 1 <= port <= 65535:
            raise IdentityError("OWNER_SECURITY_TARGET_MISMATCH")
        if not isinstance(catalog_instance_id, str) or not catalog_instance_id:
            raise IdentityError("OWNER_SECURITY_TARGET_MISMATCH")
        self.port = port
        self.capability = SecretStr(secret_text(capability))
        self.catalog_instance_id = catalog_instance_id

    def call(self, operation, request_id, **values):
        payload = {"operation": operation, "request_id": str(request_id), **values}
        payload = {
            key: value.get_secret_value() if isinstance(value, SecretStr) else value
            for key, value in payload.items()
        }
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            connection.request(
                "POST",
                "/api/security",
                json.dumps(payload).encode(),
                {
                    "X-Local-Capability": self.capability.get_secret_value(),
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            raw = response.read(65_537)
            if len(raw) > 65_536:
                raise IdentityError("OWNER_SECURITY_STATE_INVALID")
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise IdentityError("OWNER_SECURITY_STATE_INVALID")
            if response.status != 200 or result.get("status") == "rejected":
                code = result.get("code", "OWNER_SECURITY_OPERATION_FAILED")
                if (
                    not isinstance(code, str)
                    or len(code) > 100
                    or not all(c.isupper() or c.isdigit() or c == "_" for c in code)
                ):
                    code = "OWNER_SECURITY_OPERATION_FAILED"
                raise IdentityError(code)
            if result.get("catalog_instance_id") != self.catalog_instance_id:
                raise IdentityError("OWNER_SECURITY_TARGET_MISMATCH")
            return result
        except IdentityError:
            raise
        except Exception:
            raise IdentityError("OWNER_SECURITY_WINDOW_CONNECTION_FAILED") from None
        finally:
            connection.close()


class WindowBridge:
    """The reused Tk form's small launcher/operation protocol over private RPC."""

    def __init__(self, client, request_id):
        self.client, self.request_id = client, str(request_id)
        self.operations = self
        self.operation_committed = False
        self.login_completed = False

    def _call(self, operation, **values):
        result = self.client.call(operation, self.request_id, **values)
        self.operation_committed = result["operation_committed"]
        self.login_completed = result["login_completed"]
        return result

    def inspect(self):
        result = self._call("native_inspect")
        record = SimpleNamespace(
            request_id=self.request_id,
            target_id=self.client.catalog_instance_id,
            request=NativeRequest.model_validate(result["request"]),
        )
        return record, result["facts"]

    def status(self):
        return self._call("status")

    def update(self, request_id, **values):
        if str(request_id) != self.request_id:
            raise IdentityError("OWNER_SECURITY_TARGET_MISMATCH")
        return self._call("native_update", **values)

    def execute(self, request, target_id, **secrets):
        if target_id != self.client.catalog_instance_id:
            raise IdentityError("OWNER_SECURITY_TARGET_MISMATCH")
        try:
            result = self._call("native_execute", **secrets)
        except IdentityError as exc:
            if exc.code == "OWNER_SECURITY_WINDOW_CONNECTION_FAILED":
                self.operation_committed = None
            else:
                self._call("native_inspect")
            raise
        recovery = result.get("recovery_code")
        return SecretStr(recovery) if recovery is not None else None

    def finish_recovery_display(self, request, target_id, *, new_password=None):
        if target_id != self.client.catalog_instance_id:
            raise IdentityError("OWNER_SECURITY_TARGET_MISMATCH")
        return self._call("native_finish", new_password=new_password)
