"""Final document facts, rather than a previous database, reproduce cash meaning.

This exercises the public accounting submission in a fresh synthetic company;
it does not stand in for the future offline accounting_rebuild command.
"""

import json
from copy import deepcopy
from hashlib import sha256
from uuid import uuid4

from ai_accounting_modular.application.public import create_application
from ai_accounting_modular.application.public import repository_factory as app_repository
from ai_accounting_modular.contracts.public import content_digest, validate_contract
from ai_accounting_modular.domains.operations.public import create_operations
from ai_accounting_modular.domains.operations.public import (
    repository_factory as operations_repository,
)
from ai_accounting_modular.domains.people.public import create_people
from ai_accounting_modular.domains.people.public import repository_factory as people_repository
from ai_accounting_modular.identity.public import create_identity
from ai_accounting_modular.identity.public import repository_factory as identity_repository
from ai_accounting_modular.ledger.public import LedgerQuery, create_ledger
from ai_accounting_modular.ledger.public import repository_factory as ledger_repository
from ai_accounting_modular.money.public import create_money
from ai_accounting_modular.money.public import repository_factory as money_repository
from ai_accounting_modular.records.public import create_records
from ai_accounting_modular.records.public import repository_factory as records_repository
from ai_accounting_modular.storage.public import CompanySpec, OwnerProfile, Storage, create_catalog

from .test_application import request


def _system(base, company_identity=None):
    base.mkdir()
    identity = company_identity or {
        "credit_code": "913100000000000007",
        "legal_name": "合成最终事实重放公司",
        "start_period": "2026-01",
    }
    catalog = base / "catalog.sqlite3"
    create_catalog(catalog, OwnerProfile(str(uuid4()), "合成新负责人", "synthetic-only-hash"))
    storage = Storage(
        catalog,
        repositories=[
            app_repository(),
            identity_repository(),
            ledger_repository(),
            records_repository(),
            operations_repository(),
            people_repository(),
            money_repository(),
        ],
    )
    company = str(uuid4())
    storage.create_company(
        CompanySpec(
            company,
            str(uuid4()),
            identity["credit_code"],
            identity["legal_name"],
            identity["start_period"],
            base / "company",
        )
    )
    objects, ledger = create_identity(), create_ledger()
    money = create_money(ledger=ledger, identity=objects)
    app = create_application(
        storage=storage,
        identity=objects,
        ledger=ledger,
        records=create_records(root_for=lambda read: storage.company_metadata(read).company_root),
        domains=[create_operations(), create_people(), money],
        money=money,
    )
    return app, storage, company


def _object(kind):
    return {
        "object_id": str(uuid4()),
        "kind": kind,
        "name": "合成" + kind,
        "expected_seq": 0,
        "data": {
            "bank_name": "合成银行",
            "account_number": "synthetic-only",
            "currency": "CNY",
            "owner": "company",
            "opened_period": "2026-01",
            "zero_at_opening": True,
        }
        if kind == "bank_account"
        else {},
    }


def _fact(kind, source, data, on=None):
    result = {
        "business_id": str(uuid4()),
        "kind": kind,
        "period": "2026-01",
        "evidence_ids": [source["source_id"]],
        "data": data,
    }
    if on:
        result["effective_on"] = on
    return result


def _payment(source, bank, party, target, slot, amount, *, incoming=False):
    return _fact(
        "money.movement",
        source,
        {
            "type": "settlement",
            "funds_account_id": bank["object_id"],
            "direction": "inflow" if incoming else "outflow",
            "amount_fen": str(amount),
            "payment_method": "individual",
            "counterparty_id": party["object_id"],
            "allocations": [
                {
                    "source": {"business_id": target["business_id"], "slot": slot},
                    "amount_fen": str(amount),
                }
            ],
        },
        on="2026-01-25" if incoming else "2026-01-20",
    )


