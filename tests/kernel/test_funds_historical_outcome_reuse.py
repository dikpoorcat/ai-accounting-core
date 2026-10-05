"""Local bank identity reuse retains exact frozen versions and raw fallbacks."""

import copy
import json

import pytest
import test_banking as banking
from stage9_metrics import measure_work
from test_funds_historical_account_identities import prepared, public, source
from test_integrity_content import damage
from test_payroll import profile

from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_funds import FundsRead
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.types import YearMonth

bank_book = banking.book
MONTH = YearMonth("2026-10").ordinal
FROZEN = YearMonth("2026-09").ordinal


def without_carry(monkeypatch):
    original = QueryReads.frozen_bank_account_identities

    def raw(reads, periods, **kwargs):
        kwargs["_decoded_outcomes"] = None
        return original(reads, periods, **kwargs)

    monkeypatch.setattr(QueryReads, "frozen_bank_account_identities", raw)


@pytest.mark.parametrize("page,expected", [("funds", 2), ("brief", 0)])
def test_public_reuse_reduces_actual_body_transfer_and_decode(
    bank_book, monkeypatch, page, expected,
):
    engine, _, _ = prepared(bank_book)

    def response():
        result = getattr(Dashboard(engine), page)("2026-10")
        result.pop("generated_at", None)
        result["data"].pop("generated_at", None)
        return result

    narrow_work, narrow = measure_work(engine, response)
    with monkeypatch.context() as raw:
        without_carry(raw)
        raw_work, baseline = measure_work(engine, response)
    assert narrow == baseline
    # Funds consumes one exact frozen identity per bank. Brief consumes only
    # money totals and has no account-identity body to carry between calls.
    if page == "funds":
        assert narrow["data"]["bank_account_count"] == expected
    else:
        assert narrow["data"]["funds_overview"]["bank_fen"] == 1000
    assert (raw_work["counters"]["calculation_result_rows_loaded"]
            - narrow_work["counters"]["calculation_result_rows_loaded"]) == expected
    assert narrow_work["counters"]["returned_rows"] == raw_work["counters"]["returned_rows"]
    assert (narrow_work["counters"]["adoption_accounting_slice_reads"]
            == raw_work["counters"]["adoption_accounting_slice_reads"])
    for counter in ("returned_value_bytes", "calculation_result_bytes_loaded",
                    "calculation_result_json_decodes", "stdlib_json_input_bytes"):
        if expected:
            assert narrow_work["counters"][counter] < raw_work["counters"][counter]
        else:
            assert narrow_work["counters"][counter] == raw_work["counters"][counter]


@pytest.mark.parametrize("formatting", ["canonical", "noncanonical", "incompatible_local"])
def test_exact_local_decode_is_bound_before_raw_bytes_are_omitted(bank_book, formatting):
    engine, _, _ = prepared(bank_book)
    row = source(engine)
    if formatting == "noncanonical":
        raw = json.dumps(json.loads(row["outcome"]), ensure_ascii=False, indent=2)
        damage(engine, "calculation", "UPDATE calculation SET outcome=? WHERE id=?",
               (raw, row["id"]))

    # Instrument the real connection owned by the operation, rather than an
    # already-created snapshot; measure_work installs the SQLite wrapper.
    def measured(carry):
        with QueryReads.snapshot(engine) as reads:
            outcomes = {}
            reads.metadata({row["id"]}, _decoded_outcomes=outcomes)
            if formatting == "incompatible_local":
                outcomes[row["id"]]["values"]["bank_account_id"] = "zero-bank"
            before = (set(reads._verified_sql_outcomes), dict(reads._verified_source_contents),
                      copy.deepcopy(reads._metadata))
            found = reads.frozen_bank_account_identities(
                {row["id"]: FROZEN}, through_period=MONTH,
                _decoded_outcomes=outcomes if carry else None,
            )
            assert before == (set(reads._verified_sql_outcomes),
                              dict(reads._verified_source_contents), reads._metadata)
            return found

    narrow_work, narrow = measure_work(engine, lambda: measured(True))
    raw_work, baseline = measure_work(engine, lambda: measured(False))
    assert narrow == baseline == {row["id"]: "bank-a"}
    expected = 0 if formatting == "incompatible_local" else 1
    assert (raw_work["counters"]["calculation_result_rows_loaded"]
            - narrow_work["counters"]["calculation_result_rows_loaded"]) == expected
    assert (raw_work["counters"]["calculation_result_json_decodes"]
            - narrow_work["counters"]["calculation_result_json_decodes"]) == expected


