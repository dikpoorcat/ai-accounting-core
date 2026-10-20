"""Real people + application + identity + ledger + records on temporary companies."""

import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest

from ai_accounting_modular.application.public import create_application
from ai_accounting_modular.application.public import repository_factory as app_repository
from ai_accounting_modular.domains.people.public import create_people
from ai_accounting_modular.domains.people.public import repository_factory as people_repository
from ai_accounting_modular.identity.public import create_identity
from ai_accounting_modular.identity.public import repository_factory as identity_repository
from ai_accounting_modular.ledger.public import create_ledger
from ai_accounting_modular.ledger.public import repository_factory as ledger_repository
from ai_accounting_modular.money.public import MoneyQuery, create_money
from ai_accounting_modular.money.public import repository_factory as money_repository
from ai_accounting_modular.records.public import create_records
from ai_accounting_modular.records.public import repository_factory as records_repository
from ai_accounting_modular.storage.public import CompanySpec, OwnerProfile, Storage, create_catalog

from .test_people import ACTUAL, EMPLOYMENT, PERSON, SOURCE, case, no_contributions


def _assemble(tmp_path, *, with_money=False):
    catalog = tmp_path / "catalog.sqlite3"
    create_catalog(catalog, OwnerProfile(str(uuid4()), "合成老板", "synthetic-password-hash"))
    repositories = [
        app_repository(),
        identity_repository(),
        ledger_repository(),
        records_repository(),
        people_repository(),
    ]
    if with_money:
        repositories.append(money_repository())
    storage = Storage(catalog, repositories=repositories)
    company_id = str(uuid4())
    storage.create_company(
        CompanySpec(
            company_id,
            str(uuid4()),
            "913100000000000001",
            "合成薪酬公司",
            "2026-01",
            tmp_path / "company",
        )
    )
    identity, ledger = create_identity(), create_ledger()
    money = create_money(identity=identity, ledger=ledger) if with_money else None
    app = create_application(
        storage=storage,
        identity=identity,
        ledger=ledger,
        records=create_records(root_for=lambda read: storage.company_metadata(read).company_root),
        domains=[create_people(), money] if money else [create_people()],
        **({"money": money} if money else {}),
    )
    return app, company_id


@pytest.fixture
def assembled(tmp_path):
    return _assemble(tmp_path)


@pytest.fixture
def assembled_with_money(tmp_path):
    return _assemble(tmp_path, with_money=True)


def request(company_id, *facts):
    text = "合成工资、实缴和最终扣税资料；仅用于软件验收，不是真实公司资料。"
    roles = {}
    for fact in facts:
        data = fact["data"]
        for field in ("person_id", "employee_id"):
            if field in data:
                roles[data[field]] = "person"
        if data.get("destination", {}).get("type") == "project":
            roles[data["destination"]["project_id"]] = "project"
    return {
        "contract": "ai-accounting-modular/1",
        "company_id": company_id,
        "command_id": str(uuid4()),
        "objects": [
            {
                "object_id": object_id,
                "kind": kind,
                "name": "合成人员" if kind == "person" else "合成项目",
                "expected_seq": 0,
                "data": {},
            }
            for object_id, kind in roles.items()
        ],
        "sources": [
            {
                "source_id": SOURCE,
                "kind": "owner_statement",
                "text": text,
                "sha256": sha256(text.encode()).hexdigest(),
                "archive_month": "2026-01",
            }
        ],
        "mutations": [
            {"op": "create", "expected_seq": 0, "fact": deepcopy(fact)} for fact in facts
        ],
    }


def get(app, company_id, *ids):
    return app.accounting_get(
        {
            "company_id": company_id,
            "business_ids": list(ids),
            "include": ["facts", "results", "sources"],
        }
    )


def replacement(company_id, fact, *, seq=1):
    return {
        "contract": "ai-accounting-modular/1",
        "company_id": company_id,
        "command_id": str(uuid4()),
        "mutations": [{"op": "replace", "expected_seq": seq, "fact": fact}],
    }


