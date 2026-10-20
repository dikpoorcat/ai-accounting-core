"""Observable current identity behavior against real isolated company databases."""

from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError, content_digest
from ai_accounting_modular.identity.public import IdentityContext


def person(name="张三", *, ident=None, seq=0, **extra):
    return {
        "object_id": ident or str(uuid4()),
        "kind": "person",
        "name": name,
        "expected_seq": seq,
        "data": {},
    } | extra


def save(case, objects, *, company_index=0, command_id=None, sources=True):
    storage, companies, identity = case
    with storage.write_unit(companies[company_index].company_id) as uow:
        plan = identity.plan_change(objects, IdentityContext(uow, command_id or str(uuid4())))
        identity.apply_current(plan, uow)
        if sources:
            uow.repository("records").save(plan.source_uses)
    return plan


def read(case, ids, company_index=0):
    storage, companies, identity = case
    with storage.read_unit(companies[company_index].company_id) as context:
        return context.head, identity.get_batch(ids, context)


def stored(company):
    connection = sqlite3.connect(company.database_path)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute("SELECT * FROM identity_object")]
    finally:
        connection.close()


def test_fixed_contract_objects_do_not_create_business_or_vouchers(identity_case):
    sample = json.loads(
        (
            Path(__file__).resolve().parents[3] / "docs/redesign/examples/core-acceptance.json"
        ).read_text("utf-8")
    )
    plan = save(identity_case, sample["objects"])
    head, objects = read(identity_case, [obj["object_id"] for obj in sample["objects"]])
    assert head == 1
    assert [obj["name"] for obj in objects] == ["合成维修供应商", "合成员工甲", "合成公司银行账户"]
    assert all(obj["seq"] == 1 for obj in objects)
    assert plan.objects == objects
    with sqlite3.connect(identity_case[1][0].database_path) as connection:
        assert connection.execute("SELECT count(*) FROM business_head").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM voucher").fetchone()[0] == 0


def test_rename_changes_digest_preserves_identity_and_rejects_old_create(identity_case):
    original = person()
    first = save(identity_case, [original]).objects[0]
    renamed = original | {"name": "张三丰", "expected_seq": 1}
    second = save(identity_case, [renamed]).objects[0]
    assert second["object_id"] == first["object_id"]
    assert second["seq"] == 2
    assert second["content_digest"] != first["content_digest"]
    assert read(identity_case, [original["object_id"]])[1][0]["name"] == "张三丰"
    with pytest.raises(ContractError) as error:
        save(identity_case, [original])
    assert error.value.code == "conflict"
    rows = stored(identity_case[1][0])
    assert len(rows) == 1 and json.loads(rows[0]["data_json"])["name"] == "张三丰"


def test_no_change_or_reply_lost_retry_does_not_advance_seq_head_or_command(identity_case):
    obj = person()
    command = str(uuid4())
    save(identity_case, [obj], command_id=command)
    save(identity_case, [obj])
    save(identity_case, [obj | {"expected_seq": 1}])
    assert read(identity_case, [obj["object_id"]])[0] == 1
    row = stored(identity_case[1][0])[0]
    assert (row["seq"], row["latest_expected_seq"], row["latest_command_id"]) == (1, 0, command)
    update = obj | {"name": "李四", "expected_seq": 1}
    update_command = str(uuid4())
    save(identity_case, [update], command_id=update_command)
    retry = save(identity_case, [update])
    assert retry.changed_ids == ()
    assert read(identity_case, [obj["object_id"]])[0] == 2
    row = stored(identity_case[1][0])[0]
    assert (row["seq"], row["latest_expected_seq"], row["latest_command_id"]) == (
        2,
        1,
        update_command,
    )


def test_same_name_distinct_people_remain_distinct_and_mixed_create_can_reuse(identity_case):
    a, b = person(), person()
    save(identity_case, [a])
    plan = save(identity_case, [a, b])
    assert plan.changed_ids == (b["object_id"],)
    head, result = read(
        identity_case, [b["object_id"], str(uuid4()), a["object_id"], b["object_id"]]
    )
    assert head == 2
    assert [obj["object_id"] for obj in result] == [b["object_id"], a["object_id"]]
    assert result[0]["name"] == result[1]["name"] == "张三"
    save(identity_case, [a, b])
    assert read(identity_case, [a["object_id"]])[0] == 2


