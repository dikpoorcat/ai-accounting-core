"""Final company documents use the real accounting core without AI wire budgets."""

from copy import deepcopy
from hashlib import sha256
from uuid import uuid4

import pytest

from ai_accounting_modular.application import _execution
from ai_accounting_modular.application._submission import Submission
from ai_accounting_modular.application.public import repository_factory as application_repository
from ai_accounting_modular.contracts.public import ContractError, canonical_json
from ai_accounting_modular.domains.operations.public import (
    repository_factory as operations_repository,
)
from ai_accounting_modular.identity.public import repository_factory as identity_repository
from ai_accounting_modular.ledger.public import repository_factory as ledger_repository
from ai_accounting_modular.records.public import (
    RecordsChange,
    RecordsContext,
    copy_standard_bundle,
    open_standard_bundle,
)
from ai_accounting_modular.records.public import repository_factory as records_repository
from ai_accounting_modular.storage.public import (
    CompanySpec,
    create_company_database,
    open_database_unit,
)

from .conftest import initial_request, source


def repositories():
    return (
        application_repository(),
        identity_repository(),
        operations_repository(),
        ledger_repository(),
        records_repository(),
    )


def unit(root, mode="read", *, owners=None):
    return open_database_unit(
        root / "runtime/company.sqlite3",
        repositories=repositories() if owners is None else owners,
        company_root=root,
        mode=mode,
    )


def new_target(bundle, root):
    company, database = str(uuid4()), str(uuid4())
    identity = bundle.company_identity
    copy_standard_bundle(bundle, root)
    create_company_database(
        CompanySpec(
            company,
            database,
            identity["credit_code"],
            identity["legal_name"],
            identity["start_period"],
            root,
        )
    )
    return company, database


def seed(app, company, request, *, notes=(), scopes=()):
    submission = Submission.from_request(request)
    with app.storage.read_unit(company) as read:
        with app.records.prepare_sources(submission.sources, read) as prepared:
            with app.storage.write_unit(company) as write:
                _execution.execute(app, submission, write, prepared_sources=prepared)
                if notes:
                    app.records.apply_current(
                        app.records.plan_change(
                            RecordsChange(notes=tuple(notes)), RecordsContext(write, str(uuid4()))
                        ),
                        write,
                    )
                for scope, issues in scopes:
                    plan = app.records.plan_change(
                        RecordsChange(
                            unresolved=tuple(issues), unresolved_scope=scope, expected_scope_head=0
                        ),
                        RecordsContext(write, str(uuid4())),
                    )
                    app.records.apply_current(plan, write)
                app.records.mark_document_targets(("company",), write)
    status = app.accounting_documents({"action": "finish", "company_id": company})
    assert status["state"] == "ready"
    with app.storage.read_unit(company) as read:
        root = app.storage.company_metadata(read).company_root
    return open_standard_bundle(root / "standard/current.json")


def rebuild(app, bundle, target):
    owners = app.repositories or repositories()
    with unit(target, owners=owners) as read:
        with app.records.prepare_sources(bundle.sources, read) as prepared:
            with unit(target, "write", owners=owners) as write:
                return app.rebuild_standard(bundle, write, prepared_sources=prepared)