@pytest.mark.parametrize("corruption", ["fresh_body", "fresh_duplicate", "reusable_leaf"])
def test_mixed_identity_failure_cannot_publish_body_or_metadata(bank_book, corruption):
    engine, _, _ = prepared(bank_book)
    good, bad = source(engine), source(engine, "statement-zero-bank")
    if corruption.startswith("fresh"):
        replacement = (
            '"bank_account_id":"zero-bank","bank_account_id":"bank-a"'
            if corruption == "fresh_duplicate" else '"bank_account_id":"bank-a"'
        )
        raw = bad["outcome"].replace('"bank_account_id":"zero-bank"', replacement)
        assert raw != bad["outcome"]
        damage(engine, "calculation", "UPDATE calculation SET outcome=? WHERE id=?",
               (raw, bad["id"]))
    with QueryReads.snapshot(engine) as reads:
        outcomes = {}
        reads.metadata({good["id"]}, _decoded_outcomes=outcomes)
        before = (copy.deepcopy(reads._metadata), set(reads._verified_sql_outcomes),
                  dict(reads._verified_source_contents), copy.deepcopy(outcomes))
        periods = {good["id"]: FROZEN, bad["id"]: FROZEN}
        if corruption == "reusable_leaf":
            # A strictly decoded object cannot authorize another month's leaf.
            periods[good["id"]] = MONTH
        with pytest.raises(KernelError) as failure:
            reads.frozen_bank_account_identities(
                periods, through_period=MONTH, _decoded_outcomes=outcomes,
            )
        assert failure.value.code == "content_integrity_failed"
        assert before == (reads._metadata, set(reads._verified_sql_outcomes),
                          dict(reads._verified_source_contents), outcomes)
        assert bad["id"] not in reads._metadata
        assert bad["id"] not in reads._verified_sql_outcomes


def test_metadata_carry_rejects_duplicate_before_any_state_success(bank_book):
    engine, _, _ = prepared(bank_book)
    good, bad = source(engine), source(engine, "statement-zero-bank")
    raw = bad["outcome"].replace('"bank_account_id":"zero-bank"',
                                '"bank_account_id":"zero-bank","bank_account_id":"bank-a"')
    damage(engine, "calculation", "UPDATE calculation SET outcome=? WHERE id=?",
           (raw, bad["id"]))
    with QueryReads.snapshot(engine) as reads:
        outcomes = {}
        with pytest.raises(KernelError):
            reads.metadata({good["id"], bad["id"]}, _decoded_outcomes=outcomes)
        assert outcomes == reads._metadata == reads._verified_source_contents == {}
        assert reads._verified_sql_outcomes == set()
    with pytest.raises(KernelError):
        Dashboard(engine).funds("2026-10")


def test_exact_ids_remain_bounded_when_unrelated_objects_and_revisions_grow(bank_book):
    engine, save, _ = prepared(bank_book)
    row = source(engine)

    def read():
        with QueryReads.snapshot(engine) as reads:
            outcomes = {}
            reads.metadata({row["id"]}, _decoded_outcomes=outcomes)
            return reads.frozen_bank_account_identities(
                {row["id"]: FROZEN}, through_period=MONTH, _decoded_outcomes=outcomes,
            )

    before, expected = measure_work(engine, read)
    for index in range(8):
        for revision in range(3):
            fact = profile(employee_id=f"unrelated-{index}",
                           social_insurance_base_fen=100_000 + revision)
            save(fact.kind, f"unrelated-profile-{index}",
                 fact.model_dump(mode="json"), revision)
    after, actual = measure_work(engine, read)
    assert actual == expected == {row["id"]: "bank-a"}
    for counter in ("returned_rows", "returned_value_bytes", "calculation_result_rows_loaded",
                    "calculation_result_json_decodes", "stdlib_json_input_bytes"):
        assert before["counters"][counter] == after["counters"][counter]
    assert after["counters"]["sqlite_vm_steps"] <= before["counters"]["sqlite_vm_steps"] + 100


def test_unowned_funds_retains_raw_state_route(bank_book):
    engine, _, _ = prepared(bank_book)
    with Dashboard(engine)._snapshot("2026-10") as snap:
        expected = FundsRead(snap).account_summary()
    with Dashboard(engine)._snapshot("2026-10") as snap:
        snap.reads = QueryReads(engine, snap.connection)
        snap.queries = BusinessQueries(engine, reads=snap.reads)
        read = FundsRead(snap)
        assert read._state_outcomes is None
        assert read.account_summary() == expected
        assert read.snap.reads._verified_sql_outcomes == set()