def test_real_wage_same_unit_sources_and_open_correction_preserve_number(assembled):
    app, company_id = assembled
    wage = case("B17")
    submitted = request(company_id, wage, case("B16"), case("B15"), case("B14"))
    created = app.accounting_apply(submitted)
    assert created["completion"] == "complete", created
    original = get(app, company_id, wage["business_id"])["businesses"][0]
    assert original["results"][0]["target"]["values"]["net_fen"] == "830000"
    original_ref = original["results"][0]["reference"]
    assert original_ref["voucher_number"] == 1
    wage["data"]["gross_fen"] = "1100000"
    changed = app.accounting_apply(replacement(company_id, wage))
    assert changed["completion"] == "complete", changed
    current = get(app, company_id, wage["business_id"])["businesses"][0]
    assert current["results"][0]["target"]["values"]["net_fen"] == "930000"
    assert current["results"][0]["reference"]["voucher_number"] == original_ref["voucher_number"]
    assert current["results"][0]["reference"]["result_id"] == original_ref["result_id"]
    assert len(current["results"][0]["vouchers"]) == 1
    assert "830000" not in json.dumps(current)
    assert app.accounting_apply(submitted)["error"] == "conflict"
    assert app.accounting_apply(replacement(company_id, wage, seq=1))["accounting"] == "unchanged"


def test_same_wage_natural_key_rejects_duplicate_but_bonuses_remain_independent(assembled):
    app, company_id = assembled
    wage = case("B17Z")
    other = deepcopy(wage)
    other["business_id"] = str(uuid4())
    result = app.accounting_apply(request(company_id, no_contributions(), wage, other))
    assert result["completion"] == "not_saved", result
    assert get(app, company_id, wage["business_id"], other["business_id"])["businesses"] == []
    bonus = case("B17B")
    second = deepcopy(bonus)
    second["business_id"] = str(uuid4())
    result = app.accounting_apply(request(company_id, bonus, second))
    assert result["completion"] == "complete", result
    assert len(get(app, company_id, bonus["business_id"], second["business_id"])["businesses"]) == 2


def test_actual_assessment_change_recalculates_dependent_wage_without_second_cost(assembled):
    app, company_id = assembled
    wage = case("B17A")
    assert (
        app.accounting_apply(request(company_id, case("B14"), case("B16"), wage))["completion"]
        == "complete"
    )
    actual = case("B16")
    actual["data"]["items"][0]["employee_fen"] = "90000"
    actual["data"]["items"][0]["employer_fen"] = "180000"
    result = app.accounting_apply(replacement(company_id, actual))
    assert result["completion"] == "complete", result
    state = get(app, company_id, wage["business_id"])["businesses"][0]
    assert state["state"]["seq"] == 1
    values = state["results"][0]["target"]["values"]
    assert values["net_fen"] == "840000"
    assert values["cost_fen"] == "1230000"
    assert len(state["results"][0]["vouchers"]) == 1


def test_unused_estimation_base_change_keeps_adopted_actual_wage_and_voucher(assembled):
    app, company_id = assembled
    wage = case("B17A")
    created = app.accounting_apply(request(company_id, case("B14"), case("B16"), wage))
    assert created["completion"] == "complete", created
    before = get(app, company_id, wage["business_id"])["businesses"][0]["results"][0]
    employment = case("B14")
    employment["data"]["social_insurance_base_fen"] = "2000000"
    employment["data"]["housing_fund_base_fen"] = "2000000"
    result = app.accounting_apply(replacement(company_id, employment))
    assert result["completion"] == "complete", result
    after = get(app, company_id, wage["business_id"])["businesses"][0]["results"][0]
    assert after["target"] == before["target"]
    for original, current in zip(before["vouchers"], after["vouchers"], strict=True):
        for field in ("voucher_id", "number", "period", "role", "summary", "lines"):
            assert current[field] == original[field]
    assert after["reference"]["result_id"] == before["reference"]["result_id"]
    assert after["basis"]["adopted_quantities"] == before["basis"]["adopted_quantities"]


def test_missing_tax_source_rolls_back_sources_objects_and_other_business(assembled):
    app, company_id = assembled
    wage = case("B17Z")
    missing_id = str(uuid4())
    wage["data"]["tax_basis"]["source"]["source_id"] = missing_id
    result = app.accounting_apply(request(company_id, no_contributions(), wage))
    assert result["completion"] == "not_saved", result
    assert result["error"] == "needs_information", result
    with app.storage.read_unit(company_id) as read:
        assert app.identity.get_batch([PERSON], read) == ()
        assert app.ledger.get_results([wage["business_id"], EMPLOYMENT], read) == ()
        assert app.records.resolve_sources([SOURCE], read) == ()