def _recovery(source, target, slot, party, amount):
    return _fact(
        "operations.overpayment",
        source,
        {
            "source": {"business_id": target["business_id"], "slot": slot},
            "counterparty_id": party["object_id"],
            "amount_fen": str(amount),
        },
    )


def _statement(text):
    return {
        "source_id": str(uuid4()),
        "kind": "owner_statement",
        "text": text,
        "sha256": sha256(text.encode()).hexdigest(),
        "archive_month": "2026-01",
    }


def _expense(source, supplier, amount, category):
    return _fact(
        "operations.expense",
        source,
        {
            "creditor_id": supplier["object_id"],
            "creditor_kind": "supplier",
            "amount_fen": str(amount),
            "expense_class": category,
        },
    )


def _reimbursement(source, payer, allocations):
    return _fact(
        "operations.reimbursement",
        source,
        {
            "type": "acceptance",
            "payer_id": payer["object_id"],
            "payer_kind": "employee",
            "sources": [
                {
                    "source": {"business_id": fact["business_id"], "slot": slot},
                    "amount_fen": str(amount),
                    "recipient_id": recipient["object_id"],
                }
                for fact, slot, amount, recipient in allocations
            ],
        },
    )


def _apply(system, *facts, **kwargs):
    app, _, company = system
    result = app.accounting_apply(request(company, *facts, **kwargs))
    assert result.get("completion") == "complete", result


def _final_documents(root):
    """Read only the standalone current manifest, its shards and referenced originals."""
    manifest = json.loads((root / "standard/current.json").read_bytes())
    validate_contract("core.schema.json", "DocumentManifest", manifest)
    assert (
        content_digest({k: v for k, v in manifest.items() if k != "set_id"}) == manifest["set_id"]
    )
    bundle = {"manifest": manifest, "facts": [], "objects": [], "sources": [], "originals": {}}
    for shard in manifest["shards"]:
        raw = (root / shard["path"]).read_bytes()
        assert len(raw) == shard["byte_size"] and sha256(raw).hexdigest() == shard["sha256"]
        part = json.loads(raw)
        validate_contract("core.schema.json", "StandardPart", part)
        assert part["key"] == shard["key"]
        for key in ("facts", "objects", "sources"):
            bundle[key].extend(part.get(key, ()))
        assert not part.get("issues") and not part.get("notes")
    for source in bundle["sources"]:
        if source["kind"] == "file":
            raw = (root / source["relative_path"]).read_bytes()
            assert sha256(raw).hexdigest() == source["sha256"]
            assert len(raw) == source["byte_size"]
            bundle["originals"][source["relative_path"]] = raw
    return bundle


def _replay(bundle, base):
    """No old Storage, company ID, owner, sequence, adopted plan or result is accepted."""
    system = _system(base, bundle["manifest"]["company_identity"])
    for relative, raw in bundle["originals"].items():
        target = base / "company" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    objects = [{**obj, "expected_seq": 0} for obj in bundle["objects"]]
    # All mutually dependent final facts enter one atomic submission, in reverse
    # document order. Replaying discarded wrong intermediate facts is unnecessary.
    _apply(system, *reversed(bundle["facts"]), objects=objects, sources=bundle["sources"])
    assert _final_documents(base / "company") == bundle
    return system


def _cash_snapshot(system, bank_id, refs):
    app, storage, company = system
    with storage.read_unit(company) as read:
        movements = app.ledger.fund_movements(bank_id, "2026-01", "2026-01", read)
        obligations = app.ledger.resource_definitions(refs, "obligation", read)
        balances = app.ledger.resolve(refs, "obligation", read)
    return {
        "movements": sorted((item.value for item in movements), key=lambda x: x["movement_id"]),
        "obligations": [item.value for item in obligations],
        "balances": [
            {key: value for key, value in item["balance"].items() if key != "adopted_result"}
            for item in balances
        ],
    }


