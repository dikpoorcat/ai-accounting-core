"""Notes and unresolved facts participate in the same real atomic submission."""

from copy import deepcopy
from uuid import uuid4

from ai_accounting_modular.contracts.public import loads_json
from ai_accounting_modular.host.public import create_kernel
from ai_accounting_modular.storage.public import OwnerProfile, create_catalog

from ._working_case import case as case
from .conftest import source


def note(scope, text, source, *, seq=0, basis=None):
    value = {"scope": scope, "expected_seq": seq, "text": text, "evidence_ids": [source]}
    if basis is not None:
        value["expected_basis_head"] = basis
    return value


def test_notes_return_real_heads_retry_without_writes_and_clear_only_stale_month_body(case):
    fact = case.seed()
    head = case.note_head()
    request = case.request(
        notes=[
            note("company", "公司经营维修业务。", case.source_id),
            note("period:2026-01", "一月维修费1000元。", case.source_id, basis=head),
        ]
    )
    response = case.app.accounting_apply(request)
    assert response["completion"] == "complete", response
    assert response["accounting"] == "unchanged"
    assert [item["seq"] for item in response["participants"]["notes"]] == [1, 1]
    with case.kernel.storage.read_unit(case.company) as read:
        before = read.head
    assert case.app.accounting_apply(request)["changes"] == "none"
    with case.kernel.storage.read_unit(case.company) as read:
        assert read.head == before
    changed = deepcopy(fact)
    changed["data"]["amount_fen"] = "120000"
    request = case.request(changed)
    request["mutations"][0].update(op="replace", expected_seq=1)
    assert case.app.accounting_apply(request)["completion"] == "complete"
    current = {item["scope"]: item for item in case.context()["notes"]}
    assert current["company"]["seq"] == 1
    assert current["company"]["text"] == "公司经营维修业务。"
    assert current["period:2026-01"] == {
        "scope": "period:2026-01",
        "seq": 2,
        "state": "needs_refresh",
        "text": None,
        "basis_head": case.note_head(),
        "evidence_ids": [],
    }
    with case.kernel.storage.read_unit(case.company) as read:
        with case.kernel.records.lease_document_set(read) as manifest:
            shards = {item["key"]: item for item in manifest["shards"]}
            assert "notes:period:2026-01" not in shards
            shard = shards["notes:company"]
            payload = loads_json(
                (
                    case.kernel.storage.company_metadata(read).company_root / shard["path"]
                ).read_bytes()
            )
            assert payload["notes"] == [
                {
                    "scope": "company",
                    "text": "公司经营维修业务。",
                    "evidence_ids": [case.source_id],
                }
            ]
            assert "一月维修费1000元。" not in str(payload)


def test_old_month_explanation_cannot_be_attached_to_new_numbers_in_the_same_unit(case):
    fact = case.seed()
    before = case.get(fact["business_id"])
    head = case.note_head()
    changed = deepcopy(fact)
    changed["data"]["amount_fen"] = "120000"
    extra_source = source("合成并发说明冲突时不得保存这份新资料。")
    extra_object = str(uuid4())
    request = case.request(
        changed,
        sources=[extra_source],
        objects=[
            {
                "object_id": extra_object,
                "kind": "organization",
                "name": "合成应回滚档案",
                "expected_seq": 0,
                "data": {},
            }
        ],
        notes=[note("period:2026-01", "旧依据上的1000元。", case.source_id, basis=head)],
    )
    request["mutations"][0].update(op="replace", expected_seq=1)
    response = case.app.accounting_apply(request)
    assert response["completion"] == "not_saved", response
    assert response["error"] == "conflict"
    assert case.get(fact["business_id"]) == before
    assert case.context()["notes"] == []
    with case.kernel.storage.read_unit(case.company) as read:
        assert case.kernel.identity.get_batch([extra_object], read) == ()
        assert case.kernel.records.resolve_sources([extra_source["source_id"]], read) == ()


def test_company_note_survives_unrelated_accounting_and_stale_note_rolls_back_new_fact(case):
    case.seed()
    request = case.request(notes=[note("company", "当前正确背景。", case.source_id)])
    assert case.app.accounting_apply(request)["completion"] == "complete"
    new = case.expense("30000")
    stale = case.request(new, notes=[note("company", "并发旧说明。", case.source_id)])
    response = case.app.accounting_apply(stale)
    assert response["completion"] == "not_saved" and response["error"] == "conflict", response
    assert case.get(new["business_id"])["missing_business_ids"] == [new["business_id"]]
    assert case.context()["notes"][0]["text"] == "当前正确背景。"


