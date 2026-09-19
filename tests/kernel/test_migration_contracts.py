"""Incremental structure contracts remain exact SQL snapshots after expansion."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from ai_accounting.kernel.migration_contracts import diff_contracts, load_contracts
from ai_accounting.kernel.versions import contract, fingerprint, known_contracts


def write(directory, version, data, kind="business"):
    (directory / f"v{version}_{kind}.json").write_text(json.dumps(data), encoding="utf-8")


def contracts():
    first = contract("CREATE TABLE a(id INTEGER PRIMARY KEY); CREATE INDEX a_idx ON a(id);")
    second = contract(
        "CREATE TABLE a(id INTEGER PRIMARY KEY); CREATE TABLE b(id TEXT);"
        "CREATE INDEX a_idx ON a(id DESC);"
    )
    third = contract("CREATE TABLE a(id INTEGER PRIMARY KEY); CREATE TABLE b(id TEXT);")
    return (
        {"version": 1, "objects": first, "sha256": fingerprint(first).hex()},
        {
            "version": 2,
            "base_version": 1,
            "base_sha256": fingerprint(first).hex(),
            "add": [row for row in second if row["name"] == "b"],
            "remove": [],
            "replace": [row for row in second if row["name"] == "a_idx"],
            "sha256": fingerprint(second).hex(),
        },
        {
            "version": 3,
            "base_version": 2,
            "base_sha256": fingerprint(second).hex(),
            "add": [],
            "remove": [{"type": "index", "name": "a_idx"}],
            "replace": [],
            "sha256": fingerprint(third).hex(),
        },
        second,
        third,
    )


def test_parent_chain_expands_to_independently_compiled_full_contract(tmp_path):
    first, second, third, expected_second, expected_third = contracts()
    for data in (first, second, third):
        write(tmp_path, data["version"], data)
    expanded = load_contracts(tmp_path, "business")
    assert expanded[2] == {"version": 2, "objects": expected_second, "sha256": second["sha256"]}
    assert expanded[3] == {"version": 3, "objects": expected_third, "sha256": third["sha256"]}
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    assert diff_contracts(expanded[1], expanded[2]) == [
        {
            "type": "index",
            "name": "a_idx",
            "old_sql": "CREATE INDEX a_idx ON a(id)",
            "new_sql": "CREATE INDEX a_idx ON a(id DESC)",
        },
        {"type": "table", "name": "b", "old_sql": None, "new_sql": "CREATE TABLE b(id TEXT)"},
    ]
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before


@pytest.mark.parametrize(
    "damage",
    [
        "missing_parent",
        "parent_digest",
        "target_digest",
        "duplicate_add",
        "duplicate_remove",
        "duplicate_replace",
        "cross_add_replace",
        "cross_add_remove",
        "cross_remove_replace",
        "add_existing",
        "remove_missing",
        "replace_missing",
        "same_parent",
        "future_parent",
        "wrong_kind_parent",
        "parent_corrupted",
        "parent_duplicate",
        "invalid_object",
        "unknown_field",
        "boolean_parent",
        "wrong_filename",
    ],
)
def test_invalid_incremental_contracts_are_rejected(tmp_path, damage):
    first, second, _, _, _ = deepcopy(contracts())
    table_a = next(row for row in first["objects"] if row["name"] == "a")
    table_b = second["add"][0]
    index = second["replace"][0]
    if damage == "parent_digest":
        second["base_sha256"] = "0" * 64
    elif damage == "target_digest":
        second["sha256"] = "0" * 64
    elif damage == "duplicate_add":
        second["add"].append(table_b)
    elif damage == "duplicate_remove":
        second["remove"] = [{"type": "table", "name": "a"}] * 2
    elif damage == "duplicate_replace":
        second["replace"].append(index)
    elif damage == "cross_add_replace":
        second["replace"].append(table_b)
    elif damage == "cross_add_remove":
        second["remove"].append({"type": "table", "name": "b"})
    elif damage == "cross_remove_replace":
        second["remove"].append({"type": "index", "name": "a_idx"})
    elif damage == "add_existing":
        second["add"].append(table_a)
    elif damage == "remove_missing":
        second["remove"].append({"type": "table", "name": "missing"})
    elif damage == "replace_missing":
        second["replace"].append({"type": "table", "name": "missing", "sql": "CREATE TABLE x(a)"})
    elif damage == "same_parent":
        second["base_version"] = 2
    elif damage == "future_parent":
        second["base_version"] = 3
    elif damage == "parent_corrupted":
        first["sha256"] = "0" * 64
    elif damage == "parent_duplicate":
        first["objects"].append(table_a)
    elif damage == "invalid_object":
        second["add"][0]["sql"] = None
    elif damage == "unknown_field":
        second["arbitrary_sql"] = "DROP TABLE a"
    elif damage == "boolean_parent":
        second["base_version"] = True
    if damage != "missing_parent":
        write(tmp_path, 1, first, "catalog" if damage == "wrong_kind_parent" else "business")
    write(tmp_path, 7 if damage == "wrong_filename" else 2, second)
    with pytest.raises(RuntimeError):
        load_contracts(tmp_path, "business")


def test_sql_whitespace_is_part_of_contract_and_visible_in_diff(tmp_path):
    first, _, _, _, _ = contracts()
    changed = deepcopy(first)
    changed["objects"][0]["sql"] = changed["objects"][0]["sql"].replace(" ON ", "  ON ")
    assert (
        diff_contracts(first, changed)[0]["old_sql"] != diff_contracts(first, changed)[0]["new_sql"]
    )
    write(tmp_path, 1, changed)
    with pytest.raises(RuntimeError, match="damaged"):
        load_contracts(tmp_path, "business")


def test_recorded_v10_variant_cannot_be_an_incremental_parent(tmp_path):
    from ai_accounting.kernel.versions import recorded_business_v10_variant

    recorded = recorded_business_v10_variant()
    (tmp_path / "recorded_business_v10_pre_account_indexes.json").write_text(
        json.dumps(recorded), encoding="utf-8"
    )
    write(
        tmp_path,
        11,
        {
            "version": 11,
            "base_version": 10,
            "base_sha256": recorded["sha256"],
            "add": [],
            "remove": [],
            "replace": [],
            "sha256": recorded["sha256"],
        },
    )
    with pytest.raises(RuntimeError, match="missing"):
        load_contracts(tmp_path, "business")


def test_all_packaged_contracts_are_present_and_frozen_sql_is_retained():
    import ai_accounting.kernel.versions as versions

    directory = Path(versions.__file__).with_name("migrations")
    for kind, expected_versions in (("business", set(range(1, 13))), ("catalog", {0, 2, 3})):
        loaded = known_contracts(kind)
        assert set(loaded) == expected_versions
        for path in directory.glob(f"v*_{kind}.json"):
            frozen = json.loads(path.read_text("utf-8"))
            assert loaded[frozen["version"]]["objects"] == frozen["objects"]
            assert loaded[frozen["version"]]["sha256"] == frozen["sha256"]


@pytest.mark.parametrize(
    "kind,parent_version,target_version", [("business", 12, 13), ("catalog", 3, 4)]
)
def test_future_versions_require_delta_and_expand_the_same_target(
    tmp_path, kind, parent_version, target_version
):
    parent = known_contracts(kind)[parent_version]
    added = contract("CREATE TABLE migration_contract_probe(id INTEGER PRIMARY KEY) STRICT;")
    target_objects = sorted(
        [*parent["objects"], *added], key=lambda row: (row["type"], row["name"])
    )
    target = {
        "version": target_version,
        "objects": target_objects,
        "sha256": fingerprint(target_objects).hex(),
    }
    write(tmp_path, parent_version, parent, kind)
    write(tmp_path, target_version, target, kind)
    with pytest.raises(RuntimeError, match="must be incremental"):
        load_contracts(tmp_path, kind)

    write(
        tmp_path,
        target_version,
        {
            "version": target_version,
            "base_version": parent_version,
            "base_sha256": parent["sha256"],
            "add": added,
            "remove": [],
            "replace": [],
            "sha256": target["sha256"],
        },
        kind,
    )
    expanded = load_contracts(tmp_path, kind)
    assert expanded[parent_version] == parent
    assert expanded[target_version] == target
