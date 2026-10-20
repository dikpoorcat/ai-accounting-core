from types import SimpleNamespace

import pytest

from ai_accounting_modular.contracts.public import (
    ContractError,
    PageQuery,
    QuerySelection,
    ReadPage,
    query_selection,
    validate_contract,
)


def test_two_explicit_selections_leave_the_callers_snapshot_and_default_unchanged():
    read = SimpleNamespace(view="current", as_of=None)
    historic = QuerySelection("as_posted", "2026-01")
    assert query_selection(read, historic) is historic
    assert query_selection(read) == QuerySelection("current")
    assert (read.view, read.as_of) == ("current", None)


@pytest.mark.parametrize(
    "view,as_of", [("current", "2026-01"), ("as_posted", None), ("as_posted", "2026-01-01")]
)
def test_selection_refuses_to_mix_adoption_cutoff_and_economic_day(view, as_of):
    with pytest.raises(ContractError):
        QuerySelection(view, as_of)


def test_page_keeps_complete_filtered_counts_separate_from_its_bounded_rows():
    query = PageQuery(20, expected_version="a" * 64)
    page = ReadPage("a" * 64, 1000, 25, tuple(range(query.limit)), "opaque-tail")
    assert (page.total_count, page.filtered_count, len(page.items)) == (1000, 25, 20)
    assert PageQuery(500).limit == 500
    for invalid in (True, 0, 501):
        with pytest.raises(ContractError):
            PageQuery(invalid)
    with pytest.raises(ContractError):
        ReadPage("a" * 64, 10, 25, (1,))


def test_fact_membership_needs_no_accounting_slot_or_plan():
    relation = {
        "consumer_business_id": "00000000-0000-4000-8000-000000000001",
        "producer_kind": "object",
        "producer_id": "00000000-0000-4000-8000-000000000002",
        "relation_kind": "identity",
    }
    validate_contract("business-facts.schema.json", "DirectRelation", relation)
    for extra in ({"consumer_result_slot": "main"}, {"amount_fen": "0"}):
        with pytest.raises(ContractError):
            validate_contract("business-facts.schema.json", "DirectRelation", relation | extra)
