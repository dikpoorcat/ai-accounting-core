"""Closed payroll, later tax/cash, exact backup and final-fact rebuild agree."""

import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest

from ai_accounting_modular.agent.public import create_agent
from ai_accounting_modular.host.public import create_kernel, create_offline_application
from ai_accounting_modular.ledger.public import AccountBalanceKey, LedgerQuery
from ai_accounting_modular.records.public import open_standard_bundle
from ai_accounting_modular.storage.public import (
    CompanySpec,
    OwnerProfile,
    create_catalog,
    open_database_unit,
)

from ._working_case import Case

MONTHLY_ORACLE = {
    # bank, employee wage, employee reimbursement, withholding tax, current expense
    "2026-09": (2000000, 1000000, 30000, None, 1030000),
    "2026-10": (990000, 0, 0, 20000, 0),
    "2026-11": (970000, 0, 0, 0, 0),
}


@pytest.fixture
def case(tmp_path):
    catalog = tmp_path / "catalog.sqlite3"
    create_catalog(catalog, OwnerProfile(str(uuid4()), "合成负责人", "synthetic-only-hash"))
    kernel = create_kernel(catalog)
    company = str(uuid4())
    kernel.storage.create_company(
        CompanySpec(
            company,
            str(uuid4()),
            "913100000000000001",
            "合成薪酬接续公司",
            "2026-09",
            tmp_path / "company",
            tmp_path / "company-backups",
        )
    )
    try:
        yield Case(kernel, company, str(uuid4()), str(uuid4()))
    finally:
        kernel.close()


def fact(kind, data, source, period, *, actual=None):
    result = {
        "business_id": str(uuid4()),
        "kind": kind,
        "period": period,
        "evidence_ids": [source],
        "data": data,
    }
    if actual:
        result["effective_on"] = actual
    return result


def evidence(text, period, *, source_id=None):
    return {
        "source_id": source_id or str(uuid4()),
        "kind": "owner_statement",
        "text": text,
        "sha256": sha256(text.encode("utf-8")).hexdigest(),
        "archive_month": period,
    }


def apply(agent, case, *facts, **fields):
    payload = case.request(*facts, **fields)
    result = agent.dispatch("accounting_apply", payload)
    assert result["completion"] == "complete", json.dumps(result, ensure_ascii=False, indent=2)
    return payload


def close(agent, company, period):
    checked = agent.dispatch(
        "accounting_period", {"company_id": company, "action": "check", "period": period}
    )
    assert checked["ready"], checked
    result = agent.dispatch(
        "accounting_period",
        {
            "company_id": company,
            "action": "close",
            "period": period,
            "expected_head": checked["head"],
        },
    )
    assert result["state"] == "closed" and result["document_state"] == "ready", result


def reconcile(agent, case, bank, source, period, opening, closing, expected):
    with case.kernel.storage.read_unit(case.company) as read:
        (movement,) = case.kernel.ledger.fund_movements(bank, period, period, read)
    actual, direction, amount = expected
    value = movement.value
    assert (value["actual_on"], value["direction"], value["amount_fen"]) == expected
    row_id = str(uuid4())
    rows = [
        {
            "row_id": row_id,
            "source": {"source_id": source},
            "actual_on": actual,
            "direction": direction,
            "amount_fen": amount,
        }
    ]
    matches = [{"statement_row_ids": [row_id], "fund_movement_ids": [value["movement_id"]]}]
    statement = fact(
        "money.bank_statement",
        {"funds_account_id": bank, "opening_fen": opening, "closing_fen": closing, "rows": rows},
        source,
        period,
    )
    matching = fact(
        "money.reconciliation",
        {"statement_id": statement["business_id"], "matches": matches},
        source,
        period,
    )
    apply(agent, case, statement, matching)


def snapshot(app, company, period, read):
    return app.ledger.financial_snapshot(
        {"company_id": company, "period_from": period, "period_to": period, "view": "as_posted"},
        read,
    )


def positions(app, read, period, references):
    return {
        (item.definition.value["business_id"], item.definition.value["slot"]): item.amounts
        for item in app.ledger.get_obligation_positions(period, read, references=references)
    }


