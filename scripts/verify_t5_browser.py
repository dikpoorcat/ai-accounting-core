"""Run the T5 browser seam against one existing candidate and a fresh synthetic root.

Use the candidate's isolated Python. The one-use browser ticket is passed only
through the child process input; output and screenshots contain no credentials.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path


def seed(app):
    from ai_accounting.kernel.dashboard import Dashboard
    from ai_accounting.kernel.display import Display
    from ai_accounting.kernel.types import YearMonth

    companies = []
    for ordinal, suffix in enumerate(("甲", "乙")):
        company = app.catalog.create_company(
            "91310000123456789" + ("A" if ordinal == 0 else "B"),
            f"T5合成{suffix}公司",
        )
        engine = app.engine(company["id"])
        proof = engine.register_evidence(
            b"T5 synthetic browser facts only",
            "text/plain",
            "T5合成确认资料",
            request_id="browser-evidence",
        )["digest"]
        subjects = []
        for month, count in (("2026-09", 102 if ordinal == 0 else 2), ("2026-10", 1)):
            for index in range(count):
                subject = f"expense-{month}-{index:03d}"
                engine.save_fact(
                    "expense",
                    subject,
                    {
                        "period": month,
                        "amount_fen": 100 + index,
                        "counterparty_id": f"supplier-{ordinal}",
                        "expense_class": "administration",
                        "creditor_kind": "supplier",
                    },
                    evidence=(proof,),
                    expected_revision=0,
                    request_id="save-" + subject,
                )
                subjects.append(subject)
        employee = f"employee-{ordinal}"
        asset = f"asset-{ordinal}"
        engine.save_fact(
            "reimbursed_asset",
            asset,
            {
                "period": "2026-09",
                "asset_type": "fixed",
                "cost_fen": 120000,
                "company_acceptance_confirmed": True,
                "creditors": [{"employee_id": employee, "amount_fen": 120000}],
            },
            evidence=(proof,),
            expected_revision=0,
            request_id="save-asset",
        )
        subjects.append(asset)
        preview = engine.preview(subjects)
        engine.confirm(
            subjects,
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id="publish-browser-facts",
        )
        for kind, entity, name in (
            ("employee", employee, f"T5{suffix}员工"),
            ("asset", asset, f"T5{suffix}待启用设备"),
        ):
            Display(engine).save_display_profile(
                {
                    "kind": kind,
                    "entity_id": entity,
                    "display_name": name,
                    "source": "T5合成管理确认",
                    **(
                        {"employment_start": "2026-09", "employment_status": "active"}
                        if kind == "employee"
                        else {}
                    ),
                },
                expected_revision=0,
                request_id="display-" + kind,
            )
        dashboard = Dashboard(engine)
        assert dashboard.employees("2026-09")["data"]["employees"]["items"]
        assert dashboard.assets("2026-09")["data"]["collections"]["assets"]["items"]
        brief = dashboard.brief("2026-09", limit=100)
        with engine.store.connection(read_only=True) as connection:
            target = dict(
                connection.execute(
                    "SELECT vv.id,v.number FROM voucher v "
                    "JOIN voucher_current vc ON vc.voucher_id=v.id "
                    "JOIN voucher_version vv ON vv.id=vc.version_id "
                    "WHERE vv.period=? ORDER BY v.number DESC LIMIT 1",
                    (YearMonth("2026-09").ordinal,),
                ).fetchone()
            )
        companies.append(
            {
                "id": company["id"],
                "name": company["name"],
                "employee_name": f"T5{suffix}员工",
                "asset_name": f"T5{suffix}待启用设备",
                "voucher_count": brief["data"]["voucher_count"],
                "target_number": target["number"],
                "target_version_id": target["id"],
            }
        )
    assert companies[0]["voucher_count"] > 100
    return companies


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--node", type=Path, required=True)
    parser.add_argument("--playwright-module", type=Path, required=True)
    parser.add_argument("--browser-channel", default="msedge")
    args = parser.parse_args()
    package, root, output = (value.resolve() for value in (args.package, args.root, args.output))
    assert sys.flags.isolated == 1
    assert Path(sys.base_prefix).resolve() == package / "runtime"
    assert not root.exists() and not output.exists(), "Use fresh synthetic root/output paths"
    assert not root.is_relative_to(package) and not output.is_relative_to(package)
    output.mkdir(parents=True)
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    spec = importlib.util.spec_from_file_location(
        "t5_candidate_verifier", package / "tools/verify_local_package.py"
    )
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    from ai_accounting.kernel.build import calculator_build_id
    from ai_accounting.kernel.daemon import ServiceClient
    from ai_accounting.kernel.http import dashboard_directory

    assert calculator_build_id() == manifest["runtime"]["build_id"]
    assert dashboard_directory() == package / "frontend/dist"
    assets = {}
    for relative, expected in manifest["files"].items():
        if relative.startswith("frontend/dist/"):
            path = package / relative
            with path.open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            assert actual == expected["sha256"], relative
            assets[relative.removeprefix("frontend/dist/")] = actual
    try:
        app, server, metadata = verifier.start_resident(root)
        companies = seed(app)
        for company in companies:
            verifier.assert_current_contracts(app, company["id"])
        client = ServiceClient(root, metadata=metadata)
        ticket_url = client.browser_url()["url"]
        runner = Path(__file__).resolve().parents[1] / "frontend/tests/browser-t5-integration.cjs"
        configuration = {
            "origin": f"http://127.0.0.1:{server.server_port}",
            "ticket_url": ticket_url,
            "companies": companies,
            "output": str(output),
            "assets": assets,
            "channel": args.browser_channel,
            "playwright_module": str(args.playwright_module.resolve()),
        }
        result = subprocess.run(
            [str(args.node.resolve()), str(runner)],
            input=json.dumps(configuration, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=180,
        )
        # Do not echo browser errors containing navigation URLs or a ticket.
        if result.returncode:
            (output / "failure.json").write_text(result.stdout, encoding="utf-8")
            raise RuntimeError("T5 browser runner failed; see sanitized failure.json")
        report = json.loads(result.stdout)
        report.update(
            {
                "package": str(package),
                "synthetic_root": str(root),
                "runtime": manifest["runtime"],
                "browser_assets_sha256": assets,
                "business_schema_version": 11,
                "catalog_schema_version": 3,
            }
        )
    finally:
        verifier.stop_residents()
    report["synthetic_credentials_revoked_and_removed"] = True
    (output / "result.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