@pytest.mark.parametrize(
    "patch",
    [
        {"data": {"salary": "10000"}},
        {"data": {"salary": 1.1}},
        {"kind": "free_form"},
        {"expected_seq": True},
        {"expected_seq": -1},
        {"name": ""},
        {"object_id": "not-a-uuid"},
        {"data": {"phone": None}},
        {"data": {"phone": 1.1}},
        {"extra": "not allowed"},
    ],
)
def test_strict_objects_reject_unknown_accounting_fields_and_bad_types(identity_case, patch):
    with pytest.raises(ContractError) as error:
        save(identity_case, [person() | patch])
    assert error.value.code == "invalid_input"
    assert stored(identity_case[1][0]) == []


def test_management_unknowns_are_optional_and_account_zero_opening_is_explicit(identity_case):
    bank = {
        "object_id": str(uuid4()),
        "kind": "bank_account",
        "name": "公司银行",
        "expected_seq": 0,
        "data": {
            "bank_name": "合成银行",
            "account_number": "TEST",
            "currency": "CNY",
            "owner": "company",
        },
    }
    save(identity_case, [person(), bank])
    for patch in (
        {"opened_period": "2026-02"},
        {"zero_at_opening": True},
        {"opened_period": "2026-13", "zero_at_opening": True},
        {"starting_balance": "100"},
    ):
        with pytest.raises(ContractError):
            save(identity_case, [bank | {"expected_seq": 1, "data": bank["data"] | patch}])
    changed = bank | {
        "expected_seq": 1,
        "data": bank["data"] | {"opened_period": "2026-02", "zero_at_opening": True},
    }
    assert save(identity_case, [changed]).objects[0]["seq"] == 2


def test_kind_cannot_change_and_a_conflicting_participant_rolls_back_whole_plan(identity_case):
    a, b = person("甲"), person("乙")
    save(identity_case, [a, b])
    with pytest.raises(ContractError) as error:
        save(identity_case, [a | {"kind": "organization", "expected_seq": 1}])
    assert error.value.code == "conflict"
    with pytest.raises(ContractError):
        save(
            identity_case,
            [
                a | {"name": "甲的新名", "expected_seq": 1},
                b | {"name": "乙的新名", "expected_seq": 0},
            ],
        )
    assert [obj["name"] for obj in read(identity_case, [a["object_id"], b["object_id"]])[1]] == [
        "甲",
        "乙",
    ]
    assert read(identity_case, [a["object_id"]])[0] == 1


def test_duplicate_object_in_one_submission_is_rejected_even_if_identical(identity_case):
    obj = person()
    with pytest.raises(ContractError, match="只能提交一次"):
        save(identity_case, [obj, deepcopy(obj)])
    assert stored(identity_case[1][0]) == []


def test_source_set_order_is_irrelevant_but_source_changes_are_real_changes(identity_case):
    a, b, c = str(uuid4()), str(uuid4()), str(uuid4())
    obj = person(evidence_ids=[b, a, a])
    first = save(identity_case, [obj]).objects[0]
    assert first["evidence_ids"] == sorted([a, b])
    second = save(identity_case, [obj | {"evidence_ids": [a, b]}])
    assert second.changed_ids == ()
    changed = save(identity_case, [obj | {"expected_seq": 1, "evidence_ids": [c]}]).objects[0]
    assert changed["seq"] == 2 and changed["content_digest"] != first["content_digest"]
    assert read(identity_case, [obj["object_id"]])[1][0]["evidence_ids"] == [c]
    assert json.loads(stored(identity_case[1][0])[0]["data_json"]) == changed


def test_identity_exports_complete_own_body_without_reading_derived_source_index(identity_case):
    obj = person(evidence_ids=[str(uuid4())])
    plan = save(identity_case, [obj], sources=False)
    assert read(identity_case, [obj["object_id"]]) == (1, plan.objects)
    with sqlite3.connect(identity_case[1][0].database_path) as connection:
        assert connection.execute("SELECT count(*) FROM source_use").fetchone()[0] == 0


def test_explicit_references_allow_forward_same_submission_and_report_missing(identity_case):
    operator = person()
    project = {
        "object_id": str(uuid4()),
        "kind": "project",
        "name": "合作项目",
        "expected_seq": 0,
        "data": {"operator_id": operator["object_id"]},
    }
    assert len(save(identity_case, [project, operator]).objects) == 2
    missing = project | {"object_id": str(uuid4()), "data": {"operator_id": str(uuid4())}}
    with pytest.raises(ContractError) as error:
        save(identity_case, [missing])
    assert error.value.code == "needs_information"
    assert error.value.fact_issues[0]["field"] == "/objects/0/data/operator_id"
    assert read(identity_case, [operator["object_id"]])[0] == 1


