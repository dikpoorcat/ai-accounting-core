"""Known accounting contradictions and arithmetic limits cross the real application."""

from copy import deepcopy
from hashlib import sha256
from uuid import uuid4

import pytest

from ai_accounting_modular.application.public import create_application
from ai_accounting_modular.application.public import repository_factory as application_repository
from ai_accounting_modular.contracts.public import MAX_FEN, validate_contract
from ai_accounting_modular.domains.operations.public import create_operations
from ai_accounting_modular.domains.operations.public import (
    repository_factory as operations_repository,
)
from ai_accounting_modular.identity.public import create_identity
from ai_accounting_modular.identity.public import repository_factory as identity_repository
from ai_accounting_modular.ledger.public import create_ledger
from ai_accounting_modular.ledger.public import repository_factory as ledger_repository
from ai_accounting_modular.records.public import create_records
from ai_accounting_modular.records.public import repository_factory as records_repository
from ai_accounting_modular.storage.public import CompanySpec, OwnerProfile, Storage, create_catalog


@pytest.fixture
def application(tmp_path):
    catalog = tmp_path / "catalog.sqlite3"
    create_catalog(catalog, OwnerProfile(str(uuid4()), "合成老板", "synthetic-password-hash"))
    storage = Storage(
        catalog,
        repositories=[
            application_repository(),
            identity_repository(),
            operations_repository(),
            ledger_repository(),
            records_repository(),
        ],
    )
    company = str(uuid4())
    storage.create_company(
        CompanySpec(
            company,
            str(uuid4()),
            "913100000000000001",
            "合成经营边界公司",
            "2026-01",
            tmp_path / "company",
        )
    )
    records = create_records(root_for=lambda read: storage.company_metadata(read).company_root)
    app = create_application(
        storage=storage,
        identity=create_identity(),
        ledger=create_ledger(),
        records=records,
        domains=[create_operations()],
    )
    return app, company


def evidence():
    text = "合成资料：核对预收税点、转债金额及事务回滚。"
    return {
        "source_id": str(uuid4()),
        "kind": "owner_statement",
        "text": text,
        "sha256": sha256(text.encode("utf-8")).hexdigest(),
        "archive_month": "2026-01",
    }


def obj(kind="organization"):
    return {
        "object_id": str(uuid4()),
        "kind": kind,
        "name": "合成业务对象",
        "expected_seq": 0,
        "data": {},
    }


def fact(kind, source_id, data):
    return {
        "business_id": str(uuid4()),
        "kind": "operations." + kind,
        "period": "2026-01",
        "evidence_ids": [source_id],
        "data": data,
    }


def expense(party_id, source_id, amount="100000"):
    return fact(
        "expense",
        source_id,
        {
            "creditor_id": party_id,
            "creditor_kind": "supplier",
            "amount_fen": amount,
            "expense_class": "administration",
        },
    )


def request(company, facts, *, objects=(), sources=()):
    return {
        "contract": "ai-accounting-modular/1",
        "company_id": company,
        "command_id": str(uuid4()),
        "mutations": [{"op": "create", "expected_seq": 0, "fact": value} for value in facts],
        "objects": list(objects),
        "sources": list(sources),
    }


def get(app, company, facts):
    return app.accounting_get(
        {
            "company_id": company,
            "business_ids": [value["business_id"] for value in facts],
            "include": ["facts", "results", "sources"],
        }
    )


def vat():
    return {
        "policy_id": str(uuid4()),
        "exemption_eligible": False,
        "invoice_kind": "ordinary",
        "waive_exemption": False,
        "waive_rate_reduction": False,
    }


@pytest.mark.parametrize(
    "status,point,with_vat,error",
    [
        ("not_due", "2026-01", True, "invalid_input"),
        ("not_due", "2026-01", False, "needs_information"),
        ("not_due", "2025-12", False, "needs_information"),
        ("not_due", "2026-02", True, "invalid_input"),
        ("not_due", "derived", False, "dependency_conflict"),
        ("not_due", None, True, "invalid_input"),
        ("due", "2026-02", True, "invalid_input"),
        ("due", "2025-12", True, "dependency_conflict"),
        ("due", "derived", True, "dependency_conflict"),
    ],
)
def test_advance_checks_every_supplied_tax_point_and_rolls_back_whole_submission(
    application,
    status,
    point,
    with_vat,
    error,
):
    app, company = application
    party, source = obj(), evidence()
    data = {
        "side": "customer",
        "counterparty_id": party["object_id"],
        "amount_fen": "103000",
        "tax_status": status,
    }
    if point == "derived":
        data["tax_point"] = {"type": "derived", "tax_point_id": str(uuid4())}
    elif point is not None:
        data["tax_point"] = {
            "type": "explicit",
            "tax_period": point,
            "source": {"source_id": source["source_id"], "location": "合成税点说明"},
        }
    if with_vat:
        data["vat"] = vat()
    values = [
        expense(party["object_id"], source["source_id"]),
        fact("advance", source["source_id"], data),
    ]
    command = request(company, values, objects=[party], sources=[source])
    validate_contract("core.schema.json", "ApplyRequest", command)
    result = app.accounting_apply(command)
    assert result["error"] == error, result
    assert result["completion"] == "not_saved" and result["changes"] == "none"
    assert get(app, company, values)["businesses"] == []
    assert get(app, company, values)["head"] == 0
    with app.storage.read_unit(company) as read:
        assert app.identity.get_batch([party["object_id"]], read) == ()
        assert app.records.resolve_sources([source["source_id"]], read) == ()
    if point not in {None, "derived"} and not with_vat:
        assert result["fact_issues"][0]["field"] == "/data/vat"
    if error == "invalid_input":
        assert result["fact_issues"][0]["field"] == "/data/vat/policy_id"
        assert result["fact_issues"][0]["code"] == "reference_not_found"
    if point == "derived":
        assert result["fact_issues"][0]["field"] == "/data/tax_point/tax_point_id"
        assert result["fact_issues"][0]["code"] == "result_not_ready"
    if with_vat and point == "2025-12":
        assert result["fact_issues"][0]["field"] == "/data/tax_point"
        assert result["fact_issues"][0]["code"] == "tax_source_required"