def test_labor_reclassification_and_standard_documents_keep_only_current_fact(assembled):
    app, company_id = assembled
    labor = case("B17L")
    result = app.accounting_apply(request(company_id, labor))
    assert result["completion"] == "complete", result
    first = get(app, company_id, labor["business_id"])["businesses"][0]
    labor["data"].update(
        withholding_state="deduction_established", tax_basis=case("B17B")["data"]["tax_basis"]
    )
    result = app.accounting_apply(replacement(company_id, labor))
    assert result["completion"] == "complete", result
    current = get(app, company_id, labor["business_id"])["businesses"][0]
    assert (
        current["results"][0]["reference"]["voucher_number"]
        == first["results"][0]["reference"]["voucher_number"]
    )
    assert current["results"][0]["target"]["values"]["cost_fen"] == "100000"
    with app.storage.read_unit(company_id) as read:
        obligations = app.ledger.resolve(
            [
                {"business_id": labor["business_id"], "slot": "net_pay"},
                {"business_id": labor["business_id"], "slot": "tax"},
            ],
            "obligation",
            read,
        )
        assert [item["obligation"]["amount_fen"] for item in obligations] == ["90000", "10000"]
    status = app.accounting_documents({"company_id": company_id, "action": "export"})
    manifest_path = Path(status["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    exported = []
    for shard in manifest["shards"]:
        part = json.loads((manifest_path.parent.parent / shard["path"]).read_text(encoding="utf-8"))
        exported.extend(part.get("facts", []))
    assert exported == [labor]


def test_open_misclassification_can_replace_wage_with_labor_under_same_identity(assembled):
    app, company_id = assembled
    wage = case("B17Z")
    assert (
        app.accounting_apply(request(company_id, no_contributions(), wage))["completion"]
        == "complete"
    )
    labor = case("B17L")
    labor["business_id"] = wage["business_id"]
    labor["data"]["person_id"] = PERSON
    result = app.accounting_apply(replacement(company_id, labor))
    assert result["completion"] == "complete", result
    current = get(app, company_id, wage["business_id"])["businesses"][0]
    assert current["fact"] == labor
    assert current["results"][0]["target"]["values"]["remuneration_type"] == "labor"
    assert [item["account_code"] for item in current["results"][0]["vouchers"][0]["lines"]] == [
        "540104",
        "224104",
    ]


def _seed_paid_wage(system):
    app, company_id = system
    wage = case("B17Z")
    initial = request(company_id, no_contributions(), wage)
    bank = str(uuid4())
    initial["objects"].append(
        {
            "object_id": bank,
            "kind": "bank_account",
            "name": "合成银行",
            "expected_seq": 0,
            "data": {
                "bank_name": "合成银行",
                "account_number": "synthetic-only",
                "currency": "CNY",
                "owner": "company",
            },
        }
    )
    paid = {
        "business_id": str(uuid4()),
        "kind": "money.movement",
        "period": "2026-01",
        "effective_on": "2026-01-20",
        "evidence_ids": [SOURCE],
        "data": {
            "type": "settlement",
            "funds_account_id": bank,
            "direction": "outflow",
            "amount_fen": "100000",
            "payment_method": "individual",
            "counterparty_id": PERSON,
            "allocations": [
                {
                    "source": {"business_id": wage["business_id"], "slot": "net_pay"},
                    "amount_fen": "100000",
                }
            ],
        },
    }
    initial["mutations"].append({"op": "create", "expected_seq": 0, "fact": paid})
    result = app.accounting_apply(initial)
    assert result["completion"] == "complete", result
    return wage, paid, bank


def test_paid_wage_misclassification_recalculates_payment_without_changing_real_money(
    assembled_with_money,
):
    app, company_id = assembled_with_money
    wage, paid, bank = _seed_paid_wage(assembled_with_money)
    with app.storage.read_unit(company_id) as read:
        before = app.money.query_movements(MoneyQuery(bank, "2026-01", "2026-01"), read)
    labor = case("B17L")
    labor["business_id"] = wage["business_id"]
    labor["data"]["person_id"] = PERSON
    result = app.accounting_apply(replacement(company_id, labor))
    assert result["completion"] == "complete", result
    state = get(app, company_id, wage["business_id"], paid["business_id"])
    by_id = {item["fact"]["business_id"]: item for item in state["businesses"]}
    assert by_id[wage["business_id"]]["fact"] == labor
    assert by_id[paid["business_id"]]["fact"] == paid
    payment_lines = by_id[paid["business_id"]]["results"][0]["vouchers"][0]["lines"]
    assert [
        (line["account_code"], line["debit_fen"], line["credit_fen"]) for line in payment_lines
    ] == [
        ("224104", "100000", "0"),
        ("1002", "0", "100000"),
    ]
    with app.storage.read_unit(company_id) as read:
        after = app.money.query_movements(MoneyQuery(bank, "2026-01", "2026-01"), read)
        assert before.total == after.total == 1
        original, current = before.items[0]["movement"], after.items[0]["movement"]
        for field in (
            "movement_id",
            "public_slot",
            "funds_account_id",
            "actual_on",
            "direction",
            "amount_fen",
            "party_id",
        ):
            assert current[field] == original[field]
        resolved = app.ledger.resolve(
            [{"business_id": wage["business_id"], "slot": "net_pay"}], "obligation", read
        )[0]
        assert resolved["obligation"]["account_code"] == "224104"
        assert resolved["balance"]["money_settled_fen"] == "100000"
        assert resolved["balance"]["remaining_fen"] == "0"


@pytest.mark.parametrize("change", ["lower_wage", "withdraw", "establish_labor_tax"])
def test_paid_remuneration_conflict_rolls_back_without_losing_real_payment(
    assembled_with_money, change
):
    app, company_id = assembled_with_money
    wage, paid, bank = _seed_paid_wage(assembled_with_money)
    if change == "establish_labor_tax":
        labor = case("B17L")
        labor["business_id"] = wage["business_id"]
        labor["data"]["person_id"] = PERSON
        corrected = app.accounting_apply(replacement(company_id, labor))
        assert corrected["completion"] == "complete", corrected
        wage = labor
    before = get(app, company_id, wage["business_id"], paid["business_id"])
    with app.storage.read_unit(company_id) as read:
        before_money = app.money.query_movements(MoneyQuery(bank, "2026-01", "2026-01"), read)
    if change == "withdraw":
        call = {
            "contract": "ai-accounting-modular/1",
            "company_id": company_id,
            "command_id": str(uuid4()),
            "mutations": [
                {"op": "withdraw", "business_id": wage["business_id"], "expected_seq": 1}
            ],
        }
    else:
        if change == "lower_wage":
            wage["data"]["gross_fen"] = "80000"
        else:
            wage["data"].update(
                withholding_state="deduction_established",
                tax_basis=case("B17B")["data"]["tax_basis"],
            )
        call = replacement(company_id, wage, seq=2 if change == "establish_labor_tax" else 1)
    result = app.accounting_apply(call)
    assert result["completion"] == "not_saved", result
    assert result["error"] == "dependency_conflict", result
    assert get(app, company_id, wage["business_id"], paid["business_id"]) == before
    with app.storage.read_unit(company_id) as read:
        after_money = app.money.query_movements(MoneyQuery(bank, "2026-01", "2026-01"), read)
        assert after_money.items == before_money.items


def test_public_query_current_is_company_owner_scoped_and_withdrawal_removes_body(assembled):
    app, company_id = assembled
    actual = case("B16")
    created = app.accounting_apply(request(company_id, actual))
    assert created["completion"] == "complete", created
    with app.storage.read_unit(company_id) as read:
        facts = create_people().query_current(
            {
                "company_id": company_id,
                "family": "people",
                "period_from": "2026-01",
                "period_to": "2026-01",
                "object_id": PERSON,
                "limit": 10,
            },
            read,
        )
        assert facts == (actual,)
    result = app.accounting_apply(
        {
            "contract": "ai-accounting-modular/1",
            "company_id": company_id,
            "command_id": str(uuid4()),
            "mutations": [{"op": "withdraw", "business_id": ACTUAL, "expected_seq": 1}],
        }
    )
    assert result["completion"] == "complete", result
    with app.storage.read_unit(company_id) as read:
        assert create_people().export_current([ACTUAL], read) == ()