def test_company_larger_than_wire_budget_keeps_cross_batch_objects_and_exact_amounts(
    assembled, tmp_path, monkeypatch
):
    app, company = assembled
    request = initial_request(company)
    supplier = request["objects"][0]["object_id"]
    request["sources"] = []
    request["mutations"] = []
    person = str(uuid4())
    request["objects"] = [
        {
            "object_id": str(uuid4()),
            "kind": "project",
            "name": f"项目{index}",
            "data": {"operator_id": person},
            "expected_seq": 0,
        }
        for index in range(501)
    ] + [
        {
            "object_id": person,
            "kind": "person",
            "name": "共同经营人",
            "data": {},
            "expected_seq": 0,
        },
        {
            "object_id": supplier,
            "kind": "organization",
            "name": "维修商",
            "data": {},
            "expected_seq": 0,
        },
    ]
    template = initial_request(company)["mutations"][0]["fact"]
    for index in range(501):
        evidence = source(f"真实费用{index}：" + "资料" * 3000)
        request["sources"].append(evidence)
        fact = deepcopy(template)
        fact.update(business_id=str(uuid4()), evidence_ids=[evidence["source_id"]])
        fact["data"].update(creditor_id=supplier, amount_fen="1")
        request["mutations"].append({"op": "create", "expected_seq": 0, "fact": fact})
    assert len(canonical_json(request).encode()) > 8 * 1024 * 1024
    rejected = app.accounting_apply(request)
    assert rejected.get("code", rejected.get("error")) == "invalid_input"
    bundle = seed(app, company, request)
    target = tmp_path / "large-target"
    new_company, new_database = new_target(bundle, target)
    app.storage = None
    monkeypatch.setattr(app, "_states", lambda *a, **kw: pytest.fail("重建不组装有界AI回执"))
    outcome = rebuild(app, bundle, target)
    assert len(outcome.heads) == 501
    assert len(outcome.objects) == 503
    assert outcome.accounting_changed
    with unit(target) as read:
        assert read.company_id == new_company != company
        assert read.database_id == new_database
        references = []
        ids = [fact["business_id"] for fact in bundle.facts]
        for offset in range(0, len(ids), 500):
            references.extend(app.ledger.get_results(ids[offset : offset + 500], read))
        assert len(references) == 501
        assert (
            sum(
                int(line["debit_fen"])
                for result in references
                for voucher in result["vouchers"]
                for line in voucher["lines"]
            )
            == 501
        )
        projects = [obj for obj in outcome.objects if obj["kind"] == "project"]
        assert all(obj["data"]["operator_id"] == person for obj in projects)
        assert all(obj["seq"] == 1 for obj in outcome.objects)


def test_empty_bundle_does_not_manufacture_a_business_head_for_document_initialization(
    assembled, tmp_path
):
    app, company = assembled
    assert app.accounting_documents({"action": "finish", "company_id": company})["state"] == "ready"
    with app.storage.read_unit(company) as read:
        root = app.storage.company_metadata(read).company_root
    bundle = open_standard_bundle(root / "standard/current.json")
    target = tmp_path / "empty-target"
    new_target(bundle, target)
    outcome = rebuild(app, bundle, target)
    assert outcome.heads == ()
    assert not outcome.accounting_changed
    assert not outcome.changed
    with unit(target) as read:
        assert read.head == 0
        assert app.ledger.latest_period(read) is None
        assert app.records.document_status(read)["pending_scopes"] == []


def test_notes_and_multiple_issue_scopes_are_all_installed_with_new_sequence(assembled, tmp_path):
    app, company = assembled
    request = initial_request(company)
    evidence = request["sources"][0]["source_id"]
    issues = [
        (
            f"独立范围{index}",
            [
                {
                    "issue_id": str(uuid4()),
                    "scope": f"独立范围{index}",
                    "evidence_ids": [evidence],
                    "missing_fields": [],
                    "question": "月底自行申报",
                    "assignee": "owner",
                    "action": "external_filing",
                }
                for _ in range(501 if index == 0 else 1)
            ],
        )
        for index in range(3)
    ]
    bundle = seed(
        app,
        company,
        request,
        notes=[
            {
                "scope": "company",
                "expected_seq": 0,
                "text": "公司只经营维修业务。",
                "evidence_ids": [evidence],
            }
        ],
        scopes=issues,
    )
    target = tmp_path / "context-target"
    new_target(bundle, target)
    outcome = rebuild(app, bundle, target)
    assert len(outcome.issue_scopes) == 3
    assert outcome.notes[0]["text"] == "公司只经营维修业务。"
    assert outcome.notes[0]["seq"] == 1
    with unit(target) as read:
        for scope, expected in issues:
            current = app.records.current_issues(scope, read)
            assert current["head"] == 1
            assert current["issues"] == sorted(expected, key=lambda item: item["issue_id"])