def test_final_zero_wage_documents_replay_paid_and_refunded_money_without_old_database(tmp_path):
    original = _system(tmp_path / "original")
    bank, employee = _object("bank_account"), _object("person")
    source_id = str(uuid4())
    raw = "合成最终确认：应发工资0分，已付100000分，已由原收款人全额退回。".encode()
    relative = f"originals/2026-01/{source_id}--工资核对.txt"
    path = tmp_path / "original/company" / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    source = {
        "source_id": source_id,
        "kind": "file",
        "relative_path": relative,
        "original_name": "工资核对.txt",
        "media_type": "text/plain",
        "byte_size": len(raw),
        "sha256": sha256(raw).hexdigest(),
        "archive_month": "2026-01",
    }
    employment = _fact(
        "people.employment",
        source,
        {
            "employee_id": employee["object_id"],
            "effective_from": "2026-01",
            "social_insurance_participating": False,
            "housing_fund_participating": False,
            "contribution_shortfall": "reject",
        },
    )
    wage = _fact(
        "people.remuneration",
        source,
        {
            "type": "wage",
            "person_id": employee["object_id"],
            "gross_fen": "100000",
            "expense_area": "administration",
            "employment_id": employment["business_id"],
            "tax_basis": {
                "tax_period": "2026-01",
                "tax_fen": "0",
                "basis_type": "final_statement",
                "source": {"source_id": source_id},
            },
        },
    )
    paid = _payment(source, bank, employee, wage, "net_pay", 100000)
    _apply(original, paid, wage, employment, objects=[bank, employee], sources=[source])
    final_wage = deepcopy(wage)
    final_wage["data"]["gross_fen"] = "0"
    excess = _recovery(source, wage, "net_pay", employee, 100000)
    received = _payment(source, bank, employee, excess, "recovery", 100000, incoming=True)
    _apply(
        original,
        mutations=[
            {"op": "create", "expected_seq": 0, "fact": received},
            {"op": "replace", "expected_seq": 1, "fact": final_wage},
            {"op": "create", "expected_seq": 0, "fact": excess},
        ],
    )
    bundle = _final_documents(tmp_path / "original/company")
    wages = [fact for fact in bundle["facts"] if fact["kind"] == "people.remuneration"]
    assert wages == [final_wage]
    refs = [
        {"business_id": wage["business_id"], "slot": "net_pay"},
        {"business_id": excess["business_id"], "slot": "recovery"},
    ]
    expected = _cash_snapshot(original, bank["object_id"], refs)
    assert expected["obligations"][0]["amount_fen"] == "0"
    assert all(
        item["cashflow_weights"] == [{"category": "employee_payments", "weight": "1"}]
        for item in expected["obligations"]
    )
    assert len(expected["movements"]) == 2
    assert all(
        item["cashflow_parts"] == [{"category": "employee_payments", "amount_fen": "100000"}]
        for item in expected["movements"]
    )
    rebuilt = _replay(bundle, tmp_path / "rebuilt")
    assert rebuilt[2] != original[2]
    assert _cash_snapshot(rebuilt, bank["object_id"], refs) == expected