def test_all_frozen_versions_of_one_subject_survive_local_carry(bank_book, monkeypatch):
    engine, save, publish = prepared(bank_book)
    proof = bank_book[3]
    original = source(engine)
    reviewed_ids = {original["id"]}
    for revision, month in enumerate(("2026-10", "2026-11"), 1):
        # Correct the preserved extraction into more original rows with the
        # same whole funding amount. Each closed correction is independently
        # adopted in its explicit open posting month; no-impact reviews keep
        # their original posting month and cannot stand in for this case.
        count = revision + 2
        entries = [banking.entry(f"part-{index}", amount=1000 // count)
                   for index in range(count - 1)]
        entries.append(banking.entry(f"part-{count - 1}",
                                     amount=1000 - sum(item["signed_fen"] for item in entries)))
        data = {"period": "2026-09", "bank_account_id": "bank-a", "opening_fen": 0,
                "closing_fen": 1000, "entries": entries}
        engine.amend_fact("bank_statement", "statement-bank-a", data, evidence=(proof,),
                          expected_revision=revision, recording_error_confirmed=True,
                          request_id="amend-statement-" + month)
        reconciliation = {"period": "2026-09", "bank_account_id": "bank-a",
                          "statement_id": "statement-bank-a",
                          "matches": [banking.match(item["reference"]) for item in entries]}
        engine.amend_fact("bank_reconciliation", "reconciliation-bank-a", reconciliation,
                          evidence=(proof,), expected_revision=revision,
                          recording_error_confirmed=True,
                          request_id="amend-reconciliation-" + month)
        subjects = ["statement-bank-a", "reconciliation-bank-a"]
        preview = engine.preview(subjects, posting_period=month)
        result = engine.confirm(subjects, posting_period=month, preview_digest=preview["digest"],
                                epochs=preview["epochs"], request_id="review-" + month)
        reviewed = next(item for item in result["results"]
                        if item["subject_id"] == "statement-bank-a")
        assert reviewed["impact"] == "accounting_changed"
        reviewed_ids.add(source(engine)["id"])
        for bank, balance in (("bank-a", 1000), ("zero-bank", 0)):
            subject = f"statement-{bank}-{month}"
            banking.statement(save, publish, [], subject=subject, bank=bank,
                              month=month, initial=balance)
            banking.reconciliation(save, publish, [], subject=f"reconciliation-{bank}-{month}",
                                   statement_id=subject, bank=bank, month=month)
        periods = banking.inventories(engine, proof, month, {"bank"})
        try:
            banking.close_month(periods, proof, month)
        except KernelError as error:
            pytest.fail(str(error.response()))
    banking.inventories(engine, proof, "2026-12", set())
    with Dashboard(engine)._snapshot("2026-12") as snap:
        read = FundsRead(snap)
        exact = {ident for ident, state in read.states.items()
                 if state["subject_id"] == "statement-bank-a"}
        assert exact == reviewed_ids and len(exact) == 3
        assert exact <= read._state_outcomes.keys()
        result = read.account_summary()
        assert result["inflow_fen"] == result["outflow_fen"] == 0
        assert {key for key in read.account_rows if key[0] == "bank"} == {
            ("bank", "bank-a"), ("bank", "zero-bank"),
        }
        assert not exact & snap.reads._verified_source_contents.keys()
    narrow = public(engine, "2026-12")
    with monkeypatch.context() as raw:
        without_carry(raw)
        assert narrow == public(engine, "2026-12")


def test_missing_historical_publication_keeps_original_selector_failure(bank_book, monkeypatch):
    engine, _, _ = prepared(bank_book)
    row = source(engine)
    before = public(engine)
    with Dashboard(engine)._snapshot("2026-10") as snap:
        assert row["id"] in FundsRead(snap).states
    damage(engine, "calculation_publication",
           "DELETE FROM calculation_publication WHERE calculation_id=?", (row["id"],),
           foreign_keys=False)
    original = BusinessQueries._selected_accounting

    def old_selector(queries, *args, **kwargs):
        kwargs.pop("_owner_outcomes", None)
        return original(queries, *args, **kwargs)

    errors, omitted, amounts = [], [], []
    for old in (False, True):
        with monkeypatch.context() as patch:
            if old:
                patch.setattr(BusinessQueries, "_selected_accounting", old_selector)
            try:
                response = Dashboard(engine).funds("2026-10")
            except KernelError as error:
                errors.append(error.code)
            else:
                errors.append(None)
                amounts.append({key: response["data"][key] for key in
                                ("bank_account_count", "total_fen", "bank_opening_fen")})
                with Dashboard(engine)._snapshot("2026-10") as snap:
                    omitted.append(row["id"] not in FundsRead(snap).states)
    assert errors == ["content_integrity_failed", "content_integrity_failed"], {
        "errors": errors, "omitted": omitted, "amounts": amounts,
        "before": {key: before["funds"]["data"][key] for key in
                   ("bank_account_count", "total_fen", "bank_opening_fen")},
    }
