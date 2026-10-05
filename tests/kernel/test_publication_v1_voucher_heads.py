"""Fixed v1 forward heads use real synthetic publications and fixed decoding.

These cases prove missing pointers and stale pointer targets. They do not claim
independent coverage of absent reversal versions or combined source deletions;
the complete v1 source verifier keeps its own checks.
"""

import json

import pytest
from test_integrity_content import damage
from test_publication_periods import close, publish, save
from test_publication_periods import company as company  # noqa: F401

from ai_accounting.kernel import publication, publication_v1, query_reads, stored_json
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.history_encoding_v1 import digest
from ai_accounting.kernel.types import YearMonth


def read_heads(company, through="2026-04", *, month=None, subjects=None):
    with company[0].store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        return publication_v1.verify_open_voucher_heads(
            connection, YearMonth(through).ordinal,
            posting_period=YearMonth(month).ordinal if month else None,
            subject_ids=subjects,
        )


def prepare(company, mode):
    save(company, 0 if mode == "initial_zero" else 100)
    publish(company, "initial")
    if mode in {"initial", "initial_zero"}:
        return
    if mode in {"review", "clear"}:
        save(company, 100 if mode == "review" else 0, 1)
        publish(company, "second")
        if mode == "review":
            save(company, 100, 2)
            publish(company, "third-review")
        return
    close(company, "2026-01")
    if mode == "closed_review":
        save(company, 100, 1)
        publish(company, "closed-review")
        return
    save(company, 0 if mode == "correction_zero" else 120, 1)
    publish(company, "correction", "2026-03")
    if mode in {"replace", "move"}:
        save(company, 130, 2, period="2026-04" if mode == "move" else "2026-01")
        publish(company, "replace-correction")


@pytest.mark.parametrize("mode", [
    "initial", "initial_zero", "review", "clear", "closed_review",
    "correction", "correction_zero", "replace", "move",
])
def test_fixed_v1_legal_head_states(company, mode):
    prepare(company, mode)
    with company[0].store.connection(read_only=True) as connection:
        before = [tuple(row) for row in connection.execute(
            "SELECT voucher_id,version_id FROM voucher_current ORDER BY voucher_id"
        )]
        if mode == "clear":
            assert before == []
            assert connection.execute(
                "SELECT voucher_id FROM calculation_publication ORDER BY sequence DESC LIMIT 1"
            ).fetchone()[0] is not None
        if mode == "correction_zero":
            assert connection.execute(
                "SELECT voucher_id FROM calculation_publication ORDER BY sequence DESC LIMIT 1"
            ).fetchone()[0] is None
            assert len(connection.execute(
                "SELECT v.id FROM voucher_current h JOIN voucher_version v "
                "ON v.id=h.version_id WHERE v.reverses_id IS NOT NULL"
            ).fetchall()) == 1
    assert read_heads(company) is None
    assert read_heads(company) is None
    with company[0].store.connection(read_only=True) as connection:
        assert [tuple(row) for row in connection.execute(
            "SELECT voucher_id,version_id FROM voucher_current ORDER BY voucher_id"
        )] == before


@pytest.mark.parametrize("mode", ["initial", "review"])
@pytest.mark.parametrize("previous_success", [False, True])
def test_fixed_v1_missing_normal_head_fails_after_optional_success(company, mode, previous_success):
    prepare(company, mode)
    if previous_success:
        assert read_heads(company) is None
    damage(company[0], "voucher_current", "DELETE FROM voucher_current", foreign_keys=False)
    for _ in range(2):
        with pytest.raises(KernelError) as failure:
            read_heads(company)
        assert failure.value.details["reason"] == "nonzero_publication_head_missing"


@pytest.mark.parametrize("review", [False, True])
def test_fixed_v1_old_normal_version_is_not_current_adoption(company, review):
    prepare(company, "initial")
    with company[0].store.connection(read_only=True) as connection:
        old = connection.execute("SELECT version_id FROM voucher_current").fetchone()[0]
    save(company, 120, 1)
    publish(company, "replace")
    if review:
        save(company, 120, 2)
        publish(company, "review-replacement")
    assert read_heads(company) is None
    damage(company[0], "voucher_current", "UPDATE voucher_current SET version_id=?", (old,))
    with pytest.raises(KernelError) as failure:
        read_heads(company)
    assert failure.value.details["reason"] == "current_voucher_head_mismatch"


@pytest.mark.parametrize("mode", ["correction", "correction_zero", "replace", "move"])
def test_fixed_v1_missing_reversal_head_is_not_zero_or_old_history(company, mode):
    prepare(company, mode)
    assert read_heads(company) is None
    with company[0].store.connection(read_only=True) as connection:
        reverse = connection.execute(
            "SELECT v.voucher_id FROM voucher_current h JOIN voucher_version v "
            "ON v.id=h.version_id WHERE v.reverses_id IS NOT NULL"
        ).fetchone()[0]
    damage(company[0], "voucher_current", "DELETE FROM voucher_current WHERE voucher_id=?",
           (reverse,), foreign_keys=False)
    with pytest.raises(KernelError) as failure:
        read_heads(company)
    assert failure.value.details["reason"] == "current_reversal_head_mismatch"


