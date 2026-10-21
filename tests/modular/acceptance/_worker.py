"""Test-only process and fault boundaries. No test hook enters the product protocol."""

import json
import os
import sqlite3
import sys
import traceback
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit
from urllib.request import url2pathname
from uuid import uuid4
from zipfile import ZipFile

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.host.public import create_kernel, create_offline_application
from ai_accounting_modular.records.public import open_standard_bundle
from ai_accounting_modular.storage.public import CompanySpec, OwnerProfile, create_catalog

from ._support import (
    COMPANY,
    DATABASE,
    SPEC,
    FixedDateTime,
    digest,
    documents,
    http,
    public_heads,
    ready,
    snapshot,
    source_files,
    wire,
)


class Worker:
    def __init__(self, root):
        self.root = root
        self.catalog = root / "catalog.sqlite3"
        fresh = not self.catalog.exists()
        if fresh:
            create_catalog(
                self.catalog,
                OwnerProfile(
                    "30000000-0000-4000-8000-000000000001", "合成负责人", "synthetic-hash"
                ),
            )
        self.kernel = create_kernel(self.catalog)
        self.kernel.dashboard.now = FixedDateTime.now
        if fresh:
            identity = SPEC["company"]["identity"]
            self.kernel.storage.create_company(
                CompanySpec(
                    COMPANY,
                    DATABASE,
                    identity["credit_code"],
                    identity["legal_name"],
                    identity["start_period"],
                    root / "company",
                    backup_directory=root / "configured-backups",
                )
            )
        self.paused = None

    def crash(self, code, **evidence):
        (self.root / "crash.json").write_text(json.dumps(evidence), encoding="utf-8")
        os._exit(code)

    def apply(self, request, fail_at=None):
        assert fail_at in {
            None,
            "after_changed_shards_fsynced_before_current_manifest_replace",
            "after_current_manifest_persisted_before_database_publication_ack",
            "after_database_commit_and_documents_ready_before_wire_response",
        }, fail_at
        with ExitStack() as hooks:
            if fail_at == "after_changed_shards_fsynced_before_current_manifest_replace":

                def before_publish(*args):
                    current = self.kernel.application.accounting_get(
                        dict(
                            company_id=COMPANY,
                            business_ids=[SPEC["ids"]["expense_a"]],
                            include=["facts"],
                        )
                    )
                    self.crash(
                        81, committed_business_period=current["businesses"][0]["fact"]["period"]
                    )

                hooks.enter_context(
                    patch.object(self.kernel.records, "publish_documents", before_publish)
                )
            elif fail_at == "after_current_manifest_persisted_before_database_publication_ack":
                from ai_accounting_modular.records import _documents

                original = _documents.atomic_write

                def write(root, relative, data):
                    original(root, relative, data)
                    if str(relative).replace("\\", "/") == "standard/current.json":
                        self.crash(82)

                hooks.enter_context(patch.object(_documents, "atomic_write", write))
            result = self.kernel.application.accounting_apply(request)
            if fail_at == "after_database_commit_and_documents_ready_before_wire_response":
                assert result["documents"] == "ready", result
                self.crash(83)
            return result

    def backup(self, request, pause=False, fail_at=None):
        if pause:
            self.paused = self.kernel.application.capture_backup_snapshot(COMPANY, DATABASE)
            self.plan = self.paused.__enter__()
            receipt = self.kernel.files.ensure_current(
                self.plan, self.plan["scope_head"], str(uuid4())
            )
            self.leased_paths = [
                (
                    self.root / "company/runtime/tasks" / self.plan["capture_id"]
                    if item["source_root"] == "capture"
                    else self.root / "company"
                )
                / item["relative_path"]
                for item in self.plan["inputs"]
            ]
            return dict(
                task_id=receipt["task_id"],
                database_head=self.plan["manifest"]["database_head"],
                set_id=self.plan["manifest"]["document_set_id"],
                source_manifest=source_files(self.plan["manifest"]),
            )
        result = self.kernel.application.accounting_backup(request)
        if fail_at:
            assert fail_at == "after_file_task_committed_before_producer_start"
            self.crash(84, task_id=result["task_id"], task_state=result["state"])
        return result

    def finish_backup(self, task_id):
        deleted = sum(not path.exists() for path in self.leased_paths) if self.paused else 0
        self.kernel.files.run(COMPANY, task_id)
        receipt = self.kernel.files.get(COMPANY, task_id)
        assert receipt["state"] == "succeeded", receipt
        delivered = self.root / ("delivered-" + task_id + ".zip")
        with self.kernel.files.verified_download(COMPANY, task_id) as value:
            with delivered.open("wb") as target:
                while block := value.stream.read(1024 * 1024):
                    target.write(block)
        app = create_offline_application()
        with delivered.open("rb") as stream:
            with app.backup_verifier.open(stream) as verified:
                manifest = verified.manifest
                package_root = self.root / ("package-" + task_id)
                # The full verifier has rejected unsafe/duplicate ZIP paths first.
                with ZipFile(delivered) as archive:
                    archive.extractall(package_root)
        if self.paused:
            self.paused.__exit__(None, None, None)
            self.paused = None
        with ZipFile(delivered) as archive:
            archive_files = archive.namelist()
        return dict(
            receipt=receipt,
            verified_package_path=str(delivered),
            package_sha256=digest(delivered),
            package_manifest=manifest,
            package_root=str(package_root),
            archive_files=archive_files,
            deleted_while_leased_file_count=deleted,
        )

    def isolated(self, request, forbidden_paths):
        target = Path(request["new_target_directory"])
        existed = target.exists()
        forbidden = {Path(path).resolve() for path in forbidden_paths}
        original = sqlite3.connect
        attempts = []

        def connect(database, *args, **kwargs):
            path = (
                url2pathname(urlsplit(str(database)).path)
                if str(database).startswith("file:")
                else database
            )
            if Path(path).resolve() in forbidden:
                attempts.append(str(database))
                raise AssertionError("Offline target attempted to read source database")
            return original(database, *args, **kwargs)

        set_id = (
            open_standard_bundle(request["source_path"]).set_id
            if request["action"] == "rebuild"
            else None
        )
        with patch.object(sqlite3, "connect", connect):
            result = create_offline_application().accounting_isolated(request)
        assert result["state"] == "created", result
        return dict(
            result=result,
            existing_target_overwritten=existed,
            old_database_read_count=len(attempts),
            standard_set_id=set_id,
        )

    def dispatch(self, action, **args):
        if action == "apply":
            return self.apply(**args)
        if action == "snapshot":
            return snapshot(self.kernel, **args)
        if action == "documents":
            return self.kernel.application.accounting_documents(args["request"])
        if action == "published":
            return documents(self.root / "company")
        if action == "period":
            return self.kernel.application.accounting_period(args["request"])
        if action == "ready":
            return ready(self.kernel, **args)
        if action == "http":
            return http(self.kernel, **args)
        if action == "heads":
            return public_heads(self.kernel)
        if action == "run_pending":
            return self.kernel.reading.run_pending(COMPANY, limit=100)
        if action == "issues":
            with self.kernel.storage.read_unit(COMPANY) as read:
                return self.kernel.records.current_issues(args["scope"], read)
        if action == "backup":
            return self.backup(**args)
        if action == "job":
            return self.kernel.application.accounting_jobs(
                dict(company_id=COMPANY, task_id=args["task_id"])
            )
        if action == "finish_backup":
            return self.finish_backup(**args)
        if action == "isolated":
            return self.isolated(**args)
        if action == "inspect_target":
            target = Path(args["root"])
            catalog = self.root / ("target-" + str(uuid4()) + ".sqlite3")
            create_catalog(
                catalog, OwnerProfile(str(uuid4()), "独立目标合成负责人", "synthetic-hash")
            )
            kernel = create_kernel(catalog)
            try:
                metadata = kernel.storage.register_company(target)
                return snapshot(
                    kernel, metadata.company_id, args.get("business_ids", ()), args.get("roles", ())
                )
            finally:
                kernel.close()
        raise AssertionError("Unknown test action: " + action)


def main():
    with ExitStack() as clock:
        from .performance.ledger_variant import install_ledger_implementation

        clock.enter_context(install_ledger_implementation(sys.argv[2]))
        for name in (
            "ai_accounting_modular.ledger._closing.datetime",
            "ai_accounting_modular.reading._brief.datetime",
            "ai_accounting_modular.reading._business_status.datetime",
        ):
            clock.enter_context(patch(name, FixedDateTime))
        worker = Worker(Path(sys.argv[1]))
        print(json.dumps({"started": True, "build_label": sys.argv[3]}), flush=True)
        for line in sys.stdin:
            request = json.loads(line)
            if request["action"] == "close":
                worker.kernel.close()
                print("{}", flush=True)
                break
            try:
                result = worker.dispatch(**request)
                packet = wire(result)
            except ContractError as error:
                packet = {"error": error.to_response()}
            except Exception:
                packet = {"traceback": traceback.format_exc()}
            print(json.dumps(packet, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
