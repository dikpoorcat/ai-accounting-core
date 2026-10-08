"""Voucher progress presence shares exact objects and checked relationship scopes."""

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from stage9_metrics import measure_work
from test_banking import book as book, opening
from test_engine import calculate, close, engine as engine, publish, save
from test_integrity_content import damage

from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.contracts import KernelError, Outcome
from ai_accounting.kernel.dashboard import Dashboard, _voucher_profile_progress


def _voucher():
    return {
        "summary": "本月费用", "list_summary": "费用", "type": "费用",
        "business_amount_label": "确认费用", "asset": {"asset_id": "asset", "name": "设备"},
        "asset_members": [],
        "lines": [{"party": "甲", "source_label": "购置设备",
                   "parties": [{"id": "party", "name": "甲"}]}],
    }


def _profiles(**groups):
    return {"deleted": False, "profiles": {"business": {"values": {}}, **groups}}


def _profile(ident, name):
    return {"entity_id": ident, "values": {"display_name": name}}


def _source_obligation(engine, monkeypatch, amount=...):
    def evaluate(version, context):
        original = calculate(version, context)
        if version.subject_id != "source":
            return original
        return Outcome(original.lines, {**original.values, "obligations": [{
            "key": "test_charge:source:primary", "name": "primary",
            "amount_fen": original.values["amount"] if amount is ... else amount,
            "account": "2202", "normal": "credit", "category": "payable",
            "counterparty_id": None,
        }]})

    monkeypatch.setitem(engine.store.registry.evaluators, "test_charge", evaluate)


@pytest.mark.parametrize("focused_subject", ["source", "plain"])
def test_checked_page_and_focus_only_send_remaining_subjects_to_public_reader(
    engine, monkeypatch, focused_subject,
):
    _source_obligation(engine, monkeypatch)
    first = "plain" if focused_subject == "source" else "source"
    for subject in (first, focused_subject):
        save(engine, subject=subject, request="save-" + subject)
    publish(engine, [first, focused_subject])
    calls = []
    original = BusinessQueries.business_progress

    def observed(queries, connection, period, subjects):
        calls.append(set(subjects))
        return original(queries, connection, period, subjects)

    monkeypatch.setattr(BusinessQueries, "business_progress", observed)
    data = Dashboard(engine).brief(
        "2026-01", section="vouchers", limit=1, voucher_number=2,
    )["data"]
    vouchers = [*data["collections"]["vouchers"]["items"], data["focused_voucher"]]
    assert {item["subject_id"]: item["has_business_progress"] for item in vouchers} == {
        "source": True, "plain": False,
    }
    assert calls == [{"plain"}]
    with Dashboard(engine)._snapshot("2026-01") as snap:
        assert original(snap.queries, snap.connection, snap.period, {"source", "plain"}) == {
            "source": True, "plain": False,
        }


@pytest.mark.parametrize("amount", [0, None, 100])
def test_checked_own_obligation_is_progress_even_when_amount_is_zero_or_unknown(
    engine, monkeypatch, amount,
):
    _source_obligation(engine, monkeypatch, amount)
    save(engine, subject="source")
    publish(engine, ["source"])

    def unexpected(*args, **kwargs):
        raise AssertionError("checked own obligation already establishes progress")

    monkeypatch.setattr(BusinessQueries, "business_progress", unexpected)
    voucher = Dashboard(engine).brief("2026-01", section="vouchers")["data"]["collections"]["vouchers"]["items"][0]
    assert voucher["has_business_progress"] is True


@pytest.mark.parametrize("changed", ["source_calculation_id", "source_business", "empty_key", "nonstring_key"])
def test_shortcut_requires_exact_own_identity_and_valid_key(engine, monkeypatch, changed):
    from ai_accounting.kernel import dashboard as module

    _source_obligation(engine, monkeypatch)
    save(engine, subject="source")
    publish(engine, ["source"])
    original_prime = module._brief_prime_activity

    def mismatched(snap, rows):
        resolutions = deepcopy(original_prime(snap, rows))
        for resolution in resolutions.values():
            item = resolution["obligations"][0]
            if changed == "source_calculation_id":
                item[changed] = "another-calculation"
            elif changed == "source_business":
                item[changed] = {"subject_id": "another-subject"}
            else:
                item["key"] = "" if changed == "empty_key" else 1
        return resolutions

    monkeypatch.setattr(module, "_brief_prime_activity", mismatched)
    calls = []
    original = BusinessQueries.business_progress

    def observed(queries, connection, period, subjects):
        calls.append(set(subjects))
        return original(queries, connection, period, subjects)

    monkeypatch.setattr(BusinessQueries, "business_progress", observed)
    voucher = Dashboard(engine).brief("2026-01", section="vouchers")["data"]["collections"]["vouchers"]["items"][0]
    assert voucher["has_business_progress"] is True
    assert calls == [{"source"}]


