"""A synthetic benchmark cannot bypass its installed content-version reader."""

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest
import stage9_book
import stage9_independent_book

from ai_accounting.kernel.catalog import Catalog
from ai_accounting.kernel.contracts import KernelError
from scripts import benchmark_stage9, benchmark_stage9_switch_company
from scripts.stage9_verified_open_preview import verify_book_open_preview


@pytest.mark.parametrize("independent", [False, True])
def test_sample_verification_uses_registered_reader_and_preserves_failure(
    tmp_path, monkeypatch, independent
):
    workspace = tmp_path / "workspace"
    (workspace / ".tmp").mkdir(parents=True)
    monkeypatch.setenv("STAGE9_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setattr(sys, "prefix", str(workspace / ".tmp-kernel-venv"))
    root = workspace / ".tmp/stage9-registered-reader"
    if independent:
        fixture = stage9_independent_book.IndependentBook
        book = fixture(root, objects=2, businesses=4)
        dimensions = ["--objects", "2", "--businesses", "4"]
        mode = ["--resume", "--verify-only"]
        main = stage9_independent_book.main
    else:
        fixture = stage9_book.MixedBook
        book = fixture(root, employees=1, businesses=26)
        dimensions = ["--employees", "1", "--businesses", "26"]
        mode = ["--read-existing", "--verify-only"]
        main = benchmark_stage9.main
    book.add_month(0, close=False)
    bundle = book.engine.store.bundle
    version = bundle.current_versions["company"]
    original = bundle.company_verifiers[version]
    calls = []
    fail = False

    def registered(connection, selected_bundle):
        assert connection.in_transaction
        calls.append(selected_bundle.current_versions["company"])
        if fail:
            raise KernelError("content_integrity_failed", "synthetic historical reader failure")
        return original(connection, selected_bundle)

    book.engine.store.bundle = replace(bundle, company_verifiers={version: registered})
    monkeypatch.setattr(fixture, "resume", lambda _root: book)
    repository = Path(__file__).resolve().parents[2]
    for should_fail in (False, True):
        fail = should_fail
        output = workspace / f".tmp/stage9-registered-{should_fail}.json"
        argv = [
            "benchmark", "--workspace", str(workspace), "--root", str(root),
            "--output", str(output), "--months", "1", *dimensions, *mode,
        ]
        if not independent:
            argv += ["--source", str(repository)]
        monkeypatch.setattr(sys, "argv", argv)
        if should_fail:
            with pytest.raises(KernelError, match="synthetic historical reader failure"):
                main()
        else:
            main()
        report = json.loads(output.read_text("utf-8"))
        assert report["status"] == ("verification_failed" if should_fail else "complete")
        if not should_fail:
            assert report["integrity_contract"] == bundle.database_format("company")
            assert report["integrity"]["status"] == "verified"
    assert calls == [version, version]


def test_switch_company_verification_uses_installed_reader(tmp_path):
    book = stage9_book.MixedBook(tmp_path / "stage9-switch-reader", employees=1, businesses=26)
    book.add_month(0, close=False)
    bundle = book.engine.store.bundle
    version = bundle.current_versions["company"]
    calls = []

    def reject(connection, selected_bundle):
        assert connection.in_transaction and selected_bundle is book.engine.store.bundle
        calls.append(version)
        raise KernelError("content_integrity_failed", "synthetic switch reader failure")

    book.engine.store.bundle = replace(bundle, company_verifiers={version: reject})
    with pytest.raises(KernelError, match="synthetic switch reader failure"):
        benchmark_stage9_switch_company.registered_integrity(book.engine)
    assert calls == [version]


def test_switch_read_existing_reverifies_without_creating_and_rejects_changed_checkpoint(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    (workspace / ".tmp").mkdir(parents=True)
    monkeypatch.setenv("STAGE9_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setattr(sys, "prefix", str(workspace / ".tmp-kernel-venv"))
    root = workspace / ".tmp/stage9-switch-read-existing"
    book = stage9_book.MixedBook(root, employees=1, businesses=26)
    book.add_month(0, close=False)
    contract, integrity = benchmark_stage9_switch_company.registered_integrity(book.engine)
    repository = Path(__file__).resolve().parents[2]
    current = verify_book_open_preview(
        book.engine,
        checkpoint_path=root / "stage9-builder.json",
        company=book.company,
        snapshots=book.snapshots,
        period="2016-01",
        source=repository,
    )
    primary = {
        **book.describe(),
        "status": "complete",
        "source": str(repository),
        "requested_months": 1,
        "integrity_contract": contract,
        "integrity": integrity,
        "verified_open_preview": current["verified_open_preview"],
    }
    primary_path = workspace / ".tmp/stage9-switch-primary.json"
    primary_path.write_text(json.dumps(primary, ensure_ascii=False), encoding="utf-8")
    creation_path = workspace / ".tmp/stage9-switch-created.json"
    monkeypatch.setattr(
        sys, "argv",
        [
            "switch", "--workspace", str(workspace), "--source", str(repository),
            "--book-report", str(primary_path), "--output", str(creation_path),
        ],
    )
    benchmark_stage9_switch_company.main()
    # A historical creation report is a read-only input. The new current-source
    # attestation is produced by reverify, not required from that old report.
    old_creation = json.loads(creation_path.read_text(encoding="utf-8"))
    old_creation.pop("verified_open_preview")
    creation_path.write_text(json.dumps(old_creation, ensure_ascii=False), encoding="utf-8")
    checkpoint = root / "stage9-switch-checkpoint/stage9-builder.json"
    original_checkpoint = checkpoint.read_bytes()
    output = workspace / ".tmp/stage9-switch-reverified.json"
    monkeypatch.setattr(
        sys, "argv",
        [
            "switch", "--workspace", str(workspace), "--source", str(repository),
            "--book-report", str(primary_path), "--switch-report", str(creation_path),
            "--read-existing", "--output", str(output),
        ],
    )

    def unexpected_write(*_args, **_kwargs):
        raise AssertionError("Read-existing must not construct or create a catalog")

    monkeypatch.setattr(Catalog, "__init__", unexpected_write)
    benchmark_stage9_switch_company.main()
    verified = json.loads(output.read_text(encoding="utf-8"))
    created = json.loads(creation_path.read_text(encoding="utf-8"))
    assert verified["status"] == "complete"
    assert verified["integrity"]["status"] == "verified"
    assert verified["company"] == created["company"]
    assert verified["source"] == str(repository)
    assert verified["creation_source"] == created["source"]
    assert verified["verified_open_preview"]["source"] == str(repository)
    assert checkpoint.read_bytes() == original_checkpoint

    state = json.loads(original_checkpoint)
    state["epochs"][0] += 1
    checkpoint.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    rejected = workspace / ".tmp/stage9-switch-rejected.json"
    monkeypatch.setattr(sys, "argv", [*sys.argv[:-1], str(rejected)])
    with pytest.raises(ValueError, match="state, repair revision or identity changed"):
        benchmark_stage9_switch_company.main()
    assert not rejected.exists()

    state = json.loads(original_checkpoint)
    state["company"]["id"] = "wrong-company-id"
    checkpoint.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="differs from the synthetic checkpoint"):
        benchmark_stage9_switch_company.main()
    assert not rejected.exists()
