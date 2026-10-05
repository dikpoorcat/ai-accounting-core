"""Typed owner facts prove kind before either identity lane excludes owners."""

import pytest
from test_asset_batch_reads import prepare_batch_assets
from test_asset_batches import month
from test_asset_owner_frozen_scope import amend_activation
from test_asset_owner_frozen_scope import owner_book as _owner_book
from test_integrity_content import damage, verify
from test_payroll_corrections import Company

from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.types import YearMonth

owner_book = _owner_book


def assert_no_owner_identity_success(reads, ordinal):
    assert ("asset_owner_identity_selection", ordinal) not in reads._report_snapshot_cache
    assert ("asset_activation_identity_events", ordinal) not in reads._report_snapshot_cache
    assert not reads._asset_members


def private_lanes(snap):
    return (
        lambda: snap.queries._selected_asset_activation_identities(snap.connection, snap.period),
        lambda: snap.queries._selected_asset_member_heads(
            snap.connection, snap.period, asset_ids={"computer", "chair"},
        ),
    )


def change_owner_kind(engine, kind, target):
    with engine.store.connection(read_only=True) as connection:
        owner = dict(connection.execute(
            "SELECT * FROM calculation WHERE kind=? ORDER BY period LIMIT 1", (kind,),
        ).fetchone())
        original_fact = dict(connection.execute(
            "SELECT * FROM fact_revision WHERE id=?", (owner["fact_id"],),
        ).fetchone())
    damage(engine, "calculation", "UPDATE calculation SET kind=? WHERE id=?",
           (target, owner["id"]))
    damage(engine, "subject", "UPDATE subject SET kind=? WHERE id=?",
           (target, owner["subject_id"]))
    with engine.store.connection(read_only=True) as connection:
        actual = dict(connection.execute(
            "SELECT * FROM calculation WHERE id=?", (owner["id"],),
        ).fetchone())
        assert {k: v for k, v in actual.items() if k != "kind"} == {
            k: v for k, v in owner.items() if k != "kind"
        }
        assert dict(connection.execute(
            "SELECT * FROM fact_revision WHERE id=?", (owner["fact_id"],),
        ).fetchone()) == original_fact
    return owner


def assert_private_lanes_reject(engine, period, *, warm):
    from ai_accounting.kernel.contracts import KernelError

    # Every new snapshot must repeat the typed proof, including one whose
    # frozen declarations and matching mutable identity headers were warmed.
    for _ in range(2):
        with Dashboard(engine)._snapshot(period) as snap:
            if warm:
                snap.queries._frozen_asset_owner_sources(snap.connection, YearMonth(snap.period))
            typed_before = snap.reads._fact_versions.copy()
            for consume in private_lanes(snap):
                with pytest.raises(KernelError) as failure:
                    consume()
                assert failure.value.code == "content_integrity_failed"
                assert failure.value.details["reason"] in {
                    "owner_typed_fact_missing", "invalid_stored_content",
                }
                assert_no_owner_identity_success(snap.reads, snap.month)
                assert snap.reads._fact_versions == typed_before
    with pytest.raises(KernelError):
        Dashboard(engine).assets(period, preparation="deferred")
    with pytest.raises(KernelError):
        verify(engine)


@pytest.mark.parametrize("warm", [False, True])
@pytest.mark.parametrize("kind,target", [
    ("asset_activation_batch", "asset_consumption_month"),
    ("asset_consumption_month", "asset_activation_batch"),
])
def test_joint_owner_kind_change_is_rejected_in_both_private_lanes(owner_book, kind, target, warm):
    company, _ = owner_book
    change_owner_kind(company.engine, kind, target)
    assert_private_lanes_reject(company.engine, "2026-03", warm=warm)


@pytest.mark.parametrize("warm", [False, True])
def test_empty_consumption_directory_does_not_prove_owner_kind(tmp_path, warm):
    company = Company(tmp_path / "empty-owner-kind.sqlite")
    prepare_batch_assets(company)
    with company.engine.store.connection(read_only=True) as connection:
        proof = company.engine.store.current_fact(connection, "computer").evidence[0]
    month(company.engine, proof, "2026-02", "empty-kind-owner")
    company.close("2026-02")
    with Dashboard(company.engine)._snapshot("2026-02") as snap:
        selected = snap.queries._asset_owner_metadata_selection(snap.connection, snap.period)
        zero = next(ident for ident, owner in selected[1].items()
                    if owner["kind"] == "asset_consumption_month")
        from ai_accounting.kernel.types import digest

        assert selected[2][zero] == digest([]).hex()
        assert any(event["calculation_id"] == zero for event in selected[0])
    change_owner_kind(company.engine, "asset_consumption_month", "asset_activation_batch")
    assert_private_lanes_reject(company.engine, "2026-02", warm=warm)


def assert_private_lanes_succeed(engine, period):
    with Dashboard(engine)._snapshot(period) as snap:
        activation, consumption = (consume() for consume in private_lanes(snap))
        assert {item["asset_id"] for item in activation} == {"computer", "chair"}
        assert ("asset_owner_identity_selection", snap.month) in snap.reads._report_snapshot_cache
        assert all(item["kind"] == "asset_consumption" for item in consumption)
    assert verify(engine)["status"] == "verified"


def test_typed_kind_proof_keeps_no_impact_old_voucher_new_basis(tmp_path):
    company = Company(tmp_path / "review-kind-owner.sqlite")
    prepare_batch_assets(company)
    proof = company.engine.register_evidence(
        b"Confirmed unchanged activation facts", "text/plain", "unchanged activation",
        request_id="kind-review-proof",
    )["digest"]
    result = amend_activation(
        company, proof, revision=1, posting_period="2026-02", benefit_area="administration",
    )
    assert any(item["impact"] == "review_no_impact" for item in result["results"])
    company.close("2026-02")
    assert_private_lanes_succeed(company.engine, "2026-02")


def test_typed_kind_proof_keeps_continuous_closed_corrections(owner_book):
    company, proof = owner_book
    amend_activation(company, proof, revision=1, posting_period="2026-04", benefit_area="sales")
    month(company.engine, proof, "2026-04", "kind-april")
    company.close("2026-04")
    assert_private_lanes_succeed(company.engine, "2026-04")
    amend_activation(
        company, proof, revision=2, posting_period="2026-05", benefit_area="administration",
    )
    month(company.engine, proof, "2026-05", "kind-may")
    company.close("2026-05")
    assert_private_lanes_succeed(company.engine, "2026-05")