def test_checked_obligation_preserves_original_reversal_and_replacement_progress(engine, monkeypatch):
    _source_obligation(engine, monkeypatch)
    save(engine, subject="source")
    publish(engine, ["source"])
    close(engine)
    save(engine, subject="source", amount=125, revision=1, request="replace")
    publish(engine, ["source"], request="replacement", posting_period="2026-02")

    def unexpected(*args, **kwargs):
        raise AssertionError("all displayed versions have checked own obligations")

    monkeypatch.setattr(BusinessQueries, "business_progress", unexpected)
    january = Dashboard(engine).brief("2026-01", section="vouchers")["data"]["collections"]["vouchers"]["items"]
    february = Dashboard(engine).brief("2026-02", section="vouchers")["data"]["collections"]["vouchers"]["items"]
    assert len(january) == 1 and january[0]["state"] == "已入账"
    assert len(february) == 2 and {item["state"] for item in february} == {"已入账", "冲正"}
    assert all(item["has_business_progress"] is True for item in [*january, *february])


def test_profile_progress_is_not_shared_between_voucher_versions(engine, monkeypatch):
    from ai_accounting.kernel import dashboard as module

    save(engine)
    publish(engine)
    close(engine)
    save(engine, amount=125, revision=1, request="replace")
    publish(engine, request="replacement", posting_period="2026-02")
    monkeypatch.setattr(module, "_voucher_profile_progress", lambda voucher, context: voucher["state"] == "冲正")
    calls = []
    original = BusinessQueries.business_progress

    def observed(queries, connection, period, subjects):
        calls.append(set(subjects))
        return original(queries, connection, period, subjects)

    monkeypatch.setattr(BusinessQueries, "business_progress", observed)
    vouchers = Dashboard(engine).brief("2026-02", section="vouchers")["data"]["collections"]["vouchers"]["items"]
    assert {item["state"]: item["has_business_progress"] for item in vouchers} == {
        "冲正": True, "已入账": False,
    }
    assert calls == [{"charge"}]


def test_withdrawn_open_source_does_not_leave_a_progress_voucher(engine, monkeypatch):
    _source_obligation(engine, monkeypatch)
    save(engine, subject="source")
    publish(engine, ["source"])
    save(engine, subject="unpublished", request="keep-month")
    preview = engine.preview_delete("source")
    engine.delete("source", preview_digest=preview["digest"], epochs=preview["epochs"], request_id="withdraw")
    assert Dashboard(engine).brief("2026-01", section="vouchers")["data"]["collections"]["vouchers"]["items"] == []
    with pytest.raises(KernelError) as failure:
        Dashboard(engine).brief("2026-01", section="vouchers", voucher_number=1)
    assert failure.value.code == "dashboard_voucher_not_found"


@pytest.mark.parametrize("broken", ["outcome", "fact_seal", "publication"])
def test_checked_obligation_shortcut_still_rejects_damaged_selected_source(
    engine, monkeypatch, broken,
):
    _source_obligation(engine, monkeypatch)
    save(engine, subject="source")
    publish(engine, ["source"])
    assert Dashboard(engine).brief("2026-01")["data"]["collections"]["vouchers"]["items"][0]["has_business_progress"] is True
    with engine.store.connection(read_only=True) as connection:
        row = connection.execute("SELECT id,fact_id,outcome FROM calculation WHERE subject_id='source'").fetchone()
    if broken == "outcome":
        outcome = json.loads(row["outcome"])
        outcome["values"]["amount"] += 1
        damage(engine, "calculation", "UPDATE calculation SET outcome=? WHERE id=?",
               (json.dumps(outcome), row["id"]))
    elif broken == "fact_seal":
        damage(engine, "fact_seal", "DELETE FROM fact_seal WHERE fact_id=?",
               (row["fact_id"],), foreign_keys=False)
    else:
        damage(engine, "calculation_publication", "DELETE FROM calculation_publication WHERE calculation_id=?",
               (row["id"],), foreign_keys=False)
    with pytest.raises(KernelError) as failure:
        Dashboard(engine).brief("2026-01", section="vouchers", limit=1)
    assert failure.value.code == "content_integrity_failed"


