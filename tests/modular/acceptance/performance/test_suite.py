"""A small real construction chain proves independent prefix/full deliveries."""

from collections import Counter

import pytest

from ai_accounting_modular.records.public import open_standard_bundle

from .._support import COMPANY, DATABASE
from . import dataset, suite
from .dataset import count_businesses, describe, open_dataset
from .workload import month


def test_official_checkpoints_and_shared_probe_checkpoint_are_explicit():
    assert suite._checkpoints((12, 48, 120)) == {
        11: [("prefix-12", 12)],
        12: [("full-12", None)],
        47: [("prefix-48", 48)],
        48: [("full-48", None)],
        119: [("prefix-120", 120)],
    }
    assert suite._checkpoints((2, 3))[2] == [("full-2", None), ("prefix-3", 3)]


def test_suite_refuses_existing_targets_and_ambiguous_probe_inputs(tmp_path):
    existing = tmp_path / "existing"
    existing.mkdir()
    marker = existing / "keep.txt"
    marker.write_text("untouched", encoding="utf-8")
    with pytest.raises(FileExistsError):
        suite.build(existing)
    assert marker.read_text(encoding="utf-8") == "untouched"
    for fields in (
        {"probe_scales": (2,)},
        {"probe_business_count": 240},
        {"probe_scales": (3, 2), "probe_business_count": 240},
        {"probe_scales": (2,), "probe_business_count": 0},
    ):
        target = tmp_path / "unused"
        with pytest.raises(ValueError):
            suite.build(target, **fields)
        assert not target.exists()


def test_one_real_chain_archives_once_per_month_and_preserves_each_restored_prefix(
    tmp_path, monkeypatch, capsys
):
    archives, packages, restored_bytes = [], [], {}
    create_kernel, package, restore = dataset.create_kernel, suite.package, suite.restore

    def observed_kernel(catalog):
        kernel = create_kernel(catalog)
        prepare = kernel.records.prepare_documents

        def archive(snapshot):
            prepared = prepare(snapshot)
            archives.append(str(catalog))
            return prepared

        monkeypatch.setattr(kernel.records, "prepare_documents", archive)
        return kernel

    def observed_package(root, output, mode):
        packages.append(describe(root)["months"])
        return package(root, output, mode)

    def observed_restore(portable, target, manifest, mode):
        restore(portable, target, manifest, mode)
        restored_bytes[target] = (target / "company/standard/current.json").read_bytes()

    monkeypatch.setattr(dataset, "create_kernel", observed_kernel)
    monkeypatch.setattr(suite, "package", observed_package)
    monkeypatch.setattr(suite, "restore", observed_restore)
    root = tmp_path / "suite"
    result = suite.build(root, "aggregate_join", probe_scales=(2, 3), probe_business_count=240)
    assert result["status"] == "passed" and result["official"] is False
    assert result["construction_chains"] == 1
    assert packages == [1, 2]
    assert archives == [str(root / "full-3/catalog.sqlite3")] * 3
    assert set(result["datasets"]) == {"prefix-2", "full-2", "prefix-3", "full-3"}
    facts = {}
    for name, item in result["datasets"].items():
        target = root / name
        manifest = describe(target)
        assert item["manifest"] == manifest
        assert manifest["official_scale"] is False
        assert manifest["company_id"] == COMPANY and manifest["database_id"] == DATABASE
        months = {"prefix-2": 1, "full-2": 2, "prefix-3": 2, "full-3": 3}[name]
        assert manifest["months"] == len(manifest["months_data"]) == months
        assert manifest["period_to"] == month(months - 1)
        if name.startswith("prefix"):
            intended = int(name.split("-")[-1])
            assert manifest["intended_scale_months"] == intended
            assert manifest["pending_period"] == month(intended - 1)
        else:
            assert "pending_period" not in manifest and "intended_scale_months" not in manifest
        with open_dataset(target, "aggregate_join") as (kernel, _):
            with kernel.storage.read_unit(COMPANY) as read:
                metadata = kernel.storage.company_metadata(read)
                assert metadata.company_root.resolve() == target / "company"
                assert kernel.records.document_status(read)["state"] == "ready"
            for index in range(months):
                assert count_businesses(kernel, month(index)) == 240
            assert count_businesses(kernel, month(months)) == 0
        bundle = open_standard_bundle(target / "company/standard/current.json")
        facts[name] = {fact["business_id"]: fact for fact in bundle.facts}
        assert len(facts[name]) == months * 240
        assert Counter(fact["period"] for fact in bundle.facts) == {
            month(index): 240 for index in range(months)
        }
        if target in restored_bytes:
            assert (target / "company/standard/current.json").read_bytes() == restored_bytes[target]
    assert facts["full-2"] == facts["prefix-3"]
    assert facts["prefix-2"].items() <= facts["full-2"].items()
    assert facts["full-2"].items() <= facts["full-3"].items()
    stderr = capsys.readouterr().err
    assert all(f"{index}/3" in stderr for index in (1, 2, 3))
