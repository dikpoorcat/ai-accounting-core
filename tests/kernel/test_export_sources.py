"""Bank-payment discovery covers current facts and saved unpaid payment rights."""

from __future__ import annotations

from contextlib import contextmanager
from functools import partial

import pytest
from entity_fixture import seed_fact_entities
from opening_fixture import adopt_empty_opening
from test_integrity_content import damage
from test_opening_continuation import _close_without_current_business
from test_opening_continuation import book as opening_book_fixture
from test_payroll_corrections import Company

from ai_accounting.kernel.command_schema import command_models, validate_command
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.domains.opening import OpeningObligation, OpeningPayrollPayable
from ai_accounting.kernel.domains.transactions import Allocation, Expense, PassThrough, Payment
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.entities import Entities
from ai_accounting.kernel.export_sources import ExportSources
from ai_accounting.kernel.identity_corrections import IdentityCorrections
from ai_accounting.kernel.periods import Periods
from ai_accounting.kernel.response_contracts import RESPONSE_ADAPTERS, validate_response
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.service import LocalService, default_registry
from ai_accounting.kernel.storage import Store

opening_book = opening_book_fixture


def reimbursement(*, amount=10000, period="2026-01", creditor="employee"):
    return Expense(
        period=period,
        counterparty_id="employee",
        amount_fen=amount,
        expense_class="administration",
        creditor_kind=creditor,
    )


@pytest.fixture
def company(tmp_path):
    return Company(tmp_path / "sources.sqlite")


def items(company, period="2026-01", **options):
    return ExportSources(company.engine).query(period, **options)["items"]


def test_discovery_needs_no_bank_template_accounts_or_monthly_readiness(company):
    company.save(reimbursement(), "reimbursement")
    company.publish("reimbursement")
    company.save(reimbursement(amount=777), "not-yet-published")
    company.save(reimbursement(creditor="supplier"), "supplier")
    Entities(company.engine).update_entity_profile(
        "employee",
        {"display_name": "合成最终收款人"},
        source="synthetic named recipient",
        expected_revision=1,
        request_id=company.request(),
    )
    company.save(
        PassThrough(
            period="2026-01",
            payer_id="payer",
            beneficiary_id="employee",
            amount_fen=25000,
            rights_and_obligation_confirmed=True,
        ),
        "pass-through",
    )
    company.publish("pass-through")
    before = {
        table: company.count(table) for table in ("request", "audit", "jobs", "payee_revision")
    }
    page = ExportSources(company.engine).query("2026-01")
    assert page["sort"] == "subject_id_asc"
    assert [item["subject_id"] for item in page["items"]] == [
        "not-yet-published",
        "pass-through",
        "reimbursement",
    ]
    by_source = {item["subject_id"]: item for item in page["items"]}
    assert by_source["not-yet-published"]["status"] == "unpublished"
    assert by_source["not-yet-published"]["obligations"] is None
    assert by_source["not-yet-published"]["calculation_id"] is None
    transfer = by_source["pass-through"]
    assert transfer["status"] == "published"
    assert transfer["payment_category"] == "代付款"
    assert transfer["obligations"] == [
        {
            "name": "remittance",
            "key": "pass_through:pass-through:remittance",
            "party_id": "employee",
            "amount_fen": 25000,
            "remaining_fen": 25000,
        }
    ]
    assert by_source["reimbursement"]["payment_category"] == "报销"
    assert before == {table: company.count(table) for table in before}
    assert validate_response("export_sources", page) == page
    wire = RESPONSE_ADAPTERS["export_sources"].dump_python(page, mode="json")
    assert wire["items"][1]["obligations"][0]["remaining_fen"] == "25000"


def test_pending_source_uses_formal_amount_and_shows_current_and_adopted_identity(company):
    company.save(reimbursement(amount=10000), "reimbursement")
    company.publish("reimbursement")
    original = items(company)[0]
    company.save(reimbursement(amount=20000), "reimbursement", revision=1)
    candidate = items(company)[0]
    assert candidate["status"] == "pending"
    assert candidate["fact_id"] != candidate["adopted_fact_id"]
    assert candidate["adopted_fact_id"] == original["fact_id"]
    assert candidate["calculation_id"] == original["calculation_id"]
    assert candidate["revision"] == 2
    assert candidate["obligations"][0]["amount_fen"] == 10000
    company.publish("reimbursement")
    assert items(company)[0]["obligations"][0]["amount_fen"] == 20000


