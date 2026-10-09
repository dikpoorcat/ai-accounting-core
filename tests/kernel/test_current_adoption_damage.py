"""A selected open voucher must retain its formal current adoption."""

import pytest
from test_banking import book as _bank_book
from test_banking import funding
from test_engine import close as close_simple
from test_engine import engine as _simple_engine
from test_engine import publish as publish_simple
from test_engine import save as save_simple
from test_integrity_content import damage
from test_payroll import payroll
from test_payroll_corrections import company as _payroll_company
from test_reimbursement_assets import accepted_batch, activation, batch_card

from ai_accounting.kernel.asset_batches import AssetBatches
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.query_reads import QueryReads, verify_current_voucher_publications
from ai_accounting.kernel.types import YearMonth


@pytest.fixture
def bank_book(tmp_path):
    return _bank_book.__wrapped__(tmp_path)


@pytest.fixture
def payroll_company(tmp_path):
    return _payroll_company.__wrapped__(tmp_path)


@pytest.fixture
def simple_engine(tmp_path):
    return _simple_engine.__wrapped__(tmp_path)


def _delete_current_publication(engine, subject):
    with engine.store.connection(read_only=True) as connection:
        publication = connection.execute(
            "SELECT p.id FROM calculation_current c JOIN calculation_publication p "
            "ON p.calculation_id=c.calculation_id WHERE c.subject_id=?",
            (subject,),
        ).fetchone()[0]
    damage(
        engine,
        "calculation_publication",
        "DELETE FROM calculation_publication WHERE id=?",
        (publication,),
        foreign_keys=False,
    )


def _current_vouchers(engine, subject):
    with engine.store.connection(read_only=True) as connection:
        return {
            row[0]
            for row in connection.execute(
                "SELECT v.id FROM voucher_current h JOIN voucher_version v "
                "ON v.id=h.version_id JOIN calculation c ON c.id=v.calculation_id "
                "WHERE c.subject_id=?",
                (subject,),
            )
        }


def _assert_missing_current_publication(engine, subject):
    with engine.store.connection(read_only=True) as connection:
        with pytest.raises(KernelError) as failure:
            verify_current_voucher_publications(connection, _current_vouchers(engine, subject))
    assert failure.value.code == "content_integrity_failed"


def _assert_current_publication(engine, subject):
    with engine.store.connection(read_only=True) as connection:
        verify_current_voucher_publications(connection, _current_vouchers(engine, subject))


def test_funds_rejects_missing_current_publication_of_posted_voucher(bank_book):
    engine, save, publish, _ = bank_book
    funding(save, publish)
    assert Dashboard(engine).funds("2026-09")["data"] is not None
    _assert_current_publication(engine, "funding")
    _delete_current_publication(engine, "funding")
    _assert_missing_current_publication(engine, "funding")
    with pytest.raises(KernelError, match="发布|采用|来源") as failure:
        Dashboard(engine).funds("2026-09")
    assert failure.value.code == "content_integrity_failed"


def test_assets_rejects_missing_current_publication(bank_book):
    engine, save, publish, _ = bank_book
    save(
        "asset",
        "asset",
        {
            "period": "2026-09",
            "asset_id": "machine",
            "asset_type": "fixed",
            "supplier_id": "supplier",
            "acquisition_date": "2026-09-01",
            "cost_fen": 1000,
            "acquisition_basis": "direct_purchase",
        },
    )
    publish("asset")
    assert Dashboard(engine).assets("2026-09")["data"] is not None
    _assert_current_publication(engine, "asset")
    _delete_current_publication(engine, "asset")
    _assert_missing_current_publication(engine, "asset")
    with pytest.raises(KernelError, match="发布|采用|来源"):
        Dashboard(engine).assets("2026-09")


def test_employees_rejects_missing_current_publication(payroll_company):
    company = payroll_company
    company.publish("january")
    assert Dashboard(company.engine).employees("2026-01", employee_filter="all")["data"] is not None
    _assert_current_publication(company.engine, "january")
    _delete_current_publication(company.engine, "january")
    _assert_missing_current_publication(company.engine, "january")
    with pytest.raises(KernelError, match="发布|采用|来源"):
        Dashboard(company.engine).employees("2026-01", employee_filter="all")