def test_complete_initial_objects_resolve_references_across_wire_batches(identity_case):
    operator = person()
    project = {
        "object_id": str(uuid4()),
        "kind": "project",
        "name": "最后一项人员负责的项目",
        "expected_seq": 0,
        "data": {"operator_id": operator["object_id"]},
    }
    objects = [project, *(person(str(index)) for index in range(500)), operator]
    saved = save(identity_case, objects).objects
    assert len(saved) == 502 and all(value["seq"] == 1 for value in saved)
    _, selected = read(identity_case, [project["object_id"], operator["object_id"]])
    assert selected[0]["data"]["operator_id"] == selected[1]["object_id"]


def test_all_declared_object_types_can_be_created_without_business_facts(identity_case):
    values = [
        ("organization", {}),
        ("cash_account", {"currency": "CNY"}),
        (
            "platform_account",
            {"platform": "合成平台", "account_key": "TEST", "currency": "CNY", "owner": "company"},
        ),
        ("investment_product", {"category": "cooperation"}),
        ("asset", {"asset_type": "fixed", "category": "办公设备"}),
        ("project", {}),
    ]
    inputs = [person(kind=kind, data=data, name=kind) for kind, data in values]
    assert {obj["kind"] for obj in save(identity_case, inputs).objects} == {
        kind for kind, _ in values
    }


def test_company_isolation_and_plan_scope_are_enforced(identity_case):
    obj = person()
    save(identity_case, [obj])
    assert read(identity_case, [obj["object_id"]], 1) == (0, ())
    save(identity_case, [obj | {"name": "第二家公司的同一外部ID"}], company_index=1)
    assert read(identity_case, [obj["object_id"]])[1][0]["name"] == "张三"
    storage, companies, identity = identity_case
    with storage.read_unit(companies[0].company_id) as context:
        plan = identity.plan_change(
            [obj | {"expected_seq": 1, "name": "更新"}], IdentityContext(context, str(uuid4()))
        )
    with pytest.raises(ContractError, match="快照"):
        with storage.write_unit(companies[1].company_id) as uow:
            identity.apply_current(plan, uow)
    save(identity_case, [person()])
    with pytest.raises(ContractError, match="快照"):
        with storage.write_unit(companies[0].company_id) as uow:
            identity.apply_current(plan, uow)


def test_detached_plan_overlay_cannot_be_mutated_by_consumer(identity_case):
    obj = person(data={"phone": "123"})
    plan = save(identity_case, [obj])
    detached = plan.objects
    detached[0]["data"]["phone"] = "999"
    detached[0]["evidence_ids"].append(str(uuid4()))
    assert plan.objects[0]["data"] == {"phone": "123"}
    assert plan.objects[0]["evidence_ids"] == []
    assert (
        content_digest(
            {key: plan.objects[0][key] for key in ("kind", "name", "data", "evidence_ids")}
        )
        == plan.objects[0]["content_digest"]
    )


def test_body_or_late_validation_failure_rolls_back_applied_changes(identity_case):
    storage, companies, identity = identity_case
    for during_commit in (False, True):
        with pytest.raises(RuntimeError, match="合成失败"):
            with storage.write_unit(companies[0].company_id) as uow:
                plan = identity.plan_change([person()], IdentityContext(uow, str(uuid4())))
                identity.apply_current(plan, uow)
                if during_commit:
                    uow.check_before_commit(lambda: (_ for _ in ()).throw(RuntimeError("合成失败")))
                else:
                    raise RuntimeError("合成失败")
        assert stored(companies[0]) == []


def test_ledger_historical_view_does_not_change_current_object_lookup(identity_case):
    storage, companies, identity = identity_case
    obj = person()
    save(identity_case, [obj])
    save(identity_case, [obj | {"expected_seq": 1, "name": "当前正确姓名"}])
    with storage.read_unit(
        companies[0].company_id, view="as_posted", as_of="2026-01"
    ) as read_context:
        assert identity.get_batch([obj["object_id"]], read_context)[0]["name"] == "当前正确姓名"


@pytest.mark.parametrize(
    "column,value",
    [
        ("name", "被破坏的投影"),
        ("sort_key", "incorrect"),
        ("seq", 7),
        ("kind", "organization"),
        ("content_digest", "0" * 64),
        ("data_json", "{}"),
    ],
)
def test_inconsistent_body_or_search_projection_is_rejected(identity_case, column, value):
    obj = person()
    save(identity_case, [obj])
    # Deliberately corrupt only this synthetic database to exercise owner invariants.
    with sqlite3.connect(identity_case[1][0].database_path) as connection:
        connection.execute(f"UPDATE identity_object SET {column}=?", (value,))
    with pytest.raises(ContractError) as error:
        read(identity_case, [obj["object_id"]])
    assert error.value.code == "storage_corrupt"
