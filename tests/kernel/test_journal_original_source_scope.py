"""Different original months do not expand money proof to sibling sources."""

import pytest
from test_engine import close, evidence, publish, save
from test_engine import engine as engine  # noqa: F401
from test_integrity_content import damage

from ai_accounting.kernel import integrity
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods


def _close_february(engine):
    periods, proof = Periods(engine), evidence(engine)
    for category in MATERIAL_CATEGORIES:
        periods.inventory(
            "2026-02", category, evidence=[], expected=0, no_business=True,
            confirmation_evidence=proof, request_id=f"feb-inventory-{category}",
        )
    preview = periods.preview_close("2026-02", owner_confirmation=proof)
    periods.close("2026-02", owner_confirmation=proof, preview_digest=preview["digest"],
                  epochs=preview["epochs"], request_id="close-feb")


def _different_original_months(engine, *, review=False):
    save(engine, subject="a", amount=100, request="a")
    save(engine, subject="b", amount=200, request="b")
    _, january = publish(engine, ["a", "b"])
    close(engine)
    old_b = next(row["calculation_id"] for row in january["results"] if row["subject_id"] == "b")
    save(engine, subject="b", amount=240, revision=1, request="b-feb")
    _, february = publish(engine, ["b"], request="pub-feb", posting_period="2026-02")
    adopted_b = february["results"][0]["calculation_id"]
    if review:
        save(engine, subject="b", amount=240, revision=2, request="b-review")
        _, reviewed = publish(engine, ["b"], request="pub-review")
        adopted_b = reviewed["results"][0]["calculation_id"]
    _close_february(engine)
    save(engine, subject="a", amount=150, revision=1, request="a-mar")
    save(engine, subject="b", amount=260, revision=3 if review else 2, request="b-mar")
    publish(engine, ["a", "b"], request="pub-mar", posting_period="2026-03")
    return old_b, adopted_b


@pytest.mark.parametrize("review", [False, True])
def test_exact_originals_decode_four_sources_or_the_necessary_no_impact_adoption(
    engine, monkeypatch, record_property, review
):
    old_b, adopted_b = _different_original_months(engine, review=review)
    decoded, original = [], integrity._object

    def observe(raw, component, ident):
        value = original(raw, component, ident)
        if component == "calculation":
            decoded.append((ident, len(raw)))
        return value

    monkeypatch.setattr(integrity, "_object", observe)
    with Dashboard(engine)._snapshot("2026-03") as snap:
        selected = list(snap.connection.execute(*snap.month_journal.sql()))
        originals = snap.reads.vouchers(
            row["reverses_id"] for row in selected if row["reverses_id"]
        )
        expected = {
            row[key] for row in selected
            for key in ("basis_calculation_id", "voucher_calculation_id")
        } | {row["calculation_id"] for row in originals.values()} | {adopted_b}
        decoded.clear()
        assert snap.month_journal.account_amounts() == {
            "5602": [410, 340], "2202": [340, 410],
        }
        assert len(expected) == (5 if review else 4)
        assert snap.reads._verified_source_contents.keys() == expected
        assert {ident for ident, _ in decoded} == expected
        assert len(decoded) == len(expected)
        assert old_b not in expected
        record_property("consumed_result_decodes", len(decoded))
        record_property("consumed_result_bytes", sum(size for _, size in decoded))


def test_unconsumed_sibling_body_is_not_read_but_complete_core_still_rejects_it(engine):
    old_b, _ = _different_original_months(engine)
    damage(engine, "calculation", "UPDATE calculation SET outcome=? WHERE id=?",
           ('{"values":{},"lines":[],"balances":[]}', old_b))
    with Dashboard(engine)._snapshot("2026-03") as snap:
        assert snap.month_journal.account_amounts() == {
            "5602": [410, 340], "2202": [340, 410],
        }
        assert old_b not in snap.reads._verified_source_contents
    with engine.store.connection(read_only=True) as connection:
        with pytest.raises(KernelError) as failure:
            integrity.verify_integrity(engine, connection)
        assert failure.value.code == "content_integrity_failed"


@pytest.mark.parametrize("corruption", [
    "original_body", "frozen_owner", "line", "adopted_publication",
])
def test_each_actual_original_and_reviewed_frozen_adoption_still_rejects_damage(engine, corruption):
    _, adopted_b = _different_original_months(engine, review=True)
    with Dashboard(engine)._snapshot("2026-03") as snap:
        rows = list(snap.connection.execute(*snap.month_journal.sql()))
        original = next(snap.reads.voucher(row["reverses_id"]) for row in rows
                        if row["reverses_id"] and row["basis_subject_id"] == "b")
    if corruption == "original_body":
        damage(engine, "calculation", "UPDATE calculation SET outcome=? WHERE id=?",
               ('{"values":{},"lines":[],"balances":[]}', adopted_b))
    elif corruption == "frozen_owner":
        # Valid equal-line reviewed content must not replace the independently
        # frozen original voucher owner, even in a multi-original-month batch.
        damage(engine, "voucher_version", "UPDATE voucher_version SET calculation_id=? WHERE id=?",
               (adopted_b, original["id"]))
    elif corruption == "line":
        damage(engine, "voucher_line", "DELETE FROM voucher_line WHERE version_id=? AND line_no=1",
               (original["id"],))
    else:
        damage(engine, "calculation_publication",
               "UPDATE calculation_publication SET posting_period=posting_period+1 "
               "WHERE calculation_id=?", (adopted_b,))
    with Dashboard(engine)._snapshot("2026-03") as snap:
        with pytest.raises(KernelError) as failure:
            snap.month_journal.account_amounts()
        assert failure.value.code == "content_integrity_failed"
        assert snap.month_journal.verified_rows() is None


def test_original_reverse_directory_does_not_replace_exact_frozen_authority(engine):
    _different_original_months(engine, review=True)
    with Dashboard(engine)._snapshot("2026-03") as snap:
        rows = list(snap.connection.execute(*snap.month_journal.sql()))
        original = next(snap.reads.voucher(row["reverses_id"]) for row in rows
                        if row["reverses_id"] and row["basis_subject_id"] == "b")
    damage(engine, "close_reference",
           "UPDATE close_reference SET position='99999' WHERE reference_type='voucher' "
           "AND reference_id=?", (original["id"],))
    # The original is authenticated directly against its independent close
    # slice, not selected through this damaged reverse directory. A complete
    # read-index verification still refuses to bless the directory corruption.
    with Dashboard(engine)._snapshot("2026-03") as snap:
        assert snap.month_journal.account_amounts() == {
            "5602": [410, 340], "2202": [340, 410],
        }
    with engine.store.connection(read_only=True) as connection:
        with pytest.raises(KernelError) as failure:
            integrity.verify_integrity(engine, connection)
        assert failure.value.code == "read_index_integrity_failed"
