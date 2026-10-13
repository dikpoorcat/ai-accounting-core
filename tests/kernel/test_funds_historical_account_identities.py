"""Historical zero-account identity does not consume statement/match children."""

import json

import pytest
import test_banking as banking
import test_investments as investments
from stage9_metrics import measure_work
from test_dashboard_model_transactions import raw_read
from test_integrity_content import damage

from ai_accounting.kernel import engine as engine_module
from ai_accounting.kernel.backup import BackupError, backup_to_file
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_async import _produce
from ai_accounting.kernel.dashboard_funds import FundsRead, funds
from ai_accounting.kernel.entities import Entities
from ai_accounting.kernel.integrity import verify_integrity
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.types import YearMonth, canonical, digest

bank_book = banking.book


@pytest.fixture
def investment_book(tmp_path):
    from opening_fixture import adopt_empty_opening

    book = investments.book.__wrapped__(tmp_path)
    # This scenario's first formally recorded subscriptions belong to December.
    adopt_empty_opening(book[0], period="2025-12")
    return book


def prepared(book, *, closed=True):
    engine, save, publish, proof = book
    for bank, amount in (("bank-a", 1000), ("zero-bank", 0)):
        banking.opening(save, publish, bank=bank)
        if amount:
            banking.funding(save, publish, subject="funding", bank=bank, amount=amount)
        entries = (
            [banking.entry("first", amount=400), banking.entry("second", amount=600)]
            if amount
            else []
        )
        banking.statement(save, publish, entries, subject="statement-" + bank, bank=bank)
        banking.reconciliation(
            save,
            publish,
            [banking.match(e["reference"]) for e in entries],
            subject="reconciliation-" + bank,
            statement_id="statement-" + bank,
            bank=bank,
        )
    if closed:
        banking.close_month(
            banking.inventories(engine, proof, "2026-09", {"bank", "transactions"}),
            proof,
            "2026-09",
        )
    # Establish an actual open next month without inventing a money event.
    banking.inventories(engine, proof, "2026-10", set())
    return engine, save, publish


def public(engine, period="2026-10", *, default_model=False):
    result = {}
    for name in ("funds", "brief"):
        options = (
            {"movement_account_selection": "first"} if name == "funds" and default_model else {}
        )
        response = getattr(Dashboard(engine), name)(period, **options)
        response.pop("generated_at", None)
        response["data"].pop("generated_at", None)
        result[name] = response
    return result


def without_snapshot_versions(response):
    # Async publication IDs and the independent native snapshot digest are
    # different contracts. All business fields and other metadata must agree.
    return {name: {key: value for key, value in page.items() if key != "snapshot_version"}
            for name, page in response.items()}


def assert_bank_identity_totals(response):
    data = response["funds"]["data"]
    assert data["bank_account_count"] == 2
    assert data["total_fen"] == 1000
    assert {
        item["account_id"]: (item["type"], item["closing_fen"])
        for item in data["collections"]["accounts"]["items"]
    } == {"bank-a": ("bank", 1000), "zero-bank": ("bank", 0)}
    assert data["bank_statement"]["missing_account_count"] == 2
    assert response["brief"]["data"]["funds_overview"]["bank_fen"] == 1000


def source(engine, subject="statement-bank-a"):
    with engine.store.connection(read_only=True) as connection:
        return dict(
            connection.execute(
                "SELECT c.* FROM calculation_current h JOIN calculation c ON c.id=h.calculation_id "
                "WHERE h.subject_id=?",
                (subject,),
            ).fetchone()
        )


def force_complete(monkeypatch):
    original = QueryReads.frozen_bank_account_identities

    def complete(reads, frozen_periods, **kwargs):
        reads.verify_selected_content(frozen_periods)
        return original(reads, frozen_periods, **kwargs)

    monkeypatch.setattr(QueryReads, "frozen_bank_account_identities", complete)


def test_public_historical_zero_accounts_match_complete_scope_with_less_source_payload(
    bank_book,
    monkeypatch,
):
    engine, _, _ = prepared(bank_book)
    normal = public(engine)
    # Measure the native identity consumer and its complete-source comparison;
    # the certified model deliberately does not expand these history bodies.
    with raw_read():
        narrow_work, narrow = measure_work(engine, lambda: public(engine))
    assert normal == narrow
    assert_bank_identity_totals(narrow)
    with raw_read(), monkeypatch.context() as old:
        force_complete(old)
        complete_work, complete = measure_work(engine, lambda: public(engine))
    assert narrow == complete
    assert (
        narrow_work["counters"]["returned_value_bytes"]
        < complete_work["counters"]["returned_value_bytes"]
    )
    assert (
        narrow_work["counters"]["stdlib_json_input_bytes"]
        < complete_work["counters"]["stdlib_json_input_bytes"]
    )