def test_discovery_reads_current_and_adopted_fact_bodies_once(company, monkeypatch):
    company.save(reimbursement(amount=10000), "reimbursement")
    company.publish("reimbursement")
    company.save(reimbursement(amount=20000), "reimbursement", revision=1)
    company.save(reimbursement(amount=777), "unpublished")
    store = company.engine.store
    original_connection = store.connection
    statements = []

    @contextmanager
    def traced_connection(*args, **kwargs):
        with original_connection(*args, **kwargs) as connection:
            connection.set_trace_callback(statements.append)
            yield connection

    monkeypatch.setattr(store, "connection", traced_connection)
    found = {item["subject_id"]: item for item in items(company)}
    assert found["reimbursement"]["status"] == "pending"
    assert found["reimbursement"]["fact_id"] != found["reimbursement"]["adopted_fact_id"]
    assert found["reimbursement"]["obligations"][0]["amount_fen"] == 10000
    assert found["unpublished"]["obligations"] is None
    body_reads = [
        statement for statement in statements
        if statement.startswith("SELECT") and "JOIN fact_expense " in statement
    ]
    assert len(body_reads) == 1


def test_current_eligibility_changes_remain_visible_until_recalculated(company):
    company.save(reimbursement(creditor="supplier"), "reimbursement")
    company.publish("reimbursement")
    assert items(company) == []
    company.save(reimbursement(), "reimbursement", revision=1)
    candidate = items(company)[0]
    assert candidate["status"] == "pending"
    assert candidate["obligations"] is None
    company.publish("reimbursement")
    assert items(company)[0]["obligations"][0]["remaining_fen"] == 10000
    company.save(reimbursement(creditor="supplier"), "reimbursement", revision=2)
    candidate = items(company)[0]
    assert candidate["status"] == "pending"
    assert candidate["obligations"][0]["remaining_fen"] == 10000
    company.publish("reimbursement")
    assert items(company) == []


def test_actual_partial_and_full_payments_and_pending_settled_source(company):
    company.save(reimbursement(), "reimbursement")
    company.publish("reimbursement")
    for subject, amount in (("partial", 2500), ("final", 7500)):
        company.save(
            Payment(
                period="2026-01",
                actual_date="2026-01-20",
                bank_account_id="bank",
                counterparty_id="employee",
                direction="outflow",
                amount_fen=amount,
                allocations=(
                    Allocation(
                        source_kind="expense",
                        source_id="reimbursement",
                        obligation="primary",
                        amount_fen=amount,
                    ),
                ),
            ),
            subject,
        )
        company.publish(subject)
        if subject == "partial":
            assert items(company)[0]["obligations"][0]["remaining_fen"] == 7500
    assert items(company) == []
    company.save(reimbursement(amount=12500), "reimbursement", revision=1)
    candidate = items(company)[0]
    assert candidate["status"] == "pending"
    assert candidate["obligations"] == []


def test_cross_month_management_grouping_and_custom_purpose(company):
    company.save(reimbursement(), "january")
    company.publish("january")
    company.save(reimbursement(period="2026-02"), "february")
    Periods(company.engine).management(
        "january",
        payment_period="2026-02",
        payment_category="管理确认用途",
        expected_revision=0,
        request_id=company.request(),
    )
    assert items(company) == []
    found = {item["subject_id"]: item for item in items(company, "2026-02")}
    assert found["january"]["accounting_period"] == "2026-01"
    assert found["january"]["payment_period"] == "2026-02"
    assert found["january"]["payment_category"] == "管理确认用途"
    assert found["january"]["management_revision_id"] is not None
    assert found["february"]["status"] == "unpublished"


