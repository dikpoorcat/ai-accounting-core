"""Explicit synthetic coverage for tests of accounting, not AI evidence judgment."""

from ai_accounting_modular.contracts.public import content_digest


def coverage_basis(first, last, source_id):
    return {
        "coverage": [
            {
                "period_from": first,
                "period_to": last,
                "sources": [{"source_id": source_id}],
            }
        ],
        "additional_sources": [],
        "note_refs": [],
    }


def ledger_work_basis(checked, source_id):
    """Supply the isolated Ledger owner's already-reviewed synthetic handoff."""
    actual_range = {"period_from": checked.period_from, "period_to": checked.period}
    return {
        "actual_range": actual_range,
        "basis": coverage_basis(checked.period_from, checked.period, source_id),
        "public_head": checked.head,
        "material_version": content_digest({"fixture": actual_range, "source_id": source_id}),
        "source_ids": [source_id],
    }


def checked_close_request(app, company_id, period, source_id, *, basis=None):
    """Read current preconditions without writing sources or changing accounting."""
    discovered = app.accounting_period(
        {
            "company_id": company_id,
            "action": "check",
            "period": period,
        }
    )
    if basis is None:
        basis = coverage_basis(discovered["actual_range"]["period_from"], period, source_id)
    candidate = app.accounting_period(
        {
            "company_id": company_id,
            "action": "check",
            "period": period,
            "basis": basis,
        }
    )
    checked = app.accounting_period(
        {
            "company_id": company_id,
            "action": "check",
            "period": period,
            "basis": basis,
            "expected_material_version": candidate["material_version"],
        }
    )
    request = {
        "company_id": company_id,
        "action": "close",
        "actual_range": checked["actual_range"],
        "basis": basis,
        "expected_material_version": candidate["material_version"],
        "expected_head": checked["head"],
    }
    return checked, request
