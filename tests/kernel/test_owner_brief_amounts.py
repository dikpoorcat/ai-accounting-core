"""Posted operating money keeps exact source trust without party balance-sheet work."""

import pytest
from test_engine import close, publish, save
from test_engine import engine as engine_fixture
from test_integrity_content import damage

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.types import YearMonth

engine = engine_fixture
PERIOD = "2026-01"
MONTH = YearMonth(PERIOD).ordinal


def _posted_pair(engine, *, closed=False):
    save(engine, subject="first", amount=100, request="save-first")
    publish(engine, ["first"], request="post-first")
    save(engine, subject="off-page", amount=200, request="save-second")
    publish(engine, ["off-page"], request="post-second")
    if closed:
        close(engine)
    response = Dashboard(engine).brief(PERIOD, section="vouchers", limit=1)
    data = response["data"]
    assert data["position"] == {
        "month_revenue_fen": 0, "month_expense_fen": 300,
        "month_result_fen": -300, "complete": True,
    }
    assert data["collections"]["vouchers"]["items"][0]["subject_id"] == "first"
    assert data["collections"]["vouchers"]["page"]["total_count"] == 2
    with engine.store.connection(read_only=True) as connection:
        return dict(connection.execute(
            "SELECT v.id,v.calculation_id,c.fact_id,c.outcome FROM voucher_version v "
            "JOIN calculation c ON c.id=v.calculation_id WHERE c.subject_id='off-page'"
        ).fetchone())


@pytest.mark.parametrize("closed", [False, True])
@pytest.mark.parametrize("corruption", [
    "missing_line", "projection_amount", "projection_same_net", "balanced_lines_and_projection",
    "counterbalanced_vouchers", "voucher_total", "calculation_seal", "fact_seal",
    "duplicate_outcome", "fact_amount",
])
def test_whole_month_amounts_reject_off_page_source_damage(engine, closed, corruption):
    row = _posted_pair(engine, closed=closed)
    if corruption == "missing_line":
        damage(engine, "voucher_line", "DELETE FROM voucher_line WHERE version_id=? AND line_no=1",
               (row["id"],))
    elif corruption == "projection_amount":
        damage(engine, "monthly_account",
               "UPDATE monthly_account SET debit=debit+1 WHERE period=? AND account='5602'",
               (MONTH,))
    elif corruption == "projection_same_net":
        damage(engine, "monthly_account",
               "UPDATE monthly_account SET debit=debit+1,credit=credit+1 "
               "WHERE period=? AND account='5602'", (MONTH,))
    elif corruption == "balanced_lines_and_projection":
        damage(engine, "voucher_line",
               "UPDATE voucher_line SET debit=CASE WHEN debit>0 THEN debit+1 ELSE 0 END,"
               "credit=CASE WHEN credit>0 THEN credit+1 ELSE 0 END WHERE version_id=?",
               (row["id"],))
        damage(engine, "voucher_version", "UPDATE voucher_version SET total=total+1 WHERE id=?",
               (row["id"],))
        damage(engine, "monthly_account",
               "UPDATE monthly_account SET debit=debit+CASE WHEN account='5602' THEN 1 ELSE 0 END,"
               "credit=credit+CASE WHEN account='2202' THEN 1 ELSE 0 END WHERE period=?", (MONTH,))
    elif corruption == "counterbalanced_vouchers":
        # Whole-month debit, credit, net and balance remain unchanged. The
        # immutable per-voucher sources still disagree with the stored lines.
        damage(engine, "voucher_line",
               "UPDATE voucher_line SET "
               "debit=CASE WHEN debit>0 THEN debit+CASE WHEN version_id=? THEN 1 ELSE -1 END "
               "ELSE 0 END,credit=CASE WHEN credit>0 THEN credit+CASE WHEN version_id=? "
               "THEN 1 ELSE -1 END ELSE 0 END",
               (row["id"], row["id"]))
        damage(engine, "voucher_version",
               "UPDATE voucher_version SET total=total+CASE WHEN id=? THEN 1 ELSE -1 END",
               (row["id"],))
    elif corruption == "voucher_total":
        damage(engine, "voucher_version", "UPDATE voucher_version SET total=total+1 WHERE id=?",
               (row["id"],))
    elif corruption in {"calculation_seal", "fact_seal"}:
        table, field, ident = (
            ("calculation_seal", "calculation_id", row["calculation_id"])
            if corruption == "calculation_seal" else ("fact_seal", "fact_id", row["fact_id"])
        )
        damage(engine, table, f"DELETE FROM {table} WHERE {field}=?", (ident,), foreign_keys=False)
    elif corruption == "duplicate_outcome":
        damage(engine, "calculation", "UPDATE calculation SET outcome=? WHERE id=?",
               ('{"values":{},' + row["outcome"][1:], row["calculation_id"]))
    else:
        damage(engine, "fact_test_charge",
               "UPDATE fact_test_charge SET amount=amount+1 WHERE revision_id=?", (row["fact_id"],))
    with pytest.raises(KernelError) as failure:
        Dashboard(engine).brief(PERIOD, section="vouchers", limit=1)
    assert failure.value.code == "content_integrity_failed"