def test_late_scope_failure_cannot_commit_an_earlier_successful_accounting_core(
    assembled, tmp_path, monkeypatch
):
    app, company = assembled
    request = initial_request(company)
    issues = [
        (
            scope,
            [
                {
                    "issue_id": str(uuid4()),
                    "scope": scope,
                    "evidence_ids": [],
                    "missing_fields": [],
                    "question": "稍后申报",
                    "assignee": "owner",
                    "action": "external_filing",
                }
            ],
        )
        for scope in ("a", "b")
    ]
    bundle = seed(app, company, request, scopes=issues)
    target = tmp_path / "failed-target"
    new_target(bundle, target)
    original = app.records.apply_current

    def fail(plan, write):
        if plan.issue_scope is not None and plan.issue_scope["scope"] == "b":
            raise ContractError("conflict", "最后范围发生冲突。")
        return original(plan, write)

    monkeypatch.setattr(app.records, "apply_current", fail)
    with pytest.raises(ContractError, match="整公司重建未完整成功"):
        with unit(target, "write") as write:
            with pytest.raises(ContractError, match="最后范围"):
                app.rebuild_standard(bundle, write)
    with unit(target) as read:
        assert read.head == 0
        assert app.ledger.latest_period(read) is None
        assert app.identity.get_batch([request["objects"][0]["object_id"]], read) == ()
        assert app.records.resolve_sources([request["sources"][0]["source_id"]], read) == ()


def test_full_file_source_preserves_human_name_and_explicit_media_type(
    assembled, tmp_path, monkeypatch
):
    app, company = assembled
    request = initial_request(company)
    with app.storage.read_unit(company) as read:
        root = app.storage.company_metadata(read).company_root
    source_id, name, raw = str(uuid4()), "原件名称.abc", b"one verified original"
    relative = f"originals/2026-01/{source_id}/{name}"
    path = root / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    request["sources"] = [
        {
            "source_id": source_id,
            "kind": "file",
            "relative_path": relative,
            "original_name": name,
            "media_type": "application/x-approved-original",
            "byte_size": len(raw),
            "sha256": sha256(raw).hexdigest(),
            "archive_month": "2026-01",
        }
    ]
    request["mutations"][0]["fact"]["evidence_ids"] = [source_id]
    bundle = seed(app, company, request)
    target = tmp_path / "file-target"
    new_target(bundle, target)
    monkeypatch.setattr(
        app.records, "prepare_source", lambda *a, **kw: pytest.fail("完整来源不得重猜文件名")
    )
    rebuild(app, bundle, target)
    with unit(target) as read:
        assert app.records.resolve_sources([source_id], read) == tuple(request["sources"])
    assert (target / relative).read_bytes() == raw


def test_month_note_is_bound_to_new_formal_results_instead_of_old_runtime_heads(
    assembled, tmp_path
):
    app, company = assembled
    request = initial_request(company)
    assert app.accounting_apply(request)["completion"] == "complete"
    scope = {
        "company_id": company,
        "period_from": "2026-01",
        "period_to": "2026-01",
        "view": "current",
    }
    with app.storage.read_unit(company) as read:
        old_head = app.ledger.financial_snapshot(scope, read)["selection"]["selection_digest"]
        root = app.storage.company_metadata(read).company_root
    note = {
        "scope": "period:2026-01",
        "text": "本月维修费已经核实。",
        "expected_seq": 0,
        "expected_basis_head": old_head,
        "evidence_ids": request["mutations"][0]["fact"]["evidence_ids"],
    }
    outcome = app.accounting_apply(
        {
            "contract": request["contract"],
            "company_id": company,
            "command_id": str(uuid4()),
            "mutations": [],
            "notes": [note],
        }
    )
    assert outcome["completion"] == "complete"
    bundle = open_standard_bundle(root / "standard/current.json")
    target = tmp_path / "new-note-target"
    new_company, _ = new_target(bundle, target)
    rebuilt = rebuild(app, bundle, target)
    assert rebuilt.notes[0]["seq"] == 1
    assert rebuilt.notes[0]["text"] == note["text"]
    with unit(target) as read:
        new_head = app.ledger.financial_snapshot({**scope, "company_id": new_company}, read)[
            "selection"
        ]["selection_digest"]
        assert rebuilt.notes[0]["basis_head"] == new_head
        assert rebuilt.notes[0]["basis_head"] != old_head
