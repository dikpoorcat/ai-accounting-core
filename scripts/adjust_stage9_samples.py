"""Adjust only the three named synthetic Stage 9 roots, serially and explicitly."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import tempfile
from contextlib import closing
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1]
ROOTS = {
    key: WORKSPACE / ".tmp" / ("stage9-preparation-20261008-ready-" + key)
    for key in ("main12", "main48", "main120")
}
COMPANY_RELATIVE = Path("91310000123456789S/company.sqlite")


def _runtime():
    sys.path.insert(0, str(WORKSPACE / "src"))
    if __package__:
        from . import stage9_history_adjustment as adjustment
    else:
        import stage9_history_adjustment as adjustment
    from ai_accounting.kernel.backup import create_portable, verify_file
    from ai_accounting.kernel.permissions import (
        create_private_file,
        ensure_private_directory,
        reject_reparse_path,
    )
    from ai_accounting.kernel.runtime import connect, private_file_lock

    return (
        adjustment,
        create_portable,
        verify_file,
        create_private_file,
        ensure_private_directory,
        reject_reparse_path,
        connect,
        private_file_lock,
    )


def _write(path, value, create_private_file):
    create_private_file(path)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)


def update_construction_checkpoint(root, company, result):
    """Bind explicit format changes to fresh state without renewing old proof."""
    from ai_accounting.kernel.permissions import create_private_file
    from ai_accounting.kernel.runtime import connect

    checkpoint_path = root / "stage9-builder.json"
    raw = checkpoint_path.read_bytes()
    checkpoint = json.loads(raw)
    with closing(connect(company, read_only=True)) as connection:
        state = list(connection.execute("SELECT * FROM state WHERE id=1").fetchone())
        identity = dict(connection.execute(
            "SELECT company_id,taxpayer_id,database_id FROM identity WHERE id=1"
        ).fetchone())
        receipts = [
            {
                key: value.hex() if isinstance(value, bytes) else value
                for key, value in dict(row).items()
            }
            for row in connection.execute("SELECT * FROM schema_draft_history ORDER BY sequence")
        ]
    previous = checkpoint.get("synthetic_format_adjustment")
    if previous is not None:
        if Path(previous["receipt_file"]).name != previous["receipt_file"]:
            raise ValueError("Adjustment receipt must stay inside its synthetic root")
        receipt_path = root / previous["receipt_file"]
        receipt_raw = receipt_path.read_bytes()
        receipt = json.loads(receipt_raw)
        original_name = receipt["original_checkpoint_file"]
        if Path(original_name).name != original_name:
            raise ValueError("Original checkpoint must stay inside its synthetic root")
        if (
            hashlib.sha256(receipt_raw).hexdigest() != previous["receipt_sha256"]
            or receipt["current_state"] != state
            or checkpoint["epochs"] != state
            or receipt["identity"] != identity
            or hashlib.sha256((root / original_name).read_bytes()).hexdigest()
            != receipt["original_checkpoint_sha256"]
        ):
            raise ValueError("Adjusted construction receipt no longer binds current state")
        return previous
    before = checkpoint.get("epochs")
    employees = result["workforce_format"]["regular_employees"]
    if (
        not isinstance(before, list) or len(before) != 6
        or any(type(value) is not int for value in before)
        or [value - old for value, old in zip(state, before, strict=True)]
        != [0, 0, 1, employees, 0, 0]
        or checkpoint.get("company", {}).get("id") != identity["company_id"]
        or checkpoint.get("company", {}).get("database_id") != identity["database_id"]
    ):
        raise ValueError("Construction state differs from the exact authorized format changes")
    original_sha = hashlib.sha256(raw).hexdigest()
    original_path = root / ("stage9-builder-original-" + original_sha + ".json")
    if original_path.exists():
        if original_path.read_bytes() != raw:
            raise ValueError("Preserved construction checkpoint changed")
    else:
        create_private_file(original_path)
        original_path.write_bytes(raw)
    receipt = {
        "format": "stage9-synthetic-format-adjustment/1",
        "identity": identity,
        "original_checkpoint_sha256": original_sha,
        "original_checkpoint_file": original_path.name,
        "original_state": before,
        "current_state": state,
        "schema_adjustment_receipts": receipts,
        "company_adjustment": result.get("company_adjustment"),
        "workforce_format": result["workforce_format"],
        "target_verification": result["target_verification"],
        "storage_budget": result.get("storage_budget"),
        "qualification_required": True,
    }
    receipt_raw = json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    receipt_sha = hashlib.sha256(receipt_raw).hexdigest()
    receipt_path = root / ("stage9-format-adjustment-" + receipt_sha + ".json")
    if receipt_path.exists():
        if receipt_path.read_bytes() != receipt_raw:
            raise ValueError("Preserved adjustment receipt changed")
    else:
        create_private_file(receipt_path)
        receipt_path.write_bytes(receipt_raw)
    binding = {"receipt_file": receipt_path.name, "receipt_sha256": receipt_sha}
    checkpoint["epochs"] = state
    checkpoint["synthetic_format_adjustment"] = binding
    temporary = root / ("stage9-builder-adjusted-" + receipt_sha + ".json")
    _write(temporary, checkpoint, create_private_file)
    temporary.replace(checkpoint_path)
    return binding


def synthetic_fund_settings(connection, *, company_name):
    """Explicit current MixedBook identities, never inferred from account balances."""
    from ai_accounting.kernel.entities import _profile_record
    from ai_accounting.kernel.types import YearMonth

    accounts = []
    for entity in connection.execute(
        "SELECT id,account_type FROM entity WHERE kind='fund_account' ORDER BY id"
    ):
        row = connection.execute(
            "SELECT * FROM entity_profile_revision WHERE entity_id=? "
            "ORDER BY revision DESC LIMIT 1",
            (entity["id"],),
        ).fetchone()
        if row is None:
            raise ValueError("Synthetic fund account has no registered source profile")
        profile = _profile_record(row)
        name = profile.get("display_name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Synthetic fund account requires its explicit registered name")
        kind = entity["account_type"]
        if kind == "bank":
            data = {
                "bank_name": "合成规模测试银行",
                "bank_account_number": "1234567890123456",
                "account_holder_name": company_name,
            }
        elif kind == "platform":
            data = {
                "provider_name": "合成规模测试平台",
                "account_identifier": name,
                "account_holder_name": company_name,
            }
        elif kind == "cash":
            data = {"display_name": name}
        else:
            raise ValueError("Unknown synthetic fund account type")
        accounts.append(
            {
                "entity_id": entity["id"],
                "data": data,
                "source": "合成样本格式升级设置：当前MixedBook资金账户定义；"
                "保留原注册账户身份与业务，不冒充旧原件负责人确认",
                "evidence_digest": profile["evidence_digest"],
            }
        )
    previous = connection.execute("SELECT max(period) FROM period_close").fetchone()[0]
    first = (
        previous + 1
        if previous is not None
        else connection.execute("SELECT min(period) FROM fact_revision").fetchone()[0]
    )
    return {
        "accounts": accounts,
        "first_period": str(YearMonth.from_ordinal(first)) if first is not None else None,
        "period_basis": "explicit earliest open month after the retained last close",
    }


def append_workforce_format(company, *, expected_employees):
    """Convert only explicit retained synthetic hire statements through typed writes."""
    from ai_accounting.kernel.engine import Engine
    from ai_accounting.kernel.entities import Entities, WorkerRegistration, _profile_record
    from ai_accounting.kernel.entity_profile_history import retained_profile_ids
    from ai_accounting.kernel.storage import Store

    adjustment, _, _, _, _, _, connect, _ = _runtime()
    bundle = adjustment.target_bundle()
    with closing(connect(company, read_only=True)) as connection:
        identity = connection.execute("SELECT * FROM identity WHERE id=1").fetchone()
        state_before = dict(connection.execute("SELECT * FROM state WHERE id=1").fetchone())
        employees = [
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT employee_id FROM fact_payroll_pre_revision ORDER BY employee_id"
            )
        ]
        if len(employees) != expected_employees:
            raise ValueError(
                "Retained synthetic wage employee count differs from the declared sample"
            )
        histories = [
            [
                _profile_record(row)
                for row in connection.execute(
                    "SELECT * FROM entity_profile_revision WHERE entity_id=? ORDER BY revision",
                    (employee,),
                )
            ]
            for employee in employees
        ]
        retained = retained_profile_ids(connection, histories)
        candidates = []
        for employee, history in zip(employees, histories, strict=True):
            original = [row for row in history if row["id"] in retained]
            if not original:
                raise ValueError("Synthetic wage employee has no authenticated retained profile")
            profile = original[-1]
            if (
                profile.get("active") is not True
                or profile.get("employment_status") != "active"
                or profile.get("employment_state") != "regular"
                or profile.get("employment_end") is not None
                or not isinstance(profile.get("employment_start"), str)
            ):
                raise ValueError(
                    "Only explicit regular active employment with a source date can convert"
                )
            current_events = [row for row in history if row.get("workforce_event") is not None]
            source = (
                "合成样本格式转换：保留档案 "
                + profile["id"]
                + " 的明确任职资料；原来源："
                + profile["source"]
            )
            if current_events:
                if (
                    len(current_events) != 1
                    or current_events[0]["source"] != source
                    or (
                        current_events[0]["workforce_event"]["action"] != "hire"
                        or current_events[0]["workforce_event"]["effective_date"]
                        != profile["employment_start"]
                    )
                ):
                    raise ValueError(
                        "An existing workforce event does not match this exact conversion"
                    )
                continue
            if history[-1]["id"] != profile["id"]:
                raise ValueError("A newer profile must not be overwritten by a source conversion")
            candidates.append((employee, profile, source))
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        protected = {
            "fact_revision",
            "calculation",
            "voucher",
            "voucher_version",
            "voucher_line",
            *adjustment.PRESERVED_BODIES.values(),
            *(table for table in tables if table.startswith("close_") or table == "period_close"),
        } & tables
        before = {
            table: adjustment._row_digest(
                connection,
                table,
                adjustment._columns(connection, table),
            )
            for table in protected
        }
    store = Store(company, bundle, identity["company_id"], identity["database_id"])
    entities = Entities(Engine(store))
    appended = []
    for employee, profile, source in candidates:
        saved = entities.register_worker(
            WorkerRegistration(action="hire", effective_date=profile["employment_start"]),
            entity_id=employee,
            expected_revision=profile["revision"],
            source=source,
            evidence_digest=profile["evidence_digest"],
            request_id="synthetic-workforce-format-" + profile["id"],
        )
        appended.append({"original_profile_id": profile["id"], "result": saved})
    with closing(connect(company, read_only=True)) as connection:
        state_after = dict(connection.execute("SELECT * FROM state WHERE id=1").fetchone())
        for table, digest in before.items():
            if (
                adjustment._row_digest(connection, table, adjustment._columns(connection, table))
                != digest
            ):
                raise ValueError("Workforce format conversion altered immutable accounting history")
        if state_after["management"] - state_before["management"] != len(appended) or any(
            state_after[key] != state_before[key] for key in state_before if key != "management"
        ):
            raise ValueError("Workforce format conversion advanced unexpected accounting state")
        from ai_accounting.kernel.employee_employment import projection, records

        regular = sum(
            projection(records(connection, employee))["employment_state"] == "regular"
            for employee in employees
        )
        if regular != expected_employees:
            raise ValueError("The converted current workforce does not contain the full sample")
    return {
        "status": "converted",
        "employees": expected_employees,
        "regular_employees": regular,
        "appended_profiles": appended,
        "state_before": state_before,
        "state_after": state_after,
        "unchanged_accounting_tables": before,
    }


def storage_budget(company, catalog):
    """Serial peak: retained ZIP plus max(extraction, migration WAL), not three copies."""
    company_bytes = company.stat().st_size
    catalog_bytes = catalog.stat().st_size
    wal = Path(str(company) + "-wal")
    wal_bytes = wal.stat().st_size if wal.exists() else 0
    catalog_wal = Path(str(catalog) + "-wal")
    catalog_wal_bytes = catalog_wal.stat().st_size if catalog_wal.exists() else 0
    database_bound = company_bytes + wal_bytes
    zip_bound = database_bound + database_bound // 100 + 1024 * 1024
    backup_peak = zip_bound + database_bound
    migration_peak = zip_bound + 2 * (company_bytes + wal_bytes)
    temporary_directory = Path(tempfile.gettempdir()).resolve()
    same_volume = temporary_directory.anchor.casefold() == company.resolve().anchor.casefold()
    temporary_required = database_bound + 64 * 1024 * 1024
    required = (
        max(backup_peak if same_volume else zip_bound, migration_peak)
        + 2 * (catalog_bytes + catalog_wal_bytes) + 64 * 1024 * 1024
    )
    free = shutil.disk_usage(company.parent).free
    temporary_free = shutil.disk_usage(temporary_directory).free
    return {
        "company_main_bytes": company_bytes, "company_wal_bytes": wal_bytes,
        "catalog_main_bytes": catalog_bytes, "zip_upper_bound_bytes": zip_bound,
        "catalog_wal_bytes": catalog_wal_bytes,
        "portable_verification_peak_bytes": backup_peak,
        "migration_peak_bytes": migration_peak,
        "additional_free_required_bytes": required, "free_bytes": free,
        "verification_temporary_directory": str(temporary_directory),
        "verification_temporary_same_volume": same_volume,
        "verification_temporary_free_required_bytes": temporary_required,
        "verification_temporary_free_bytes": temporary_free,
        "sufficient": free >= required and temporary_free >= temporary_required,
        "serial_samples": True,
    }


def run_sample(key, phase, output, *, fault=None, progress=lambda value: None):
    (
        adjustment,
        create_portable,
        verify_file,
        create_private_file,
        ensure_private_directory,
        reject_reparse_path,
        connect,
        private_file_lock,
    ) = _runtime()
    root = reject_reparse_path(ROOTS[key])
    if root.resolve() != ROOTS[key] or not root.is_dir():
        raise ValueError("Only an existing exact synthetic fixture root is accepted")
    company = reject_reparse_path(root / COMPANY_RELATIVE)
    catalog = reject_reparse_path(root / "catalog.sqlite")
    if not company.is_file() or not catalog.is_file():
        raise ValueError("Synthetic fixture files are missing")
    result = {"sample": key, "phase": phase, "status": "started"}
    progress(result)
    # The daemon uses the same lock; refuse concurrent resident access.
    with private_file_lock(root / ".resident.lock") as acquired:
        if not acquired:
            raise ValueError("Synthetic fixture has an active resident owner")
        plans = {}
        source_guards = {}
        for kind, path in (("catalog", catalog), ("company", company)):
            with closing(connect(path, read_only=True)) as connection:
                connection.execute("BEGIN")
                plans[kind] = adjustment.plan_connection(connection, kind=kind)
                if phase == "apply" and kind == "company":
                    source_guards[kind] = adjustment.capture_source_guard(connection, kind=kind)
                if kind == "company":
                    identity = connection.execute(
                        "SELECT company_id,taxpayer_id,database_id FROM identity WHERE id=1"
                    ).fetchone()
                    if identity is None or identity[1] != "91310000123456789S":
                        raise ValueError("Only the fixed synthetic company is accepted")
        with closing(connect(catalog, read_only=True)) as connection:
            companies = connection.execute(
                "SELECT id,taxpayer_id,path,database_id,name FROM company"
            ).fetchall()
            if (
                len(companies) != 1
                or (companies[0][0], companies[0][1], companies[0][3]) != tuple(identity)
                or Path(companies[0][2]).resolve() != company
            ):
                raise ValueError("Catalog does not bind the sole exact synthetic company file")
        with closing(connect(company, read_only=True)) as connection:
            fund_settings = synthetic_fund_settings(connection, company_name=companies[0]["name"])
        result["synthetic_fund_settings"] = fund_settings
        result["plan"] = plans
        progress(result)
        if phase == "plan":
            result["status"] = "planned"
            return result
        source_bundle = adjustment.source_bundle()
        source_checks = verify_file(
            company,
            _bundle=adjustment.target_bundle()
            if plans["company"]["status"] == "already_adjusted"
            else source_bundle,
        )
        result["source_verification"] = source_checks["verification"]
        progress(result)
        if (
            source_checks["verification"]["status"] != "verified"
            or source_checks["verification"]["limitations"]
        ):
            raise ValueError("Source content has not passed complete independent verification")
        if phase == "check":
            result["status"] = "checked"
            return result
        if all(plan["status"] == "already_adjusted" for plan in plans.values()):
            result["workforce_format"] = append_workforce_format(company, expected_employees=50)
            progress(result)
            result["target_verification"] = verify_file(
                company, _bundle=adjustment.target_bundle(),
            )["verification"]
            progress(result)
            if (
                result["target_verification"]["status"] != "verified"
                or result["target_verification"]["limitations"]
            ):
                raise ValueError("Converted workforce has not passed complete verification")
            result["status"] = "already_adjusted"
            result["construction_checkpoint"] = update_construction_checkpoint(
                root, company, result
            )
            return result
        backup_root = ensure_private_directory(output.parent / (output.stem + "-backup-" + key))
        result["storage_budget"] = storage_budget(company, catalog)
        progress(result)
        if not result["storage_budget"]["sufficient"]:
            raise ValueError("Insufficient free space for this serial verified adjustment")
        if plans["company"]["status"] != "already_adjusted":
            backup = create_portable(company, backup_root, _bundle=source_bundle)
            result["verified_company_backup"] = str(
                backup_root / "91310000123456789S.finance-company.zip"
            )
            result["backup_verification"] = (
                backup["verification"] if "verification" in backup else "verified_portable"
            )
            progress(result)
        catalog_backup = backup_root / "catalog.sqlite"
        create_private_file(catalog_backup)
        with (
            closing(connect(catalog, read_only=True)) as original,
            closing(connect(catalog_backup)) as saved,
        ):
            from ai_accounting.kernel.backup import copy_to_unpublished_database
            from ai_accounting.kernel.versions import objects

            copy_to_unpublished_database(original, saved)
            if objects(saved) != objects(original):
                raise ValueError("Catalog backup structure changed")
            for row in original.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ):
                table = row[0]
                columns = adjustment._columns(original, table)
                if adjustment._row_digest(original, table, columns) != adjustment._row_digest(
                    saved, table, columns
                ):
                    raise ValueError("Catalog backup original rows changed")
            saved.execute("BEGIN")
            source_guards["catalog"] = adjustment.capture_source_guard(saved, kind="catalog")
        result["catalog_backup"] = str(catalog_backup)
        progress(result)
        # Each file commits independently. A retry recognizes the exact target;
        # company is adjusted first, then catalog. Prior receipts stay intact.
        for kind, path in (("company", company), ("catalog", catalog)):
            with closing(connect(path)) as connection:
                result[kind + "_adjustment"] = adjustment.adjust_connection(
                    connection,
                    kind=kind,
                    fault=fault,
                    fund_accounts=fund_settings["accounts"] if kind == "company" else None,
                    first_fund_period=fund_settings["first_period"] if kind == "company" else None,
                    expected_source_guard=source_guards.get(kind),
                )
            progress(result)
        result["workforce_format"] = append_workforce_format(company, expected_employees=50)
        progress(result)
        result["target_verification"] = verify_file(
            company, _bundle=adjustment.target_bundle(),
        )["verification"]
        progress(result)
        if (
            result["target_verification"]["status"] != "verified"
            or result["target_verification"]["limitations"]
        ):
            raise ValueError("Target content has not passed complete independent verification")
        result["status"] = "adjusted"
        result["construction_checkpoint"] = update_construction_checkpoint(root, company, result)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("plan", "check", "apply"), required=True)
    parser.add_argument("--samples", choices=tuple(ROOTS), nargs="+", default=list(ROOTS))
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{1,79}", args.run_id):
        parser.error("run-id must be a new simple artifact name")
    if len(set(args.samples)) != len(args.samples):
        parser.error("Duplicate sample keys are not allowed")
    runtime = _runtime()
    create_private_file, ensure_private_directory = runtime[3], runtime[4]
    directory = ensure_private_directory(WORKSPACE / ".tmp")
    output = directory / ("stage9-adjustment-" + args.run_id + ".json")
    started = output.with_name(output.stem + "-started.json")
    sample_outputs = {
        key: output.with_name(output.stem + "-" + key + ".json") for key in args.samples
    }
    progress_outputs = {
        key: output.with_name(output.stem + "-" + key + "-progress.json") for key in args.samples
    }
    if any(path.exists() for path in (
        output, started, *sample_outputs.values(), *progress_outputs.values()
    )):
        parser.error("Preserve prior adjustment reports; choose a new run-id")
    report = {"phase": args.phase, "status": "started", "samples": {}, "timing_performed": False}
    _write(started, report, create_private_file)
    try:
        for key in args.samples:
            print(json.dumps({"sample": key, "phase": args.phase, "status": "started"}), flush=True)
            counter = 0

            def save_progress(value, sample_key=key):
                nonlocal counter
                counter += 1
                report["samples"][sample_key] = json.loads(json.dumps(value))
                temporary = progress_outputs[sample_key].with_suffix(f".{counter}.tmp")
                _write(temporary, value, create_private_file)
                temporary.replace(progress_outputs[sample_key])

            report["samples"][key] = run_sample(
                key, args.phase, output, progress=save_progress
            )
            _write(sample_outputs[key], report["samples"][key], create_private_file)
            print(
                json.dumps({"sample": key, "status": report["samples"][key]["status"]}), flush=True
            )
        report["status"] = "complete"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = {"type": type(exc).__name__, "code": getattr(exc, "code", None)}
        raise
    finally:
        _write(output, report, create_private_file)
        print(json.dumps({"status": report["status"], "report": str(output)}), flush=True)


if __name__ == "__main__":
    main()