def test_final_mixed_reimbursement_documents_replay_changed_composition_and_excess_range(tmp_path):
    original = _system(tmp_path / "original")
    bank, employee, supplier = _object("bank_account"), _object("person"), _object("organization")
    source = _statement(
        "合成最终核对：服务费和管理费各1分，个人垫付2分；公司实付3分，多付1分已退回。"
    )
    costs = [
        _expense(source, supplier, amount, category)
        for amount, category in ((1, "service"), (2, "administration"))
    ]
    reimbursement = _reimbursement(
        source,
        employee,
        [(cost, "primary", cost["data"]["amount_fen"], supplier) for cost in costs],
    )
    paid = _payment(source, bank, employee, reimbursement, "reimbursement", 3)
    _apply(
        original,
        paid,
        reimbursement,
        *costs,
        objects=[bank, employee, supplier],
        sources=[source],
    )
    final_cost, final_reimbursement = deepcopy(costs[1]), deepcopy(reimbursement)
    final_cost["data"]["amount_fen"] = "1"
    final_reimbursement["data"]["sources"][1]["amount_fen"] = "1"
    excess = _recovery(source, reimbursement, "reimbursement", employee, 1)
    received = _payment(source, bank, employee, excess, "recovery", 1, incoming=True)
    _apply(
        original,
        mutations=[
            {"op": "create", "expected_seq": 0, "fact": received},
            {"op": "replace", "expected_seq": 1, "fact": final_reimbursement},
            {"op": "create", "expected_seq": 0, "fact": excess},
            {"op": "replace", "expected_seq": 1, "fact": final_cost},
        ],
    )
    bundle = _final_documents(tmp_path / "original/company")
    assert final_cost in bundle["facts"] and costs[1] not in bundle["facts"]
    assert final_reimbursement in bundle["facts"] and reimbursement not in bundle["facts"]
    refs = [
        {"business_id": reimbursement["business_id"], "slot": "reimbursement"},
        {"business_id": excess["business_id"], "slot": "recovery"},
    ]
    expected = _cash_snapshot(original, bank["object_id"], refs)
    assert expected["obligations"][0]["cashflow_weights"] == [
        {"category": "materials_services_payments", "weight": "1"},
        {"category": "other_operating_payments", "weight": "1"},
    ]
    assert expected["obligations"][1]["cashflow_weights"] == [
        {"category": "materials_services_payments", "weight": "1"}
    ]
    by_direction = {item["direction"]: item for item in expected["movements"]}
    assert by_direction["outflow"]["cashflow_parts"] == [
        {"category": "materials_services_payments", "amount_fen": "2"},
        {"category": "other_operating_payments", "amount_fen": "1"},
    ]
    assert by_direction["inflow"]["cashflow_parts"] == [
        {"category": "materials_services_payments", "amount_fen": "1"}
    ]
    rebuilt = _replay(bundle, tmp_path / "rebuilt")
    assert rebuilt[2] != original[2]
    assert _cash_snapshot(rebuilt, bank["object_id"], refs) == expected


def test_final_documents_replay_unknown_mixed_remaining_lineage_without_guessing(tmp_path):
    original = _system(tmp_path / "original")
    bank, first_payer, second_payer, supplier = (
        _object(kind) for kind in ("bank_account", "person", "person", "organization")
    )
    source = _statement("合成混合垫付款3分，先偿还1分，剩余2分转由另一人垫付后偿还。")
    costs = [
        _expense(source, supplier, amount, category)
        for amount, category in ((1, "service"), (2, "administration"))
    ]
    first = _reimbursement(
        source,
        first_payer,
        [(cost, "primary", cost["data"]["amount_fen"], supplier) for cost in costs],
    )
    paid = _payment(source, bank, first_payer, first, "reimbursement", 1)
    second = _reimbursement(source, second_payer, [(first, "reimbursement", 2, first_payer)])
    paid_remaining = _payment(source, bank, second_payer, second, "reimbursement", 2)
    _apply(
        original,
        paid_remaining,
        second,
        paid,
        first,
        *costs,
        objects=[bank, first_payer, second_payer, supplier],
        sources=[source],
    )
    bundle = _final_documents(tmp_path / "original/company")
    refs = [
        {"business_id": first["business_id"], "slot": "reimbursement"},
        {"business_id": second["business_id"], "slot": "reimbursement"},
    ]
    expected = _cash_snapshot(original, bank["object_id"], refs)
    assert "cashflow_weights" in expected["obligations"][0]
    assert "cashflow_weights" not in expected["obligations"][1]
    by_amount = {item["amount_fen"]: item for item in expected["movements"]}
    assert by_amount["1"]["cashflow_parts"] == [
        {"category": "other_operating_payments", "amount_fen": "1"}
    ]
    # A mixed source after a partial payment needs remaining-classification
    # lineage not currently implemented. Missing classification is neither an
    # empty known zero nor permission to copy a guessed share of the source.
    assert "cashflow_parts" not in by_amount["2"]
    rebuilt = _replay(bundle, tmp_path / "rebuilt")
    assert _cash_snapshot(rebuilt, bank["object_id"], refs) == expected