def test_pending_accounting_month_change_is_discoverable_in_current_and_adopted_month(company):
    company.save(reimbursement(), "reimbursement")
    company.publish("reimbursement")
    company.save(reimbursement(period="2026-02", amount=20000), "reimbursement", revision=1)
    january, february = items(company)[0], items(company, "2026-02")[0]
    assert january["status"] == february["status"] == "pending"
    assert january["accounting_period"] == february["accounting_period"] == "2026-01"
    assert (
        january["current_accounting_period"] == february["current_accounting_period"] == "2026-02"
    )
    assert january["payment_period"] == "2026-01"
    assert february["payment_period"] == "2026-02"
    assert february["obligations"][0]["amount_fen"] == 10000
    Periods(company.engine).management(
        "reimbursement",
        payment_period="2026-03",
        payment_category=None,
        expected_revision=0,
        request_id=company.request(),
    )
    assert items(company) == items(company, "2026-02") == []
    assert items(company, "2026-03")[0]["status"] == "pending"


def test_pagination_has_no_gaps_when_ineligible_sources_are_between_candidates(company):
    for subject, creditor in (
        ("a", "employee"),
        ("b", "supplier"),
        ("c", "employee"),
        ("d", "supplier"),
        ("e", "individual"),
    ):
        company.save(reimbursement(creditor=creditor), subject)
    discovery = ExportSources(company.engine)
    first = discovery.query("2026-01", limit=1)
    second = discovery.query("2026-01", limit=1, cursor=first["next_cursor"])
    third = discovery.query("2026-01", limit=1, cursor=second["next_cursor"])
    assert [page["items"][0]["subject_id"] for page in (first, second, third)] == ["a", "c", "e"]
    assert third["next_cursor"] is None
    assert discovery.query("2026-02")["items"] == []


def test_pagination_continues_across_a_whole_filtered_internal_batch(company):
    seed_fact_entities(company.engine, reimbursement())
    records = []
    for index in range(105):
        fact = reimbursement(
            amount=100 + index, creditor="employee" if index in {0, 104} else "supplier"
        )
        records.append(
            {
                "kind": fact.kind,
                "subject_id": f"source-{index:03d}",
                "data": fact.model_dump(mode="json"),
                "evidence": [company.owner_confirmation],
                "expected_revision": 0,
            }
        )
    company.engine.save_facts(records, request_id=company.request())
    discovery = ExportSources(company.engine)
    first = discovery.query("2026-01", limit=1)
    assert [item["subject_id"] for item in first["items"]] == ["source-000"]
    assert first["next_cursor"] is not None
    second = discovery.query("2026-01", limit=1, cursor=first["next_cursor"])
    assert [item["subject_id"] for item in second["items"]] == ["source-104"]
    assert second["next_cursor"] is None


@pytest.mark.parametrize("lane", ["accounting", "material", "management", "read_repair_revision"])
def test_each_state_lane_and_read_repair_invalidates_cursor(company, lane):
    for subject in ("a", "b"):
        company.save(reimbursement(), subject)
    cursor = ExportSources(company.engine).query("2026-01", limit=1)["next_cursor"]
    with company.engine.store.connection() as connection:
        connection.execute(f"UPDATE state SET {lane}={lane}+1 WHERE id=1")
    with pytest.raises(KernelError) as failure:
        ExportSources(company.engine).query("2026-01", limit=1, cursor=cursor)
    assert failure.value.code == "export_cursor_stale"


@pytest.mark.parametrize("attribute", ["company_id", "database_id", "period"])
def test_cursor_cannot_be_reused_for_another_company_database_or_month(
    company, attribute, tmp_path
):
    for subject in ("a", "b"):
        company.save(reimbursement(), subject)
    discovery = ExportSources(company.engine)
    cursor = discovery.query("2026-01", limit=1)["next_cursor"]
    period = "2026-01"
    if attribute == "period":
        period = "2026-02"
    else:
        original = company.engine.store
        discovery = ExportSources(
            Engine(
                Store.create(
                    tmp_path / "another.sqlite",
                    production_bundle(),
                    "another-company" if attribute == "company_id" else original.company_id,
                    "91310000123456789A",
                    "another-database" if attribute == "database_id" else original.database_id,
                )
            )
        )
    with pytest.raises(KernelError) as failure:
        discovery.query(period, cursor=cursor)
    assert failure.value.code == "export_cursor_stale"