def test_fixed_v1_old_reversal_version_after_source_month_move_is_rejected(company):
    prepare(company, "correction")
    with company[0].store.connection(read_only=True) as connection:
        old = connection.execute(
            "SELECT v.voucher_id,v.id FROM voucher_current h JOIN voucher_version v "
            "ON v.id=h.version_id WHERE v.reverses_id IS NOT NULL"
        ).fetchone()
    save(company, 130, 2, period="2026-04")
    publish(company, "move")
    assert read_heads(company) is None
    damage(
        company[0], "voucher_current", "UPDATE voucher_current SET version_id=? WHERE voucher_id=?",
        (old["id"], old["voucher_id"]),
    )
    with pytest.raises(KernelError) as failure:
        read_heads(company)
    assert failure.value.details["reason"] == "current_reversal_head_mismatch"


@pytest.mark.parametrize("mutation", ["duplicate", "digest", "line_shape", "noncanonical"])
def test_fixed_v1_cleared_body_keeps_strict_fixed_json_and_digest(company, mutation):
    prepare(company, "clear")
    with company[0].store.connection(read_only=True) as connection:
        current = connection.execute(
            "SELECT c.id,c.outcome FROM calculation_current h JOIN calculation c "
            "ON c.id=h.calculation_id"
        ).fetchone()
    outcome = json.loads(current["outcome"])
    if mutation == "duplicate":
        raw = '{"lines":[],' + current["outcome"][1:]
        damage(company[0], "calculation", "UPDATE calculation SET outcome=? WHERE id=?",
               (raw, current["id"]))
    elif mutation == "digest":
        damage(company[0], "calculation", "UPDATE calculation SET digest=? WHERE id=?",
               (bytes(32), current["id"]))
    elif mutation == "line_shape":
        outcome["lines"] = {}
        damage(company[0], "calculation", "UPDATE calculation SET outcome=?,digest=? WHERE id=?",
               (json.dumps(outcome), digest(outcome), current["id"]))
    else:
        damage(company[0], "calculation", "UPDATE calculation SET outcome=? WHERE id=?",
               (json.dumps(outcome, indent=2), current["id"]))
    if mutation == "noncanonical":
        assert read_heads(company) is None
    else:
        with pytest.raises(KernelError):
            read_heads(company)


@pytest.mark.parametrize("mutation", ["head", "calculation", "fact", "subject_kind"])
def test_fixed_v1_terminal_source_metadata_cannot_disappear(company, mutation):
    prepare(company, "initial")
    mutations = {
        "head": ("calculation_current", "DELETE FROM calculation_current"),
        "calculation": ("calculation", "DELETE FROM calculation"),
        "fact": ("fact_revision", "DELETE FROM fact_revision"),
        "subject_kind": ("subject", "UPDATE subject SET kind='wrong-kind'"),
    }
    table, sql = mutations[mutation]
    damage(company[0], table, sql, foreign_keys=False)
    with pytest.raises(KernelError) as failure:
        read_heads(company)
    assert failure.value.details["reason"] == "current_source_identity"


def test_fixed_v1_cutoff_exact_month_and_subject_scope_exclude_future_damage(company):
    prepare(company, "initial")
    save(company, 200, period="2026-02", subject="future")
    publish(company, "future", subject="future")
    with company[0].store.connection(read_only=True) as connection:
        future = connection.execute(
            "SELECT voucher_id FROM calculation_publication WHERE subject_id='future'"
        ).fetchone()[0]
    damage(company[0], "voucher_current", "DELETE FROM voucher_current WHERE voucher_id=?",
           (future,), foreign_keys=False)
    assert read_heads(company, "2026-01") is None
    assert read_heads(company, "2026-02", month="2026-01") is None
    assert read_heads(company, "2026-02", subjects={"position"}) is None
    assert read_heads(company, "2026-02", subjects=set()) is None
    with pytest.raises(KernelError):
        read_heads(company, "2026-02")


@pytest.mark.parametrize("mode", ["review", "clear", "move"])
def test_fixed_v1_heads_do_not_call_current_publication_or_json_helpers(company, monkeypatch, mode):
    prepare(company, mode)

    def forbidden(*args, **kwargs):
        pytest.fail("fixed v1 called a mutable current implementation")

    with company[0].store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        monkeypatch.setattr(publication, "verify_record", forbidden)
        monkeypatch.setattr(query_reads, "verify_open_voucher_scope", forbidden)
        monkeypatch.setattr(query_reads, "verify_current_publication_voucher_heads", forbidden)
        monkeypatch.setattr(query_reads, "verify_current_voucher_publications", forbidden)
        monkeypatch.setattr(stored_json, "loads_unique", forbidden)
        monkeypatch.setattr(stored_json, "verify_outcome_bytes", forbidden)
        assert publication_v1.verify_open_voucher_heads(
            connection, YearMonth("2026-04").ordinal
        ) is None
