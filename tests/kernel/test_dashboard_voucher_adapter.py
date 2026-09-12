"""Dashboard facts, money and version navigation against native published outcomes."""

import pytest
import test_payroll_reserve_payment as reserve_payroll
from test_bank_group_matching import batch_payment
from test_banking import book as book
from test_banking import funding
from test_engine import close, publish, save
from test_engine import engine as engine
from test_payroll_preparation import company as payroll_company
from test_service_tax_points import company as _service_company
from test_service_tax_points import receipt, sale
from test_tax_import import complete_details

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.display import Display
from ai_accounting.kernel.domains.cash import CashPayment
from ai_accounting.kernel.domains.platforms import PlatformMovement, PlatformPayment
from ai_accounting.kernel.domains.transactions import Allocation, Payment
from ai_accounting.kernel.exports import Exports

service_company = _service_company


def test_brief_includes_pending_intangible_in_asset_amount_and_count(book):
    engine, save, publish, _ = book
    save(
        "asset",
        "software",
        {
            "period": "2026-09",
            "asset_type": "intangible",
            "supplier_id": "supplier",
            "acquisition_date": "2026-09-01",
            "acquisition_basis": "direct_purchase",
            "cost_fen": 1600000,
        },
    )
    publish("software")
    result = Dashboard(engine).brief("2026-09")["data"]["long_term_assets"]
    assert result["net_fen"] == 1600000
    assert result["pending_count"] == 1
    assert result["intangible_active_count"] == 0


def test_business_receipt_is_not_voucher_total_including_vat_transfer(service_company):
    sale(service_company, tax_obligation_date="2026-04-02")
    receipt(service_company, "first", 40000, "2026-04-02")
    service_company.publish("first")
    data = Dashboard(service_company.engine).brief("2026-04")["data"]
    voucher = data["vouchers"][0]
    assert voucher["amount_fen"] == 41479
    assert voucher["business_amount_fen"] == voucher["components"][0]["amount_fen"] == 40000
    assert voucher["fund_inflow_fen"] == 40000
    assert voucher["fund_outflow_fen"] == 0
    assert voucher["funds"][0]["amount_fen"] == 40000
    assert data["activity_groups"][0]["rows"][0]["amount_fen"] == 40000
    assert voucher["lines"][2]["account"] == "待转销项税额"
    assert voucher["lines"][0]["party"] == voucher["lines"][2]["party"] == ""


def test_batch_recipients_and_individual_obligation_lines_use_exact_relationship(book):
    engine, save, publish, proof = book
    batch_payment(save, publish)
    exports = Exports(engine)
    for ident, name in (("alice", "甲员工"), ("bob", "乙员工")):
        exports.save_payee(
            ident,
            name=name,
            account="0001",
            evidence_digest=proof,
            expected_revision=0,
            request_id="name-" + ident,
        )
    voucher = next(
        item
        for item in Dashboard(engine).brief("2026-09")["data"]["vouchers"]
        if item["kind"] == "payment"
    )
    assert {item["party"] for item in voucher["settlements"]} == {"甲员工", "乙员工"}
    assert [item["party"] for item in voucher["lines"]] == ["甲员工", "", "乙员工", ""]
    assert "甲员工" in voucher["components"][0]["parties"]


def test_payroll_batch_placeholder_does_not_create_a_missing_person(tmp_path):
    company = reserve_payroll.company.__wrapped__(tmp_path)
    reserve_payroll.prepare(company)
    company.publish("scope", "gross-batch")
    for ident, name in (("one", "甲员工"), ("two", "乙员工")):
        Display(company.engine).save_display_profile(
            {
                "kind": "employee",
                "entity_id": ident,
                "display_name": name,
                "source": "合成批量工资收款人资料",
            },
            expected_revision=0,
            request_id="name-" + ident,
        )
    voucher = next(
        item
        for item in Dashboard(company.engine).brief("2026-02")["data"]["vouchers"]
        if item["kind"] == "payroll_reserve_payment"
    )
    component = voucher["components"][0]
    assert component["parties"] == ["甲员工", "乙员工"]
    assert {item["party_id"] for item in component["party_sources"]} == {"one", "two"}
    assert {item["name"] for item in component["party_sources"]} == {"甲员工", "乙员工"}
    assert all(item["source"] == "display_profile" for item in component["party_sources"])
    # Both employees have the same net salary. Equal amounts must not erase their identities.
    assert [line["party"] for line in voucher["lines"][:4]] == ["甲员工", "", "乙员工", ""]