@pytest.mark.parametrize("cursor", ["", "not a cursor", "W10=", "e30=", "x" * 2049])
def test_malformed_cursor_is_rejected(company, cursor):
    with pytest.raises(KernelError) as failure:
        ExportSources(company.engine).query("2026-01", cursor=cursor)
    assert failure.value.code == "export_cursor_invalid"


def test_opening_eligibility_does_not_include_tax_or_supplier_items(company):
    for subject, nature in (
        ("employee", "employee_reimbursement"),
        ("owner", "owner_reimbursement"),
        ("supplier", "supplier_service_payable"),
    ):
        company.save(
            OpeningObligation(
                period="2026-01",
                package_id="unpublished-opening",
                counterparty_id="employee",
                nature=nature,
                outstanding_fen=100,
                business_reference="original-" + subject,
            ),
            subject,
        )
    for subject, component in (("net", "net"), ("tax", "withheld_tax")):
        company.save(
            OpeningPayrollPayable(
                period="2026-01",
                package_id="unpublished-opening",
                employee_id="employee",
                recipient_id="employee",
                payroll_period="2025-12",
                component=component,
                outstanding_fen=100,
            ),
            subject,
        )
    assert {item["subject_id"] for item in items(company)} == {"employee", "owner", "net"}
    assert all(item["obligations"] is None for item in items(company))


def test_published_opening_discovers_net_wages_and_both_reimbursement_natures(opening_book):
    engine, _, _, package, _ = opening_book
    members = [("opening_bank", "bank", {"bank_account_id": "bank", "balance_fen": 100000})]
    for subject, nature, amount in (
        ("employee-reimbursement", "employee_reimbursement", 10000),
        ("owner-reimbursement", "owner_reimbursement", 15000),
        ("supplier", "supplier_service_payable", 20000),
    ):
        members.append(
            (
                "opening_obligation",
                subject,
                {
                    "counterparty_id": "employee",
                    "nature": nature,
                    "outstanding_fen": amount,
                    "business_reference": "opening-" + subject,
                },
            )
        )
    for subject, component, amount in (
        ("net-wage", "net", 25000),
        ("withheld-tax", "withheld_tax", 5000),
    ):
        members.append(
            (
                "opening_payroll_payable",
                subject,
                {
                    "employee_id": "employee",
                    "recipient_id": "employee",
                    "payroll_period": "2025-12",
                    "component": component,
                    "outstanding_fen": amount,
                },
            )
        )
    members.append(
        (
            "opening_equity",
            "equity",
            {
                "equity_kind": "paid_in_capital",
                "balance_fen": 25000,
                "holder_or_basis_id": "owner",
            },
        )
    )
    package(members)
    candidates = ExportSources(engine).query("2026-01")["items"]
    assert {item["subject_id"]: item["obligations"][0]["remaining_fen"] for item in candidates} == {
        "employee-reimbursement": 10000,
        "owner-reimbursement": 15000,
        "net-wage": 25000,
    }
    assert all(item["status"] == "published" for item in candidates)
    assert all(item["obligations"][0]["name"] == "primary" for item in candidates)


