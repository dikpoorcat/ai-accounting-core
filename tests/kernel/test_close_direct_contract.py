"""Direct adoption stays exact across reviews and cumulative dependencies."""

import hashlib
import json
from copy import deepcopy
from typing import ClassVar, Literal

import pytest
from monthly_close_fixture import close_months, ready
from pydantic import ValidationError
from schema_fixture import test_bundle
from test_engine import close, evidence, publish, save
from test_engine import engine as engine  # noqa: F401
from test_integrity_content import damage, verify
from test_payroll_corrections import Company

from ai_accounting.kernel.close_contract import require_close_contract
from ai_accounting.kernel.close_review import (
    DASHBOARD_CLOSE_REVIEW_ADAPTER,
    read_collection,
    verify_owner_review_integrity,
)
from ai_accounting.kernel.contracts import (
    BalanceEffect,
    Fact,
    KernelError,
    Line,
    Outcome,
    Read,
    Registry,
)
from ai_accounting.kernel.domains.transactions import Expense
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.integrity import verify_integrity
from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.types import PositiveFen, canonical, digest


class PartyMovement(Fact):
    kind: ClassVar[str] = "test_party_movement"
    side: Literal["receivable", "payable"]
    counterparty_id: str
    amount: PositiveFen


def calculate_party_movement(version, _context):
    fact = version.fact
    receivable = fact.side == "receivable"
    obligation = {
        "name": "primary",
        "key": f"test_party_movement:{version.subject_id}:primary",
        "amount_fen": fact.amount,
        "account": "2202",
        "normal": "debit" if receivable else "credit",
        "category": fact.side,
        "counterparty_id": fact.counterparty_id,
        "cashflow": "operating",
    }
    return Outcome(
        (
            (Line("2202", debit=fact.amount), Line("5001", credit=fact.amount))
            if receivable
            else (Line("5602", debit=fact.amount), Line("2202", credit=fact.amount))
        ),
        {"amount": fact.amount, "obligations": [obligation]},
        (BalanceEffect(obligation["key"], fact.amount, fact.side),),
    )


class CashSpend(Fact):
    kind: ClassVar[str] = "test_cash_spend"
    amount: PositiveFen


def calculate_cash_spend(version, _context):
    fact = version.fact
    return Outcome(
        (Line("5602", debit=fact.amount), Line("1001", credit=fact.amount)),
        {"amount_fen": fact.amount},
        (BalanceEffect(f"cash:{version.subject_id}", -fact.amount, "cash"),),
    )


def replace_manifest(engine, manifest):
    damage(
        engine,
        "period_close",
        "UPDATE period_close SET manifest=?,digest=?",
        (canonical(manifest), digest(manifest)),
    )


def test_review_before_close_records_owner_and_exact_reviewed_basis(engine):
    save(engine)
    _, first = publish(engine)
    save(engine, revision=1, request="review")
    _, second = publish(engine, request="review-publish")
    manifest = close(engine)
    owner = first["results"][0]["calculation_id"]
    basis = second["results"][0]["calculation_id"]
    assert owner != basis
    assert manifest["vouchers"][0]["calculation_id"] == owner
    assert manifest["vouchers"][0]["adopted_calculation_id"] == basis
    assert [item["calculation_id"] for item in manifest["adopted_results"]] == [basis]
    assert "calculations" not in manifest and "facts" not in manifest
    assert verify(engine)["status"] == "verified"


def test_review_after_close_does_not_replace_frozen_adoption(engine):
    save(engine)
    publish(engine)
    before = close(engine)
    save(engine, revision=1, request="later-review")
    publish(engine, request="later-publish")
    assert Periods(engine).closed_report("2026-01") == before
    assert verify(engine)["status"] == "verified"
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute("SELECT max(sequence) FROM calculation_publication").fetchone()[0]
            > before["publication_sequence"]
        )


def test_zero_line_state_is_direct_and_omission_is_damage(engine):
    engine.save_fact(
        "test_charge",
        "charge",
        {"period": "2026-01", "amount": 100, "suppress_posting": True},
        evidence=(evidence(engine),),
        expected_revision=0,
        request_id="state",
    )
    publish(engine)
    manifest = close(engine)
    assert manifest["vouchers"] == []
    assert manifest["adopted_results"][0]["role"] == "state_only"
    manifest["adopted_results"] = []
    replace_manifest(engine, manifest)
    with pytest.raises(KernelError) as failure:
        verify(engine, include_indexes=False)
    assert failure.value.details["reason"] == "direct_adoption_set_mismatch"


