"""Known-business settlement pages remain local while real display changes are visible."""

from ai_accounting_modular.contracts.public import PageQuery
from ai_accounting_modular.ledger.public import SettlementEventQuery
from tests.modular.application._working_case import case as case
from tests.modular.domains.people.test_read_remuneration import bank_object

from .test_brief_open import apply, payment
from .test_business_status import progress


def test_related_event_cursor_survives_unrelated_real_payment_but_counts_full_wide_scope(case):
    original = case.seed()
    bank = bank_object()
    other = case.expense("700")
    apply(
        case,
        other,
        payment(case, original["business_id"], bank["object_id"], "100"),
        payment(case, original["business_id"], bank["object_id"], "200"),
        payment(case, other["business_id"], bank["object_id"], "10"),
        objects=[bank],
    )
    ledger = case.kernel.ledger
    query = SettlementEventQuery(
        "2026-01",
        "2026-01",
        source_refs=({"business_id": original["business_id"], "slot": "primary"},),
    )
    with case.kernel.storage.read_unit(case.company) as read:
        first = ledger.query_business_settlements(query, PageQuery(1), read)
        wide = ledger.query_settlement_events(query, PageQuery(1), read)
    assert first.total_count == first.filtered_count == 2
    assert wide.total_count == 3 and wide.filtered_count == 2
    apply(case, payment(case, other["business_id"], bank["object_id"], "20"))
    with case.kernel.storage.read_unit(case.company) as read:
        next_page = ledger.query_business_settlements(
            query, PageQuery(1, first.next_cursor, first.version), read
        )
        wide = ledger.query_settlement_events(query, PageQuery(1), read)
    assert next_page.version == first.version
    assert next_page.total_count == 2 and next_page.next_cursor is None
    assert {item.use.value["amount_fen"] for item in (*first.items, *next_page.items)} == {
        "100",
        "200",
    }
    assert wide.total_count == 4 and wide.filtered_count == 2


def test_related_party_rename_still_updates_business_progress_without_changing_amount(case):
    original = case.seed()
    before = progress(case, original["business_id"])
    apply(
        case,
        objects=[
            {
                "object_id": case.party,
                "kind": "organization",
                "name": "已核实的新名称",
                "expected_seq": 1,
                "data": {},
            }
        ],
    )
    after = progress(case, original["business_id"])
    assert after["snapshot_version"] != before["snapshot_version"]
    assert after["data"]["settlements"] == before["data"]["settlements"]
    profile = after["data"]["display_profiles"]["counterparties"][0]
    assert profile["values"]["display_name"] == "已核实的新名称"