def test_existing_later_month_note_is_invalidated_even_before_that_work_month_is_entered(case):
    case.seed()
    item = note(
        "period:2026-04",
        "四月读取到的累计结果说明。",
        case.source_id,
        basis=case.note_head("2026-04"),
    )
    assert case.app.accounting_apply(case.request(notes=[item]))["completion"] == "complete"
    assert (
        case.app.accounting_apply(case.request(case.expense("20000", "2026-03")))["completion"]
        == "complete"
    )
    refreshed = case.context(note_scopes=[item["scope"]], issue_scopes=[])["notes"][0]
    assert refreshed["state"] == "needs_refresh" and refreshed["text"] is None


def test_unresolved_scope_is_complete_replace_with_real_retry_clear_and_conflict(case):
    case.seed()
    issue = {
        "issue_id": str(uuid4()),
        "scope": "custom:jan",
        "period": "2026-01",
        "evidence_ids": [case.source_id],
        "missing_fields": ["/data/amount_fen"],
        "question": "请核实资料中的实际费用金额。",
        "assignee": "ai",
        "action": "clarify_reality",
    }
    request = case.request(
        unresolved=[issue], unresolved_scope=issue["scope"], expected_scope_head=0
    )
    saved = case.app.accounting_apply(request)
    assert saved["completion"] == "complete", saved
    assert saved["participants"]["issue_scopes"] == [
        {"scope": issue["scope"], "head": 1, "issues": [issue]}
    ]
    assert case.app.accounting_apply(request)["changes"] == "none"
    assert case.context(issue_scopes=[issue["scope"]])["issues"] == [issue]
    cleared = case.app.accounting_apply(
        case.request(unresolved=[], unresolved_scope=issue["scope"], expected_scope_head=1)
    )
    assert cleared["participants"]["issue_scopes"][0] == {
        "scope": issue["scope"],
        "head": 2,
        "issues": [],
    }
    new = case.expense("20000")
    stale = case.app.accounting_apply(
        case.request(
            new, unresolved=[issue], unresolved_scope=issue["scope"], expected_scope_head=1
        )
    )
    assert stale["completion"] == "not_saved" and stale["error"] == "conflict", stale
    assert case.get(new["business_id"])["missing_business_ids"] == [new["business_id"]]


def test_context_does_not_guess_company_or_load_unrequested_scopes(case, monkeypatch):
    empty = case.app.accounting_context({})
    assert empty["state"] == "no_company"
    assert "company_id" not in empty and "database_id" not in empty
    assert empty["available_companies"][0]["company_id"] == case.company
    case.seed()
    context = case.context()
    assert context["open_periods"] == ["2026-01"]
    assert context["closed_periods"] == []
    assert [item["scope"] for item in context["issue_scopes"]] == ["company", "period:2026-01"]

    def forbidden(*args, **kwargs):
        raise AssertionError("显式空说明范围不能触发三表/全范围说明读取。")

    monkeypatch.setattr(case.kernel.ledger, "financial_snapshot", forbidden)
    monkeypatch.setattr(case.kernel.records, "period_notes", forbidden)
    assert case.context(note_scopes=[], issue_scopes=[])["note_basis_heads"] == []


def test_context_without_any_registered_company_has_no_invented_identity(tmp_path):
    catalog = tmp_path / "empty-catalog.sqlite3"
    create_catalog(catalog, OwnerProfile(str(uuid4()), "合成负责人", "synthetic-hash"))
    kernel = create_kernel(catalog)
    try:
        assert kernel.application.accounting_context({}) == {
            "contract": "ai-accounting-modular/1",
            "state": "no_company",
            "available_companies": [],
        }
    finally:
        kernel.close()


def test_partial_known_fact_stays_in_current_work_and_does_not_create_a_business(case):
    case.seed()
    business = str(uuid4())
    issue = {
        "issue_id": str(uuid4()),
        "scope": "custom:missing",
        "period": "2026-01",
        "business_id": business,
        "known_fact": {
            "business_id": business,
            "kind": "operations.expense",
            "period": "2026-01",
            "evidence_ids": [case.source_id],
            "data": {
                "creditor_id": case.party,
                "creditor_kind": "supplier",
                "expense_class": "administration",
            },
        },
        "evidence_ids": [case.source_id],
        "missing_fields": ["/data/amount_fen"],
        "question": "请从原件核实实际维修费。",
        "assignee": "ai",
        "action": "provide_material",
    }
    response = case.app.accounting_apply(
        case.request(unresolved=[issue], unresolved_scope=issue["scope"], expected_scope_head=0)
    )
    assert response["completion"] == "complete" and response["accounting"] == "unchanged", response
    assert case.context(issue_scopes=[issue["scope"]])["issues"] == [issue]
    assert case.get(business)["missing_business_ids"] == [business]
