"""Wage identity reads retain independently authenticated frozen result bindings."""

import json

import pytest
from test_integrity_content import damage, verify
from test_payroll import actual, payroll
from test_payroll_corrections import company as _company
from test_settlement_late_reviews import prepared, review

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import PAYROLL_KINDS, Dashboard
from ai_accounting.kernel.dashboard_reads import payroll_head_metadata, verified_payroll_heads
from ai_accounting.kernel.types import YearMonth, canonical, digest

company = _company


def heads(snap):
    return payroll_head_metadata(
        snap, PAYROLL_KINDS | {"opening_payroll_payable"}, line_count_period=snap.period
    )


@pytest.mark.parametrize("zero", [False, True])
def test_verified_wage_heads_match_full_selection_after_multiple_late_reviews(
    tmp_path, monkeypatch, zero
):
    company = prepared(tmp_path, zero=zero)
    original = Dashboard(company.engine).employees("2026-01")
    review(company, monkeypatch, 1)
    review(company, monkeypatch, 2)
    company.save(
        payroll(
            period="2026-02",
            accounting_gross_salary_fen=0 if zero else 1_000_000,
            tax_reported_salary_fen=0 if zero else 1_000_000,
        ),
        "february",
    )
    company.confirm_payroll("february")
    company.publish("february")
    company.close("2026-02")
    # The frozen month remains an original adoption despite newer current heads.
    later = Dashboard(company.engine).employees("2026-01")
    assert later["data"]["employees"] == original["data"]["employees"]
    with Dashboard(company.engine)._snapshot("2026-02") as snap:
        selected_heads = heads(snap)
        narrow = verified_payroll_heads(snap, selected_heads)
        full = snap.calculations.selected(
            kinds=PAYROLL_KINDS | {"opening_payroll_payable"},
            subjects={head["subject_id"] for head in selected_heads},
        )
        assert {
            subject: (calc["id"], calc["fact_id"], calc["posting_period"])
            for subject, calc in narrow.items()
        } == {
            subject: (
                calc["id"],
                calc["fact_id"],
                str(YearMonth.from_ordinal(calc["posting_period"])),
            )
            for subject, calc in full.items()
        }
        assert set(narrow) == {"january", "february"}


@pytest.mark.parametrize("change", ["different_body", "body_only", "duplicate", "noncanonical"])
def test_frozen_wage_head_bytes_require_authenticated_adoption(tmp_path, change):
    company = prepared(tmp_path)
    company.save(payroll(period="2026-02"), "february")
    company.confirm_payroll("february")
    company.publish("february")
    baseline = Dashboard(company.engine).employees("2026-02", preparation="deferred")
    with company.engine.store.connection(read_only=True) as connection:
        saved = connection.execute(
            "SELECT c.id,c.outcome,c.digest FROM calculation c JOIN calculation_current h "
            "ON h.calculation_id=c.id WHERE h.subject_id='january'"
        ).fetchone()
        ident, raw, expected = tuple(saved)
    body = json.loads(raw)
    if change in {"different_body", "body_only"}:
        # A valid JSON result and matching mutable row digest cannot replace
        # the result independently saved in the frozen adoption leaf.
        body["synthetic_changed_result"] = True
        changed = canonical(body)
        saved_digest = digest(body) if change == "different_body" else expected
    elif change == "duplicate":
        changed = raw[:-1] + ',"lines":[]}'
        saved_digest = expected
    else:
        changed, saved_digest = json.dumps(body, indent=2), expected
    damage(
        company.engine,
        "calculation",
        "UPDATE calculation SET outcome=?,digest=? WHERE id=?",
        (changed, saved_digest, ident),
    )
    # This later posting scope avoids interpreting the old month's JSON1 line
    # count first: the test reaches the independently anchored head verifier.
    with Dashboard(company.engine)._snapshot("2026-02") as snap:
        candidates = heads(snap)
        if change == "different_body":
            with pytest.raises(KernelError) as failure:
                verified_payroll_heads(snap, candidates)
            assert failure.value.code == "content_integrity_failed"
        else:
            # An unused historical body is outside the identity proof. Its
            # digest still binds to the independently frozen adopted leaf.
            assert verified_payroll_heads(snap, candidates)["january"]["id"] == ident
            assert ident not in snap.reads._verified_sql_outcomes
            assert ident not in snap.reads._verified_source_contents
        # Core history selection retains strict unique-member/content checks;
        # identity success is never its proof.
        if change != "noncanonical":
            with pytest.raises(KernelError) as failure:
                snap.calculations.selected(kinds=PAYROLL_KINDS, subjects={"january"})
            assert failure.value.code == "content_integrity_failed"
    if change != "noncanonical":
        # A focused card has the same current amount scope as the default
        # list; neither turns into a full historical-content verifier.
        for kwargs in ({}, {"employee_id": "employee", "section": "employees"}):
            assert Dashboard(company.engine).employees(
                "2026-02", preparation="deferred", **kwargs
            )["data"]["employees"] == baseline["data"]["employees"]
        with pytest.raises(KernelError):
            verify(company.engine)
        from ai_accounting.kernel.backup import BackupError, verify_file

        with pytest.raises(BackupError):
            verify_file(company.engine.store.path)