def test_page_progress_does_not_expand_offpage_payments_for_checked_source(
    book, monkeypatch, record_property,
):
    from ai_accounting.kernel import dashboard as module

    engine, save_fact, publish_fact, _ = book
    opening(save_fact, publish_fact)
    save_fact("expense", "source", {
        "period": "2026-09", "counterparty_id": "supplier", "amount_fen": 1000,
        "expense_class": "administration", "creditor_kind": "supplier",
    })
    publish_fact("source")

    def unexpected(*args, **kwargs):
        raise AssertionError("checked page source must not expand the relation closure")

    monkeypatch.setattr(BusinessQueries, "business_progress", unexpected)
    samples = []
    progress_subjects = []
    original_funds = module._funds

    def before_funds(snap, **options):
        progress_subjects.append({item["subject_id"] for item in snap.reads._calculations.values()})
        return original_funds(snap, **options)

    monkeypatch.setattr(module, "_funds", before_funds)
    for count in (0, 3, 6):
        if count:
            for index in range(count - 3, count):
                save_fact("payment", "paid-" + str(index), {
                    "period": "2026-09", "actual_date": f"2026-09-{10 + index}",
                    "direction": "outflow", "bank_account_id": "bank-a",
                    "counterparty_id": "supplier", "amount_fen": index + 1,
                    "allocations": [{"source_kind": "expense", "source_id": "source",
                                     "obligation": "primary", "amount_fen": index + 1}],
                })
            publish_fact(*["paid-" + str(index) for index in range(count - 3, count)])
        work, response = measure_work(engine, lambda: Dashboard(engine).brief(
            "2026-09", section="vouchers", limit=1,
        )["data"])
        vouchers = response["collections"]["vouchers"]
        assert [(item["subject_id"], item["has_business_progress"]) for item in vouchers["items"]] == [("source", True)]
        assert vouchers["page"]["total_count"] == count + 1
        paid = sum(range(1, count + 1))
        assert response["funds_overview"]["outflow_fen"] == paid
        assert response["open_items"]["payable_fen"] == 1000 - paid
        samples.append({"payments": count, "counters": work["counters"]})
    # The remainder of the page still proves complete amounts; only the
    # progress existence phase must avoid these off-page payment bodies.
    assert progress_subjects == [{"source"}] * 3
    record_property("checked_progress_page_work", json.dumps(samples, sort_keys=True))


def test_profile_presence_excludes_exact_visible_text_and_objects_per_version():
    voucher = _voucher()
    context = _profiles(counterparties=[_profile("party", "甲")], assets=[_profile("asset", "设备")])
    context["profiles"]["business"]["values"] = {"purpose": "购置设备", "note": "确认费用"}
    assert _voucher_profile_progress(voucher, context) is False
    # A different saved voucher of the same business has different visible text.
    old = deepcopy(voucher)
    old["lines"][0]["source_label"] = "原购置用途"
    assert _voucher_profile_progress(old, context) is True
    context["deleted"] = True
    assert _voucher_profile_progress(voucher, context) is True


@pytest.mark.parametrize("groups", [
    {"employees": [_profile("party", "甲")], "counterparties": [_profile("party", "甲")]},
    {"counterparties": [_profile("another-party", "甲")]},
    {"assets": [_profile("another-asset", "设备")]},
    {"fund_accounts": [_profile("party", "甲")]},
])
def test_profile_presence_retains_distinct_identity_roles_and_fund_accounts(groups):
    assert _voucher_profile_progress(_voucher(), _profiles(**groups)) is True


def test_no_progress_page_and_focus_never_read_full_business_status(engine, monkeypatch):
    for index in range(4):
        save(engine, subject=f"charge-{index}", request=f"save-{index}")
    publish(engine, [f"charge-{index}" for index in range(4)])

    def unexpected(*args, **kwargs):
        raise AssertionError("bounded progress presence must not read complete status")

    monkeypatch.setattr(BusinessQueries, "_business_status", unexpected)
    calls = []
    original = BusinessQueries.business_progress

    def observed(queries, connection, period, subjects):
        calls.append(set(subjects))
        return original(queries, connection, period, subjects)

    monkeypatch.setattr(BusinessQueries, "business_progress", observed)
    response = Dashboard(engine).brief("2026-01", section="vouchers", limit=2, voucher_number=4)["data"]
    vouchers = response["collections"]["vouchers"]["items"]
    assert [item["has_business_progress"] for item in vouchers] == [False, False]
    assert response["focused_voucher"]["has_business_progress"] is False
    assert calls == [{"charge-0", "charge-1", "charge-3"}]