@pytest.mark.parametrize("channel", ["bank", "cash", "platform"])
def test_social_payment_uses_actual_recipient_and_exact_wage_sources(tmp_path, channel):
    company = reserve_payroll.company.__wrapped__(tmp_path)
    allocations = []
    for employee, name in (("one", "甲员工"), ("two", "乙员工")):
        Display(company.engine).save_display_profile(
            {"kind": "employee", "entity_id": employee, "display_name": name, "source": "合成资料"},
            expected_revision=0,
            request_id="profile-" + employee,
        )
        wage = company.current("wage-" + employee)
        for obligation in wage.values["obligations"]:
            if obligation["name"] in {"employee_social", "employer_social"}:
                assert obligation["counterparty_id"] is None
                allocations.append(
                    Allocation(
                        source_kind="payroll",
                        source_id="wage-" + employee,
                        obligation=obligation["name"],
                        amount_fen=obligation["amount_fen"],
                    )
                )
    Display(company.engine).save_display_profile(
        {
            "kind": "counterparty",
            "entity_id": "authority",
            "display_name": "合成社保收款机构",
            "source": "付款确认",
        },
        expected_revision=0,
        request_id="recipient-name",
    )
    fields = dict(
        period="2026-02",
        actual_date="2026-02-10",
        direction="outflow",
        counterparty_id="authority",
        amount_fen=sum(a.amount_fen for a in allocations),
        allocations=tuple(allocations),
    )
    if channel == "bank":
        payment = Payment(**fields, bank_account_id="bank")
    elif channel == "cash":
        payment = CashPayment(**fields, cash_account_id="cash")
    else:
        movement = PlatformMovement(
            period="2026-02",
            actual_date="2026-02-10",
            platform_account_id="platform",
            transaction_reference="social-payment",
            direction="outflow",
            amount_fen=fields["amount_fen"],
            source_evidence_digest=company.owner_confirmation,
            source_location="row:1",
        )
        company.engine.save_fact(
            "platform_movement",
            "movement",
            movement.model_dump(mode="json"),
            evidence=(company.owner_confirmation,),
            expected_revision=0,
            request_id="platform-source",
        )
        company.publish("movement")
        payment = PlatformPayment(
            **fields, platform_account_id="platform", movement_ids=("movement",)
        )
    company.save(payment, "social-payment")
    company.publish("social-payment")
    before = company.engine.ledger("2026-02")
    voucher = Dashboard(company.engine).brief("2026-02")["data"]["vouchers"][0]
    assert [line["party"] for line in voucher["lines"]] == ["合成社保收款机构", ""] * 4
    for index, line in enumerate(voucher["lines"]):
        if index % 2:
            assert line["source_label"] == ""
        else:
            assert "2026-01" in line["source_label"]
            assert ("甲员工" if index < 4 else "乙员工") in line["source_label"]
    assert company.engine.ledger("2026-02") == before


def test_voucher_and_trace_show_immutable_evidence_names_without_loading_files(book):
    import hashlib

    engine, _, publish_fact, _ = book
    proofs = [
        engine.register_evidence(
            content,
            "application/pdf",
            name,
            request_id=f"file-{index}",
        )["digest"]
        for index, (content, name) in enumerate(
            (
                (b"first", r"C:\originals\银行付款凭据.pdf"),
                (b"second", "/originals/银行付款凭据.pdf"),
                (b"unnamed", ""),
                (b"legacy", hashlib.sha256(b"legacy").hexdigest()),
            )
        )
    ]
    engine.save_fact(
        "cash_funding",
        "receipt",
        {
            "period": "2026-09",
            "actual_date": "2026-09-01",
            "cash_account_id": "cash",
            "owner_id": "owner",
            "funding_kind": "capital",
            "amount_fen": 100,
        },
        evidence=tuple(proofs),
        expected_revision=0,
        request_id="receipt",
    )
    publish_fact("receipt")
    data = Dashboard(engine).brief("2026-09")["data"]
    voucher = data["vouchers"][0]
    names = voucher["evidence_details"]
    assert [item["name"] for item in names].count("银行付款凭据.pdf") == 2
    assert [item["name"] for item in names].count("") == 2
    assert {item["digest"] for item in names} == set(proofs)
    assert data["activity_groups"][0]["rows"][0]["evidence_details"] == names
    trace = engine.trace(voucher_version_id=voucher["voucher_version_id"])
    assert trace["evidence_details"] == names
    assert voucher["evidence"] == sorted(proofs)
    with engine.store.connection(read_only=True) as connection:
        import sqlite3

        def deny_contents(action, table, column, *_):
            return (
                sqlite3.SQLITE_DENY
                if table == "evidence" and column == "content"
                else sqlite3.SQLITE_OK
            )

        connection.set_authorizer(deny_contents)
        assert engine.store.evidence_metadata(connection, proofs + ["0" * 64]) == names


