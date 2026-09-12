"""Verify T6 against current source and the ordinary frontend/dist build.

Run with .tmp-kernel-venv/Scripts/python.exe. All service data and owners are
synthetic. The one-use ticket travels only through child stdin, never reports.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch


def load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def seed(app, t5):
    """Reuse T5's two companies/months, adding real paginated funds facts."""
    from ai_accounting.kernel.dashboard import Dashboard
    from ai_accounting.kernel.display import Display
    from ai_accounting.kernel.types import YearMonth

    companies = t5.seed(app)
    for ordinal, company in enumerate(companies):
        engine = app.engine(company["id"])
        proof = engine.register_evidence(
            b"T6 synthetic funds only",
            "text/plain",
            "T6合成资金确认资料",
            request_id="t6-funds-evidence",
        )["digest"]
        count = 102 if ordinal == 0 else 2
        facts = [
            {
                "kind": "funding",
                "subject_id": f"t6-funding-{index:03d}",
                "data": {
                    "period": "2026-09",
                    "actual_date": "2026-09-02",
                    "owner_id": "synthetic-owner",
                    "amount_fen": 10000 + index,
                    "funding_kind": "capital",
                    "bank_account_id": f"bank-{index:03d}",
                },
                "evidence": [proof],
                "expected_revision": 0,
            }
            for index in range(count)
        ]
        engine.save_facts(facts, request_id="t6-save-funding")
        subjects = [fact["subject_id"] for fact in facts]
        preview = engine.preview(subjects)
        engine.confirm(
            subjects,
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id="t6-confirm-funding",
        )
        bank_name = f"T6{'甲' if ordinal == 0 else '乙'}公司后页账户"
        Display(engine).save_display_profile(
            {
                "kind": "fund_account",
                "entity_id": f"bank-{count - 1:03d}",
                "display_name": bank_name,
                "source": "T6合成账户管理确认",
            },
            expected_revision=0,
            request_id="t6-display-bank",
        )
        # Long content is a real management profile, never a mocked response.
        asset_name = (
            company["asset_name"]
            + "（用于长期名称换行检查的合成管理资料，设备与配套设施均属于本项待启用资产）"
        )
        Display(engine).save_display_profile(
            {
                "kind": "asset",
                "entity_id": f"asset-{ordinal}",
                "display_name": asset_name,
                "source": "T6合成长名称管理确认",
            },
            expected_revision=1,
            request_id="t6-long-asset-name",
        )
        company.update(
            asset_name=asset_name,
            account_count=count,
            last_account_id=f"bank-{count - 1:03d}",
            last_account_name=bank_name,
        )
        dashboard = Dashboard(engine)
        company["voucher_count"] = dashboard.brief("2026-09")["data"]["voucher_count"]
        with engine.store.connection(read_only=True) as connection:
            target = connection.execute(
                "SELECT vv.id,v.number FROM voucher v "
                "JOIN voucher_current vc ON vc.voucher_id=v.id "
                "JOIN voucher_version vv ON vv.id=vc.version_id "
                "WHERE vv.period=? ORDER BY v.number DESC LIMIT 1",
                (YearMonth("2026-09").ordinal,),
            ).fetchone()
        company.update(target_number=target["number"], target_version_id=target["id"])
    return companies


