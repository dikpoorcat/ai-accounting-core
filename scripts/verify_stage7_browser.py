"""Run the actual five-page build against synthetic Stage 7 review states.

Use the repository virtual environment. Browser tickets go through child stdin,
never command arguments or the saved report. All credentials stay in memory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--node", type=Path, required=True)
    parser.add_argument("--playwright-module", type=Path, required=True)
    parser.add_argument("--browser-channel", default="msedge")
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[1]
    assert Path(sys.prefix).resolve() == repository / ".tmp-kernel-venv"
    sys.path[:0] = [str(repository / "src"), str(repository / "tests/kernel")]

    from material_fixture import supporting_text
    from monthly_close_fixture import ready
    from pydantic import SecretStr
    from test_dashboard_empty_replay import company_call, finish_payment, replay_sources

    from ai_accounting.kernel.daemon import ServiceClient, build_native_security_controller
    from ai_accounting.kernel.http import create_server
    from ai_accounting.kernel.security.credentials import InMemoryCredentialStore
    from ai_accounting.kernel.security.transport import NativeHttpClient
    from ai_accounting.kernel.service import LocalService

    root, output = args.root.resolve(), args.output.resolve()
    assert not root.exists() and not output.exists(), "Use fresh synthetic paths"
    assert root != output and not root.is_relative_to(output) and not output.is_relative_to(root)
    output.mkdir(parents=True)
    static = repository / "frontend/dist"
    assert (static / "index.html").is_file(), "Build the frontend first"

    def build_hashes():
        return {
            str(path.relative_to(static)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(static.rglob("*"))
            if path.is_file()
        }

    hashes = build_hashes()
    app = LocalService(root)
    password = SecretStr("Synthetic-browser-stage7-only-2026")
    app.security.provision("synthetic-owner", password)
    store = InMemoryCredentialStore()
    server, capability = create_server(app, port=0, static_directory=static)
    app.security_controller = build_native_security_controller(
        app,
        server,
        capability,
        credential_store=store,
        window_opener=lambda _request_id: None,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    native = NativeHttpClient(
        port=server.server_port,
        capability=capability,
        catalog_instance_id=app.security.catalog_instance_id,
    )
    result = {"status": "failed"}
    try:
        request = app.security_controller.request(kind="login")
        native.call("native_execute", request["request_id"], password=password)
        native.call("native_update", request["request_id"], status="succeeded")
        token = store.load_session_token()

        def dispatch(command, payload):
            return app.dispatch(command, payload, session_token=token)

        companies = []
        for suffix, name, state in (
            ("A", "阶段七合成已冻结企业", "closed"),
            ("B", "阶段七合成待核对企业", "prepared"),
            ("C", "阶段七合成现金业务企业", "unprepared"),
            ("D", "阶段七合成无月份企业", "empty"),
        ):
            company = app.catalog.create_company("91310000123456789" + suffix, name)
            engine = app.engine(company["id"])
            call = company_call(dispatch, company["id"])
            entry = {**company, "period": "2026-01" if state != "empty" else None, "state": state}
            if state in {"closed", "prepared"}:
                proof = engine.register_evidence(
                    (
                        "合成负责人确认管理费用1000分及本月完整资料"
                        if state == "prepared"
                        else "合成负责人无业务确认"
                    ).encode(),
                    "text/plain",
                    "合成月度确认.txt",
                    request_id="proof",
                )["digest"]
                if state == "prepared":
                    supplier = call(
                        "register_entity",
                        kind="organization",
                        data={"display_name": "合成核对供应商"},
                        source="合成费用负责人确认",
                        request_id="supplier",
                    )["entity_id"]
                    supporting_text(engine, proof)
                    call(
                        "save_fact",
                        kind="expense",
                        subject_id="review-expense",
                        data={
                            "period": "2026-01",
                            "amount_fen": 1000,
                            "counterparty_id": supplier,
                            "expense_class": "administration",
                            "creditor_kind": "supplier",
                        },
                        evidence=[proof],
                        expected_revision=0,
                        request_id="review-expense",
                    )
                    publication = call("preview", subjects=["review-expense"])
                    call(
                        "confirm",
                        subjects=["review-expense"],
                        preview_digest=publication["digest"],
                        epochs=publication["epochs"],
                        request_id="review-publish",
                    )
                    ready(
                        engine,
                        proof,
                        first="2026-01",
                        last="2026-01",
                        omit={("2026-01", "transactions")},
                    )
                    call(
                        "inventory",
                        period="2026-01",
                        category="transactions",
                        evidence=[proof],
                        expected=1,
                        no_business=False,
                        confirmation_evidence=proof,
                        request_id="inventory-transactions",
                    )
                    entry["expected_review_expense_fen"] = "1000"
                else:
                    ready(engine, proof, first="2026-01", last="2026-01")
                preview = call("preview_close", period="2026-01", owner_confirmation=proof)
                entry["preview_digest"] = preview["digest"]
                if state == "closed":
                    request = app.security_controller.request(
                        kind="approve_period_close",
                        company_id=company["id"],
                        database_id=company["database_id"],
                        period="2026-01",
                        preview_digest=preview["digest"],
                        epochs=preview["epochs"],
                    )
                    approval = native.call(
                        "native_execute", request["request_id"], password=password
                    )
                    native.call("native_update", request["request_id"], status="succeeded")
                    call(
                        "close",
                        period="2026-01",
                        owner_confirmation=proof,
                        preview_digest=preview["digest"],
                        epochs=preview["epochs"],
                        approval_id=approval["approval_id"],
                        request_id="close",
                        backup_directory=str(root / "synthetic-backups"),
                    )
            elif state == "unprepared":
                refs, proof, _ = replay_sources(call, "browser-stage7")
                preview = call("preview", subjects=[refs["funding"], refs["expense"]])
                call(
                    "confirm",
                    subjects=[refs["funding"], refs["expense"]],
                    preview_digest=preview["digest"],
                    epochs=preview["epochs"],
                    request_id="publish",
                )
                finish_payment(call, refs, proof)
                entry["expense_subject"] = refs["expense"]
            companies.append(entry)

        metadata = {
            "protocol": 2,
            "database_format": app.catalog.database_format(),
            "pid": os.getpid(),
            "port": server.server_port,
            "capability": capability,
            "catalog_id": app.security.catalog_instance_id,
            "build_id": server.build_id,
        }
        client = ServiceClient(root, metadata=metadata, credential_store=store)
        config = {
            "origin": f"http://127.0.0.1:{server.server_port}",
            "ticket_url": client.browser_url()["url"],
            "companies": companies,
            "output": str(output),
            "channel": args.browser_channel,
            "playwright_module": str(args.playwright_module.resolve()),
        }
        process = subprocess.run(
            [
                str(args.node.resolve()),
                str(repository / "frontend/tests/browser-stage7-interactions.cjs"),
            ],
            input=json.dumps(config, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=600,
        )
        result = (
            json.loads(process.stdout)
            if process.stdout
            else {
                "status": "failed",
                "message": "Browser runner returned no sanitized report",
                "returncode": process.returncode,
                "diagnostic": re.sub(r"https?://[^\s\"']+", "[browser URL]", process.stderr),
            }
        )
        assert process.returncode == 0, "Browser verification failed; see result.json"
        assert hashes == build_hashes(), "Build changed during verification"
    finally:
        token = store.load_session_token()
        if token is not None:
            app.security.logout(token)
        store.delete_session_token()
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        result.update(
            synthetic_root=str(root),
            build_assets_sha256=hashes,
            synthetic_credentials_revoked=True,
        )
        (output / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
