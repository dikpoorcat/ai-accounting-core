"""Source retirement queries the real retained adoption chain, not only frozen heads."""

from uuid import uuid4

import pytest
from test_adoption import BUSINESS, COMPANY, SOURCE, adopt_declared, expense
from test_adoption import store as store

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.ledger.public import create_ledger
from ai_accounting_modular.storage.public import CompanySpec


def with_source(source_id, amount="90000", *, seq=0):
    change = expense(amount, seq=seq)
    change["businesses"][0]["fact"]["evidence_ids"] = [source_id]
    change["plans"][0]["basis"]["source_locations"] = [
        {"source_id": source_id, "location": "核算实际采用的原件"}
    ]
    return change


def test_open_current_sources_are_protected_and_superseded_sources_are_not(store):
    ledger = create_ledger()
    newer, unused = str(uuid4()), str(uuid4())
    with store.write_unit(COMPANY) as unit:
        unit.repository("records").source(newer)
        adopt_declared(ledger, [with_source(SOURCE, "100000")], unit)
        assert ledger.referenced_source_ids((unused, SOURCE), unit) == (SOURCE,)
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [with_source(newer, seq=1)], unit)
        assert ledger.referenced_source_ids((SOURCE, unused, newer), unit) == (newer,)
    with store.read_unit(COMPANY) as read:
        assert ledger.referenced_source_ids((SOURCE, newer), read) == (newer,)


def test_frozen_original_remains_protected_after_a_new_open_correction(store):
    ledger = create_ledger()
    newer = str(uuid4())
    with store.write_unit(COMPANY) as unit:
        unit.repository("records").source(newer)
        adopt_declared(ledger, [with_source(SOURCE, "100000")], unit)
        unit.repository("ledger").freeze_heads("2026-01")
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [with_source(newer, seq=1)], unit, correction_period="2026-02")
    with store.read_unit(COMPANY) as read:
        assert ledger.referenced_source_ids((newer, SOURCE), read) == tuple(sorted((SOURCE, newer)))


def test_withdrawn_open_result_no_longer_retains_its_source(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [with_source(SOURCE)], unit)
    withdrawn = expense()
    withdrawn.update(publications=[], plans=[])
    withdrawn["businesses"][0] = {
        "business_id": BUSINESS,
        "expected_seq": 1,
        "op": "withdraw",
        "natural_key": None,
        "complete_result_slots": [],
    }
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [withdrawn], unit)
        assert ledger.referenced_source_ids((SOURCE,), unit) == ()


def test_exact_batch_uses_reverse_index_without_reading_unrelated_result_bodies(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [with_source(SOURCE)], unit)
    with store.read_unit(COMPANY) as read:
        session = read.repository("ledger")._session
        statements = []
        fetchall = session.fetchall

        def observed(sql, args=()):
            statements.append(sql)
            return fetchall(sql, args)

        session.fetchall = observed
        assert ledger.referenced_source_ids((SOURCE, str(uuid4())), read) == (SOURCE,)
        assert len(statements) == 1
        sql = statements[0]
        assert "json_each" in sql and "source_id" in sql
        assert "basis_json" not in sql and "target_json" not in sql
        plan = fetchall("EXPLAIN QUERY PLAN " + sql, ('["' + SOURCE + '"]',))
        assert any("result_source_reverse" in row[3] for row in plan)


def test_500_requested_ids_and_multiple_formal_locations_return_each_source_once(store):
    ledger = create_ledger()
    change = with_source(SOURCE)
    change["plans"][0]["basis"]["source_locations"].append(
        {"source_id": SOURCE, "location": "同一原件第二处必要位置"}
    )
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [change], unit)
    ids = [SOURCE, *(str(uuid4()) for _ in range(499))]
    with store.read_unit(COMPANY) as read:
        assert ledger.referenced_source_ids(ids, read) == (SOURCE,)


def test_current_fact_attachment_alone_is_not_a_formal_result_reference(store):
    with store.write_unit(COMPANY) as unit:
        adopt_declared(create_ledger(), [expense()], unit)
        # Current fact/source references are protected by their own owner. This
        # ledger port reports only the necessary formal adoption sources.
        assert create_ledger().referenced_source_ids((SOURCE,), unit) == ()


def test_scope_and_closed_context_are_not_bypassed_for_empty_input(store, tmp_path):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [with_source(SOURCE)], unit)
    other = str(uuid4())
    store.create_company(
        CompanySpec(
            other, str(uuid4()), "913100000000000002", "另一合成公司", "2026-01", tmp_path / "other"
        )
    )
    with store.read_unit(other) as read:
        assert ledger.referenced_source_ids((SOURCE,), read) == ()
        assert ledger.referenced_source_ids((), read) == ()
    with pytest.raises(ContractError):
        ledger.referenced_source_ids((), read)


@pytest.mark.parametrize(
    "ids",
    [
        None,
        SOURCE,
        {SOURCE},
        (SOURCE, SOURCE),
        (True,),
        ("not-an-id",),
        tuple(str(uuid4()) for _ in range(501)),
    ],
)
def test_invalid_or_unbounded_source_requests_are_rejected(store, ids):
    with store.read_unit(COMPANY) as read:
        with pytest.raises(ContractError):
            create_ledger().referenced_source_ids(ids, read)