@pytest.mark.parametrize(
    "field",
    [
        "adopted_results",
        "asset_card_adoptions",
        "opening_calculation_id",
        "publication_sequence",
        "read_version",
    ],
)
def test_missing_required_adoption_evidence_never_falls_back(engine, field):
    save(engine)
    publish(engine)
    manifest = close(engine)
    del manifest[field]
    with pytest.raises(KernelError) as failure:
        require_close_contract(manifest)
    assert failure.value.code == "content_integrity_failed"


def test_exact_frozen_month_is_required(engine):
    with pytest.raises(KernelError) as failure:
        Periods(engine).closed_report("2026-01")
    assert failure.value.code == "frozen_snapshot_unavailable"


def test_close_contract_requires_integer_versions_and_exact_fields(engine):
    save(engine)
    publish(engine)
    manifest = close(engine)
    for version in (True, 1.0):
        changed = deepcopy(manifest)
        changed["format_version"] = version
        with pytest.raises(KernelError):
            require_close_contract(changed)
        changed = deepcopy(manifest)
        changed["asset_card_adoptions"] = [
            {
                "contract_version": version,
                "asset_id": "synthetic-asset",
                "calculation_id": "synthetic-result",
                "result_digest": "a" * 64,
                "acceptance_calculation_id": "synthetic-acceptance",
                "acceptance_result_digest": "b" * 64,
            }
        ]
        with pytest.raises(KernelError):
            require_close_contract(changed)
    changed = deepcopy(manifest)
    changed["unrecognized_contract_field"] = {}
    with pytest.raises(KernelError):
        require_close_contract(changed)


def test_close_contract_requires_exact_password_approval_receipt(engine):
    save(engine)
    publish(engine)
    manifest = close(engine)
    approval = {
        "approval_id": "a" * 32,
        "preview_digest": "b" * 64,
        "owner_id": "owner",
        "catalog_instance_id": "catalog",
        "credential_version": 1,
        "confirmed_at": 1,
        "method": "local_password_reauthentication",
    }
    assert require_close_contract({**manifest, "approval": approval})["approval"] == approval
    invalid = [
        approval | {"extra": "unsupported"},
        {key: value for key, value in approval.items() if key != "owner_id"},
        approval | {"approval_id": "a" * 31},
        approval | {"preview_digest": "B" * 64},
        approval | {"credential_version": True},
        approval | {"confirmed_at": 1.0},
        approval | {"method": "chat_confirmation"},
    ]
    for receipt in invalid:
        with pytest.raises(KernelError) as failure:
            require_close_contract({**manifest, "approval": receipt})
        assert failure.value.details["reason"] == "invalid_close_approval"


def test_close_contract_rejects_removed_range_metadata(engine):
    save(engine)
    publish(engine)
    manifest = close(engine)
    scope = {"from_period": "2026-01", "through_period": "2026-03", "preview_digest": "a" * 64}
    with pytest.raises(KernelError) as failure:
        require_close_contract(manifest | {"close_range": scope})
    assert failure.value.details["reason"] == "unsupported_close_contract"


@pytest.mark.parametrize("value", [True, 1.0])
def test_close_review_schema_version_rejects_non_integer_literal(value):
    with pytest.raises(ValidationError):
        DASHBOARD_CLOSE_REVIEW_ADAPTER.validate_python(
            {
                "schema_version": value,
                "company_id": "company",
                "database_id": "database",
                "period": "2026-01",
                "state": "unprepared",
                "preview_digest": None,
                "close_digest": None,
                "reason": "尚未准备",
                "covered_by": None,
                "owner_review": None,
                "collection": None,
            }
        )


def test_owner_review_pages_bind_and_cross_fixed_blocks(engine, monkeypatch):
    import ai_accounting.kernel.close_review as close_review

    monkeypatch.setattr(close_review, "DETAIL_BLOCK_SIZE", 1)
    save(engine, subject="first", request="save-first")
    save(engine, subject="second", amount=200, request="save-second")
    publish(engine, ["first", "second"])
    manifest = close(engine)
    directory = next(
        item for item in manifest["owner_review"]["collections"] if item["section"] == "vouchers"
    )
    first_key = directory["blocks"][0]["keys"][0]
    second_key = directory["blocks"][1]["keys"][0]
    loaded_keys = []
    render_keys = close_review._render_keys

    def render_with_damaged_second_block(connection, target_engine, target_manifest, section, keys):
        loaded_keys.append((section, tuple(keys)))
        cards = render_keys(connection, target_engine, target_manifest, section, keys)
        if section == "vouchers" and second_key in keys:
            cards = [
                ({**card, "title": card["title"] + "（模拟源损坏）"})
                if card["key"] == second_key
                else card
                for card in cards
            ]
        return cards

    monkeypatch.setattr(close_review, "_render_keys", render_with_damaged_second_block)
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        first = read_collection(connection, engine, manifest, "b" * 64, "vouchers", limit=1)
        assert first["page"]["has_more"] and len(first["items"]) == 1
        assert first["items"][0]["key"] == first_key
        assert loaded_keys == [("vouchers", (first_key,))]
        with pytest.raises(KernelError) as page_failure:
            read_collection(
                connection,
                engine,
                manifest,
                "b" * 64,
                "vouchers",
                cursor=first["page"]["next_cursor"],
                limit=1,
            )
        assert page_failure.value.details["reason"] == "detail_block_digest_mismatch"
        assert loaded_keys[-1] == ("vouchers", (second_key,))
        with pytest.raises(KernelError) as full_failure:
            verify_owner_review_integrity(connection, engine, manifest)
        assert full_failure.value.details["reason"] == "owner_review_semantic_mismatch"