def test_closed_opening_candidate_uses_formally_corrected_recipient(opening_book):
    engine, _, _, package, proof = opening_book
    fields = {
        "counterparty_id": "employee",
        "nature": "employee_reimbursement",
        "outstanding_fen": 10000,
        "business_reference": "retained-original-reimbursement",
    }
    package(
        [
            ("opening_cash", "cash", {"cash_account_id": "cash", "balance_fen": 10000}),
            ("opening_obligation", "reimbursement", fields),
        ]
    )
    _close_without_current_business(engine, "2026-01", proof)
    corrected_party = Entities(engine).register_entity(
        "person",
        {"display_name": "合成修订收款人"},
        source="synthetic corrected recipient",
        request_id="corrected-recipient",
    )["entity_id"]
    correction = IdentityCorrections(engine)
    options = {
        "changes": [
            {
                "subject_id": "reimbursement",
                "expected_revision": 1,
                "action": "reassign",
                "data": {
                    "period": "2026-01",
                    "package_id": "opening",
                    **fields,
                    "counterparty_id": corrected_party,
                },
            }
        ],
        "evidence": [proof],
        "reason": "原件确认正确的报销收款人",
        "posting_period": "2026-02",
    }
    preview = correction.preview_identity_correction(**options)
    correction.confirm_identity_correction(
        **options,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="reimbursement-recipient-correction",
    )
    candidate = ExportSources(engine).query("2026-01")["items"][0]
    assert candidate["status"] == "published"
    assert candidate["obligations"][0]["party_id"] == corrected_party
    assert candidate["obligations"][0]["remaining_fen"] == 10000
    assert candidate["opening_binding_calculation_id"] is not None
    assert candidate["opening_binding_fact_id"] == candidate["current_opening_binding_fact_id"]
    with engine.store.connection(read_only=True) as connection:
        original = engine.store.current_fact(connection, "reimbursement")
        assert original.fact.counterparty_id == "employee"
        assert candidate["fact_id"] == candidate["adopted_fact_id"] == original.id
    # A pending identity review must stay discoverable with its last adopted
    # recipient; it cannot make the entire month's discovery fail.
    with engine.store.connection() as connection:
        connection.execute(
            "INSERT INTO pending VALUES(?,?)",
            ("opening-identity:reimbursement", original.id),
        )
    pending = ExportSources(engine).query("2026-01")["items"][0]
    assert pending["status"] == "pending"
    assert pending["obligations"][0]["party_id"] == corrected_party


def test_discovery_does_not_trust_modified_calculation_identity(company):
    company.save(reimbursement(), "reimbursement")
    company.publish("reimbursement")
    damage(
        company.engine,
        "calculation",
        "UPDATE calculation SET kind='labor' WHERE subject_id=?",
        ("reimbursement",),
    )
    with pytest.raises(KernelError) as failure:
        items(company)
    assert failure.value.code == "content_integrity_failed"


def test_command_schema_uses_read_only_company_scoped_public_registration():
    registry = default_registry()
    models = command_models(registry)
    validated = validate_command(
        models, "export_sources", {"company_id": "company", "period": "2026-01"}
    )
    assert validated == {"company_id": "company", "period": "2026-01", "limit": 100, "cursor": None}
    for limit in (0, 501, True):
        with pytest.raises(KernelError) as failure:
            validate_command(
                models,
                "export_sources",
                {"company_id": "company", "period": "2026-01", "limit": limit},
            )
        assert failure.value.code == "invalid_command"
    from test_schema_discovery import discover

    # Generic CLI and MCP transports both dispatch this public command registry.
    result = discover(
        (registry, models),
        view="selected",
        commands=["export_sources"],
        response_types=["export_sources"],
    )
    assert set(result["command_schemas"]) == {"export_sources"}
    assert set(result["response_schemas"]) == {"export_sources"}


def test_local_service_dispatches_bank_sources_without_recording_a_business_write(tmp_path):
    service = LocalService(tmp_path / "service")
    service.security.provision("owner", "test-password-123")
    token = service.security.login("owner", "test-password-123").session_token
    dispatch = partial(service.dispatch, session_token=token)
    company_id = dispatch(
        "create_company", {"taxpayer_id": "91310000123456789A", "name": "合成来源企业"}
    )["id"]
    engine = service.engine(company_id)
    adopt_empty_opening(engine)
    with engine.store.connection(read_only=True) as connection:
        before = tuple(connection.execute("SELECT * FROM state WHERE id=1").fetchone())
        audit = connection.execute("SELECT count(*) FROM audit").fetchone()[0]
    result = dispatch("export_sources", {"company_id": company_id, "period": "2026-01"})
    assert result["items"] == []
    assert result["company_id"] == company_id
    assert result["database_id"] == engine.store.database_id
    with engine.store.connection(read_only=True) as connection:
        assert tuple(connection.execute("SELECT * FROM state WHERE id=1").fetchone()) == before
        assert connection.execute("SELECT count(*) FROM audit").fetchone()[0] == audit
