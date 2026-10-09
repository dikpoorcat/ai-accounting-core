"""Published corrections supplement missing display identities without rewriting July."""

from copy import deepcopy

import pytest
from historical_pass_through_fixture import prior_pass_through_registration
from test_banking import book as book, close_month, inventories
from test_dashboard_open_groups import snapshot
from test_dashboard_projection import diagnostic_open_items
from test_dashboard_provenance import profile

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_party_supplements import pass_through_party_supplements
from ai_accounting.kernel.identity_corrections import IdentityCorrections


def _historical(book, *, known=False):
    engine, save, publish, proof = book
    entries = [("first", 1300), ("second", 2700), ("third", 900)]
    if known:
        entries.append(("known", 600))
        profile(engine, "counterparty", "original", display_name="合成原权利人")
    data = {}
    for subject, amount in entries:
        data[subject] = {"period": "2026-07", "payer_id": "payer",
                         "beneficiary_id": "original" if subject == "known" else None,
                         "amount_fen": amount, "rights_and_obligation_confirmed": True}
        with prior_pass_through_registration():
            save("pass_through", subject, data[subject])
            publish(subject)
    assert close_month(inventories(engine, proof, "2026-07", {"transactions"}),
                       proof, "2026-07")["status"] == "closed"
    profile(engine, "counterparty", "recipient-a", display_name="合成甲权利人")
    profile(engine, "counterparty", "recipient-b", display_name="合成乙权利人")
    return data


def _amend(book, data, subjects, *, revision=1, recipient=None):
    engine, _, _, proof = book
    amended = {}
    for subject in subjects:
        beneficiary = recipient or ("recipient-b" if subject == "third" else "recipient-a")
        amended[subject] = data[subject] | {"beneficiary_id": beneficiary}
        engine.amend_fact("pass_through", subject, amended[subject], evidence=(proof,),
                          expected_revision=revision, recording_error_confirmed=True,
                          request_id=f"supplement-{subject}-{revision}")
    return amended


def _publish(book, subjects):
    engine, *_ = book
    preview = engine.preview(subjects, posting_period="2026-09")
    return engine.confirm(subjects, preview_digest=preview["digest"], epochs=preview["epochs"],
                          posting_period="2026-09", request_id="supplement-publication")


def _remittances(engine):
    return {row["subject_id"]: row for row in
            diagnostic_open_items(engine, "2026-07")["collection"]["items"]
            if row["name"] == "remittance"}


def _frozen(engine):
    with engine.store.connection(read_only=True) as connection:
        return {table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
                for table in ("period_close", "close_reference", "settlement_state_revision")}


def test_published_identity_supplement_merges_groups_and_retains_precise_historical_sources(book):
    engine, *_ = book
    data = _historical(book)
    original = _remittances(engine)
    ledger = engine.ledger("2026-07")
    frozen = _frozen(engine)
    _amend(book, data, ["first", "second", "third"])
    _publish(book, ["first", "second", "third"])

    rows = _remittances(engine)
    assert {subject: row["party"] for subject, row in rows.items()} == {
        "first": "合成甲权利人", "second": "合成甲权利人", "third": "合成乙权利人",
    }
    for subject, row in rows.items():
        before = original[subject]
        for field in ("source_fact_id", "source_calculation_id", "source_amount_fen",
                      "paid_fen", "remaining_fen", "status", "counterparty_id"):
            assert row[field] == before[field]
        sources = row["field_sources"]["party"]
        identity = sources[0]
        assert identity["source_type"] == "fact" and identity["field"] == "beneficiary_id"
        assert identity["revision"] == 2 and identity["basis"] == "current_supplement"
        assert identity["recorded_at"] and identity["evidence"]
        assert identity["id"] != row["source_fact_id"]
        assert row["party_supplement"]["fact_id"] == identity["id"]
        assert row["party_supplement"]["posting_period"] == "2026-09"
        assert sources[1]["field"] == "display_name"

    dashboard = Dashboard(engine)
    response = dashboard.brief("2026-07", section="open_items", limit=1)
    groups = []
    while True:
        collection = response["data"]["collections"]["open_items"]
        groups.extend(collection["items"])
        if not collection["page"]["has_more"]:
            break
        response = dashboard.brief(
            "2026-07", section="open_items", limit=1,
            cursor=collection["page"]["next_cursor"], expected_version=response["snapshot_version"],
        )
    remittance_groups = [row for row in groups if row["category_key"] == "other_payables"]
    assert {(row["party"], row["member_count"], row["outstanding_fen"])
            for row in remittance_groups} == {("合成甲权利人", 2, 4000), ("合成乙权利人", 1, 900)}
    group = next(row for row in remittance_groups if row["member_count"] == 2)
    detail = dashboard.brief_group("2026-07", section="open_items", group_key=group["group_key"])[
        "data"]["collections"]["members"]["items"]
    assert {row["subject_id"] for row in detail} == {"first", "second"}
    assert all(row["party"] == "合成甲权利人" and row["source_period"] == "2026-07" for row in detail)
    assert engine.ledger("2026-07") == ledger
    assert _frozen(engine) == frozen


def test_unpublished_facts_do_not_supply_identity_or_replace_published_supplement(book):
    engine, *_ = book
    data = _historical(book)
    amended = _amend(book, data, ["first"])
    assert all(row["party"] == "最终收款人未具名" for row in _remittances(engine).values())
    _publish(book, ["first"])
    amended["first"]["amount_fen"] += 100
    _amend(book, amended, ["first"], revision=2, recipient="recipient-a")
    first = _remittances(engine)["first"]
    assert first["party"] == "合成甲权利人"
    assert first["field_sources"]["party"][0]["revision"] == 2


def test_nonempty_historical_identity_is_not_replaced_by_current_correction(book):
    engine, *_ = book
    data = _historical(book, known=True)
    before = _remittances(engine)["known"]
    command = IdentityCorrections(engine)
    options = {"changes": [{"subject_id": "known", "expected_revision": 1,
                            "action": "reassign", "data": data["known"] | {
                                "beneficiary_id": "recipient-a"}}],
               "evidence": [book[3]], "reason": "合成明确身份更正",
               "posting_period": "2026-09"}
    preview = command.preview_identity_correction(**options)
    command.confirm_identity_correction(**options, preview_digest=preview["digest"],
                                        epochs=preview["epochs"], request_id="known-correction")
    current = _remittances(engine)["known"]
    assert current["party"] == "合成原权利人"
    assert current["party_key"] == "original"
    assert current["source_fact_id"] == before["source_fact_id"]
    assert "party_supplement" not in current
    assert current["field_sources"]["party"]["basis"] == "frozen"


def test_supplement_rejects_conflicting_formal_obligation_identity(book, monkeypatch):
    engine, *_ = book
    data = _historical(book)
    original = _remittances(engine)["first"]
    _amend(book, data, ["first"])
    _publish(book, ["first"])
    with snapshot(engine, "2026-07") as snap:
        calculations = snap.reads.calculations

        def conflicting(identifiers):
            result = deepcopy(calculations(identifiers))
            for calculation in result.values():
                if calculation["fact_id"] == original["source_fact_id"]:
                    continue
                for obligation in calculation["outcome"]["values"]["obligations"]:
                    if obligation["name"] == "remittance":
                        obligation["counterparty_id"] = "recipient-b"
            return result

        monkeypatch.setattr(snap.reads, "calculations", conflicting)
        with pytest.raises(KernelError) as caught:
            pass_through_party_supplements(snap, [original])
        assert caught.value.code == "content_integrity_failed"