def test_closed_and_open_replacement_profiles_use_selected_adoption(engine):
    save(engine)
    publish(engine)
    close(engine)
    save(engine, amount=125, revision=1, request="replace")
    publish(engine, request="replacement", posting_period="2026-02")
    for period in ("2026-01", "2026-02"):
        vouchers = Dashboard(engine).brief(period)["data"]["collections"]["vouchers"]["items"]
        assert all(item["has_business_progress"] is False for item in vouchers)


@pytest.mark.parametrize("closed", [False, True])
def test_new_obligation_respects_historical_source_cutoff(engine, monkeypatch, closed):
    save(engine)
    publish(engine)
    if closed:
        close(engine)

    def with_obligation(version, context):
        original = calculate(version, context)
        return Outcome(original.lines, {**original.values, "obligations": [{
            "key": "test_charge:charge:primary", "name": "primary", "amount_fen": 125,
            "account": "2202", "normal": "credit", "category": "payable",
            "counterparty_id": None,
        }]})

    monkeypatch.setitem(engine.store.registry.evaluators, "test_charge", with_obligation)
    save(engine, amount=125, revision=1, request="add-obligation")
    publish(engine, request="new-obligation", posting_period="2026-02" if closed else None)
    dashboard = Dashboard(engine)
    status = dashboard.business_status("2026-01", "charge")["data"]
    vouchers = dashboard.brief("2026-01")["data"]["collections"]["vouchers"]["items"]
    # An open replacement belongs to this month. A future source is outside
    # this historical month even in its current settlement followup view.
    assert bool(status["settlements"]["obligations"]) is (not closed)
    assert bool(status["current_followups"]["settlements"]["obligations"]) is (not closed)
    assert all(item["has_business_progress"] is (not closed) for item in vouchers)


def test_narrow_presence_includes_obligation_and_source_and_payment_subjects(book):
    engine, save_fact, publish_fact, _ = book
    save_fact("expense", "office", {
        "period": "2026-09", "counterparty_id": "supplier", "amount_fen": 1000,
        "expense_class": "administration", "creditor_kind": "supplier",
    })
    publish_fact("office")
    save_fact("payment", "paid", {
        "period": "2026-09", "actual_date": "2026-09-10", "direction": "outflow",
        "bank_account_id": "bank", "counterparty_id": "supplier", "amount_fen": 1000,
        "allocations": [{"source_kind": "expense", "source_id": "office",
                         "obligation": "primary", "amount_fen": 1000}],
    })
    publish_fact("paid")
    with Dashboard(engine)._snapshot("2026-09") as snapshot:
        assert snapshot.queries.business_progress(snapshot.connection, "2026-09", {"office", "paid"}) == {
            "office": True, "paid": True,
        }


def test_narrow_presence_keeps_conflicting_declared_source_scope(monkeypatch):
    calc = {"id": "calc", "subject_id": "payment"}
    reads = SimpleNamespace(
        related_subjects=lambda subjects: subjects | {"payment"},
        prime_calculations=lambda ids: None,
        relations_many=lambda ids, **kwargs: {"calc": {"settlements": [{
            "index": 0, "source_business": {"subject_id": "frozen-source"},
            "settlement_business": {"subject_id": "payment"}, "state": "unresolved",
        }]}},
        calculation=lambda ident: calc,
        declared_subjects=lambda calc: {"declared-source"},
        declared_sources=lambda calc: [{"source_id": "declared-source"}],
    )
    selected = {"through_period": {"voucher_events": [{"calculation_id": "calc"}], "state_results": []}}
    queries = BusinessQueries(SimpleNamespace(store=None))
    monkeypatch.setattr(queries, "_reads", lambda connection: reads)
    monkeypatch.setattr(queries, "_selected_accounting", lambda *args, **kwargs: selected)
    subjects = {"declared-source", "frozen-source", "payment", "unrelated"}
    monkeypatch.setattr(queries, "_current_settlement_selection", lambda *args, **kwargs: (subjects, selected, "2026-09"))
    assert queries.business_progress(None, "2026-09", subjects) == {
        "declared-source": True, "frozen-source": True, "payment": True, "unrelated": False,
    }