def test_underreported_cash_cannot_partially_change_formal_results_or_documents(
    tmp_path, monkeypatch
):
    system = _system(tmp_path / "original")
    app, storage, company = system
    bank, payer, supplier = _object("bank_account"), _object("person"), _object("organization")
    source = _statement("合成费用更正后服务1分、管理3分，实付5分，多付1分已退回。")
    costs = [
        _expense(source, supplier, amount, category)
        for amount, category in ((1, "service"), (2, "administration"))
    ]
    combined = _reimbursement(
        source,
        payer,
        [(cost, "primary", cost["data"]["amount_fen"], supplier) for cost in costs],
    )
    _apply(system, combined, *costs, objects=[bank, payer, supplier], sources=[source])
    query = {
        "company_id": company,
        "business_ids": [item["business_id"] for item in [combined, *costs]],
        "include": ["facts", "results", "sources"],
    }
    before_businesses = app.accounting_get(query)
    before_documents = _final_documents(tmp_path / "original/company")
    with storage.read_unit(company) as read:
        before_head = read.head
        before_vouchers = app.ledger.query_vouchers(LedgerQuery(), read)
    final_cost, final_combined = deepcopy(costs[1]), deepcopy(combined)
    final_cost["data"]["amount_fen"] = "3"
    final_combined["data"]["sources"][1]["amount_fen"] = "3"
    paid = _payment(source, bank, payer, combined, "reimbursement", 5)
    excess = _recovery(source, combined, "reimbursement", payer, 1)
    received = _payment(source, bank, payer, excess, "recovery", 1, incoming=True)
    call = request(
        company,
        mutations=[
            {"op": "create", "expected_seq": 0, "fact": received},
            {"op": "replace", "expected_seq": 1, "fact": final_combined},
            {"op": "create", "expected_seq": 0, "fact": excess},
            {"op": "replace", "expected_seq": 1, "fact": final_cost},
            {"op": "create", "expected_seq": 0, "fact": paid},
        ],
    )
    actual_settled = app.money.actual_settled_fen
    # Low reporting still produces an in-bounds interval [1,2], but its service
    # category is wrong: the real [4,5] tail belongs to administration. Checking
    # only that the range ends before the formal total cannot detect this fault.
    monkeypatch.setattr(app.money, "actual_settled_fen", lambda source, cutoff, context: 2)
    rejected = app.accounting_apply(call)
    assert rejected.get("completion") == "not_saved", rejected
    assert rejected["error"] == "dependency_conflict", rejected
    assert app.accounting_get(query) == before_businesses
    assert _final_documents(tmp_path / "original/company") == before_documents
    with storage.read_unit(company) as read:
        assert read.head == before_head
        assert app.ledger.query_vouchers(LedgerQuery(), read) == before_vouchers
        assert app.ledger.fund_movements(bank["object_id"], "2026-01", "2026-01", read) == ()
    assert (
        app.accounting_get(
            {
                **query,
                "business_ids": [item["business_id"] for item in (paid, excess, received)],
            }
        )["businesses"]
        == []
    )

    # Exactly the same complete facts succeed once the adapter reports the
    # actual total. A failed unit did not consume its command identity or seqs.
    monkeypatch.setattr(app.money, "actual_settled_fen", actual_settled)
    accepted = app.accounting_apply(call)
    assert accepted.get("completion") == "complete", accepted
    refs = [
        {"business_id": combined["business_id"], "slot": "reimbursement"},
        {"business_id": excess["business_id"], "slot": "recovery"},
    ]
    snapshot = _cash_snapshot(system, bank["object_id"], refs)
    assert snapshot["obligations"][1]["cashflow_weights"] == [
        {"category": "other_operating_payments", "weight": "1"}
    ]
    assert sorted(item["amount_fen"] for item in snapshot["movements"]) == ["1", "5"]
