"""An actual verified backup/restore supplies identical independent timed cases."""

import json
import shutil
import stat
from dataclasses import dataclass
from hashlib import file_digest
from pathlib import Path

from argon2 import PasswordHasher

from ai_accounting_modular.host.public import create_kernel, create_offline_application
from ai_accounting_modular.storage.public import OwnerProfile, create_catalog

from .._support import COMPANY, DATABASE
from .dataset import PASSWORD, open_dataset
from .ledger_variant import install_ledger_implementation
from .workload import identity


def package(root, output, mode):
    output = Path(output)
    assert not output.exists()
    with open_dataset(root, mode) as (kernel, _):
        task = kernel.application.accounting_backup(
            {"company_id": COMPANY, "expected_database_id": DATABASE}
        )
        kernel.files.run(COMPANY, task["task_id"])
        assert kernel.files.get(COMPANY, task["task_id"])["state"] == "succeeded"
        with kernel.files.verified_download(COMPANY, task["task_id"]) as download:
            with output.open("xb") as target:
                while block := download.stream.read(1024 * 1024):
                    target.write(block)
    return output


def restore(package_path, sample_root, manifest, mode):
    """A new catalog and the real isolated restore keep source/result/voucher identities."""
    sample_root = Path(sample_root).resolve()
    assert sample_root.is_dir() and not any(sample_root.iterdir())
    with install_ledger_implementation(mode):
        offline = create_offline_application()
        result = offline.accounting_isolated(
            {
                "action": "restore",
                "mode": "run",
                "source_path": str(Path(package_path).resolve()),
                "new_target_directory": str(sample_root / "company"),
            }
        )
        assert result["state"] == "created", result
        assert result["company_id"] == COMPANY and result["database_id"] == DATABASE
    _register(sample_root, manifest, mode)


def _register(sample_root, manifest, mode):
    with install_ledger_implementation(mode):
        catalog = sample_root / "catalog.sqlite3"
        create_catalog(
            catalog,
            OwnerProfile(identity("owner"), "合成性能负责人", PasswordHasher().hash(PASSWORD)),
        )
        kernel = create_kernel(catalog)
        try:
            metadata = kernel.storage.register_company(sample_root / "company")
            assert metadata.company_id == COMPANY and metadata.database_id == DATABASE
        finally:
            kernel.close()
    (sample_root / "synthetic.json").write_text(
        json.dumps(manifest, ensure_ascii=False) + "\n", encoding="utf-8"
    )


@dataclass(frozen=True)
class _Baseline:
    company_root: Path
    files: tuple[tuple[str, int, str], ...]


def _paths(root):
    assert root.is_dir() and not root.is_symlink() and not root.is_junction()
    files, pending = [], [root]
    while pending:
        for path in pending.pop().iterdir():
            assert not path.is_symlink() and not path.is_junction(), "Linked baseline file"
            if path.is_dir():
                pending.append(path)
                continue
            assert stat.S_ISREG(path.stat().st_mode), "A baseline contains only ordinary files"
            relative = path.relative_to(root).as_posix()
            assert relative not in {
                "runtime/company.sqlite3-wal",
                "runtime/company.sqlite3-shm",
                "runtime/company.sqlite3-journal",
            }, "SQLite sidecar: close every baseline connection before copying"
            files.append(relative)
    assert "runtime/company.sqlite3" in files
    return tuple(sorted(files))


def _snapshot(root):
    files = []
    for relative in _paths(root):
        path = root / relative
        with path.open("rb") as stream:
            digest = file_digest(stream, "sha256").hexdigest()
        files.append((relative, path.stat().st_size, digest))
    return tuple(files)


def prepare_baseline(package_path, root, manifest, mode):
    """Restore and verify once, then freeze the actual closed, relocated company bytes."""
    root = Path(root).resolve()
    root.mkdir(parents=False, exist_ok=False)
    restore(package_path, root, manifest, mode)
    # Read-only verification may leave WAL/SHM behind. A closed writable unit
    # lets SQLite finish its own checkpoint; no table or formal head is changed.
    with install_ledger_implementation(mode):
        kernel = create_kernel(root / "catalog.sqlite3")
        try:
            with kernel.storage.maintenance_unit(COMPANY):
                pass
        finally:
            kernel.close()
    company = root / "company"
    return _Baseline(company, _snapshot(company))


def clone_baseline(baseline, sample_root, manifest, mode):
    """A byte-checked test fixture copy; not another invocation of product restore."""
    sample_root = Path(sample_root).resolve()
    assert sample_root.is_dir() and not any(sample_root.iterdir())
    assert not sample_root.is_relative_to(baseline.company_root)
    assert not baseline.company_root.is_relative_to(sample_root)
    assert _paths(baseline.company_root) == tuple(item[0] for item in baseline.files)
    company = sample_root / "company"
    company.mkdir()
    for relative, _, _ in baseline.files:
        target = company / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(baseline.company_root / relative, target)
    assert _snapshot(baseline.company_root) == baseline.files, "Source baseline bytes changed"
    assert _snapshot(company) == baseline.files, "Copied sample bytes differ from the baseline"
    _register(sample_root, manifest, mode)
