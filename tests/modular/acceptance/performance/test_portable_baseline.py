"""Small real fixtures prove byte-checked isolation; these are not timing results."""

import json
import os
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import CONTRACT

from .._support import COMPANY, DATABASE
from . import portable_cases as portable
from .dataset import build, open_dataset
from .projections import PAGES


def test_unreadable_subdirectory_cannot_silently_disappear_from_the_file_set(tmp_path, monkeypatch):
    runtime, unreadable = tmp_path / "runtime", tmp_path / "standard"
    runtime.mkdir()
    unreadable.mkdir()
    (runtime / "company.sqlite3").write_bytes(b"fixture file")
    enumerate_paths = Path.iterdir

    def checked(path):
        if path == unreadable:
            raise PermissionError("cannot enumerate complete baseline")
        return enumerate_paths(path)

    monkeypatch.setattr(Path, "iterdir", checked)
    with pytest.raises(PermissionError, match="complete baseline"):
        portable._snapshot(tmp_path)


@pytest.fixture(scope="module")
def baseline(tmp_path_factory):
    root = tmp_path_factory.mktemp("closed-baseline")
    original = root / "source"
    manifest = build(original, 1, probe_business_count=240)
    backup = portable.package(original, root / "baseline.zip", "original")
    with patch.object(portable, "restore", wraps=portable.restore) as restored:
        frozen = portable.prepare_baseline(backup, root / "baseline", manifest, "original")
    assert restored.call_count == 1
    return frozen, manifest


@pytest.mark.parametrize("mode", ["original", "aggregate_join"])
def test_prior_ready_baseline_reports_cold_history_without_preopening_the_new_month(
    baseline, tmp_path, mode
):
    frozen, manifest = baseline
    source = tmp_path / "source"
    source.mkdir()
    portable.clone_baseline(frozen, source, manifest, mode)
    manifest = manifest | {"pending_period": "2026-02", "intended_scale_months": 2}
    backup = portable.package(source, tmp_path / "ready-source.zip", mode)
    actual = portable.prepare_baseline(
        backup, tmp_path / "history", manifest, mode, prior_ready=True
    )
    cold = actual.cold_reading
    assert cold["measurement"] == "restored_history_cold_reading"
    assert cold["period"] == "2026-01" and cold["pending_period"] == "2026-02"
    assert cold["pending_month_businesses"] == 0 and cold["elapsed_ms"] >= 0
    assert set(cold["page_versions"]) == set(PAGES)
    assert all(len(value) == 64 for value in cold["page_versions"].values())
    assert cold["page_queries"]["quarterly-report"] == {
        "company_id": COMPANY,
        "year": 2026,
        "quarter": 1,
    }
    assert all(
        query["period"] == "2026-01"
        for page, query in cold["page_queries"].items()
        if page != "quarterly-report"
    )
    sample = tmp_path / "sample"
    sample.mkdir()
    portable.clone_baseline(actual, sample, manifest, mode)
    with open_dataset(sample, mode) as (kernel, _):
        with kernel.storage.read_unit(COMPANY) as read:
            assert read.head == cold["formal_head"]
            scopes = read.repository("reading").scopes()
            assert scopes and all(state["state"] == "ready" for state in scopes)
            monthly = [json.loads(state["scope_key"]) for state in scopes]
            assert {
                item["scope"]["period_to"] for item in monthly if item.get("page") == "monthly"
            } == {"2026-01"}
    assert portable._snapshot(actual.company_root) == actual.files


@pytest.mark.parametrize("mode", ["original", "aggregate_join"])
def test_two_byte_checked_samples_keep_identical_inputs_and_independent_real_writes(
    baseline, tmp_path, mode
):
    frozen, manifest = baseline
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    portable.clone_baseline(frozen, first, manifest, mode)
    portable.clone_baseline(frozen, second, manifest, mode)
    source = manifest["months_data"][0]["expense_id"]

    def current(kernel):
        return kernel.application.accounting_get(
            {"company_id": COMPANY, "business_ids": [source], "include": ["facts", "results"]}
        )["businesses"][0]

    with open_dataset(first, mode) as (kernel, _):
        original = current(kernel)
        assert original["fact"]["data"]["amount_fen"] == "1000"
        assert original["state"]["seq"] == 1
        with kernel.storage.read_unit(COMPANY) as read:
            metadata = kernel.storage.company_metadata(read)
            assert metadata.database_id == DATABASE
            assert metadata.company_root == first / "company"
            assert metadata.backup_directory == first / "company/backups"
            assert read.repository("reading").scopes() == ()
        fact = deepcopy(original["fact"])
        fact["data"]["amount_fen"] = fact["data"]["report_details"][0]["amount_fen"] = "1001"
        actual = kernel.application.accounting_apply(
            {
                "contract": CONTRACT,
                "company_id": COMPANY,
                "command_id": str(uuid4()),
                "mutations": [{"op": "replace", "expected_seq": 1, "fact": fact}],
            }
        )
        assert actual["completion"] == "complete", actual
        assert current(kernel)["fact"]["data"]["amount_fen"] == "1001"
    with open_dataset(second, mode) as (kernel, _):
        assert current(kernel) == original
        with kernel.storage.read_unit(COMPANY) as read:
            assert kernel.storage.company_metadata(read).company_root == second / "company"
    assert portable._snapshot(frozen.company_root) == frozen.files


@pytest.mark.parametrize("fault", ["same_bytes_count_and_mtime", "missing", "extra"])
def test_changed_source_collection_is_rejected_before_sample_registration(
    baseline, tmp_path, fault
):
    frozen, manifest = baseline
    path = frozen.company_root / "standard/README.md"
    original, stat = path.read_bytes(), path.stat()
    extra = frozen.company_root / "unlisted.txt"
    try:
        if fault == "same_bytes_count_and_mtime":
            path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        elif fault == "missing":
            path.unlink()
        else:
            extra.write_bytes(b"not part of the verified baseline")
        with pytest.raises((AssertionError, FileNotFoundError)):
            portable.clone_baseline(frozen, tmp_path, manifest, "original")
        assert not (tmp_path / "catalog.sqlite3").exists()
    finally:
        path.write_bytes(original)
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        extra.unlink(missing_ok=True)


def test_same_length_target_damage_with_preserved_mtime_is_not_accepted(
    baseline, tmp_path, monkeypatch
):
    frozen, manifest = baseline
    copy = portable.shutil.copyfile

    def damaged(source, target):
        result = copy(source, target)
        if target.name == "README.md":
            stat, value = target.stat(), target.read_bytes()
            target.write_bytes(bytes([value[0] ^ 1]) + value[1:])
            os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        return result

    monkeypatch.setattr(portable.shutil, "copyfile", damaged)
    with pytest.raises(AssertionError):
        portable.clone_baseline(frozen, tmp_path, manifest, "original")
    assert not (tmp_path / "catalog.sqlite3").exists()


def test_open_sqlite_sidecar_is_refused_instead_of_copying_only_the_main_file(baseline, tmp_path):
    frozen, manifest = baseline
    with open_dataset(frozen.company_root.parent, "original") as (kernel, _):
        with kernel.storage.write_unit(COMPANY):
            assert list((frozen.company_root / "runtime").glob("company.sqlite3-*"))
            with pytest.raises(AssertionError, match="sidecar"):
                portable.clone_baseline(frozen, tmp_path, manifest, "original")
    assert not (tmp_path / "catalog.sqlite3").exists()
    assert portable._snapshot(frozen.company_root) == frozen.files