def test_owner_review_uses_real_expense_amount_and_business_kind(tmp_path):
    company = Company(tmp_path / "expense.sqlite")
    company.save(
        Expense(
            period="2026-01",
            counterparty_id="supplier",
            amount_fen=12_345,
            expense_class="administration",
            creditor_kind="supplier",
        ),
        "office-cost",
    )
    company.publish("office-cost")
    manifest = company.close("2026-01")
    summary = next(
        item for item in manifest["owner_review"]["business_summary"] if item["kind"] == "expense"
    )
    assert summary["label"] == "费用"
    assert summary["amount_label"] == "业务确认金额"
    assert summary["action"] == "business" and summary["reversal"] is False
    assert summary["count"] == 1
    assert summary["business_amount_fen"] == 12_345
    assert summary["journal_total_fen"] == 12_345


def test_owner_review_financial_position_keeps_opposite_parties_separate(tmp_path):
    registry = Registry()
    registry.register(PartyMovement, calculate_party_movement)
    engine = Engine(
        Store.create(
            tmp_path / "party-position.sqlite",
            test_bundle(registry),
            "party-company",
            "91310000123456789A",
            "party-db",
        )
    )
    proof = evidence(engine)
    for subject, side, party, amount in (
        ("customer-receivable", "receivable", "customer-a", 200),
        ("supplier-payable", "payable", "supplier-b", 100),
    ):
        engine.save_fact(
            PartyMovement.kind,
            subject,
            {"period": "2026-01", "side": side, "counterparty_id": party, "amount": amount},
            evidence=(proof,),
            expected_revision=0,
            request_id="save-" + subject,
        )
    publish(engine, ["customer-receivable", "supplier-payable"], request="publish-position")
    manifest = close(engine)
    accounting = manifest["owner_review"]["accounting_summary"]
    assert accounting["ending_assets_fen"] == 200
    assert accounting["ending_liabilities_fen"] == 100
    assert accounting["ending_equity_fen"] == 100
    assert accounting["financial_position_balanced"] is True


def test_closed_month_cash_correction_is_not_reported_as_new_receipt_or_payment(tmp_path):
    registry = Registry()
    registry.register(CashSpend, calculate_cash_spend)
    engine = Engine(
        Store.create(
            tmp_path / "cash-correction.sqlite",
            test_bundle(registry),
            "cash-company",
            "91310000123456789A",
            "cash-db",
        )
    )
    proof = evidence(engine)
    ready(engine, proof)
    engine.save_fact(
        CashSpend.kind,
        "bank-cost",
        {"period": "2026-01", "amount": 100},
        evidence=(proof,),
        expected_revision=0,
        request_id="save-cash-original",
    )
    publish(engine, ["bank-cost"], request="publish-cash-original")
    periods = Periods(engine)
    close_months(periods, proof, last="2026-02")
    january = periods.closed_report("2026-01")["owner_review"]["accounting_summary"]
    assert january["actual_payments_fen"] == 100

    engine.save_fact(
        CashSpend.kind,
        "bank-cost",
        {"period": "2026-01", "amount": 120},
        evidence=(proof,),
        expected_revision=1,
        request_id="save-cash-correction",
    )
    publish(
        engine,
        ["bank-cost"],
        request="publish-cash-correction",
        posting_period="2026-03",
    )
    close_months(periods, proof, first="2026-03", last="2026-03")
    march = periods.closed_report("2026-03")["owner_review"]
    assert march["accounting_summary"]["actual_receipts_fen"] == 0
    assert march["accounting_summary"]["actual_payments_fen"] == 0
    corrections = [item for item in march["business_summary"] if item["action"] == "correction"]
    assert {(item["reversal"], item["business_amount_fen"]) for item in corrections} == {
        (True, -100),
        (False, 120),
    }


