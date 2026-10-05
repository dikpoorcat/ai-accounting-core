"""Complete verified sources independently require each open correction reversal."""

import pytest
from test_integrity_content import damage, verify
from test_publication_periods import close, publish, save
from test_publication_periods import company as company  # noqa: F401

from ai_accounting.kernel import publication, publication_v1
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.integrity import _check_sources
from ai_accounting.kernel.maintenance import Maintenance


@pytest.fixture(params=[publication, publication_v1], ids=["current", "fixed_v1"])
def reader(request):
    return request.param


def prepare(company, mode):
    revision = 0
    save(company, 0 if mode == "zero_baseline" else 100)
    publish(company, "initial")
    if mode in {"review_baseline", "cleared_baseline"}:
        revision += 1
        save(company, 0 if mode == "cleared_baseline" else 100, revision)
        publish(company, "baseline-review-or-clear")
    close(company, "2026-01")
    revision += 1
    save(company, 0 if mode == "zero_correction" else 120, revision)
    publish(company, "correction", "2026-03")
    if mode in {"same_month_replace", "move_review"}:
        revision += 1
        save(company, 130, revision, period="2026-04" if mode == "move_review" else "2026-01")
        publish(company, "replace-correction")
    if mode == "move_review":
        revision += 1
        save(company, 130, revision, period="2026-04")
        publish(company, "review-moved-correction")
    if mode == "second_closed_correction":
        close(company, "2026-03")
        revision += 1
        save(company, 140, revision)
        publish(company, "second-correction", "2026-05")


def sources(company):
    engine = company[0]
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        return _check_sources(engine, connection)


def check(reader, company, source):
    with company[0].store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        return reader.verify_open_correction_heads(
            connection, source["calculations"], source["publications"], source["vouchers"],
        )


@pytest.mark.parametrize("mode", [
    "review_baseline", "zero_baseline", "cleared_baseline", "zero_correction",
    "same_month_replace", "move_review", "second_closed_correction",
])
def test_open_correction_expected_versions_follow_real_publication_segments(company, reader, mode):
    prepare(company, mode)
    source = sources(company)
    reverse_count = sum(row["reverses_id"] is not None for row in source["vouchers"].values())
    assert reverse_count == (
        0 if mode in {"zero_baseline", "cleared_baseline"}
        else 2 if mode in {"move_review", "second_closed_correction"} else 1
    )
    assert check(reader, company, source) is None


@pytest.mark.parametrize("mode", ["zero_correction", "move_review"])
def test_missing_reverse_version_cannot_disappear_from_verified_expected_set(company, reader, mode):
    prepare(company, mode)
    source = sources(company)
    assert check(reader, company, source) is None
    with company[0].store.connection(read_only=True) as connection:
        reverse = dict(connection.execute(
            "SELECT v.* FROM voucher_current h JOIN voucher_version v ON v.id=h.version_id "
            "WHERE v.reverses_id IS NOT NULL AND NOT EXISTS("
            "SELECT 1 FROM period_close p WHERE p.period=v.period)"
        ).fetchone())
    damage(company[0], "voucher_current", "DELETE FROM voucher_current WHERE voucher_id=?",
           (reverse["voucher_id"],), foreign_keys=False)
    damage(company[0], "voucher_line", "DELETE FROM voucher_line WHERE version_id=?",
           (reverse["id"],), foreign_keys=False)
    damage(company[0], "voucher_version", "DELETE FROM voucher_version WHERE id=?",
           (reverse["id"],), foreign_keys=False)
    # Every remaining immutable row was strictly checked before the damage.
    # Removing the absent version from that scope must not remove its obligation.
    source["vouchers"].pop(reverse["id"])
    with pytest.raises(KernelError) as failure:
        check(reader, company, source)
    assert failure.value.details["reason"] == "correction_voucher_source_missing"


def test_missing_reverse_source_is_rejected_before_projection_repair(company):
    prepare(company, "review_baseline")
    engine = company[0]
    assert verify(engine, include_projections=False, include_indexes=False)["status"] == "verified"
    with engine.store.connection(read_only=True) as connection:
        reverse = dict(connection.execute(
            "SELECT * FROM voucher_version WHERE reverses_id IS NOT NULL"
        ).fetchone())
    damage(engine, "voucher_current", "DELETE FROM voucher_current WHERE voucher_id=?",
           (reverse["voucher_id"],), foreign_keys=False)
    damage(engine, "voucher_line", "DELETE FROM voucher_line WHERE version_id=?",
           (reverse["id"],), foreign_keys=False)
    damage(engine, "voucher_version", "DELETE FROM voucher_version WHERE id=?",
           (reverse["id"],), foreign_keys=False)

    def stored():
        with engine.store.connection(read_only=True) as connection:
            return {
                table: [tuple(row) for row in connection.execute("SELECT * FROM " + table)]
                for table in ("state", "monthly_account", "period_balance",
                              "calculation", "calculation_publication", "voucher_current")
            }

    before = stored()
    with pytest.raises(KernelError) as failure:
        verify(engine, include_projections=False, include_indexes=False)
    assert failure.value.details["reason"] == "correction_voucher_source_missing"
    with pytest.raises(KernelError) as failure:
        Maintenance(engine).rebuild_projections(request_id="missing-reverse-repair")
    assert failure.value.details["reason"] == "correction_voucher_source_missing"
    assert stored() == before


def test_fixed_correction_proof_does_not_call_current_encoding_or_proof(company, monkeypatch):
    prepare(company, "move_review")
    source = sources(company)

    def forbidden(*args, **kwargs):
        pytest.fail("fixed v1 used mutable current correction proof or encoding")

    with company[0].store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        monkeypatch.setattr(publication, "digest", forbidden)
        monkeypatch.setattr(publication, "canonical", forbidden)
        monkeypatch.setattr(publication, "verify_open_correction_heads", forbidden)
        assert publication_v1.verify_open_correction_heads(
            connection, source["calculations"], source["publications"], source["vouchers"],
        ) is None
