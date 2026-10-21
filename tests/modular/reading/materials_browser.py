"""Explicit synthetic browser functional check; never a performance measurement.

Run with the repository Python and an already built modular frontend. The Node
and Playwright paths are explicit, no existing service or real company is used.
"""

import argparse
import json
import subprocess
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from argon2 import PasswordHasher

from ai_accounting_modular.host.runtime.public import create_runtime
from ai_accounting_modular.storage.public import CompanySpec, OwnerProfile, create_catalog

from ..host.runtime.conftest import PASSWORD, business_request, native_login

ROOT = Path(__file__).resolve().parents[3]
HARNESS = ROOT / "frontend/tests/browser-review-materials.cjs"
TEXT = "<script>window.__reviewSourceExecuted = true</script>\n老板说明：按原文字查阅。"
FILE_NAME = "合成原始凭证_设备维修服务合同及付款说明_仅供下载不得内联执行.html"
FILE = b"<html><script>window.__reviewSourceExecuted = true</script></html>\n"


def source(index, text):
    return {
        "source_id": f"40000000-0000-4000-8000-{index:012d}",
        "kind": "owner_statement",
        "text": text,
        "sha256": sha256(text.encode()).hexdigest(),
        "archive_month": "2026-01",
    }


def fixtures(runtime, company, directory):
    kernel = runtime.kernel
    request = business_request(company)
    primary = request["mutations"][1]["fact"]
    original = directory / "company/originals" / FILE_NAME
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(FILE)
    with kernel.storage.read_unit(company) as read:
        file_source = kernel.records.prepare_source(
            {
                "source_id": "40000000-0000-4000-8000-000000000001",
                "kind": "file",
                "relative_path": "originals/" + FILE_NAME,
                "archive_month": "2026-01",
            },
            read,
        )
    sources = [file_source, source(2, TEXT)] + [
        source(index, f"第{index}份合成老板说明") for index in range(3, 26)
    ]
    request["sources"].extend(sources)
    primary["evidence_ids"] = [item["source_id"] for item in sources]
    account, statement = str(uuid4()), str(uuid4())
    request["objects"].append(
        {
            "object_id": account,
            "kind": "bank_account",
            "name": "合成资料银行",
            "expected_seq": 0,
            "data": {
                "bank_name": "合成银行",
                "account_number": "synthetic-source-account",
                "currency": "CNY",
                "owner": "company",
                "opened_period": "2026-01",
                "zero_at_opening": True,
            },
        }
    )
    request["mutations"].append(
        {
            "op": "create",
            "expected_seq": 0,
            "fact": {
                "business_id": statement,
                "kind": "money.bank_statement",
                "period": "2026-01",
                "evidence_ids": [item["source_id"] for item in sources],
                "data": {
                    "funds_account_id": account,
                    "opening_fen": "0",
                    "closing_fen": "2500",
                    "rows": [
                        {
                            "row_id": str(uuid4()),
                            "source": {
                                "source_id": item["source_id"],
                                "location": (
                                    f"合成银行原始对账单第{index + 1}行："
                                    "设备维修服务合同附件中的付款事项及老板补充确认资料位置"
                                ),
                            },
                            "actual_on": "2026-01-15",
                            "direction": "inflow",
                            "amount_fen": "100",
                        }
                        for index, item in enumerate(sources)
                    ],
                },
            },
        }
    )
    saved = kernel.application.accounting_apply(request)
    assert saved["completion"] == "complete", saved

    def submit(fact, *, replace=False, correction_period=None):
        value = {
            "contract": "ai-accounting-modular/1",
            "company_id": company,
            "command_id": str(uuid4()),
            "mutations": [
                {
                    "op": "replace" if replace else "create",
                    "expected_seq": 1 if replace else 0,
                    "fact": fact,
                }
            ],
        }
        if correction_period:
            value["correction_period"] = correction_period
        result = kernel.application.accounting_apply(value)
        assert result["completion"] == "complete", result
        return result["businesses"][0]["results"][0]

    other = deepcopy(primary)
    other["business_id"] = str(uuid4())
    other["evidence_ids"] = [sources[1]["source_id"]]
    frozen = submit(other)
    with kernel.storage.write_unit(company) as unit:
        check = kernel.ledger.check_period("2026-01", unit)
        assert check.ready
        kernel.ledger.close_period("2026-01", check.head, unit)
    other["data"]["amount_fen"] = "90000"
    other["evidence_ids"] = [sources[2]["source_id"]]
    submit(other, replace=True, correction_period="2026-02")
    opened = deepcopy(other)
    opened.update(business_id=str(uuid4()), period="2026-02")
    old_open = submit(opened)
    opened["data"]["amount_fen"] = "80000"
    submit(opened, replace=True)
    return {
        "company_id": company,
        "current_business_id": statement,
        "other_business_id": other["business_id"],
        "frozen": {
            "business_id": other["business_id"],
            "result_id": frozen["result_id"],
            "generation": frozen["generation"],
        },
        "old_open": {
            "business_id": opened["business_id"],
            "result_id": old_open["result_id"],
            "generation": old_open["generation"],
        },
        "source_ids": [item["source_id"] for item in sources],
        "statement": TEXT,
        "file_name": FILE_NAME,
        "file_sha256": sha256(FILE).hexdigest(),
        "file_size": len(FILE),
    }


def run(node, playwright_module, static_directory, output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    static_directory = Path(static_directory).resolve()
    assert (static_directory / "index.html").is_file(), "Build modular frontend first"
    with TemporaryDirectory(prefix="materials-browser-") as temporary:
        directory = Path(temporary)
        catalog = directory / "catalog.sqlite3"
        create_catalog(
            catalog, OwnerProfile(str(uuid4()), "合成审查负责人", PasswordHasher().hash(PASSWORD))
        )
        runtime = create_runtime(
            catalog, static_directory=static_directory, window_opener=lambda _: None
        )
        try:
            company = str(uuid4())
            runtime.kernel.storage.create_company(
                CompanySpec(
                    company,
                    str(uuid4()),
                    "913100000000000001",
                    "合成来源查阅公司",
                    "2026-01",
                    directory / "company",
                )
            )
            config = fixtures(runtime, company, directory)
            runtime.start()
            config["unauthenticated_ticket_url"] = runtime.browser_url()
            native_login(runtime)
            config.update(
                ticket_url=runtime.browser_url(),
                origin=f"http://127.0.0.1:{runtime.server.server_port}",
                playwright_module=str(Path(playwright_module).resolve()),
                output_directory=str(output),
            )
            completed = subprocess.run(
                [str(node), str(HARNESS)],
                input=json.dumps(config),
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=240,
                check=False,
            )
            try:
                result = json.loads(completed.stdout)
            except ValueError:
                raise AssertionError("Browser did not return its structured report") from None
            result["harness_sha256"] = sha256(HARNESS.read_bytes()).hexdigest()
            result["static_index_sha256"] = sha256(
                (static_directory / "index.html").read_bytes()
            ).hexdigest()
            (output / "result.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            assert completed.returncode == 0 and result["status"] == "passed", result
            return result
        finally:
            runtime.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", required=True)
    parser.add_argument("--playwright-module", required=True)
    parser.add_argument(
        "--static-directory", default=str(ROOT / "src/ai_accounting_modular/static/dashboard")
    )
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            run(args.node, args.playwright_module, args.static_directory, args.output),
            ensure_ascii=False,
            indent=2,
        )
    )
