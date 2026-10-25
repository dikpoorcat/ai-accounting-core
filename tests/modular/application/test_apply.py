"""Fixed amounts cross real application, identity, operations, ledger and records."""

import json
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest

from ai_accounting_modular.application._context import SubmissionContext
from ai_accounting_modular.application._repository import ApplicationRepository
from ai_accounting_modular.contracts.public import ContractError, validate_contract

from .conftest import get, initial_request, source


def manifest_facts(app, company_id):
    status = app.accounting_documents({"company_id": company_id, "action": "export"})
    manifest_path = Path(status["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    facts = []
    for shard in manifest["shards"]:
        part = json.loads((manifest_path.parent.parent / shard["path"]).read_text(encoding="utf-8"))
        facts.extend(part.get("facts", ()))
    return status, facts


def test_expense_real_chain_preserves_voucher_number_and_removes_old_body(assembled):
    app, company_id = assembled
    request = initial_request(company_id)
    business_id = request["mutations"][0]["fact"]["business_id"]
    created = app.accounting_apply(request)
    assert created["completion"] == "complete", created
    assert created["accounting"] == "saved"
    assert created["participants"]["objects"][0]["seq"] == 1
    original = get(app, company_id, business_id)["businesses"][0]
    result_ref = original["results"][0]["reference"]
    assert original["results"][0]["target"]["values"]["amount_fen"] == "100000"
    replacement = deepcopy(request)
    replacement["command_id"] = str(uuid4())
    replacement["mutations"][0].update(op="replace", expected_seq=1)
    replacement["mutations"][0]["fact"]["data"]["amount_fen"] = "90000"
    replaced = app.accounting_apply(replacement)
    assert replaced["completion"] == "complete", replaced
    current = get(app, company_id, business_id)["businesses"][0]
    assert current["state"]["seq"] == 2
    assert current["fact"]["data"]["amount_fen"] == "90000"
    assert len(current["results"]) == 1
    reference = current["results"][0]["reference"]
    assert reference["result_id"] == result_ref["result_id"]
    assert reference["voucher_number"] == result_ref["voucher_number"]
    assert reference["generation"] == result_ref["generation"] + 1
    assert len(current["results"][0]["vouchers"]) == 1
    lines = current["results"][0]["vouchers"][0]["lines"]
    assert [(line["account_code"], line["debit_fen"], line["credit_fen"]) for line in lines] == [
        ("5602", "90000", "0"),
        ("2202", "0", "90000"),
    ]
    with app.storage.read_unit(company_id) as read:
        obligation = app.ledger.resolve(
            [{"business_id": business_id, "slot": "primary"}], "obligation", read
        )[0]
        assert obligation["obligation"]["amount_fen"] == "90000"
        assert obligation["balance"]["remaining_fen"] == "90000"
    status, facts = manifest_facts(app, company_id)
    assert status["state"] == "ready"
    assert len(facts) == 1 and facts[0]["data"]["amount_fen"] == "90000"
    assert "100000" not in json.dumps(current, ensure_ascii=False)


def test_withdraw_tombstone_late_create_and_explicit_restore(assembled):
    app, company_id = assembled
    request = initial_request(company_id)
    business_id = request["mutations"][0]["fact"]["business_id"]
    assert app.accounting_apply(request)["completion"] == "complete"
    withdrawal = {
        "contract": request["contract"],
        "company_id": company_id,
        "command_id": str(uuid4()),
        "mutations": [{"op": "withdraw", "business_id": business_id, "expected_seq": 1}],
    }
    result = app.accounting_apply(withdrawal)
    assert result["completion"] == "complete", result
    assert result["businesses"][0]["seq"] == 2
    assert result["businesses"][0]["state"] == "withdrawn"
    withdrawn = get(app, company_id, business_id)["businesses"][0]
    assert "fact" not in withdrawn
    assert withdrawn["results"] == []
    assert manifest_facts(app, company_id)[1] == []
    assert app.accounting_apply(withdrawal)["changes"] == "none"
    conflict = app.accounting_apply(request)
    assert conflict["error"] == "conflict"
    restore = deepcopy(request)
    restore["command_id"] = str(uuid4())
    restore["mutations"][0].update(op="restore", expected_seq=2)
    restored = app.accounting_apply(restore)
    assert restored["completion"] == "complete", restored
    assert restored["businesses"][0]["seq"] == 3
    assert len(manifest_facts(app, company_id)[1]) == 1
    assert app.accounting_apply(restore)["changes"] == "none"


def test_lost_reply_retries_and_existing_a_plus_new_b_do_not_duplicate(assembled):
    app, company_id = assembled
    request = initial_request(company_id)
    business_a = request["mutations"][0]["fact"]["business_id"]
    first = app.accounting_apply(request)
    before = get(app, company_id, business_a)
    retry = app.accounting_apply(request)
    assert retry["changes"] == "none"
    assert retry["accounting"] == "unchanged"
    assert get(app, company_id, business_a) == before
    assert retry["document_set_id"] == first["document_set_id"]
    combined = deepcopy(request)
    combined["command_id"] = str(uuid4())
    added = deepcopy(combined["mutations"][0])
    business_b = str(uuid4())
    added["fact"]["business_id"] = business_b
    combined["mutations"].append(added)
    result = app.accounting_apply(combined)
    assert result["completion"] == "complete", result
    assert [business["seq"] for business in result["businesses"]] == [1, 1]
    assert get(app, company_id, business_a)["businesses"] == before["businesses"]
    assert len(manifest_facts(app, company_id)[1]) == 2
    complete = get(app, company_id, business_a, business_b)
    assert app.accounting_apply(combined)["changes"] == "none"
    assert get(app, company_id, business_a, business_b) == complete


def test_stale_changed_participant_rejects_whole_mixed_submission(assembled):
    app, company_id = assembled
    request = initial_request(company_id)
    app.accounting_apply(request)
    business_a = request["mutations"][0]["fact"]["business_id"]
    replace = deepcopy(request)
    replace["mutations"][0].update(op="replace", expected_seq=1)
    replace["mutations"][0]["fact"]["data"]["amount_fen"] = "90000"
    assert app.accounting_apply(replace)["completion"] == "complete"
    replace2 = deepcopy(replace)
    replace2["mutations"][0].update(expected_seq=2)
    replace2["mutations"][0]["fact"]["data"]["amount_fen"] = "80000"
    assert app.accounting_apply(replace2)["completion"] == "complete"
    before = get(app, company_id, business_a)
    late = deepcopy(replace)
    new_b = deepcopy(request["mutations"][0])
    new_b["fact"]["business_id"] = str(uuid4())
    late["mutations"].append(new_b)
    assert app.accounting_apply(late)["error"] == "conflict"
    assert get(app, company_id, business_a) == before
    assert get(app, company_id, new_b["fact"]["business_id"])["businesses"] == []


def test_late_owner_failure_rolls_back_every_participant(assembled, monkeypatch):
    app, company_id = assembled
    request = initial_request(company_id)
    business_id = request["mutations"][0]["fact"]["business_id"]
    real = app.records.replace_source_uses

    def fail_business(kind, key, ids, unit):
        if kind == "business":
            raise ContractError("temporary_failure", "合成测试：采用后来源边写入失败。")
        return real(kind, key, ids, unit)

    monkeypatch.setattr(app.records, "replace_source_uses", fail_business)
    result = app.accounting_apply(request)
    assert result["completion"] == "not_saved" and result["error"] == "temporary_failure"
    assert get(app, company_id, business_id)["head"] == 0
    assert get(app, company_id, business_id)["businesses"] == []
    with app.storage.read_unit(company_id) as read:
        assert app.identity.get_batch([request["objects"][0]["object_id"]], read) == ()
        assert app.records.resolve_sources([request["sources"][0]["source_id"]], read) == ()
        assert app.ledger.get_results([business_id], read) == ()


def test_document_failure_retry_finishes_without_rebooking(assembled, monkeypatch):
    app, company_id = assembled
    request = initial_request(company_id)
    business_id = request["mutations"][0]["fact"]["business_id"]
    prepare = app.records.prepare_documents

    def interrupted(snapshot):
        raise OSError("合成测试：写标准文档暂时失败。")

    monkeypatch.setattr(app.records, "prepare_documents", interrupted)
    result = app.accounting_apply(request)
    assert result["accounting"] == "saved", result
    assert result["completion"] == "saved_documents_pending"
    before = get(app, company_id, business_id)
    monkeypatch.setattr(app.records, "prepare_documents", prepare)
    retried = app.accounting_apply(request)
    assert retried["completion"] == "complete", retried
    assert retried["accounting"] == "unchanged" and retried["changes"] == "none"
    assert get(app, company_id, business_id) == before
    assert len(manifest_facts(app, company_id)[1]) == 1


def test_invalid_inputs_and_unimplemented_kinds_never_save(assembled):
    app, company_id = assembled
    request = initial_request(company_id)
    request["mutations"][0]["fact"]["data"]["amount_fen"] = 100000.0
    result = app.accounting_apply(request)
    assert result["status"] == "rejected"
    request = initial_request(company_id)
    request["mutations"] *= 2
    assert app.accounting_apply(request)["error"] == "invalid_input"
    request = initial_request(company_id)
    request["mutations"] = [
        {
            "op": "create",
            "expected_seq": 0,
            "fact": {
                "business_id": str(uuid4()),
                "kind": "people.employment",
                "period": "2026-01",
                "evidence_ids": [request["sources"][0]["source_id"]],
                "data": {
                    "employee_id": request["objects"][0]["object_id"],
                    "effective_from": "2026-01",
                    "social_insurance_participating": False,
                    "housing_fund_participating": False,
                    "contribution_shortfall": "reject",
                },
            },
        }
    ]
    validate_contract("core.schema.json", "ApplyRequest", request)
    assert app.accounting_apply(request)["error"] == "unsupported_business"
    with app.storage.read_unit(company_id) as read:
        assert read.head == 0


def test_same_fact_evidence_order_is_not_a_change(assembled):
    app, company_id = assembled
    request = initial_request(company_id)
    second = source("合成验收：供应商确认同一费用。")
    request["sources"].append(second)
    request["mutations"][0]["fact"]["evidence_ids"].append(second["source_id"])
    first = app.accounting_apply(request)
    retry = deepcopy(request)
    retry["command_id"] = str(uuid4())
    retry["mutations"][0]["fact"]["evidence_ids"].reverse()
    result = app.accounting_apply(retry)
    assert result["completion"] == "complete", result
    assert result["changes"] == "none"
    assert result["document_set_id"] == first["document_set_id"]


def test_one_changed_month_reuses_other_document_shards_without_exporting_their_facts(
    assembled, monkeypatch
):
    app, company_id = assembled
    january = initial_request(company_id)
    january_id = january["mutations"][0]["fact"]["business_id"]
    assert app.accounting_apply(january)["completion"] == "complete"
    february = deepcopy(january)
    february["command_id"] = str(uuid4())
    february_id = str(uuid4())
    february["mutations"][0]["fact"].update(business_id=february_id, period="2026-02")
    owner = app.registry.owner("operations.expense")
    export = owner.export_current
    exported = []

    def observe_export(ids, read):
        exported.extend(ids)
        return export(ids, read)

    monkeypatch.setattr(owner, "export_current", observe_export)
    assert app.accounting_apply(february)["completion"] == "complete"
    assert january_id not in exported
    assert exported and set(exported) == {february_id}
    exported.clear()
    february["mutations"][0].update(op="replace", expected_seq=1)
    february["mutations"][0]["fact"]["period"] = "2026-03"
    moved = app.accounting_apply(february)
    assert moved["completion"] == "complete", moved
    assert january_id not in exported
    status, facts = manifest_facts(app, company_id)
    assert {fact["period"] for fact in facts} == {"2026-01", "2026-03"}
    assert not any(
        "2026-02" in shard["key"]
        for shard in json.loads(Path(status["manifest_path"]).read_text(encoding="utf-8"))["shards"]
    )


def test_owner_projection_check_prevents_committing_an_identity_without_its_fact(
    assembled, monkeypatch
):
    app, company_id = assembled
    request = initial_request(company_id)
    business_id = request["mutations"][0]["fact"]["business_id"]
    monkeypatch.setattr(
        app.registry.owner("operations.expense"), "apply_current", lambda plan, unit: None
    )
    result = app.accounting_apply(request)
    assert result["completion"] == "not_saved" and result["error"] == "storage_corrupt"
    observed = get(app, company_id, business_id)
    assert observed["head"] == 0 and observed["businesses"] == []
    with app.storage.read_unit(company_id) as read:
        assert app.ledger.get_results([business_id], read) == ()


def test_conflicting_object_or_fixed_source_rejects_the_complete_submission(assembled):
    app, company_id = assembled
    request = initial_request(company_id)
    assert app.accounting_apply(request)["completion"] == "complete"
    business_id = request["mutations"][0]["fact"]["business_id"]
    before = get(app, company_id, business_id)
    for participant in ("object", "source"):
        changed = deepcopy(request)
        new_id = str(uuid4())
        changed["mutations"][0]["fact"]["business_id"] = new_id
        if participant == "object":
            changed["objects"][0]["name"] = "旧序号不能更改真实名称"
        else:
            changed["sources"][0] = source("同身份不能偷偷替换事实来源。") | {
                "source_id": request["sources"][0]["source_id"],
            }
        result = app.accounting_apply(changed)
        assert result["completion"] == "not_saved", result
        assert result["error"] == "conflict"
        assert get(app, company_id, business_id) == before
        assert get(app, company_id, new_id)["businesses"] == []


def test_late_document_failure_does_not_mark_a_newer_published_set_as_failed(
    assembled, monkeypatch
):
    app, company_id = assembled
    request = initial_request(company_id)
    business_id = request["mutations"][0]["fact"]["business_id"]
    prepare = app.records.prepare_documents

    def old_work_fails_after_new_work_finishes(snapshot):
        monkeypatch.setattr(app.records, "prepare_documents", prepare)
        replacement = deepcopy(request)
        replacement["command_id"] = str(uuid4())
        replacement["mutations"][0].update(op="replace", expected_seq=1)
        replacement["mutations"][0]["fact"]["data"]["amount_fen"] = "90000"
        result = app.accounting_apply(replacement)
        assert result["completion"] == "complete", result
        raise OSError("合成测试：旧文档工作此时才报告失败。")

    monkeypatch.setattr(app.records, "prepare_documents", old_work_fails_after_new_work_finishes)
    result = app.accounting_apply(request)
    assert result["completion"] == "complete", result
    status, facts = manifest_facts(app, company_id)
    assert status["state"] == "ready" and status["errors"] == []
    assert facts[0]["data"]["amount_fen"] == "90000"
    assert get(app, company_id, business_id)["businesses"][0]["state"]["seq"] == 2


def test_a_fact_can_use_more_than_one_public_source_read_batch(assembled):
    app, company_id = assembled
    request = initial_request(company_id)
    additional = [source(f"合成凭据第{number}份，均属同一费用。") for number in range(500)]
    registration = {
        "contract": request["contract"],
        "company_id": company_id,
        "command_id": str(uuid4()),
        "mutations": [],
        "sources": additional,
    }
    assert app.accounting_apply(registration)["completion"] == "complete"
    request["mutations"][0]["fact"]["evidence_ids"].extend(item["source_id"] for item in additional)
    result = app.accounting_apply(request)
    assert result["completion"] == "complete", result
    business_id = request["mutations"][0]["fact"]["business_id"]
    observed = get(app, company_id, business_id)["businesses"][0]
    assert len(observed["sources"]) == 501
    assert len(observed["results"][0]["basis"]["source_locations"]) == 501


def test_management_rename_is_committed_without_claiming_formal_accounting_changed(assembled):
    app, company_id = assembled
    request = initial_request(company_id)
    business_id = request["mutations"][0]["fact"]["business_id"]
    assert app.accounting_apply(request)["completion"] == "complete"
    before = get(app, company_id, business_id)["businesses"][0]
    rename = deepcopy(request)
    rename["command_id"] = str(uuid4())
    rename["mutations"] = []
    rename["objects"][0].update(name="合成维修商的新名称", expected_seq=1)
    renamed = app.accounting_apply(rename)
    assert renamed["completion"] == "complete", renamed
    assert renamed["changes"] == "committed"
    assert renamed["accounting"] == "unchanged"
    assert renamed["reading"] == "unchanged"
    assert renamed["participants"]["objects"][0]["seq"] == 2
    assert get(app, company_id, business_id)["businesses"][0] == before
    with app.storage.read_unit(company_id) as read:
        assert (
            app.identity.get_batch([request["objects"][0]["object_id"]], read)[0]["name"]
            == "合成维修商的新名称"
        )


def test_missing_source_identifies_the_actual_business_in_a_mixed_submission(assembled):
    app, company_id = assembled
    request = initial_request(company_id)
    business_a = request["mutations"][0]["fact"]["business_id"]
    assert app.accounting_apply(request)["completion"] == "complete"
    before = get(app, company_id, business_a)
    request = deepcopy(request)
    request["command_id"] = str(uuid4())
    new_source, missing = source("合成资料B，本单应一起回滚。"), str(uuid4())
    request["sources"].append(new_source)
    new_object = str(uuid4())
    request["objects"].append(
        {
            "object_id": new_object,
            "kind": "organization",
            "name": "本单新交易方",
            "expected_seq": 0,
            "data": {},
        }
    )
    added = []
    for source_id in (new_source["source_id"], missing):
        mutation = deepcopy(request["mutations"][0])
        mutation["fact"]["business_id"] = str(uuid4())
        mutation["fact"]["evidence_ids"] = [source_id]
        mutation["fact"]["data"]["creditor_id"] = new_object
        added.append(mutation)
    request["mutations"].extend(added)
    failed = app.accounting_apply(request)
    assert failed["completion"] == "not_saved" and failed["error"] == "invalid_input"
    issue = failed["fact_issues"][0]
    assert issue["business_id"] == added[1]["fact"]["business_id"]
    assert issue["field"] == "/mutations/2/fact/evidence_ids/0"
    assert missing in issue["message"] and issue["blocking_scope"] == "business"
    assert get(app, company_id, business_a) == before
    for mutation in added:
        assert get(app, company_id, mutation["fact"]["business_id"])["businesses"] == []
    with app.storage.read_unit(company_id) as read:
        assert app.identity.get_batch([new_object], read) == ()
        assert app.records.resolve_sources([new_source["source_id"]], read) == ()


def test_missing_object_source_identifies_the_actual_object_input(assembled):
    app, company_id = assembled
    request = initial_request(company_id)
    missing = str(uuid4())
    request["objects"][0]["evidence_ids"] = [missing]
    failed = app.accounting_apply(request)
    assert failed["error"] == "invalid_input"
    issue = failed["fact_issues"][0]
    assert issue["field"] == "/objects/0/evidence_ids/0"
    assert missing in issue["message"] and "business_id" not in issue
    with app.storage.read_unit(company_id) as read:
        assert read.head == 0


def test_related_fact_port_reads_an_empty_explicit_group(assembled):
    app, company_id = assembled
    with app.storage.read_unit(company_id) as read:
        context = SubmissionContext(
            read,
            app.registry,
            app.identity,
            app.ledger,
            app.records,
            evaluation_period_to="2026-01",
        )
        assert context.related_facts(str(uuid4()), "calculation") == ()
        assert context.related_facts_many((), "calculation") == ()
        with pytest.raises(ContractError, match="500"):
            context.related_facts_many([str(uuid4()) for _ in range(501)], "calculation")


def test_result_input_reuses_validation_but_isolates_callers_and_read_units(assembled, monkeypatch):
    app, company_id = assembled
    request = initial_request(company_id)
    business_id = request["mutations"][0]["fact"]["business_id"]
    assert app.accounting_apply(request)["completion"] == "complete"
    calls, load = [], app.ledger.get_results

    def counted(ids, read):
        calls.append(tuple(ids))
        return load(ids, read)

    monkeypatch.setattr(app.ledger, "get_results", counted)
    with app.storage.read_unit(company_id) as read:
        context = SubmissionContext(
            read,
            app.registry,
            app.identity,
            app.ledger,
            app.records,
            evaluation_period_to="2026-01",
        )
        original = context.get_result(business_id, "main")
        copy = context.get_result(business_id, "main")
        copy.target["values"]["amount_fen"] = "1"
        copy.basis["input"]["amount_fen"] = "1"
        assert context.get_result(business_id, "main") == original
        assert context.get_result(business_id, "absent") is None
    assert calls == [(business_id,)]
    request["mutations"][0].update(op="replace", expected_seq=1)
    request["mutations"][0]["fact"]["data"]["amount_fen"] = "120000"
    assert app.accounting_apply(request)["completion"] == "complete"
    with app.storage.read_unit(company_id) as read:
        current = SubmissionContext(
            read,
            app.registry,
            app.identity,
            app.ledger,
            app.records,
            evaluation_period_to="2026-01",
        ).get_result(business_id, "main")
        assert current.basis["input"]["amount_fen"] == "120000"
        assert current.source != original.source


def test_publication_projection_failure_rolls_back_real_fact_adoption_sources_and_documents(
    assembled, monkeypatch
):
    app, company_id = assembled
    request = initial_request(company_id)
    business_id = request["mutations"][0]["fact"]["business_id"]
    assert app.accounting_apply(request)["completion"] == "complete"
    before = get(app, company_id, business_id)
    documents = manifest_facts(app, company_id)
    with app.storage.read_unit(company_id) as read:
        publications = read.repository("application").publications([business_id])
    additional = source("合成验收：该项实际属于二月，金额为900元。")
    replacement = deepcopy(request)
    replacement["command_id"] = str(uuid4())
    replacement["sources"].append(additional)
    replacement["mutations"][0].update(op="replace", expected_seq=1)
    replacement["mutations"][0]["fact"]["period"] = "2026-02"
    replacement["mutations"][0]["fact"]["data"]["amount_fen"] = "90000"
    replacement["mutations"][0]["fact"]["evidence_ids"].append(additional["source_id"])
    persist = ApplicationRepository.replace_publications

    def fail_after_replacement(repository, ids, values):
        assert persist(repository, ids, values)
        raise ContractError("temporary_failure", "合成故障：发布索引保存后失败。")

    monkeypatch.setattr(ApplicationRepository, "replace_publications", fail_after_replacement)
    failed = app.accounting_apply(replacement)
    assert failed["completion"] == "not_saved" and failed["error"] == "temporary_failure", failed
    assert get(app, company_id, business_id) == before
    assert manifest_facts(app, company_id) == documents
    with app.storage.read_unit(company_id) as read:
        assert read.repository("application").publications([business_id]) == publications
        assert app.records.resolve_sources([additional["source_id"]], read) == ()


def test_publication_projection_change_is_reported_without_claiming_new_accounting(
    assembled, monkeypatch
):
    app, company_id = assembled
    request = initial_request(company_id)
    business_id = request["mutations"][0]["fact"]["business_id"]
    assert app.accounting_apply(request)["completion"] == "complete"
    before = get(app, company_id, business_id)
    owner = app.registry.owner("operations.expense")
    describe = owner.describe_change

    def wider_declared_envelope(changes, context):
        description = describe(changes, context)
        for publication in description["publications"]:
            publication["period_to"] = "2026-02"
        return description

    monkeypatch.setattr(owner, "describe_change", wider_declared_envelope)
    updated = app.accounting_apply(request)
    assert updated["completion"] == "complete", updated
    assert updated["changes"] == "committed" and updated["accounting"] == "unchanged"
    after = get(app, company_id, business_id)
    assert after["head"] == before["head"] + 1
    assert after["businesses"] == before["businesses"]
    assert app.accounting_apply(request)["changes"] == "none"
    assert get(app, company_id, business_id) == after
