"""An actual verified backup/restore supplies identical independent timed cases."""

import json
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