def test_legacy_close_can_use_existing_names_without_changing_frozen_accounting(book, monkeypatch):
    engine, save, publish, proof = book
    from test_banking import close_month, inventories

    funding(save, publish)
    # Model the pre-v8 management part, while leaving the native close write immutable.
    monkeypatch.setattr(Display, "snapshot", staticmethod(lambda *args, **kwargs: {}))
    # A closed synthetic month needs the native materials and bank reconciliation fixtures.
    from test_banking import entry, opening, reconciliation, statement

    opening(save, publish)
    statement(save, publish, [entry()])
    reconciliation(
        save, publish, [{"reference": "receipt", "source_kind": "funding", "source_id": "funding"}]
    )
    close_month(
        inventories(engine, proof, "2026-09", {"bank", "financing", "transactions"}),
        proof,
        "2026-09",
    )
    with engine.store.connection(read_only=True) as connection:
        before = connection.execute("SELECT manifest FROM period_close").fetchone()[0]
    Exports(engine).save_payee(
        "owner",
        name="已提供姓名",
        account="001",
        evidence_digest=proof,
        expected_revision=0,
        request_id="owner-name",
    )
    data = Dashboard(engine).brief("2026-09")["data"]
    assert data["vouchers"][0]["components"][0]["parties"] == ["已提供姓名"]
    assert (
        next(line for line in data["vouchers"][0]["lines"] if line["code"] == "3001")["party"]
        == "已提供姓名"
    )
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT manifest FROM period_close").fetchone()[0] == before


def test_tax_identity_name_and_employee_code_are_reused(tmp_path):
    company = payroll_company(tmp_path)
    original = company.current("january")
    complete_details(company)
    item = Dashboard(company.engine).employees("2026-01")["data"]["employees"]["items"][0]
    assert item["name"] == "测试员工"
    assert item["code"] == "00007"
    assert company.current("january") == original


def test_all_three_correction_vouchers_open_their_own_basis(engine):
    save(engine, amount=100)
    publish(engine)
    close(engine)
    original = Dashboard(engine).brief("2026-01")["data"]["vouchers"][0]
    save(engine, amount=125, revision=1, request="amend")
    publish(engine, request="amend-publish", correction_period="2026-02")
    rows = Dashboard(engine).brief("2026-02")["data"]["vouchers"]
    reversal = next(item for item in rows if item["reverses_version_id"])
    replacement = next(item for item in rows if not item["reverses_version_id"])
    assert reversal["calculation_id"] == original["calculation_id"]
    for item, amount in ((original, 100), (reversal, 100), (replacement, 125)):
        result = engine.trace(item["calculation_id"], voucher_version_id=item["voucher_version_id"])
        assert result["calculation"]["outcome"]["values"]["amount"] == amount
        assert result["voucher"]["total"] == amount
        assert len(result["related_vouchers"]) == 2
        assert result["voucher"]["id"] not in {v["id"] for v in result["related_vouchers"]}
    with pytest.raises(KernelError, match="不属于"):
        engine.trace(
            replacement["calculation_id"], voucher_version_id=original["voucher_version_id"]
        )


def test_paged_queries_reject_changed_or_missing_version_before_returning_rows(book):
    engine, save, publish, _ = book
    funding(save, publish, subject="a", amount=100)
    funding(save, publish, subject="b", amount=200)
    dashboard = Dashboard(engine)
    first = dashboard.brief("2026-09", limit=1)
    funds = dashboard.funds("2026-09", limit=1)
    cursor = first["data"]["voucher_page"]["next_after_number"]
    following = dashboard.brief(
        "2026-09", after_number=cursor, limit=1, expected_version=first["snapshot_version"]
    )
    assert following["data"]["total_debit_fen"] == 300
    funding(save, publish, subject="c", amount=50)
    for kwargs in ({}, {"expected_version": first["snapshot_version"]}):
        with pytest.raises(KernelError) as failure:
            dashboard.brief("2026-09", after_number=cursor, **kwargs)
        assert failure.value.code == "dashboard_snapshot_changed"
    with pytest.raises(KernelError) as failure:
        dashboard.funds(
            "2026-09",
            after_movement=funds["data"]["movement_page"]["next_cursor"],
            expected_version=funds["snapshot_version"],
        )
    assert failure.value.code == "dashboard_snapshot_changed"
    assert dashboard.brief("2026-09")["data"]["total_debit_fen"] == 350