@pytest.mark.parametrize(
    "change",
    [
        "calculation_seal",
        "fact_seal",
        "kind",
        "fact",
        "publication",
        "adopted",
        "digest",
        "body_and_digest",
        "scalar",
        "scalar_period",
        "entity_type",
        "duplicate",
    ],
)
def test_public_historical_identity_rejects_consumed_damage(bank_book, change):
    engine, _, _ = prepared(bank_book)
    # Current v6 certifies the UI's first-account default. The unrestricted
    # funds variant remains a native read; queueing alone is not a ready model.
    produced = _produce(engine, "2026-10")
    assert produced["status"] == "ready"
    before = public(engine, default_model=True)
    assert all(page["snapshot_version"] == produced["model_version"] for page in before.values())
    assert_bank_identity_totals(before)
    with raw_read():
        assert without_snapshot_versions(public(engine, default_model=True)) == (
            without_snapshot_versions(before)
        )
    row = source(engine)
    if change in {"calculation_seal", "fact_seal"}:
        key, ident = (
            ("calculation_id", row["id"])
            if change == "calculation_seal"
            else ("fact_id", row["fact_id"])
        )
        damage(engine, change, f"DELETE FROM {change} WHERE {key}=?", (ident,), foreign_keys=False)
    elif change == "kind":
        damage(
            engine, "calculation", "UPDATE calculation SET kind='expense' WHERE id=?", (row["id"],)
        )
    elif change == "fact":
        other = source(engine, "statement-zero-bank")
        damage(
            engine,
            "calculation",
            "UPDATE calculation SET fact_id=? WHERE id=?",
            (other["fact_id"], row["id"]),
        )
    elif change == "publication":
        damage(
            engine,
            "calculation_publication",
            "DELETE FROM calculation_publication WHERE calculation_id=?",
            (row["id"],),
            foreign_keys=False,
        )
    elif change == "adopted":
        damage(
            engine,
            "close_storage_block",
            "DELETE FROM close_storage_block WHERE field='adopted_results'",
        )
    elif change == "scalar":
        damage(
            engine,
            "fact_bank_statement",
            "UPDATE fact_bank_statement SET bank_account_id='zero-bank' WHERE revision_id=?",
            (row["fact_id"],),
        )
    elif change == "scalar_period":
        damage(
            engine,
            "fact_bank_statement",
            "UPDATE fact_bank_statement SET period=period+1 WHERE revision_id=?",
            (row["fact_id"],),
        )
    elif change == "entity_type":
        damage(engine, "entity", "UPDATE entity SET account_type='cash' WHERE id='bank-a'")
    else:
        outcome = json.loads(row["outcome"])
        if change == "duplicate":
            raw = row["outcome"].replace(
                '"bank_account_id":"bank-a"',
                '"bank_account_id":"bank-a","bank_account_id":"zero-bank"',
            )
            saved_digest = row["digest"]
        elif change == "digest":
            raw, saved_digest = row["outcome"], b"x" * 32
        else:
            outcome["values"]["bank_account_id"] = "zero-bank"
            raw, saved_digest = canonical(outcome), digest(outcome)
        damage(
            engine,
            "calculation",
            "UPDATE calculation SET outcome=?,digest=? WHERE id=?",
            (raw, saved_digest, row["id"]),
        )
    # Next-month account cards are certified aggregates, not historical raw
    # statement hits. Preserve their original money, kind and account identity.
    # Native selection independently authenticates these physical witnesses.
    for _ in range(2):
        after = public(engine, default_model=True)
        assert after == before
        assert_bank_identity_totals(after)
        with raw_read(), pytest.raises(KernelError) as failure:
            Dashboard(engine).funds("2026-10", movement_account_selection="first")
        assert failure.value.code == "content_integrity_failed"
    with engine.store.connection(read_only=True) as connection, pytest.raises(KernelError):
        verify_integrity(engine, connection)


@pytest.mark.parametrize("kind", ["bank_statement", "bank_reconciliation"])
def test_historical_child_is_outside_identity_scope_but_complete_paths_reject(
    bank_book,
    tmp_path,
    kind,
):
    engine, _, _ = prepared(bank_book)
    before = public(engine)
    subject = "statement-bank-a" if kind == "bank_statement" else "reconciliation-bank-a"
    row = source(engine, subject)
    table, field, value = (
        ("fact_bank_statement_entries", "reference", "different-original-row")
        if kind == "bank_statement"
        else ("fact_bank_reconciliation_matches", "source_id", "different-source")
    )
    damage(
        engine,
        table,
        f"UPDATE {table} SET {field}=? WHERE revision_id=? AND item_no=0",
        (value, row["fact_id"]),
    )
    assert public(engine) == before
    with QueryReads.snapshot(engine) as reads:
        found = reads.frozen_bank_account_identities(
            {row["id"]: YearMonth("2026-09").ordinal},
            through_period=YearMonth("2026-10").ordinal,
        )
        assert found == {row["id"]: "bank-a"}
        assert row["id"] not in reads._verified_source_contents
        with pytest.raises(KernelError):
            reads.verify_selected_content({row["id"]})
    with engine.store.connection(read_only=True) as connection, pytest.raises(KernelError):
        verify_integrity(engine, connection)
    with pytest.raises(KernelError):
        engine.rebuild_projections(request_id="reject-historical-source-repair")
    with pytest.raises(BackupError) as rejected_backup:
        backup_to_file(engine.store.path, tmp_path / "damaged.finance-company.zip")
    assert rejected_backup.value.code == "backup_content_invalid"
    # Selecting its own month still consumes the original complete statement.
    with pytest.raises(KernelError):
        Dashboard(engine).funds("2026-09")