@pytest.mark.parametrize("closed_correction", [False, True])
def test_employee_default_matches_full_head_selection(company, monkeypatch, closed_correction):
    company.publish("january", "february")
    company.close("2026-01")
    if closed_correction:
        company.save(actual(), "actual")
        company.publish("actual", posting_period="2026-02")
    original = Dashboard(company.engine).employees("2026-02")

    def full_identity(snap, candidates):
        return snap.calculations.selected(
            kinds=PAYROLL_KINDS | {"opening_payroll_payable"},
            subjects={head["subject_id"] for head in candidates},
        )

    import ai_accounting.kernel.dashboard as dashboard_module

    monkeypatch.setattr(dashboard_module, "verified_payroll_heads", full_identity)
    full = Dashboard(company.engine).employees("2026-02")
    assert original == full


def test_canonical_frozen_wage_identity_does_not_decode_historical_result(tmp_path, monkeypatch):
    company = prepared(tmp_path)
    company.save(payroll(period="2026-02"), "february")
    company.confirm_payroll("february")
    company.publish("february")
    import ai_accounting.kernel.stored_json as stored_json

    original = stored_json.loads_unique
    decoded = []

    def observed(raw):
        decoded.append(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        return original(raw)

    with Dashboard(company.engine)._snapshot("2026-02") as snap:
        candidates = heads(snap)
        historical = next(head for head in candidates if head["subject_id"] == "january")
        saved = snap.connection.execute(
            "SELECT outcome FROM calculation WHERE id=?", (historical["id"],)
        ).fetchone()[0]
        monkeypatch.setattr(stored_json, "loads_unique", observed)
        verified_payroll_heads(snap, candidates)
        assert saved not in decoded
        # The unchanged core selector really interprets historical states.
        snap.calculations.selected(
            kinds=PAYROLL_KINDS, subjects={head["subject_id"] for head in candidates}
        )
        assert saved in decoded


def test_wage_identity_transfers_no_unused_result_and_matches_full_heads(tmp_path):
    from stage9_metrics import measure_work

    company = prepared(tmp_path)
    company.save(payroll(period="2026-02"), "february-unpublished")

    def selected(identity):
        with Dashboard(company.engine)._snapshot("2026-02") as snap:
            candidates = heads(snap)
            result = verified_payroll_heads(snap, candidates) if identity else (
                snap.calculations.selected(
                    kinds=PAYROLL_KINDS, subjects={head["subject_id"] for head in candidates}
                )
            )
            return {subject: (calc["id"], calc["fact_id"]) for subject, calc in result.items()}

    narrow_work, narrow = measure_work(company.engine, lambda: selected(True))
    full_work, full = measure_work(company.engine, lambda: selected(False))
    assert narrow == full
    assert narrow_work["counters"].get("calculation_result_rows_loaded", 0) == 0
    assert full_work["counters"]["calculation_result_rows_loaded"] > 0
    print(json.dumps({"wage_identity": narrow_work["counters"],
                      "wage_core": full_work["counters"]}, ensure_ascii=False))


@pytest.mark.parametrize("change", ["missing", "changed"])
def test_wage_identity_rejects_missing_or_bad_frozen_leaf(tmp_path, change):
    company = prepared(tmp_path)
    company.save(payroll(period="2026-02"), "february-unpublished")
    statement = (
        "DELETE FROM close_storage_block WHERE field='adopted_results'"
        if change == "missing" else
        "UPDATE close_storage_block SET content='[]' WHERE field='adopted_results'"
    )
    damage(company.engine, "close_storage_block", statement)
    with pytest.raises(KernelError) as failure:
        with Dashboard(company.engine)._snapshot("2026-02") as snap:
            verified_payroll_heads(snap, heads(snap))
    assert failure.value.code == "content_integrity_failed"


def test_open_identity_authority_work_ignores_unrelated_publications(tmp_path):
    from stage9_metrics import measure_work
    from test_labor_assets import cost

    from ai_accounting.kernel.domains.labor_assets import LaborProjectCost
    from ai_accounting.kernel.types import YearMonth

    company = prepared(tmp_path)
    company.save(payroll(period="2026-02"), "february-unpublished")
    company.confirm_payroll("february-unpublished")
    company.publish("february-unpublished")

    def selected():
        with Dashboard(company.engine)._snapshot("2026-02") as snap:
            return {subject: calc["id"] for subject, calc in
                    verified_payroll_heads(snap, heads(snap)).items()}

    def authority(work):
        return next(row for row in work["sql"]
                    if row["statement"].startswith("SELECT p.*,c.id calculation_exists,"))

    before, original = measure_work(company.engine, selected)
    with company.engine.store.connection(read_only=True) as connection:
        plan = [row[3] for row in connection.execute(
            "EXPLAIN QUERY PLAN " + authority(before)["statement"],
            (canonical(sorted(PAYROLL_KINDS | {"opening_payroll_payable"})),
             YearMonth("2026-02").ordinal),
        )]
    assert any("SEARCH s USING COVERING INDEX subject_kind" in row for row in plan)
    assert any("SEARCH p USING INDEX publication_subject" in row for row in plan)
    for index in range(24):
        company.save(LaborProjectCost.model_validate_json(json.dumps(
            cost(period="2026-02")
        )), f"unrelated-{index}")
        company.publish(f"unrelated-{index}")
    after, later = measure_work(company.engine, selected)
    assert later == original
    assert authority(after)["returned_rows"] == authority(before)["returned_rows"]
    assert authority(after)["sqlite_vm_steps"] <= authority(before)["sqlite_vm_steps"] + 100
    print(json.dumps({"authority_plan": plan,
                      "before": authority(before), "after": authority(after)}, ensure_ascii=False))


@pytest.mark.parametrize("kind", ["payroll", "reimbursed_asset"])
@pytest.mark.parametrize("damage_record", [False, True])
def test_identity_authenticates_normal_and_damaged_terminal_withdrawals(
    company, kind, damage_record
):
    from test_reimbursement_assets import asset

    from ai_accounting.kernel.dashboard import _asset_card_sources
    from ai_accounting.kernel.domains.assets import ReimbursedAsset

    subject = "february" if kind == "payroll" else "computer"
    if kind == "payroll":
        company.publish("january")
    if kind == "reimbursed_asset":
        company.save(ReimbursedAsset.model_validate_json(json.dumps(asset())), subject)
    company.publish(subject)
    engine = company.engine
    options = {"recording_error_evidence": company.owner_confirmation}
    preview = engine.preview_delete(subject, **options)
    engine.delete(subject, **options, preview_digest=preview["digest"], epochs=preview["epochs"],
                  request_id=company.request())
    if damage_record:
        damage(engine, "calculation_publication",
               "UPDATE calculation_publication SET id=id||'-damaged' "
               "WHERE subject_id=? AND mode='withdrawn'", (subject,), foreign_keys=False)

    def read_identity():
        with Dashboard(engine)._snapshot("2026-02") as snap:
            return (verified_payroll_heads(snap, heads(snap)) if kind == "payroll"
                    else _asset_card_sources(snap)["acquisitions"])

    if damage_record:
        with pytest.raises(KernelError) as failure:
            read_identity()
        assert failure.value.code == "content_integrity_failed"
    else:
        assert subject not in read_identity()
        response = (Dashboard(engine).employees if kind == "payroll" else Dashboard(engine).assets)(
            "2026-02", preparation="deferred"
        )
        if kind == "payroll":
            assert response["data"]["employees"]["gross_salary_fen"] == 0
        else:
            assert response["data"]["collections"]["assets"]["page"]["total_count"] == 0


@pytest.mark.parametrize("change", [
    "calculation_seal", "fact_seal", "other_fact", "kind", "current_head", "publication",
])
def test_public_employees_reject_damaged_open_historical_wage_identity(company, change):
    company.publish("january", "february")
    dashboard = Dashboard(company.engine)
    normal = dashboard.employees("2026-02")
    totals = normal["data"]["employees"]
    assert totals["gross_salary_fen"] == 1_000_000
    assert normal["data"]["collections"]["employees"]["page"]["total_count"] == 1
    with company.engine.store.connection(read_only=True) as connection:
        rows = {
            row["subject_id"]: dict(row)
            for row in connection.execute(
                "SELECT c.id,c.subject_id,c.fact_id FROM calculation c "
                "JOIN calculation_current h ON h.calculation_id=c.id "
                "WHERE c.subject_id IN ('january','february')"
            )
        }
    january = rows["january"]
    if change == "calculation_seal":
        damage(
            company.engine,
            "calculation_seal",
            "DELETE FROM calculation_seal WHERE calculation_id=?",
            (january["id"],),
            foreign_keys=False,
        )
    elif change == "fact_seal":
        damage(
            company.engine,
            "fact_seal",
            "DELETE FROM fact_seal WHERE fact_id=?",
            (january["fact_id"],),
            foreign_keys=False,
        )
    elif change == "other_fact":
        damage(
            company.engine,
            "calculation",
            "UPDATE calculation SET fact_id=? WHERE id=?",
            (rows["february"]["fact_id"], january["id"]),
        )
    elif change == "kind":
        damage(
            company.engine,
            "calculation",
            "UPDATE calculation SET kind='annual_bonus' WHERE id=?",
            (january["id"],),
        )
    elif change == "current_head":
        damage(
            company.engine, "calculation_current",
            "DELETE FROM calculation_current WHERE subject_id='january'",
        )
    else:
        damage(
            company.engine, "calculation_publication",
            "UPDATE calculation_publication SET mode='review_no_impact' WHERE calculation_id=?",
            (january["id"],),
        )
    # The current month's public page must authenticate older open wage heads,
    # even though it only displays February's cost and one employee card.
    with pytest.raises(KernelError) as failure:
        dashboard.employees("2026-02")
    assert failure.value.code == "content_integrity_failed"
