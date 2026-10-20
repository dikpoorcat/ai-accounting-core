"""Full-range counts, bounded pinyin pages, and snapshot cursor validity."""

from __future__ import annotations

import sqlite3
from uuid import UUID, uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.identity.public import (
    IdentityContext,
    create_identity,
    repository_factory,
)
from ai_accounting_modular.storage.public import RepositoryFactory, Storage


def populate(case, names):
    storage, companies, identity = case
    inputs = [
        {
            "object_id": str(UUID(int=index + 1)),
            "kind": "person",
            "name": name,
            "data": {},
            "expected_seq": 0,
        }
        for index, name in enumerate(names)
    ]
    for offset in range(0, len(inputs), 500):
        with storage.write_unit(companies[0].company_id) as uow:
            plan = identity.plan_change(
                inputs[offset : offset + 500], IdentityContext(uow, str(uuid4()))
            )
            identity.apply_current(plan, uow)
    return inputs


def query(company, **options):
    return {
        "company_id": company.company_id,
        "action": "find",
        "kind": "person",
        "limit": 2,
    } | options


def test_full_pinyin_homophone_then_id_order_and_complete_counts(identity_case):
    storage, companies, identity = identity_case
    inputs = populate(identity_case, ["张三", "李四", "章三", "陈六", "李四", "王七", "阿克", "安"])
    collected, cursor = [], None
    while True:
        with storage.read_unit(companies[0].company_id) as read:
            result = identity.find(
                query(companies[0], **({"cursor": cursor} if cursor else {})), read
            )
        assert result["page"]["total_count"] == result["page"]["filtered_count"] == 8
        assert result["page"]["returned_count"] == 2
        collected.extend(result["objects"])
        cursor = result["page"]["next_cursor"]
        if not result["page"]["has_more"]:
            break
    assert [obj["name"] for obj in collected] == [
        "阿克",
        "安",
        "陈六",
        "李四",
        "李四",
        "王七",
        "张三",
        "章三",
    ]
    assert [obj["object_id"] for obj in collected[3:5]] == [
        inputs[1]["object_id"],
        inputs[4]["object_id"],
    ]
    assert len({obj["object_id"] for obj in collected}) == 8


def test_literal_substring_filter_reports_full_and_filtered_counts(identity_case):
    storage, companies, identity = identity_case
    populate(identity_case, ["甲%_一", "甲%_二", "甲X三", "乙%_"])
    with storage.read_unit(companies[0].company_id) as read:
        result = identity.find(query(companies[0], name_contains="%_", limit=1), read)
        assert (
            result["page"]["total_count"],
            result["page"]["filtered_count"],
            result["page"]["returned_count"],
        ) == (4, 3, 1)
        assert result["page"]["has_more"]
        empty = identity.find(query(companies[0], name_contains="没有"), read)
        assert empty["objects"] == []
        assert empty["page"]["total_count"] == 4 and empty["page"]["filtered_count"] == 0
        assert empty["page"]["next_cursor"] is None


def test_cursor_binds_company_database_head_and_range_but_not_page_size(identity_case):
    storage, companies, identity = identity_case
    inputs = populate(identity_case, ["甲", "乙", "丙", "丁"])
    with storage.read_unit(companies[0].company_id) as read:
        cursor = identity.find(query(companies[0]), read)["page"]["next_cursor"]
        changed_size = identity.find(query(companies[0], cursor=cursor, limit=1), read)
        assert len(changed_size["objects"]) == 1
        with pytest.raises(ContractError) as error:
            identity.find(query(companies[0], cursor=cursor, name_contains="甲"), read)
        assert error.value.code == "snapshot_changed"
    with storage.read_unit(companies[1].company_id) as read:
        with pytest.raises(ContractError) as error:
            identity.find(query(companies[1], cursor=cursor), read)
        assert error.value.code == "snapshot_changed"
    with storage.write_unit(companies[0].company_id) as uow:
        plan = identity.plan_change(
            [inputs[0] | {"name": "改名", "expected_seq": 1}], IdentityContext(uow, str(uuid4()))
        )
        identity.apply_current(plan, uow)
    with storage.read_unit(companies[0].company_id) as read:
        with pytest.raises(ContractError) as error:
            identity.find(query(companies[0], cursor=cursor), read)
        assert error.value.code == "snapshot_changed"


@pytest.mark.parametrize("cursor", ["not base64", "e30=", "W10=", "//8=", "a" * 4001])
def test_invalid_cursors_are_typed_failures(identity_case, cursor):
    storage, companies, identity = identity_case
    with storage.read_unit(companies[0].company_id) as read:
        with pytest.raises(ContractError) as error:
            identity.find(query(companies[0], cursor=cursor), read)
        assert error.value.code == "invalid_input"


def test_bounded_page_reads_and_real_query_index(identity_case):
    storage, companies, _ = identity_case
    populate(identity_case, [f"人员{index:04}" for index in range(1021)])
    read_sizes = []

    class ObservedReads:
        def __init__(self, session):
            self.session = session

        def fetchall(self, sql, args):
            rows = self.session.fetchall(sql, args)
            read_sizes.append(len(rows))
            return rows

        def fetchone(self, sql, args):
            return self.session.fetchone(sql, args)

    factory = repository_factory()
    storage = Storage(
        storage.catalog_path,
        repositories=[
            RepositoryFactory(factory.schema, lambda session: factory.build(ObservedReads(session)))
        ],
    )
    identity = create_identity()
    with storage.read_unit(companies[0].company_id) as read:
        result = identity.find(query(companies[0], limit=3), read)
        result = identity.find(
            query(companies[0], limit=3, cursor=result["page"]["next_cursor"]), read
        )
        assert result["page"]["total_count"] == 1021
    assert read_sizes == [4, 4]
    with sqlite3.connect(companies[0].database_path) as connection:
        explain = connection.execute(
            "EXPLAIN QUERY PLAN SELECT object_id FROM identity_object WHERE kind=? "
            "AND (sort_key,name,object_id)>(?,?,?) ORDER BY sort_key,name,object_id LIMIT ?",
            ("person", "renyuan0002", "人员0002", str(UUID(int=3)), 4),
        ).fetchall()
    assert any("identity_object_kind_sort" in row[3] for row in explain)
    assert not any("TEMP B-TREE" in row[3] for row in explain)