def test_historical_identity_accepts_unique_noncanonical_result_and_late_review(
    bank_book, monkeypatch
):
    engine, _, publish = prepared(bank_book)
    before = public(engine)
    row = source(engine)
    raw = json.dumps(json.loads(row["outcome"]), ensure_ascii=False, indent=2)
    damage(engine, "calculation", "UPDATE calculation SET outcome=? WHERE id=?", (raw, row["id"]))
    assert public(engine) == before
    with monkeypatch.context() as version:
        version.setattr(engine_module, "PROGRAM_VERSION", "synthetic-bank-late-review")
        result = publish("statement-bank-a")
    reviewed = next(item for item in result["results"] if item["subject_id"] == "statement-bank-a")
    assert reviewed["impact"] == "review_no_impact"
    after = public(engine)
    for name in ("brief", "funds"):
        assert after[name]["snapshot_version"] != before[name]["snapshot_version"]
        assert after[name]["data"] == before[name]["data"]


def test_fixed_v1_keeps_complete_historical_source_reader(bank_book, monkeypatch):
    engine, _, _ = prepared(bank_book)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("v1 must not use current historical identity semantics")

    monkeypatch.setattr(QueryReads, "frozen_bank_account_identities", forbidden)
    with historical_content(1):
        assert Dashboard(engine).funds("2026-10")["data"]["bank_account_count"] == 2


def test_open_historical_state_still_authenticates_original_rows(bank_book):
    engine, _, _ = prepared(bank_book, closed=False)
    produced = _produce(engine, "2026-10")
    assert produced["status"] == "ready"
    before = public(engine, default_model=True)
    assert all(page["snapshot_version"] == produced["model_version"] for page in before.values())
    assert_bank_identity_totals(before)
    with raw_read():
        assert without_snapshot_versions(public(engine, default_model=True)) == (
            without_snapshot_versions(before)
        )
    row = source(engine)
    damage(
        engine,
        "fact_bank_statement_entries",
        "UPDATE fact_bank_statement_entries SET reference='damaged' WHERE revision_id=?",
        (row["fact_id"],),
    )
    for _ in range(2):
        assert public(engine, default_model=True) == before
        with raw_read(), pytest.raises(KernelError) as failure:
            Dashboard(engine).funds("2026-10", movement_account_selection="first")
        assert failure.value.code == "content_integrity_failed"
    with engine.store.connection(read_only=True) as connection, pytest.raises(KernelError):
        verify_integrity(engine, connection)


def test_inactive_registered_account_remains_a_valid_historical_identity(bank_book):
    engine, _, _ = prepared(bank_book)
    with engine.store.connection(read_only=True) as connection:
        revision = connection.execute(
            "SELECT max(revision) FROM entity_profile_revision WHERE entity_id='zero-bank'"
        ).fetchone()[0]
    Entities(engine).update_entity_profile(
        "zero-bank",
        {"active": False},
        source="synthetic owner inactivity confirmation",
        expected_revision=revision,
        request_id="inactive-zero-account",
    )
    assert Dashboard(engine).funds("2026-10")["data"]["bank_account_count"] == 2


def test_brief_funds_summary_does_not_expand_unconsumed_historical_investment_products(
    investment_book,
    monkeypatch,
):
    engine, save, publish = investment_book
    for index in range(35):
        save(
            "money_fund_subscription",
            f"old-buy-{index}",
            investments.subscription(period="2025-12", fund_id=f"historical-product-{index}"),
        )
    publish(*(f"old-buy-{index}" for index in range(35)))
    save(
        "cash_funding",
        "current-money",
        {
            "period": "2026-01",
            "owner_id": "owner",
            "funding_kind": "capital",
            "amount_fen": 1000,
            "actual_date": "2026-01-02",
            "cash_account_id": "cash",
        },
    )
    publish("current-money")

    def forbidden(*_a, **_k):
        raise AssertionError("brief does not consume investment product summaries")

    def projected(*, add_unused=False):
        with Dashboard(engine)._snapshot("2026-01") as snap:
            summary = funds(snap, summary_only=True)
            assert "investments" not in summary
            if add_unused:
                unused = FundsRead(snap).investment_summary()
                assert unused["closing_cost_fen"] == 35 * 10100
            return summary

    extra_work, before = measure_work(engine, lambda: projected(add_unused=True))
    with monkeypatch.context() as narrow:
        narrow.setattr(FundsRead, "has_investment_sources", forbidden)
        narrow.setattr(FundsRead, "investment_summary", forbidden)
        narrow_work, after = measure_work(engine, projected)
        assert Dashboard(engine).brief("2026-01")["data"]["funds_overview"]["cash_fen"] == 1000
    assert before == after
    for counter in ("returned_rows", "returned_value_bytes", "stdlib_json_input_bytes"):
        assert narrow_work["counters"][counter] < extra_work["counters"][counter]