def test_exact_month_proof_does_not_load_unrelated_months_or_replaced_open_versions(
    engine, monkeypatch
):
    import ai_accounting.kernel.integrity as integrity_module

    _posted_pair(engine)
    dashboard = Dashboard(engine)
    original_object = integrity_module._object
    decoded = []

    def observe_object(raw, component, ident):
        value = original_object(raw, component, ident)
        if component == "calculation":
            decoded.append((ident, len(raw)))
        return value

    monkeypatch.setattr(integrity_module, "_object", observe_object)

    def measured_amounts(expense):
        with dashboard._snapshot(PERIOD) as snapshot:
            decoded.clear()
            expected = {
                row[0]: row[1] for row in snapshot.connection.execute(
                    "SELECT v.id,v.calculation_id FROM voucher_current h "
                    "JOIN voucher_version v ON v.id=h.version_id WHERE v.period=?", (MONTH,)
                )
            }
            assert snapshot.month_journal.account_amounts() == {
                "5602": [expense, 0], "2202": [0, expense],
            }
            # Observe actual loaded rows and decoded result bytes, not just the
            # number of calls or the SQL spelling. Each snapshot starts fresh.
            assert snapshot.reads._lines.keys() == expected.keys()
            assert {ident for ident, _ in decoded} == set(expected.values())
            assert snapshot.reads._verified_source_contents.keys() == set(expected.values())
            return (
                sum(len(lines) for lines in snapshot.reads._lines.values()),
                len(decoded), sum(size for _, size in decoded),
            )

    baseline = measured_amounts(300)
    unrelated = [f"other-month-{index}" for index in range(40)]
    for index, subject in enumerate(unrelated):
        save(engine, subject=subject, amount=900 + index, period="2026-03",
             request=f"save-{subject}")
    publish(engine, unrelated, request="post-other-months")
    assert measured_amounts(300) == baseline
    for revision in range(1, 13):
        save(engine, subject="first", amount=100 + revision, revision=revision,
             request=f"replace-fact-{revision}")
        publish(engine, ["first"], request=f"replace-posting-{revision}")
    assert measured_amounts(312) == baseline
    assert dashboard.brief(PERIOD)["data"]["position"]["month_expense_fen"] == 312


def test_whole_month_amounts_reject_off_page_frozen_adoption_damage(engine):
    row = _posted_pair(engine, closed=True)
    damage(engine, "close_reference",
           "UPDATE close_reference SET position='99999' WHERE reference_type='voucher' "
           "AND reference_id=?", (row["id"],))
    with pytest.raises(KernelError) as failure:
        Dashboard(engine).brief(PERIOD, section="vouchers", limit=1)
    assert failure.value.code == "content_integrity_failed"


def test_operating_amounts_keep_frozen_owner_open_replacement_and_no_impact_review(engine):
    _posted_pair(engine, closed=True)
    dashboard = Dashboard(engine)
    before = dashboard.brief(PERIOD, limit=1)["data"]["position"]
    save(engine, subject="off-page", amount=240, revision=1, request="closed-correction")
    publish(engine, ["off-page"], request="post-correction", posting_period="2026-02")
    assert dashboard.brief(PERIOD, limit=1)["data"]["position"] == before
    correction = dashboard.brief("2026-02", limit=1)["data"]
    assert correction["activity_count"] == 2
    assert correction["position"]["month_expense_fen"] == 40
    assert correction["position"]["complete"]
    save(engine, subject="off-page", amount=250, revision=2, request="open-replacement")
    publish(engine, ["off-page"], request="replace-correction")
    assert dashboard.brief("2026-02")["data"]["position"]["month_expense_fen"] == 50
    assert dashboard.brief(PERIOD)["data"]["position"] == before
    with engine.store.connection(read_only=True) as connection:
        vouchers = [tuple(row) for row in connection.execute(
            "SELECT id,calculation_id FROM voucher_version v JOIN voucher_current h "
            "ON h.version_id=v.id ORDER BY id"
        )]
    save(engine, subject="off-page", amount=250, revision=3, request="unchanged-fact")
    publish(engine, ["off-page"], request="no-impact-review")
    with engine.store.connection(read_only=True) as connection:
        assert [tuple(row) for row in connection.execute(
            "SELECT id,calculation_id FROM voucher_version v JOIN voucher_current h "
            "ON h.version_id=v.id ORDER BY id"
        )] == vouchers
    assert dashboard.brief("2026-02")["data"]["position"]["month_expense_fen"] == 50
    assert dashboard.brief(PERIOD)["data"]["position"] == before


@pytest.mark.parametrize("corruption", ["reviewed_owner", "number"])
def test_open_correction_authenticates_the_original_frozen_voucher_header(engine, corruption):
    original = _posted_pair(engine)
    save(engine, subject="off-page", amount=200, revision=1, request="pre-close-no-impact")
    _, reviewed = publish(engine, ["off-page"], request="pre-close-review")
    reviewed_owner = reviewed["results"][0]["calculation_id"]
    assert reviewed_owner != original["calculation_id"]
    with engine.store.connection(read_only=True) as connection:
        original_header = dict(connection.execute(
            "SELECT * FROM voucher_version WHERE id=?", (original["id"],)
        ).fetchone())
    assert original_header["calculation_id"] == original["calculation_id"]
    close(engine)
    save(engine, subject="off-page", amount=240, revision=2, request="correct-reviewed-close")
    publish(engine, ["off-page"], request="post-reviewed-correction", posting_period="2026-02")
    assert Dashboard(engine).brief("2026-02")["data"]["position"]["month_expense_fen"] == 40
    if corruption == "reviewed_owner":
        # Both results are valid, have the same lines and name the same formal
        # voucher. Only the frozen header fixes its exact original owner.
        damage(engine, "voucher_version", "UPDATE voucher_version SET calculation_id=? WHERE id=?",
               (reviewed_owner, original["id"]))
    else:
        damage(engine, "voucher", "UPDATE voucher SET number=9999 WHERE id=?",
               (original_header["voucher_id"],))
    with pytest.raises(KernelError) as failure:
        Dashboard(engine).brief("2026-02", limit=1)
    assert failure.value.code == "content_integrity_failed"
