"""Incremental structure contracts remain exact SQL snapshots after expansion."""

import json
from copy import deepcopy

import pytest
from schema_fixture import TEST_FAMILY

from ai_accounting.kernel.migration_contracts import diff_contracts, load_contracts
from ai_accounting.kernel.schema_bundle import APPLICATION_ID, production_bundle
from ai_accounting.kernel.versions import contract, fingerprint


def write(directory, version, data, kind="company"):
    directory = directory / kind
    directory.mkdir(exist_ok=True)
    value = {
        "family": TEST_FAMILY,
        "kind": kind,
        "status": "released",
        "application_id": APPLICATION_ID,
        **data,
    }
    (directory / f"v{version}.json").write_text(json.dumps(value), encoding="utf-8")


def load(directory):
    return load_contracts(
        directory / "company", "company", family=TEST_FAMILY, application_id=APPLICATION_ID
    )


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
    expanded = load(tmp_path)
    assert expanded[2]["objects"] == expected_second
    assert expanded[2]["sha256"] == second["sha256"]
    assert expanded[3]["objects"] == expected_third
    assert expanded[3]["sha256"] == third["sha256"]
    before = {path.name: path.read_bytes() for path in (tmp_path / "company").iterdir()}
    assert diff_contracts(expanded[1], expanded[2]) == [
        {
            "type": "index",
            "name": "a_idx",
            "old_sql": "CREATE INDEX a_idx ON a(id)",
            "new_sql": "CREATE INDEX a_idx ON a(id DESC)",
        },
        {"type": "table", "name": "b", "old_sql": None, "new_sql": "CREATE TABLE b(id TEXT)"},
    ]
    assert {path.name: path.read_bytes() for path in (tmp_path / "company").iterdir()} == before


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
        write(tmp_path, 1, first, "catalog" if damage == "wrong_kind_parent" else "company")
    write(tmp_path, 7 if damage == "wrong_filename" else 2, second)
    with pytest.raises(RuntimeError):
        load(tmp_path)


def test_sql_whitespace_is_part_of_contract_and_visible_in_diff(tmp_path):
    first, _, _, _, _ = contracts()
    changed = deepcopy(first)
    changed["objects"][0]["sql"] = changed["objects"][0]["sql"].replace(" ON ", "  ON ")
    assert (
        diff_contracts(first, changed)[0]["old_sql"] != diff_contracts(first, changed)[0]["new_sql"]
    )
    write(tmp_path, 1, changed)
    with pytest.raises(RuntimeError, match="damaged"):
        load(tmp_path)


def test_only_draft_contracts_are_packaged():
    bundle = production_bundle()
    assert bundle.status == "draft"
    for kind in ("company", "catalog"):
        assert set(bundle.contracts[kind]) == {0}
        assert bundle.current(kind)["status"] == "draft"


def test_released_v2_cannot_use_full_snapshot(tmp_path):
    first, _, _, _, _ = contracts()
    write(tmp_path, 1, first)
    second = {**first, "version": 2}
    write(tmp_path, 2, second)
    with pytest.raises(RuntimeError, match="must be incremental"):
        load(tmp_path)


def test_draft_cannot_be_a_released_parent(tmp_path):
    first, second, _, _, _ = contracts()
    folder = tmp_path / "company"
    folder.mkdir()
    draft = {
        "family": TEST_FAMILY,
        "kind": "company",
        "status": "draft",
        "application_id": APPLICATION_ID,
        **first,
        "version": 0,
    }
    (folder / "draft.json").write_text(json.dumps(draft), "utf-8")
    second["base_version"] = 0
    write(tmp_path, 2, second)
    with pytest.raises(RuntimeError, match="parent version"):
        load(tmp_path)