def test_integrity_rejects_rehashed_incomplete_owner_review_directory(engine):
    save(engine)
    publish(engine)
    manifest = close(engine)
    directory = next(
        item for item in manifest["owner_review"]["collections"] if item["section"] == "vouchers"
    )
    directory["blocks"] = []
    directory["total_count"] = 0
    directory["root_digest"] = digest([]).hex()
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        with pytest.raises(KernelError) as failure:
            verify_owner_review_integrity(connection, engine, manifest)
    assert failure.value.details["reason"] == "owner_review_semantic_mismatch"


def test_close_verification_batches_inventory_and_shared_evidence(engine, monkeypatch):
    save(engine)
    publish(engine)
    close(engine)
    periods = Periods(engine)
    proof = evidence(engine)
    for category in MATERIAL_CATEGORIES:
        periods.inventory(
            "2026-02",
            category,
            evidence=[],
            expected=0,
            no_business=True,
            confirmation_evidence=proof,
            request_id="february-" + category,
        )
    preview = periods.preview_close("2026-02", owner_confirmation=proof)
    periods.close(
        "2026-02",
        owner_confirmation=proof,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="february-close",
    )
    evidence_hashes = []
    original = hashlib.sha256

    def counted(data=b"", *args, **kwargs):
        if data == b"business evidence":
            evidence_hashes.append(data)
        return original(data, *args, **kwargs)

    monkeypatch.setattr(hashlib, "sha256", counted)
    statements = []
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        connection.set_trace_callback(statements.append)
        assert verify_integrity(engine, connection, include_indexes=False)["status"] == "verified"
    inventory_reads = [sql for sql in statements if "FROM material_revision" in sql]
    # Full owner-review verification rebuilds the frozen inventory summary in
    # bounded batches; it must never regress to one query per category/month.
    assert len(inventory_reads) <= 5
    assert len(evidence_hashes) == 1


def test_opening_month_is_not_an_empty_month_that_can_be_skipped(tmp_path):
    from test_integrity_content import opening_engine

    engine = opening_engine(tmp_path)
    proof = evidence(engine)
    engine.save_fact(
        "test_opening",
        "opening",
        {"period": "2026-01", "amount": 100},
        evidence=(proof,),
        expected_revision=0,
        request_id="opening",
    )
    publish(engine, ["opening"])
    with pytest.raises(KernelError) as failure:
        Periods(engine).preview_close("2026-02", owner_confirmation=proof)
    assert failure.value.code == "earlier_period_open"
    manifest = close(engine)
    assert manifest["opening_calculation_id"] is not None
    assert verify(engine)["status"] == "verified"


class Cumulative(Fact):
    kind: ClassVar[str] = "test_cumulative_close"

    def reads(self):
        return (Read("calculation", self.kind, "*", before_period=self.period),)


def test_cumulative_dependencies_are_not_repeated_in_each_close(tmp_path):
    registry = Registry()

    def calculate(version, context):
        parents = context.select(
            Read("calculation", Cumulative.kind, "*", before_period=version.fact.period)
        )
        return Outcome(
            (Line("5602", debit=100), Line("2202", credit=100)), {"parent_count": len(parents)}
        )

    registry.register(Cumulative, calculate)
    engine = Engine(
        Store.create(
            tmp_path / "cumulative.sqlite",
            test_bundle(registry),
            "direct",
            "91310000123456789A",
            "direct-db",
        )
    )
    periods = Periods(engine)
    owner = evidence(engine)
    manifests = []
    for month in ("2026-01", "2026-02", "2026-03"):
        subject = "source-" + month
        engine.save_fact(
            Cumulative.kind,
            subject,
            {"period": month},
            evidence=(owner,),
            expected_revision=0,
            request_id="save-" + month,
        )
        publish(engine, [subject], request="publish-" + month)
        for category in MATERIAL_CATEGORIES:
            periods.inventory(
                month,
                category,
                evidence=[],
                expected=0,
                no_business=True,
                confirmation_evidence=owner,
                request_id=f"{month}-{category}",
            )
        preview = periods.preview_close(month, owner_confirmation=owner)
        periods.close(
            month,
            owner_confirmation=owner,
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id="close-" + month,
        )
        manifests.append(periods.closed_report(month))
    assert [len(item["adopted_results"]) for item in manifests] == [1, 1, 1]
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM dependency_calculation").fetchone()[0] == 3
        roots = [
            json.loads(row[0])["adopted_results"]
            for row in connection.execute("SELECT manifest FROM period_close")
        ]
        assert sum(map(len, roots)) == 3
    assert verify(engine)["status"] == "verified"
