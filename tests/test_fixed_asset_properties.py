from __future__ import annotations

from types import SimpleNamespace

from hypothesis import given, settings
from hypothesis import strategies as st

from ai_accounting.fixed_asset_service import FixedAssetService
from ai_accounting.fixed_assets import calculate_used_fixed_asset_vat


@st.composite
def _disposal_facts(draw: st.DrawFn) -> tuple[int, int, int, int]:
    cost_fen = draw(st.integers(min_value=1, max_value=1_000_000_000))
    accumulated_fen = draw(st.integers(min_value=0, max_value=cost_fen))
    gross_fen = draw(st.integers(min_value=0, max_value=1_000_000_000))
    clearance_cost_fen = draw(st.integers(min_value=0, max_value=1_000_000_000))
    return cost_fen, accumulated_fen, gross_fen, clearance_cost_fen


@given(facts=_disposal_facts())
@settings(max_examples=150, deadline=None)
def test_fixed_asset_disposal_template_is_balanced_and_gain_loss_is_conserved(
    facts: tuple[int, int, int, int],
) -> None:
    cost_fen, accumulated_fen, gross_fen, clearance_cost_fen = facts
    vat_fen = (
        calculate_used_fixed_asset_vat(gross_proceeds_fen=gross_fen).vat_fen
        if gross_fen
        else 0
    )
    book_value_fen = cost_fen - accumulated_fen
    net_proceeds_fen = gross_fen - vat_fen
    gain_fen = max(0, net_proceeds_fen - book_value_fen - clearance_cost_fen)
    loss_fen = max(0, book_value_fen + clearance_cost_fen - net_proceeds_fen)

    entries = FixedAssetService._disposal_entries(
        asset=SimpleNamespace(cost_fen=cost_fen),  # type: ignore[arg-type]
        accumulated_depreciation_fen=accumulated_fen,
        book_value_fen=book_value_fen,
        gross_proceeds_fen=gross_fen,
        vat_fen=vat_fen,
        clearance_cost_fen=clearance_cost_fen,
        gain_fen=gain_fen,
        loss_fen=loss_fen,
        settlement_method="bank" if gross_fen else "none",
        customer_id=None,
    )

    for entry in entries:
        entry.validate()
    assert sum(entry.debit_fen for entry in entries) == sum(
        entry.credit_fen for entry in entries
    )
    assert gain_fen == 0 or loss_fen == 0
    assert gain_fen - loss_fen == net_proceeds_fen - book_value_fen - clearance_cost_fen
    assert 0 <= accumulated_fen <= cost_fen
    assert book_value_fen == cost_fen - accumulated_fen