def assert_month(
    app,
    company,
    read,
    period,
    *,
    bank,
    wage,
    oil,
    tax,
):
    bank_fen, wage_fen, oil_fen, tax_fen, expense_fen = MONTHLY_ORACLE[period]
    refs = [
        {"business_id": wage, "slot": "net_pay"},
        {"business_id": oil, "slot": "primary"},
        {"business_id": tax, "slot": "tax"},
    ]
    amounts = positions(app, read, period, refs)
    assert amounts[wage, "net_pay"].remaining_fen == wage_fen
    assert amounts[oil, "primary"].remaining_fen == oil_fen
    if tax_fen is None:
        assert (tax, "tax") not in amounts
    else:
        assert amounts[tax, "tax"].remaining_fen == tax_fen
    balances = app.ledger.account_balances(
        [AccountBalanceKey("1002", bank), AccountBalanceKey("221101")], period, read
    )
    assert balances[0].ending == {"state": "known", "amount_fen": str(bank_fen)}
    assert balances[1].ending == {"state": "known", "amount_fen": str(-wage_fen)}
    report = snapshot(app, company, period, read)
    assert report["profit_statement"]["30"]["current"]["amount_fen"] == str(-expense_fen)
    assert report["pre_tax"]["profit_before_tax"]["amount_fen"] == "-1030000"
    assert report["balance_sheet"]["1"]["ending"]["amount_fen"] == str(bank_fen)
    return report


