"""Test-only empty draft restore target; AI restores through packaged stdio MCP."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import sys
import threading
from contextlib import ExitStack
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
LOGIN = "synthetic-stage9-restore-owner"
RELAY_SHA = "b6ba71077eeed6a1768c3b5466bf46f6000bbd4651d05927f9ad7124b8813e85"


def relay_module():
    path = Path(__file__).with_name("stage9_package_agent_harness.py")
    if hashlib.sha256(path.read_bytes()).hexdigest() != RELAY_SHA:
        raise ValueError("Use the explicitly reviewed MCP error/resume relay helper")
    spec = importlib.util.spec_from_file_location("stage9_restore_relay", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_target(directory, *, resume):
    directory = directory.resolve()
    if (directory.parent != (REPOSITORY / ".tmp").resolve()
            or not directory.name.startswith("stage9-final-draft-agent-restore-")):
        raise ValueError("Use an isolated Stage 9 restore target under workspace .tmp")
    if not resume and directory.exists():
        raise ValueError("Restore target must be absent; preserve existing data")
    if resume and not (directory / "prepared.json").is_file():
        raise ValueError("Resume requires the original prepared target")
    return directory


def assert_restored_identities(allowed, companies):
    """New directory security never changes the source business identities."""
    identities = {row["id"]: (row["taxpayer_id"], row["database_id"]) for row in allowed}
    if len({row["id"] for row in companies}) != len(companies) or any(
        identities.get(row["id"]) != (row["taxpayer_id"], row["database_id"])
        for row in companies
    ):
        raise ValueError("Restored company differs from the declared source identities")


def check_resume_record(record, *, directory, package, manifest_sha, build_id, formats):
    if (record["format"] != "stage9-isolated-restore-target/1"
            or record["root"] != str(directory / "data")
            or record["package"] != str(package)
            or record["manifest_sha256"] != manifest_sha or record["build_id"] != build_id
            or record["database_formats"] != formats or record["owner_login"] != LOGIN):
        raise ValueError("Restore resume package/root/security binding differs")


async def host(directory, package, *, origin=None, resume=False):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from pydantic import SecretStr

    from ai_accounting.kernel import service as service_module
    from ai_accounting.kernel.build import calculator_build_id
    from ai_accounting.kernel.daemon import ServiceClient, build_native_security_controller
    from ai_accounting.kernel.http import create_server
    from ai_accounting.kernel.permissions import ensure_private_directory, reject_reparse_path
    from ai_accounting.kernel.runtime import private_file_lock
    from ai_accounting.kernel.schema_bundle import production_bundle
    from ai_accounting.kernel.security.credentials import WindowsCredentialStore
    from ai_accounting.kernel.security.service import credential_target
    from ai_accounting.kernel.security.windows import write_protected_json
    from ai_accounting.kernel.service import LocalService

    relay = relay_module()
    directory = check_target(directory, resume=resume)
    if (package.parent != (REPOSITORY / ".tmp").resolve()
            or not package.name.startswith("stage9-development-delivery-package-")
            or Path(sys.executable).resolve() != package / "runtime/python.exe"
            or not Path(service_module.__file__).resolve().is_relative_to(package / "app")):
        raise ValueError("Use the selected independent package and its isolated interpreter")
    reject_reparse_path(package)
    reject_reparse_path(directory)
    manifest_path = package / "manifest.json"
    manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    manifest = json.loads(manifest_path.read_text("utf-8"))
    bundle = production_bundle()
    formats = {kind: bundle.database_format(kind) for kind in ("company", "catalog")}
    build_id = calculator_build_id()
    if (bundle.status != "draft" or dict(bundle.current_versions) != {"catalog": 0, "company": 0}
            or manifest["runtime"]["database_formats"] != formats
            or manifest["source_build_id"] != build_id
            or manifest["runtime"]["build_id"] != build_id):
        raise ValueError("The actual package must be the declared draft/0 build")
    if resume:
        record = json.loads((directory / "prepared.json").read_text("utf-8"))
        check_resume_record(record, directory=directory, package=package,
                            manifest_sha=manifest_sha, build_id=build_id, formats=formats)
        previous = json.loads((directory / "data/.service.json").read_text("utf-8"))
        if relay.process_alive(previous["pid"]) or (directory / "stop").exists():
            raise ValueError("Previous host is active or target was explicitly stopped")
        if not (directory / "data/catalog.sqlite").is_file():
            raise ValueError("Resume must retain the original catalog")
    else:
        if origin is None or origin.parent != (REPOSITORY / ".tmp").resolve():
            raise ValueError("Declare the existing isolated source host directory")
        source_ready = json.loads((origin / "ready.json").read_text("utf-8"))
        if source_ready["package"] != str(package) or (
            source_ready["package_database_formats"] != formats
        ):
            raise ValueError("Origin uses another package or database format")
        record = {"format": "stage9-isolated-restore-target/1", "package": str(package),
                  "root": str(directory / "data"), "origin": str(origin),
                  "manifest_sha256": manifest_sha, "build_id": build_id,
                  "database_formats": formats, "owner_login": LOGIN,
                  "allowed_source_companies": [row["company"] for row in source_ready["companies"]]}
        directory.mkdir()
    root = directory / "data"
    if not resume:
        ensure_private_directory(root)
    with ExitStack() as cleanup:
        locked = cleanup.enter_context(private_file_lock(root / ".agent-relay.lock"))
        if not locked:
            raise ValueError("The restore target already has an active relay")
        for channel in ("requests", "responses", "native_requests", "native_responses"):
            if resume:
                if not (directory / channel).is_dir():
                    raise ValueError("Resume must retain all original request/receipt channels")
            else:
                (directory / channel).mkdir()
        app = LocalService(root, enable_read_pool=True)
        cleanup.callback(app.close)
        password = SecretStr("Synthetic-stage9-restore-target-only-2026")
        if resume:
            status = app.security.status()
            if (status["catalog_instance_id"] != record["catalog_id"]
                    or status["login_name"] != LOGIN or not status["active"]):
                raise ValueError("Retain the original restore-target security identity")
        else:
            app.security.provision(LOGIN, password)
        login = app.security.login(LOGIN, password)
        if resume and login.authority.owner_id != record["owner_id"]:
            raise ValueError("Restore target owner identity changed")
        token = login.session_token
        cleanup.callback(app.security.logout, token)
        if not resume:
            record.update(catalog_id=app.security.catalog_instance_id,
                          owner_id=login.authority.owner_id)
            relay.write_json(directory / "prepared.json", record)
        credentials = WindowsCredentialStore(target_name=credential_target(
            root / "catalog.sqlite", app.security.catalog_instance_id,
        ))
        credentials.save_session_token(token)
        cleanup.callback(credentials.delete_session_token)
        server, capability = create_server(app, port=0)
        cleanup.callback(server.server_close)
        app.security_controller = build_native_security_controller(
            app, server, capability, credential_store=credentials,
            window_opener=lambda _request: None,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        cleanup.callback(thread.join, 5)
        cleanup.callback(server.shutdown)
        write_protected_json(root / ".service.json", {
            "protocol": 2, "database_format": app.catalog.database_format(),
            "pid": os.getpid(), "port": server.server_port, "capability": capability,
            "catalog_id": app.security.catalog_instance_id, "build_id": server.build_id,
        })
        attached = ServiceClient(root)
        companies = attached.dispatch("companies", {})
        if not resume and companies:
            raise ValueError("A new restore target must have zero business companies")
        assert_restored_identities(record["allowed_source_companies"], companies)
        parameters = StdioServerParameters(command=str(package / "runtime/python.exe"), args=[
            "-I", "-X", "utf8", "-m", "ai_accounting.kernel.cli", "--root", str(root), "mcp",
        ])
        async with stdio_client(parameters) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                tools = await session.list_tools()
                schema = await relay.mcp_value(session, "finance_local_schema", {})
                observed = await relay.mcp_value(session, "finance_local_command", {
                    "command": "companies", "payload": {},
                })
                if schema["database_formats"] != formats or observed["items"] != companies:
                    raise ValueError("Restore-target MCP preflight differs from the resident")
                ready = {"scope": "isolated_restore_target_preflight_not_restore_acceptance",
                         "host_pid": os.getpid(), "resume": resume, "root": str(root),
                         "package": str(package), "package_database_formats": formats,
                         "catalog_id": record["catalog_id"], "companies": companies,
                         "tools": [tool.name for tool in tools.tools],
                         "native_transport_scope": "not_enabled_for_this_restore_channel"}
                if resume:
                    previous_ready = json.loads((directory / "ready.json").read_text("utf-8"))
                    relay.write_json(
                        directory / f"previous-ready-{os.getpid()}.json", previous_ready,
                    )
                relay.write_json(directory / "ready.json", ready)
                while not (directory / "stop").exists():
                    await relay.process_mcp_requests(directory, session)
                    await asyncio.sleep(0.05)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("host", "resume", "call", "schema"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("argument", type=Path)
    parser.add_argument("--origin", type=Path)
    args = parser.parse_args()
    if args.mode in {"host", "resume"}:
        asyncio.run(host(args.directory.resolve(), args.argument.resolve(),
                         origin=args.origin.resolve() if args.origin else None,
                         resume=args.mode == "resume"))
    else:
        relay_module().submit(args.directory.resolve(), args.mode, args.argument)
