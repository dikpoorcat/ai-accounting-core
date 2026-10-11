"""On-demand source checks use real declarations, not business files or name guesses."""

from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ConfigDict, Field

from scripts import export_field_inventory as inventory


class NestedAmount(BaseModel):
    amount: int = Field(description="本项明确分配金额，整数分")


class AccountingMonth(BaseModel):
    model_config = ConfigDict(json_schema_extra={"x-accounting-fact": {"meaning": "整项事实"}})
    period: str
    first: NestedAmount
    second: list[NestedAmount]


class RegistrationMonth(BaseModel):
    period: str = Field(description="登记所属月，实际适用期独立")


class RecursiveProfile(BaseModel):
    label: str = Field(description="本节点明确名称")
    children: list["RecursiveProfile"] = Field(default_factory=list, description="直接下级节点")


def small_inventory(*, supplements=None, scripts=None, policy=None):
    return inventory.build_inventory(
        registry=SimpleNamespace(models={}),
        roots=[
            (AccountingMonth, policy or inventory.SOURCES["fact"]),
            (RegistrationMonth, inventory.SOURCES["fact"]),
        ],
        scripts=scripts or [("company", "CREATE TABLE entity(id TEXT PRIMARY KEY);")],
        supplements={} if supplements is None else supplements,
    )


def test_exact_semantics_do_not_inherit_root_meaning_or_same_named_fields():
    exact = inventory.model_key(AccountingMonth) + ".period"
    result = small_inventory(supplements={exact: "业务核算所属月"})
    fields = {field["key"]: field for field in result["fields"]}
    assert fields[exact]["meaning"] == "业务核算所属月"
    assert fields[inventory.model_key(RegistrationMonth) + ".period"]["meaning"] == (
        "登记所属月，实际适用期独立"
    )
    unknown = small_inventory()
    assert exact in unknown["missing_semantics"]
    assert next(field for field in unknown["fields"] if field["key"] == exact)["meaning"] == (
        inventory.UNDECLARED
    )


def test_formal_field_metadata_precedes_exact_supplement_and_nested_types_are_unique():
    exact = inventory.model_key(RegistrationMonth) + ".period"
    result = small_inventory(supplements={exact: "不应覆盖正式声明"})
    fields = {field["key"]: field for field in result["fields"]}
    assert fields[exact]["meaning"] == "登记所属月，实际适用期独立"
    nested = inventory.model_key(NestedAmount) + ".amount"
    assert sum(field["key"] == nested for field in result["fields"]) == 1
    assert not any("command_input" in field for field in result["fields"])
    assert all("schema" not in field for field in result["fields"])


def test_recursive_root_reference_keeps_its_fields_without_expanding_definitions():
    result = inventory.build_inventory(
        registry=SimpleNamespace(models={}),
        roots=[(RecursiveProfile, inventory.SOURCES["entity"])],
        scripts=[("company", "CREATE TABLE entity(id TEXT PRIMARY KEY);")],
        supplements={},
    )
    key = inventory.model_key(RecursiveProfile)
    assert {field["key"] for field in result["fields"]} == {key + ".label", key + ".children"}
    assert result["counts"]["models"] == 1
    assert result["counts"]["model_fields"] == 2
    assert result["missing_semantics"] == []
    assert result["errors"] == []


def test_stale_exact_key_unknown_storage_source_and_stale_reference_are_reported():
    stale = inventory.model_key(AccountingMonth) + ".removed"
    policy = inventory.Source("实际来源", "登记", "核验", ("missing/source.py",))
    result = small_inventory(
        supplements={stale: "过去的字段"},
        policy=policy,
        scripts=[("company", "CREATE TABLE unreviewed_table(id TEXT);")],
    )
    assert "失效语义补充：" + stale in result["errors"]
    assert "缺少来源归属：company.unreviewed_table" in result["errors"]
    assert "失效源码引用：missing/source.py" in result["errors"]


@pytest.mark.parametrize(
    ("database", "table"),
    [
        ("catalog", "unreviewed_table"),
        ("company", "report_unreviewed_table"),
        ("company", "close_storage_unreviewed_table"),
        ("company", "fact_unreviewed_table"),
    ],
)
def test_unreviewed_tables_never_inherit_catalog_or_prefix_sources(database, table):
    result = small_inventory(scripts=[(database, f"CREATE TABLE {table}(id TEXT);")])
    assert f"缺少来源归属：{database}.{table}" in result["errors"]
    assert inventory.source_for_table(database, table, {}) is None


def test_real_inventory_only_connects_to_memory_and_never_opens_store(monkeypatch, tmp_path):
    from ai_accounting.kernel.storage import Store

    sentinel = tmp_path / "existing-business.sqlite"
    sentinel.write_bytes(b"preserved existing file")
    before = sentinel.read_bytes()
    original = inventory.sqlite3.connect
    connections = []

    def memory_only(database, *args, **kwargs):
        assert database == ":memory:"
        connections.append(database)
        return original(database, *args, **kwargs)

    def forbidden(*_args, **_kwargs):
        pytest.fail("field checks must not open a business store")

    monkeypatch.setattr(inventory.sqlite3, "connect", memory_only)
    monkeypatch.setattr(Store, "connection", forbidden)
    result = inventory.build_inventory()
    assert connections == [":memory:", ":memory:"]
    assert result["counts"]["tables"] > 200
    assert result["counts"]["model_fields"] > 900
    assert result["errors"] == []
    assert sentinel.read_bytes() == before


@pytest.mark.parametrize("argv", [[], ["--check"]])
def test_default_and_check_never_write(monkeypatch, capsys, argv):
    result = small_inventory()
    monkeypatch.setattr(inventory, "build_inventory", lambda: result)

    def forbidden(*_args, **_kwargs):
        pytest.fail("read-only checks must not write a report")

    monkeypatch.setattr(inventory.Path, "write_text", forbidden)
    assert inventory.main(argv) == 1
    assert "缺少精确语义：" + inventory.model_key(AccountingMonth) + ".period" in (
        capsys.readouterr().out
    )


def test_explicit_output_is_readable_markdown_and_failed_checks_remain_failed(
    monkeypatch,
    tmp_path,
):
    result = small_inventory(
        supplements={inventory.model_key(AccountingMonth) + ".missing": "过时"}
    )
    monkeypatch.setattr(inventory, "build_inventory", lambda: result)
    target = tmp_path / "field-check.md"
    assert inventory.main(["--output", str(target)]) == 1
    text = target.read_text(encoding="utf-8")
    assert "## 规范模型字段" in text
    assert "## 实际持久字段" in text
    assert "失效语义补充" in text
    assert "command_schema.py" in text


@pytest.mark.parametrize(
    "arguments",
    [
        ["--write"],
        ["--check", "--output", "unexpected.md"],
        ["--output", "business.sqlite"],
    ],
)
def test_cli_rejects_implicit_write_and_non_markdown_targets(arguments):
    with pytest.raises(SystemExit) as failure:
        inventory.main(arguments)
    assert failure.value.code == 2