def test_closed_salary_later_tax_payments_restore_and_rebuild_preserve_months(
    case, tmp_path, monkeypatch
):
    agent = create_agent(case.app)
    employee, owner, bank, authority = (str(uuid4()) for _ in range(4))
    september = evidence(
        "合成公司九月新设、期初全零，九月股东投入20000元。"
        "张青九月行政工资10000元尚未发放，没有实际扣税资料；"
        "本合成案例明确不参与社保和公积金。张青为公司外勤垫付油费300元，"
        "公司承担九月行政费用，尚未报销，原发票遗失，不作进项抵扣。",
        "2026-09",
        source_id=case.source_id,
    )
    source = september["source_id"]
    opening = fact(
        "opening.package", {"basis": "newly_established", "members": []}, source, "2026-09"
    )
    employment = fact(
        "people.employment",
        {
            "employee_id": employee,
            "effective_from": "2026-09",
            "social_insurance_participating": False,
            "housing_fund_participating": False,
            "contribution_shortfall": "reject",
        },
        source,
        "2026-09",
    )
    relationship = fact(
        "people.relationship",
        {"person_id": employee, "segments": [{"state": "regular", "effective_from": "2026-09"}]},
        source,
        "2026-09",
    )
    wage = fact(
        "people.remuneration",
        {
            "type": "wage",
            "person_id": employee,
            "gross_fen": "1000000",
            "expense_area": "administration",
            "employment_id": employment["business_id"],
        },
        source,
        "2026-09",
    )
    oil = fact(
        "operations.expense",
        {
            "creditor_id": employee,
            "creditor_kind": "employee",
            "amount_fen": "30000",
            "expense_class": "administration",
        },
        source,
        "2026-09",
    )
    funding = fact(
        "financing.owner_funding",
        {
            "owner_id": owner,
            "funding_kind": "capital",
            "amount_fen": "2000000",
            "funds_account_id": bank,
        },
        source,
        "2026-09",
        actual="2026-09-01",
    )
    objects = [
        {"object_id": object_id, "kind": kind, "name": name, "expected_seq": 0, "data": data}
        for object_id, kind, name, data in (
            (employee, "person", "合成员工张青", {}),
            (owner, "person", "合成出资人", {}),
            (authority, "organization", "合成税务机关", {}),
            (
                bank,
                "bank_account",
                "合成工资账户",
                {
                    "bank_name": "合成银行",
                    "account_number": "synthetic-payroll-only",
                    "currency": "CNY",
                    "owner": "company",
                    "opened_period": "2026-09",
                    "zero_at_opening": True,
                },
            ),
        )
    ]
    initial = apply(
        agent,
        case,
        opening,
        funding,
        relationship,
        employment,
        wage,
        oil,
        objects=objects,
        sources=[september],
    )
    assert agent.dispatch("accounting_apply", initial)["accounting"] == "unchanged"
    reconcile(
        agent, case, bank, source, "2026-09", "0", "2000000", ("2026-09-01", "inflow", "2000000")
    )
    close(agent, case.company, "2026-09")
    frozen_wage = case.get(wage["business_id"])["businesses"][0]
    assert frozen_wage["fact"] == wage
    frozen_result = frozen_wage["results"][0]
    assert frozen_result["target"]["values"]["tax_fen"] == "0"
    assert "tax_period" not in frozen_result["target"]["values"]

    october = evidence(
        "合成实际工资支付资料：10月15日从张青九月工资中扣个税200元，税期为10月；"
        "实付工资9800元，同时偿还油费300元，银行合计付张青10100元。",
        "2026-10",
    )
    tax = fact(
        "people.remuneration_tax",
        {
            "remuneration_id": wage["business_id"],
            "tax_period": "2026-10",
            "basis_type": "actual_withholding",
            "components": [
                {
                    "tax_fen": "20000",
                    "source": {"source_id": october["source_id"], "location": "10月实际扣税200元"},
                }
            ],
        },
        october["source_id"],
        "2026-10",
        actual="2026-10-15",
    )
    keys = {
        "bank": bank,
        "wage": wage["business_id"],
        "oil": oil["business_id"],
        "tax": tax["business_id"],
    }
    with case.kernel.storage.read_unit(case.company) as read:
        frozen_report = assert_month(case.app, case.company, read, "2026-09", **keys)

    payment = fact(
        "money.movement",
        {
            "type": "settlement",
            "funds_account_id": bank,
            "direction": "outflow",
            "amount_fen": "1010000",
            "payment_method": "individual",
            "counterparty_id": employee,
            "allocations": [
                {
                    "source": {"business_id": wage["business_id"], "slot": "net_pay"},
                    "amount_fen": "980000",
                },
                {
                    "source": {"business_id": oil["business_id"], "slot": "primary"},
                    "amount_fen": "30000",
                },
            ],
        },
        october["source_id"],
        "2026-10",
        actual="2026-10-15",
    )
    invalid_payment = deepcopy(payment)
    invalid_payment["data"]["amount_fen"] = "1010001"
    invalid_payment["data"]["allocations"][0]["amount_fen"] = "980001"
    failed = agent.dispatch(
        "accounting_apply", case.request(tax, invalid_payment, sources=[october])
    )
    assert failed["completion"] == "not_saved", failed
    assert case.get(tax["business_id"], payment["business_id"])["businesses"] == []
    paid = apply(agent, case, tax, payment, sources=[october])
    assert agent.dispatch("accounting_apply", paid)["accounting"] == "unchanged"
    reconcile(
        agent,
        case,
        bank,
        october["source_id"],
        "2026-10",
        "2000000",
        "990000",
        ("2026-10-15", "outflow", "1010000"),
    )
    close(agent, case.company, "2026-10")
    with case.kernel.storage.read_unit(case.company) as read:
        october_report = assert_month(case.app, case.company, read, "2026-10", **keys)
        october_entries = case.kernel.ledger.query_vouchers(
            LedgerQuery(period_from="2026-10", period_to="2026-10"), read
        ).items
        assert len(october_entries) == 2
        assert {item["role"] for item in october_entries} == {"original"}
        assert all(
            not any(line["account_code"].startswith(("5", "6")) for line in item["lines"])
            for item in october_entries
        )

    november = evidence(
        "合成银行与缴税回单：11月15日公司向税务机关缴纳10月代扣张青个税200元。", "2026-11"
    )
    remitted = fact(
        "money.movement",
        {
            "type": "settlement",
            "funds_account_id": bank,
            "direction": "outflow",
            "amount_fen": "20000",
            "payment_method": "individual",
            "counterparty_id": authority,
            "allocations": [
                {
                    "source": {"business_id": tax["business_id"], "slot": "tax"},
                    "amount_fen": "20000",
                }
            ],
        },
        november["source_id"],
        "2026-11",
        actual="2026-11-15",
    )
    remittance = apply(agent, case, remitted, sources=[november])
    assert agent.dispatch("accounting_apply", remittance)["accounting"] == "unchanged"
    reconcile(
        agent,
        case,
        bank,
        november["source_id"],
        "2026-11",
        "990000",
        "970000",
        ("2026-11-15", "outflow", "20000"),
    )
    close(agent, case.company, "2026-11")
    with case.kernel.storage.read_unit(case.company) as read:
        final_report = assert_month(case.app, case.company, read, "2026-11", **keys)
        assert case.get(wage["business_id"])["businesses"][0] == frozen_wage
        again = snapshot(case.app, case.company, "2026-09", read)
        for table in ("balance_sheet", "profit_statement", "cash_flow_statement"):
            assert again[table] == frozen_report[table]
        movements = case.kernel.ledger.fund_movements(bank, "2026-01", "2026-11", read)
        assert sorted(
            (item.value["direction"], item.value["amount_fen"]) for item in movements
        ) == [("inflow", "2000000"), ("outflow", "1010000"), ("outflow", "20000")]
        metadata = case.kernel.storage.company_metadata(read)

    # The public report and dashboard share the same formal quarter, not AI totals.
    quarter = agent.dispatch(
        "accounting_report", {"company_id": case.company, "year": 2026, "quarter": 3}
    )
    assert quarter["profit_statement"]["32"]["current"]["amount_fen"] == "-1030000"
    for page in ("/api/dashboard/employees", "/api/dashboard/funds"):
        query = {"company_id": case.company, "period": "2026-10"}
        with case.kernel.dashboard.get(page, query) as response:
            assert response.status == 200 and response.body["state"] in {"ready", "preparing"}, (
                response.body
            )
        case.kernel.reading.run_pending(case.company)
        with case.kernel.dashboard.get(page, query) as response:
            assert response.body["state"] == "ready", response.body
            data = response.body["response"]["data"]
            if page.endswith("employees"):
                assert data["outstanding_remuneration_fen"] == "0"
                assert data["employees"]["individual_income_tax_fen"] == "20000"
                assert data["employees"]["ledger_cost_fen"] == "0"
            else:
                assert data["outflow_fen"] == "1010000"

    exported = agent.dispatch(
        "accounting_documents", {"company_id": case.company, "action": "export"}
    )
    bundle = open_standard_bundle(exported["manifest_path"])
    facts = {item["business_id"]: item for item in bundle.facts}
    assert facts[wage["business_id"]] == wage
    assert facts[tax["business_id"]] == tax
    assert facts[payment["business_id"]] == payment
    assert facts[remitted["business_id"]] == remitted
    assert bundle.working_period_to == "2026-11"
    source_originals = {
        item.relative_path: sha256(
            (metadata.company_root / item.relative_path).read_bytes()
        ).hexdigest()
        for item in bundle.files
    }
    with case.kernel.storage.read_unit(case.company) as read:
        frozen_results = case.app.ledger.get_results(
            [wage["business_id"], tax["business_id"]], read
        )
        tax_result = next(
            item
            for item in frozen_results
            if item["reference"]["business_id"] == tax["business_id"]
        )
        assert any(
            item["type"] == "remuneration" for item in tax_result["basis"]["adopted_quantities"]
        )
    frozen_reports = {"2026-09": frozen_report, "2026-10": october_report, "2026-11": final_report}
    backup = agent.dispatch(
        "accounting_backup",
        {"company_id": case.company, "expected_database_id": metadata.database_id},
    )
    completed = case.kernel.files.run(case.company, backup["task_id"])
    assert completed["state"] == "succeeded", completed
    receipt = agent.dispatch(
        "accounting_jobs", {"company_id": case.company, "task_id": backup["task_id"]}
    )
    assert receipt["state"] == "succeeded" and receipt["path"] == completed["path"], receipt
    package = Path(receipt["path"])
    assert package == metadata.backup_directory / f"{metadata.credit_code}.finance-company.zip"
    assert sha256(package.read_bytes()).hexdigest() == receipt["sha256"]
    case.kernel.close()

    def source_unavailable(*args, **kwargs):
        raise AssertionError("离线恢复或重建不可读取源公司库")

    monkeypatch.setattr(case.kernel.storage, "read_unit", source_unavailable)
    offline = create_offline_application()
    restored_root = tmp_path / "restored-payroll-lifecycle"
    restore = {
        "action": "restore",
        "mode": "check",
        "source_path": str(package),
        "new_target_directory": str(restored_root),
    }
    checked = offline.accounting_isolated(restore)
    assert checked["state"] == "checked" and checked["source_digest"] == receipt["sha256"], checked
    assert not restored_root.exists()
    restored = offline.accounting_isolated(restore | {"mode": "run"})
    assert restored["state"] == "created", restored
    assert (restored["company_id"], restored["database_id"]) == (
        metadata.company_id,
        metadata.database_id,
    )
    with open_database_unit(
        restored_root / "runtime/company.sqlite3",
        repositories=offline.repositories,
        company_root=restored_root,
    ) as read:
        assert offline.ledger.closed_through(read) == "2026-11"
        assert (
            offline.ledger.get_results([wage["business_id"], tax["business_id"]], read)
            == frozen_results
        )
        for period, original_report in frozen_reports.items():
            restored_report = assert_month(offline, read.company_id, read, period, **keys)
            for table in ("balance_sheet", "profit_statement", "cash_flow_statement"):
                assert restored_report[table] == original_report[table]

    target = tmp_path / "rebuilt-payroll-lifecycle"
    request = {
        "action": "rebuild",
        "mode": "check",
        "source_path": str(bundle.manifest_path),
        "new_target_directory": str(target),
    }
    assert offline.accounting_isolated(request)["state"] == "checked"
    assert not target.exists()
    rebuilt = offline.accounting_isolated(request | {"mode": "run"})
    assert rebuilt["state"] == "created", rebuilt
    with open_database_unit(
        target / "runtime/company.sqlite3", repositories=offline.repositories, company_root=target
    ) as read:
        assert offline.ledger.closed_through(read) is None
        assert_month(offline, read.company_id, read, "2026-09", **keys)
        assert_month(offline, read.company_id, read, "2026-10", **keys)
        rebuilt_report = assert_month(offline, read.company_id, read, "2026-11", **keys)
        for table in ("balance_sheet", "profit_statement", "cash_flow_statement"):
            assert rebuilt_report[table] == final_report[table]
        assert {
            item["role"] for item in offline.ledger.query_vouchers(LedgerQuery(), read).items
        } == {"original"}
    assert source_originals == {
        item.relative_path: sha256(
            (metadata.company_root / item.relative_path).read_bytes()
        ).hexdigest()
        for item in bundle.files
    }
