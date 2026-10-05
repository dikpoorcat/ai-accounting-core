"""Current money SQL consumes proved effects without repeating their full bodies."""

import copy
import sqlite3
from dataclasses import replace

import pytest
import test_banking as banking
import test_investments as investments
import test_platforms as platforms
from stage9_metrics import measure_work
from test_dashboard_funds_alignment import _publish_filter_funding
from test_funds_source_integrity import _reviewed_closed_investment
from test_funds_summary_page import _source_rows, _steps
from test_integrity_content import damage

from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_funds import FundsRead, _sql_summary_page
from ai_accounting.kernel.storage import _active_fact_reads
from ai_accounting.kernel.types import canonical

bank_book = banking.book
investment_book = investments.book
platform_book = platforms.book
MONTH = "2026-09"


def _event_read(snap):
    read = FundsRead(snap)
    assert read._verified_money_effects() is None
    read.events(current=True)
    return read


def _rows(read):
    sql, parameters = read.movements()
    return [dict(row) for row in read.connection.execute(sql + " ORDER BY page_key", parameters)]


def _fallback(read, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(FundsRead, "_verified_money_effects", lambda self: None)
        return _rows(read)


def test_current_effects_keep_cash_bank_platform_transfer_and_full_filtered_pages(
    platform_book,
    monkeypatch,
):
    engine, save, publish, _ = platform_book
    banking.funding(save, publish, amount=1000)
    save(
        "cash_funding",
        "cash",
        {
            "period": MONTH,
            "actual_date": MONTH + "-01",
            "owner_id": "owner",
            "amount_fen": 700,
            "funding_kind": "capital",
            "cash_account_id": "cash",
        },
    )
    save("platform_funding", "platform", platforms.funding_data(amount=300))
    save(
        "funds_transfer",
        "bank-transfer",
        {
            "period": MONTH,
            "actual_date": MONTH + "-02",
            "amount_fen": 100,
            "source_bank_account_id": "bank-a",
            "destination_bank_account_id": "bank-b",
        },
    )
    save(
        "cash_bank_transfer",
        "cash-transfer",
        {
            "period": MONTH,
            "actual_date": MONTH + "-02",
            "amount_fen": 50,
            "direction": "deposit",
            "cash_account_id": "cash",
            "bank_account_id": "bank-a",
        },
    )
    save("bank_platform_transfer", "platform-transfer", platforms.transfer_data(amount=25))
    # A nonmoney result and a no-voucher state must not enter the narrow input.
    save(
        "expense",
        "accrual",
        {
            "period": MONTH,
            "counterparty_id": "supplier",
            "amount_fen": 1,
            "expense_class": "administration",
            "creditor_kind": "supplier",
        },
    )
    banking.opening(save, publish)
    publish("cash", "platform", "bank-transfer", "cash-transfer", "platform-transfer", "accrual")
    with Dashboard(engine)._snapshot(MONTH) as snap:
        snap.month_journal.account_amounts()
        read = _event_read(snap)
        before = set(snap.reads._verified_source_contents)
        effects = read._verified_money_effects()
        assert len(effects) == 9
        assert {row[7] for row in effects} == {"cash", "bank", "platform"}
        expected, old_steps = _steps(
            snap.connection, lambda: _fallback(FundsRead(snap), monkeypatch)
        )
        actual, new_steps = _steps(snap.connection, lambda: _rows(read))
        assert actual == expected
        assert new_steps < old_steps
        assert before == snap.reads._verified_source_contents.keys()
        assert sum(row["internal_transfer"] for row in actual) == 6
        summary = (
            "SELECT category,balance_key,sum(signed_amount) net,count(*) n "
            "FROM source_rows GROUP BY category,balance_key"
        )
        all_summaries = None
        for limit, after in ((1, None), (2, actual[0]["page_key"]), (1, actual[-1]["page_key"])):
            new_sql, new_params = read.movements()
            with monkeypatch.context() as patch:
                patch.setattr(FundsRead, "_verified_money_effects", lambda self: None)
                old_sql, old_params = FundsRead(snap).movements()
            kwargs = dict(after=after, limit=limit, where="balance_key=?", filters=("bank-a",))
            # Cursor validity is evaluated in the complete filtered source.
            if (
                after is not None
                and next(row for row in actual if row["page_key"] == after)["balance_key"]
                != "bank-a"
            ):
                kwargs["where"], kwargs["filters"] = "1=1", ()
            new = _sql_summary_page(
                snap.connection,
                new_sql,
                new_params,
                summary,
                ("category", "balance_key", "net", "n"),
                **kwargs,
            )
            old = _sql_summary_page(
                snap.connection,
                old_sql,
                old_params,
                summary,
                ("category", "balance_key", "net", "n"),
                **kwargs,
            )
            assert (new[0], _source_rows(new[1]), new[2]) == (old[0], _source_rows(old[1]), old[2])
            if all_summaries is None:
                all_summaries = new[0]
            assert new[0] == all_summaries and new[2]["total_count"] == 9
        totals = read.account_summary()
        assert totals["inflow_fen"] == 2000 and totals["outflow_fen"] == 0
        assert totals["internal_transfer_fen"] == 175

        # Decoded unrelated bodies neither enter inputs nor get scanned.
        class ExactBodies(dict):
            def __iter__(self):
                raise AssertionError("full body cache scan")

            def items(self):
                raise AssertionError("full body cache scan")

        snap.reads._verified_source_contents = ExactBodies(snap.reads._verified_source_contents)
        snap.reads._verified_source_contents.update((f"unused-{i}", {}) for i in range(100))
        assert read._verified_money_effects() == effects


def test_exact_body_is_required_before_events_verification_can_load_it(bank_book):
    engine, save, publish, _ = bank_book
    banking.funding(save, publish)
    with Dashboard(engine)._snapshot(MONTH) as snap:
        reader = _event_read(snap)
        assert reader._verified_money_effects() is None
        assert "c.outcome" in reader.movements()[0]
    with Dashboard(engine)._snapshot(MONTH) as snap:
        snap.month_journal.account_amounts()
        reader = _event_read(snap)
        ident = reader._verified_money_effects()[0][3]
        snap.reads.metadata({ident}, state=False)
        saved = snap.reads._verified_source_contents.pop(ident)
        assert ident in snap.reads._metadata  # Metadata cannot replace this body.
        assert reader._verified_money_effects() is None
        assert "c.outcome" in reader.movements()[0]
        snap.reads._verified_source_contents[ident] = saved
        assert reader._verified_money_effects() is not None
        token = _active_fact_reads.set(None)
        try:
            assert reader._verified_money_effects() is None
        finally:
            _active_fact_reads.reset(token)
        with historical_content(1):
            assert reader._verified_money_effects() is None


def test_effect_inputs_publish_only_after_events_proof_and_share_one_selection(
    bank_book, monkeypatch
):
    engine, _, _, _ = bank_book
    _publish_filter_funding(engine, ["bank-a"] * 3)
    with Dashboard(engine)._snapshot(MONTH) as snap:
        snap.month_journal.account_amounts()
        reader = FundsRead(snap)
        selected = []
        original = reader._verified_current_event_rows

        def select(**kwargs):
            rows = original(**kwargs)
            selected.append(rows)
            return rows

        monkeypatch.setattr(reader, "_verified_current_event_rows", select)
        with monkeypatch.context() as patch:

            def reject(_rows, *, through_period):
                assert through_period == snap.month
                raise KernelError("content_integrity_failed", "synthetic reference proof failure")

            patch.setattr(snap.reads, "verify_selected_voucher_adoptions", reject)
            with pytest.raises(KernelError):
                reader.movements()
        assert reader._money_event_rows is None and reader.event_queries == {}
        assert reader._verified_money_effects() is None
        actual = _rows(reader)
        assert len(actual) == 3 and len(selected) == 2
        assert reader._money_event_rows is selected[-1]
        assert _rows(reader) == actual
        assert len(selected) == 2  # Repeated movement consumption does not rescan.


@pytest.mark.parametrize(
    "shape",
    [
        "balances_object",
        "missing_values",
        "date_object",
        "amount_bool",
        "amount_real",
        "amount_large",
        "missing_key",
    ],
)
def test_nonordinary_decoded_shape_retains_sql_fallback(bank_book, shape):
    engine, save, publish, _ = bank_book
    banking.funding(save, publish)
    with Dashboard(engine)._snapshot(MONTH) as snap:
        snap.month_journal.account_amounts()
        reader = _event_read(snap)
        ident = reader._verified_money_effects()[0][3]
        original = snap.reads._verified_source_contents[ident]
        changed = copy.deepcopy(original)
        if shape == "balances_object":
            changed["balances"] = {}
        elif shape == "missing_values":
            changed.pop("values")
        elif shape == "date_object":
            changed["values"]["actual_date"] = {}
        elif shape == "missing_key":
            changed["balances"][0].pop("key")
        else:
            changed["balances"][0]["amount"] = {
                "amount_bool": True,
                "amount_real": 1.0,
                "amount_large": 1 << 63,
            }[shape]
        # Shape probing is private; this does not create a persisted proof.
        snap.reads._verified_source_contents[ident] = changed
        assert reader._verified_money_effects() is None
        assert "c.outcome" in reader.movements()[0]
        snap.reads._verified_source_contents[ident] = original
        assert reader._verified_money_effects() is not None


def test_unknown_actual_date_and_empty_effects_preserve_sql_semantics(bank_book):
    engine, save, publish, _ = bank_book
    banking.funding(save, publish)
    with Dashboard(engine)._snapshot(MONTH) as snap:
        snap.month_journal.account_amounts()
        reader = _event_read(snap)
        ident = reader._verified_money_effects()[0][3]
        original = snap.reads._verified_source_contents[ident]
        # Exercise the private scalar consumer on copied shapes. Persisted
        # proof/rejection is covered separately; this does not attest a body.
        for balances in (original["balances"], []):
            value = copy.deepcopy(original)
            value["balances"] = balances
            value["values"].pop("actual_date", None)
            snap.reads._verified_source_contents[ident] = value
            effects = reader._verified_money_effects()
            assert all(row[-1] is None for row in effects)
            assert len(effects) == len(balances)
            actual = _rows(reader)
            assert len(actual) == len(balances)
            assert all(row["actual_date"] is None for row in actual)
        snap.reads._verified_source_contents[ident] = original


def test_effect_indices_zero_rows_and_sql_integer_overflow_are_preserved(bank_book, monkeypatch):
    engine, save, publish, _ = bank_book
    banking.funding(save, publish)
    with Dashboard(engine)._snapshot(MONTH) as snap:
        snap.month_journal.account_amounts()
        reader = _event_read(snap)
        ident = reader._verified_money_effects()[0][3]
        original = snap.reads._verified_source_contents[ident]
        value = copy.deepcopy(original)
        value["balances"] = [
            {"category": "short_term_investment", "key": "fund", "amount": 1},
            {"category": "bank", "key": "bank-a", "amount": 0},
            {"category": "cash", "key": "cash", "amount": 9},
            {"category": "bank", "key": "bank-a", "amount": -3},
        ]
        snap.reads._verified_source_contents[ident] = value
        assert [row[6] for row in reader._verified_money_effects()] == [2, 3]
        actual = _rows(reader)
        assert [row["page_key"].rsplit(":", 1)[1] for row in actual] == ["000000", "000001"]
        assert [row["signed_amount"] for row in actual] == [9, -3]
        # Keep group arithmetic in SQLite; the narrow carrier must not turn
        # its signed 64-bit overflow into Python's unlimited integer sum.
        value["balances"][2]["amount"] = (1 << 63) - 1
        value["balances"][3]["amount"] = 1
        with pytest.raises(sqlite3.OperationalError, match="integer overflow"):
            _rows(reader)
        snap.reads._verified_source_contents[ident] = original


@pytest.mark.parametrize("amount", [(1 << 53) + 9, (1 << 63) - 1])
def test_large_integer_fen_remains_exact_in_sql(bank_book, monkeypatch, amount):
    engine, save, publish, _ = bank_book
    banking.funding(save, publish, amount=amount)
    with Dashboard(engine)._snapshot(MONTH) as snap:
        snap.month_journal.account_amounts()
        actual = _rows(FundsRead(snap))
        assert actual == _fallback(FundsRead(snap), monkeypatch)
        assert actual[0]["signed_amount"] == amount
        assert type(actual[0]["signed_amount"]) is int


def test_open_replacement_and_review_retain_adopted_basis(bank_book, monkeypatch):
    engine, save, publish, proof = bank_book
    banking.funding(save, publish)
    fields = {
        "period": MONTH,
        "actual_date": MONTH + "-01",
        "owner_id": "owner",
        "funding_kind": "capital",
        "amount_fen": 700,
        "bank_account_id": "bank-a",
    }
    engine.amend_fact(
        "funding",
        "funding",
        fields,
        evidence=(proof,),
        expected_revision=1,
        recording_error_confirmed=True,
        request_id="replace",
    )
    publish("funding")
    from ai_accounting.kernel import engine as engine_module

    with monkeypatch.context() as patch:
        patch.setattr(engine_module, "PROGRAM_VERSION", "synthetic-money-review")
        reviewed = publish("funding")
    assert reviewed["results"][0]["impact"] == "review_no_impact"
    with Dashboard(engine)._snapshot(MONTH) as snap:
        snap.month_journal.account_amounts()
        reader = _event_read(snap)
        actual = _rows(reader)
        assert actual == _fallback(FundsRead(snap), monkeypatch)
        assert len(actual) == 1 and actual[0]["signed_amount"] == 700
        assert (
            actual[0]["calculation_id"]
            == snap.month_journal.verified_rows()[0]["basis_calculation_id"]
        )


def test_closed_review_stays_on_frozen_sql(investment_book, monkeypatch):
    engine, _ = _reviewed_closed_investment(investment_book, monkeypatch)
    with Dashboard(engine)._snapshot("2026-01") as snap:
        snap.month_journal.account_amounts()
        reader = _event_read(snap)
        assert reader._verified_money_effects() is None
        assert _rows(reader) == _fallback(FundsRead(snap), monkeypatch)


def test_current_reversal_uses_original_proof_and_summary_sign(bank_book, monkeypatch):
    engine, save, publish, proof = bank_book
    fields = {
        "period": MONTH,
        "actual_date": MONTH + "-03",
        "cash_account_id": "cash",
        "amount_fen": 1000,
    }
    save("managed_reserve_refund", "receipt", fields)
    publish("receipt")
    investments.close(engine, MONTH)
    engine.amend_fact(
        "managed_reserve_refund",
        "receipt",
        fields | {"amount_fen": 700},
        evidence=(proof,),
        expected_revision=1,
        recording_error_confirmed=True,
        request_id="amend",
    )
    preview = engine.preview(["receipt"], posting_period="2026-10")
    engine.confirm(
        ["receipt"],
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        posting_period="2026-10",
        request_id="correct",
    )
    with Dashboard(engine)._snapshot("2026-10") as snap:
        snap.month_journal.account_amounts()
        reader = _event_read(snap)
        assert len(reader._verified_money_effects()) == 2
        actual = _rows(reader)
        assert actual == _fallback(FundsRead(snap), monkeypatch)
        assert sum(row["signed_amount"] for row in actual) == -300
        assert reader.account_summary()["inflow_fen"] == -300


@pytest.mark.parametrize("corruption", ["body", "missing_line", "missing_source", "duplicate"])
def test_failed_scope_never_supplies_inputs_and_fresh_requests_reject_damage(bank_book, corruption):
    engine, _, _, _ = bank_book
    _publish_filter_funding(engine, ["bank-a"] * 3)
    with Dashboard(engine)._snapshot(MONTH) as snap:
        snap.month_journal.account_amounts()
        assert _event_read(snap)._verified_money_effects() is not None
    with engine.store.connection(read_only=True) as connection:
        row = dict(
            connection.execute(
                "SELECT c.id,c.outcome,v.id version_id FROM calculation c "
                "JOIN voucher_version v ON v.calculation_id=c.id "
                "WHERE c.subject_id='filter-2026-09-0002'"
            ).fetchone()
        )
    if corruption == "body":
        damage(
            engine,
            "calculation",
            "UPDATE calculation SET outcome=json_set(outcome,'$.balances[0].amount',2) WHERE id=?",
            (row["id"],),
        )
    elif corruption == "missing_line":
        damage(
            engine,
            "voucher_line",
            "DELETE FROM voucher_line WHERE version_id=? AND line_no=1",
            (row["version_id"],),
        )
    elif corruption == "missing_source":
        damage(
            engine,
            "calculation",
            "DELETE FROM calculation WHERE id=?",
            (row["id"],),
            foreign_keys=False,
        )
    else:
        damage(
            engine,
            "calculation",
            "UPDATE calculation SET outcome=? WHERE id=?",
            ('{"values":{},' + row["outcome"][1:], row["id"]),
        )
    with Dashboard(engine)._snapshot(MONTH) as snap:
        with pytest.raises(KernelError) as failure:
            snap.month_journal.account_amounts()
        assert failure.value.code == "content_integrity_failed"
        assert FundsRead(snap)._verified_money_effects() is None
    with pytest.raises(KernelError):
        Dashboard(engine).brief(MONTH, limit=1)


def test_actual_money_carrier_work_does_not_expand_with_unrelated_results_and_wide_values(
    bank_book,
    monkeypatch,
    record_property,
):
    engine, save, publish, _ = bank_book
    from ai_accounting.kernel import engine as engine_module

    original = engine.store.registry.evaluators["funding"]
    width = [0]

    def evaluate(version, context):
        result = original(version, context)
        return replace(result, values=result.values | {"synthetic_unused": "x" * width[0]})

    monkeypatch.setitem(engine.store.registry.evaluators, "funding", evaluate)
    _publish_filter_funding(engine, ["bank-a"] * 3)
    dashboard = Dashboard(engine)

    def scoped_work():
        with dashboard._snapshot(MONTH) as snap:
            # Complete month proof legitimately grows with added current
            # business. Measure it separately from consumption of money only.
            proof, _ = measure_work(engine, lambda: dashboard.brief(MONTH, limit=1))
            snap.month_journal.account_amounts()
            read = _event_read(snap)
            carrier = canonical(read._verified_money_effects())
            # Instrument the actual existing connection after complete proof;
            # do not include or pretend to avoid the month body's load/decode.
            from collections import Counter, defaultdict

            from stage9_metrics import _Connection

            counts, sql, work = Counter(), Counter(), defaultdict(Counter)
            measured = _Connection(snap.connection, counts, sql, work, set())
            read.connection = measured
            steps = [0]

            def progress():
                steps[0] += 100
                return 0

            snap.connection.set_progress_handler(progress, 100)
            try:
                rows = _rows(read)
            finally:
                snap.connection.set_progress_handler(None, 0)
            return (
                len(read._verified_money_effects()),
                len(carrier.encode()),
                steps[0],
                dict(counts),
                [row["signed_amount"] for row in rows],
                proof["counters"],
            )

    before = scoped_work()
    for period in (MONTH, "2026-08"):
        subjects = []
        for index in range(24):
            subject = f"unrelated-{period}-{index}"
            save(
                "expense",
                subject,
                {
                    "period": period,
                    "counterparty_id": "supplier",
                    "amount_fen": 1,
                    "expense_class": "administration",
                    "creditor_kind": "supplier",
                },
            )
            subjects.append(subject)
        publish(*subjects)
    width[0] = 16_384
    with monkeypatch.context() as patch:
        patch.setattr(engine_module, "PROGRAM_VERSION", "synthetic-wide-money-result")
        publish(*(f"filter-{MONTH}-{index:04d}" for index in range(3)))
    after = scoped_work()
    assert before[:5] == after[:5]
    assert after[3].get("calculation_result_rows_loaded", 0) == 0
    assert after[3].get("calculation_result_bytes_loaded", 0) == 0
    assert (
        after[5]["calculation_result_bytes_loaded"] > before[5]["calculation_result_bytes_loaded"]
    )
    assert (
        after[5]["calculation_result_json_decodes"] > before[5]["calculation_result_json_decodes"]
    )
    for label, result in (("before", before), ("after", after)):
        for field, value in zip(
            ("carrier_rows", "carrier_bytes", "vm_steps"), result[:3], strict=True
        ):
            record_property(f"{label}_{field}", value)
        record_property(f"{label}_returned_rows", result[3]["returned_rows"])
        record_property(f"{label}_returned_bytes", result[3]["returned_value_bytes"])
        record_property(f"{label}_proof_result_bytes", result[5]["calculation_result_bytes_loaded"])
        record_property(
            f"{label}_proof_result_decodes", result[5]["calculation_result_json_decodes"]
        )
