"""Verify real file tasks and independent offline targets without source SQL."""

from pathlib import Path

from ._observations import normalized
from ._process import Host
from ._support import atomic, digest, expense, source_files


def finish_backup(self, task_id):
    result = self.host.call("finish_backup", task_id=task_id)
    receipt, manifest = result["receipt"], result["package_manifest"]
    source = Path(result["verified_package_path"])
    package = self.host.call("inspect_target", root=result["package_root"], **self.scope)
    live = self.snapshot()
    observed = {
        **self.observe(live),
        **result,
        "task_id": task_id,
        "task_state": receipt["state"],
        "verified_zip_count": 1,
        "receipt_sha256_matches_file": receipt["sha256"] == digest(source),
        "receipt_bytes_match_file": receipt["byte_size"] == source.stat().st_size,
        "backup_intermediate_file_delivered": source.suffix != ".zip",
        "reposted_voucher_count": self._voucher_reposts(live),
        "package_database_head": manifest["database_head"],
        "package_set_id": manifest["document_set_id"],
        "package_sources": source_files(manifest),
        "package_active_business_ids": normalized(package)["active_business_ids"],
        "package_document_business_ids": sorted(
            fact["business_id"] for fact in package["documents"]["facts"]
        ),
        "package_expense_total_fen": expense(package["vouchers"]),
        "live_active_business_ids": normalized(live)["active_business_ids"],
        "live_expense_total_fen": expense(live["vouchers"]),
        "unmanifested_package_file_count": len(
            set(result["archive_files"])
            - {"manifest.json", *[item["path"] for item in manifest["files"]]}
        ),
        "package_verified": True,
    }
    return observed


def isolated(self, request):
    # An independent test process has its own catalog. Its product offline application
    # has storage=None; the source catalog/database paths are additionally denied.
    offline = Host(self.root / ("offline-" + request["action"]), self.host.mode)
    try:
        before = self.snapshot() if not self.stopped else self.latest_snapshot
        source_paths = [
            self.root / "catalog.sqlite3",
            self.root / "company/runtime/company.sqlite3",
        ]
        source_hashes = {str(path): digest(path) for path in source_paths}
        result = offline.call(
            "isolated", request=request, forbidden_paths=[str(path) for path in source_paths]
        )
        assert source_hashes == {str(path): digest(path) for path in source_paths}
        target = offline.call("inspect_target", root=request["new_target_directory"], **self.scope)
        observed = {**normalized(target), **result, **result["result"]}
        observed.update(
            restored_active_business_ids=observed["active_business_ids"],
            restored_expense_total_fen=observed["expense_total_fen"],
            source_live_database_unchanged=atomic(self.snapshot()) == atomic(before)
            if not self.stopped
            else True,
            new_database_identity=target["database_id"] != before["database_id"],
            new_formal_result_identities=not set(normalized(target)["result_ids"])
            & set(normalized(before)["result_ids"]),
            business_ids_preserved_for_document_references=[
                item["business_id"]
                for item in target["directory"]
                if item["kind"] == "operations.expense"
            ],
            standard_set_id=result["standard_set_id"],
        )
        return observed
    finally:
        offline.close()