def seed_report_company_default(app):
    """Three typed expenses expose a stale quarter overriding another company's default."""
    from ai_accounting.kernel.dashboard import Dashboard

    companies = []
    for ordinal, periods in enumerate((("2026-01",), ("2026-06", "2026-03"))):
        taxpayer_id = "91310000123456789" + ("A" if ordinal == 0 else "B")
        company = app.catalog.create_company(
            taxpayer_id,
            f"T6默认月份合成{'甲' if ordinal == 0 else '乙'}公司",
        )
        engine = app.engine(company["id"])
        evidence = engine.register_evidence(
            b"Synthetic report company default regression only",
            "text/plain",
            "T6合成报表月份确认资料",
            request_id="report-company-evidence",
        )["digest"]
        facts = [
            {
                "kind": "expense",
                "subject_id": f"report-expense-{period}",
                "data": {
                    "period": period,
                    "amount_fen": 100,
                    "counterparty_id": "synthetic-supplier",
                    "expense_class": "administration",
                    "creditor_kind": "supplier",
                },
                "evidence": [evidence],
                "expected_revision": 0,
            }
            for period in periods
        ]
        engine.save_facts(facts, request_id="report-company-facts")
        subjects = [fact["subject_id"] for fact in facts]
        preview = engine.preview(subjects)
        engine.confirm(
            subjects,
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id="report-company-confirm",
        )
        context = Dashboard(engine).context()
        assert [item["key"] for item in context["periods"]] == list(periods)
        assert context["default_period"] == periods[0]
        companies.append(
            {
                "id": company["id"],
                "name": company["name"],
                "taxpayer_id": taxpayer_id,
                "periods": list(periods),
                "default_period": context["default_period"],
                "default_quarter": context["default_quarter"],
            }
        )
    return companies


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--node", type=Path, required=True)
    parser.add_argument("--playwright-module", type=Path, required=True)
    parser.add_argument("--browser-channel", default="msedge")
    parser.add_argument(
        "--scenario",
        choices=("full", "navigation", "report-company-default"),
        default="full",
    )
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[1]
    assert Path(sys.prefix).resolve() == repository / ".tmp-kernel-venv", (
        "Use the repository virtual environment"
    )
    # Explicit source import also supports -I without inheriting installed code.
    sys.path.insert(0, str(repository / "src"))
    from ai_accounting.kernel import http
    from ai_accounting.kernel.daemon import ServiceClient

    assert Path(http.__file__).resolve().is_relative_to(repository / "src")
    root, output = args.root.resolve(), args.output.resolve()
    assert not root.exists() and not output.exists(), "Use fresh synthetic root/output paths"
    assert root != output and not root.is_relative_to(output) and not output.is_relative_to(root)
    static = repository / "frontend/dist"
    assert (static / "index.html").is_file() and (static / "local.html").is_file(), (
        "Run the ordinary frontend build first"
    )
    assets = {
        path.relative_to(static).as_posix(): sha256(path)
        for path in static.rglob("*")
        if path.is_file()
    }
    source_paths = sorted((repository / "frontend/src").rglob("*"))
    source_paths += [
        Path(__file__).resolve(),
        repository / "frontend/tests/browser-t6-interactions.cjs",
    ]
    source_hashes = {
        path.relative_to(repository).as_posix(): sha256(path)
        for path in source_paths
        if path.is_file()
    }
    output.mkdir(parents=True)
    verifier = load_script("t6_resident", repository / "scripts/verify_local_package.py")
    t5 = load_script("t6_seed", repository / "scripts/verify_t5_browser.py")
    create_server = http.create_server
    evidence = {
        "static_directory": str(static),
        "synthetic_root": str(root),
        "source_sha256": source_hashes,
        "build_assets_sha256": assets,
    }
    report = {"status": "failed", **evidence}
    try:
        with patch.object(
            http,
            "create_server",
            lambda service, **options: create_server(service, static_directory=static, **options),
        ):
            app, server, metadata = verifier.start_resident(root)
        companies = (
            seed_report_company_default(app)
            if args.scenario == "report-company-default"
            else seed(app, t5)
        )
        client = ServiceClient(root, metadata=metadata)
        configuration = {
            "origin": f"http://127.0.0.1:{server.server_port}",
            "ticket_url": client.browser_url()["url"],
            "companies": companies,
            "output": str(output),
            "assets": assets,
            "channel": args.browser_channel,
            "playwright_module": str(args.playwright_module.resolve()),
            "scenario": args.scenario,
        }
        result = subprocess.run(
            [
                str(args.node.resolve()),
                str(repository / "frontend/tests/browser-t6-interactions.cjs"),
            ],
            input=json.dumps(configuration, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=600,
        )
        # stderr can contain URLs; only the runner's sanitized JSON is reported.
        report = (
            json.loads(result.stdout)
            if result.stdout
            else {"status": "failed", "message": "Browser runner returned no sanitized report"}
        )
        report.update(evidence)
        if result.returncode:
            (output / "failure.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            raise RuntimeError("T6 browser runner failed; see sanitized failure.json")
        changed = [
            name for name, digest in source_hashes.items() if sha256(repository / name) != digest
        ]
        assert not changed, f"Source changed during verification: {changed}"
        assert assets == {
            path.relative_to(static).as_posix(): sha256(path)
            for path in static.rglob("*")
            if path.is_file()
        }, "Build changed during verification"
        report.update(
            static_directory=str(static),
            synthetic_root=str(root),
            source_sha256=source_hashes,
            build_assets_sha256=assets,
            changed_during_run=changed,
        )
    finally:
        verifier.stop_residents()
        (root / ".service.json").unlink(missing_ok=True)
        report["synthetic_credentials_revoked_and_removed"] = True
        report["server_stderr_policy"] = (
            "Preserve raw server stderr separately; accepted P3 response-disconnect logging "
            "is not suppressed or claimed fixed."
        )
        (output / "result.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