def test_employees_rejects_missing_no_impact_review_publication(payroll_company):
    company = payroll_company
    company.publish("january")
    company.save(payroll(), "january", revision=1)
    company.confirm_payroll("january")
    preview, _ = company.publish("january")
    assert preview["results"][0]["impact"] == "review_no_impact"
    assert Dashboard(company.engine).employees("2026-01", employee_filter="all")["data"] is not None
    _assert_current_publication(company.engine, "january")
    _delete_current_publication(company.engine, "january")
    _assert_missing_current_publication(company.engine, "january")
    with pytest.raises(KernelError) as failure:
        Dashboard(company.engine).employees("2026-01", employee_filter="all")
    assert failure.value.code == "content_integrity_failed"


def test_current_open_reversal_uses_its_correction_publication(simple_engine):
    save_simple(simple_engine)
    publish_simple(simple_engine)
    close_simple(simple_engine)
    save_simple(simple_engine, amount=150, revision=1, request="amend")
    publish_simple(simple_engine, request="correct", posting_period="2026-02")
    with simple_engine.store.connection(read_only=True) as connection:
        rows = list(
            connection.execute(
                "SELECT v.id,v.reverses_id FROM voucher_current h "
                "JOIN voucher_version v ON v.id=h.version_id"
            )
        )
        assert any(row["reverses_id"] is not None for row in rows)
        verify_current_voucher_publications(connection, {row["id"] for row in rows})


def test_current_adoption_reuses_only_successful_same_snapshot_digest(simple_engine, monkeypatch):
    from ai_accounting.kernel import publication

    save_simple(simple_engine)
    publish_simple(simple_engine)
    with simple_engine.store.connection(read_only=True) as connection:
        vouchers = {row[0] for row in connection.execute("SELECT version_id FROM voucher_current")}
    original = publication.verify_record
    checked = []

    def observe(row):
        checked.append(row["id"])
        return original(row)

    monkeypatch.setattr(publication, "verify_record", observe)
    with QueryReads.snapshot(simple_engine) as reads:
        reads.verify_publication_periods({YearMonth("2026-01").ordinal})
        verify_current_voucher_publications(reads.connection, vouchers)
        assert not checked
    with QueryReads.snapshot(simple_engine) as reads:
        verify_current_voucher_publications(reads.connection, vouchers)
        assert len(checked) == 1

    damage(
        simple_engine,
        "calculation_publication",
        "UPDATE calculation_publication SET mode='review_no_impact'",
    )
    with QueryReads.snapshot(simple_engine) as reads:
        with pytest.raises(KernelError, match="正式发布关系"):
            reads.verify_publication_periods({YearMonth("2026-01").ordinal})
        assert not reads._verified_publications
        with pytest.raises(KernelError, match="正式发布关系"):
            verify_current_voucher_publications(reads.connection, vouchers)


def test_asset_batch_member_needs_owner_adoption_not_own_publication(bank_book):
    engine, save, publish, proof = bank_book
    save("reimbursed_asset_batch", "batch", accepted_batch())
    save("reimbursed_asset", "computer", batch_card())
    publish("batch", "computer")
    members = [{"subject_id": "computer-use", "expected_revision": 0, "data": activation()}]
    options = {"evidence": (proof,), "expected_revision": 0}
    batches = AssetBatches(engine)
    preview = batches.prepare_activation_batch("activation-batch", "2026-02", members, **options)
    batches.confirm_activation_batch(
        "activation-batch",
        "2026-02",
        members,
        **options,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="activate-batch",
    )
    with engine.store.connection(read_only=True) as connection:
        member = connection.execute(
            "SELECT member_calculation_id FROM asset_batch_member LIMIT 1"
        ).fetchone()[0]
        assert (
            connection.execute(
                "SELECT 1 FROM calculation_publication WHERE calculation_id=?", (member,)
            ).fetchone()
            is None
        )
    _assert_current_publication(engine, "batch")
