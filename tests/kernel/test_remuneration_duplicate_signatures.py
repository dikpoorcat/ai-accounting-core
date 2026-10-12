"""Current source-tax labor and archived duplicate witnesses retain separate grammars."""

from test_payroll import labor

from ai_accounting.kernel.duplicates import _signature
from ai_accounting.kernel.payroll_history import HistoricalLaborRemuneration


def test_current_labor_complete_signature_detects_changed_remuneration():
    original = labor()
    assert _signature(original) == _signature(labor())
    assert _signature(original) != _signature(labor(fixed_fee_fen=1_100_000))
    assert _signature(original) != _signature(labor(expense_class="sales"))


def test_archived_labor_keeps_original_frozen_signature():
    archived = HistoricalLaborRemuneration(
        period="2026-01",
        person_id="contractor",
        income_date="2026-01-31",
        policy_id="archived-policy",
        expense_class="management",
        recipient_tax_status="ordinary_resident",
        remuneration_method="fixed",
        withholding_method="net_after_withholding",
        fixed_fee_fen=1_000_000,
    )
    assert (
        _signature(archived) == "c1b940d6d2032586e411c5bf14d46c6fe274aa663725d5e19af72d4f4345d963"
    )
    assert _signature(archived) != _signature(labor())