def test_not_due_advance_without_tax_point_does_not_require_invented_policy_or_date(application):
    app, company = application
    party, source = obj(), evidence()
    current = fact(
        "advance",
        source["source_id"],
        {
            "side": "customer",
            "counterparty_id": party["object_id"],
            "amount_fen": "103000",
            "tax_status": "not_due",
        },
    )
    result = app.accounting_apply(request(company, [current], objects=[party], sources=[source]))
    assert result["completion"] == "complete", result
    saved = get(app, company, [current])["businesses"][0]
    assert saved["fact"] == current
    assert saved["results"][0]["target"]["values"]["vat_fen"] == "0"
    assert "effective_on" not in saved["results"][0]["basis"]


def original_debts(app, company, amounts):
    supplier, payer, source = obj(), obj("person"), evidence()
    originals = [
        expense(supplier["object_id"], source["source_id"], str(value)) for value in amounts
    ]
    result = app.accounting_apply(
        request(company, originals, objects=[supplier, payer], sources=[source])
    )
    assert result["completion"] == "complete", result
    return originals, supplier, payer, source


def reimbursement(originals, supplier, payer, source_id):
    return fact(
        "reimbursement",
        source_id,
        {
            "type": "acceptance",
            "payer_id": payer["object_id"],
            "payer_kind": "owner",
            "sources": [
                {
                    "source": {"business_id": value["business_id"], "slot": "primary"},
                    "amount_fen": value["data"]["amount_fen"],
                    "recipient_id": supplier["object_id"],
                }
                for value in originals
            ],
        },
    )


def test_reimbursement_sum_overflow_returns_structured_failure_and_preserves_everything(
    application,
):
    app, company = application
    originals, supplier, payer, _ = original_debts(app, company, [MAX_FEN, 1])
    before = deepcopy(get(app, company, originals))
    new_supplier, source = obj(), evidence()
    pending = [
        expense(new_supplier["object_id"], source["source_id"], "100"),
        reimbursement(originals, supplier, payer, source["source_id"]),
    ]
    command = request(company, pending, objects=[new_supplier], sources=[source])
    validate_contract("core.schema.json", "ApplyRequest", command)
    result = app.accounting_apply(command)
    assert result["error"] == "invalid_input", result
    assert result["completion"] == "not_saved" and result["changes"] == "none"
    assert get(app, company, originals) == before
    assert get(app, company, pending)["businesses"] == []
    with app.storage.read_unit(company) as read:
        assert app.identity.get_batch([new_supplier["object_id"]], read) == ()
        assert app.records.resolve_sources([source["source_id"]], read) == ()


def test_reimbursement_exact_maximum_sum_is_valid(application):
    app, company = application
    originals, supplier, payer, source = original_debts(app, company, [MAX_FEN - 1, 1])
    current = reimbursement(originals, supplier, payer, source["source_id"])
    result = app.accounting_apply(request(company, [current]))
    assert result["completion"] == "complete", result
    saved = get(app, company, [current])["businesses"][0]
    assert saved["results"][0]["target"]["values"]["amount_fen"] == str(MAX_FEN)
    with app.storage.read_unit(company) as read:
        obligations = app.ledger.resolve(
            [{"business_id": value["business_id"], "slot": "primary"} for value in originals],
            "obligation",
            read,
        )
        assert [value["balance"]["remaining_fen"] for value in obligations] == ["0", "0"]


def test_not_due_optional_vat_cannot_reference_an_existing_expense_as_policy(application):
    app, company = application
    originals, supplier, _, source = original_debts(app, company, [100000])
    before = deepcopy(get(app, company, originals))
    policy_fields = vat()
    policy_fields["policy_id"] = originals[0]["business_id"]
    current = fact(
        "advance",
        source["source_id"],
        {
            "side": "customer",
            "counterparty_id": supplier["object_id"],
            "amount_fen": "103000",
            "tax_status": "not_due",
            "vat": policy_fields,
        },
    )
    result = app.accounting_apply(request(company, [current]))
    assert result["error"] == "invalid_input", result
    assert result["fact_issues"][0]["field"] == "/data/vat/policy_id"
    assert result["changes"] == "none"
    assert get(app, company, originals) == before
    assert get(app, company, [current])["businesses"] == []
