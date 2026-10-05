"""Metadata keeps complete owner authority and leaves money/history independent."""

import json
from contextlib import nullcontext

import pytest
from close_storage_fixture import replace_stored_manifest, stored_manifest
from stage9_metrics import measure_work
from test_asset_batch_reads import prepare_batch_assets
from test_asset_batches import month
from test_asset_owner_outcome_reads import complete_head_events
from test_banking import book as _bank_book
from test_banking import funding
from test_engine import close as close_simple
from test_engine import engine as _simple_engine
from test_engine import evidence as simple_evidence
from test_engine import publish as publish_simple
from test_engine import save as save_simple
from test_integrity_content import damage, verify
from test_payroll_corrections import Company
from test_payroll_corrections import company as _payroll_company

from ai_accounting.kernel.asset_batches import AssetBatches
from ai_accounting.kernel.backup import BackupError, verify_file
from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.maintenance import Maintenance
from ai_accounting.kernel.query_reads import QueryReads


@pytest.fixture
def metadata_company(tmp_path):
    company = Company(tmp_path / "owner-metadata.sqlite")
    prepare_batch_assets(company)
    with company.engine.store.connection(read_only=True) as connection:
        evidence = company.engine.store.current_fact(connection, "computer").evidence[0]
    return company, evidence


def read_heads(
    engine, period, asset_ids=("computer", "chair"), *, complete=False, consumption_only=True,
):
    with Dashboard(engine)._snapshot(period) as snap:
        if complete:
            events = complete_head_events(snap, set(asset_ids))
            return ([event for event in events if event["kind"] == "asset_consumption"]
                    if consumption_only else events)
        return snap.queries._selected_asset_member_heads(
            snap.connection, snap.period, asset_ids=set(asset_ids),
        )


def closed_consumption(company, evidence):
    company.close("2026-02")
    month(company.engine, evidence, "2026-03", "consume-march")
    company.close("2026-03")


@pytest.mark.parametrize("months", [1, 3])
def test_frozen_metadata_preserves_full_events_without_historical_outcome_growth(
    metadata_company, months,
):
    company, evidence = metadata_company
    engine = company.engine
    for number in range(2, months + 2):
        period = f"2026-{number:02}"
        month(engine, evidence, period, f"consume-{number}")
        company.close(period)
    compact_work, compact = measure_work(engine, lambda: read_heads(engine, period))
    full_work, full = measure_work(engine, lambda: read_heads(engine, period, complete=True))
    assert compact == full
    assert {row["asset_id"] for row in compact} == (
        {"computer", "chair"} if months > 1 else set()
    )
    all_events = read_heads(engine, period, complete=True, consumption_only=False)
    assert {row["asset_id"] for row in all_events} == {"computer", "chair"}
    assert compact_work["counters"].get("calculation_result_rows_loaded", 0) == 0
    assert full_work["counters"]["calculation_result_rows_loaded"] > months
    print(json.dumps({"months": months, "metadata": compact_work["counters"],
                      "full": full_work["counters"]}))


@pytest.mark.parametrize("warm", [False, True])
@pytest.mark.parametrize("missing", [
    "calculation", "publication", "subject", "member", "calculation_and_subject",
    "fact", "calculation_seal", "fact_seal",
])
def test_declared_frozen_owner_missing_source_is_never_absence(metadata_company, missing, warm):
    company, evidence = metadata_company
    engine = company.engine
    closed_consumption(company, evidence)
    with engine.store.connection(read_only=True) as connection:
        owner = dict(connection.execute(
            "SELECT * FROM calculation WHERE kind='asset_consumption_month'"
        ).fetchone())
    if missing in {"calculation", "calculation_and_subject"}:
        damage(engine, "calculation", "DELETE FROM calculation WHERE id=?", (owner["id"],),
               foreign_keys=False)
        if missing == "calculation_and_subject":
            damage(engine, "subject", "DELETE FROM subject WHERE id=?", (owner["subject_id"],),
                   foreign_keys=False)
    elif missing == "publication":
        damage(engine, "calculation_publication",
               "DELETE FROM calculation_publication WHERE calculation_id=?", (owner["id"],),
               foreign_keys=False)
    elif missing == "subject":
        damage(engine, "subject", "DELETE FROM subject WHERE id=?", (owner["subject_id"],),
               foreign_keys=False)
    elif missing == "member":
        damage(engine, "asset_batch_member",
               "DELETE FROM asset_batch_member WHERE owner_calculation_id=? AND position=2",
               (owner["id"],), foreign_keys=False)
    else:
        table, key, ident = {
            "fact": ("fact_revision", "id", owner["fact_id"]),
            "calculation_seal": ("calculation_seal", "calculation_id", owner["id"]),
            "fact_seal": ("fact_seal", "fact_id", owner["fact_id"]),
        }[missing]
        damage(engine, table, f"DELETE FROM {table} WHERE {key}=?", (ident,), foreign_keys=False)
    with Dashboard(engine)._snapshot("2026-03") as snap:
        if warm:
            rows = snap.reads.authoritative_close_rows(periods=(snap.month,))
            snap.reads.close_section(rows[0], "asset_batch_adoptions")
        with pytest.raises(KernelError):
            snap.queries._selected_asset_member_heads(
                snap.connection, snap.period, asset_ids={"computer"},
            )
        assert not snap.reads._asset_members
    with pytest.raises(KernelError):
        read_heads(engine, "2026-03", complete=True)
    with pytest.raises(KernelError):
        Dashboard(engine).assets("2026-03", preparation="deferred")
    with pytest.raises(KernelError):
        verify(engine)


def test_metadata_does_not_claim_unread_frozen_owner_body_proof(metadata_company):
    company, evidence = metadata_company
    engine = company.engine
    closed_consumption(company, evidence)
    with engine.store.connection(read_only=True) as connection:
        owner = connection.execute(
            "SELECT id,outcome FROM calculation WHERE kind='asset_consumption_month'"
        ).fetchone()
    body = json.loads(owner["outcome"])
    body["unused_historical_field"] = True
    damage(engine, "calculation", "UPDATE calculation SET outcome=? WHERE id=?",
           (json.dumps(body), owner["id"]))
    with Dashboard(engine)._snapshot("2026-03") as snap:
        events = snap.queries._selected_asset_member_heads(
            snap.connection, snap.period, asset_ids={"computer"},
        )
        assert any(event["owner_calculation_id"] == owner["id"] for event in events)
        assert owner["id"] not in snap.reads._verified_sql_outcomes
        assert owner["id"] not in snap.reads._verified_source_contents
        assert not snap.reads._asset_members
    with pytest.raises(KernelError):
        read_heads(engine, "2026-03", complete=True)
    with pytest.raises(KernelError):
        verify(engine)
    with pytest.raises(BackupError):
        verify_file(engine.store.path)


@pytest.mark.parametrize("warm", [False, True])
@pytest.mark.parametrize("missing", ["fact", "subject", "calculation_seal", "fact_seal"])
def test_frozen_member_headers_require_real_sources_even_for_other_asset(
    metadata_company, missing, warm,
):
    company, evidence = metadata_company
    engine = company.engine
    closed_consumption(company, evidence)
    with engine.store.connection(read_only=True) as connection:
        member = dict(connection.execute(
            "SELECT m.* FROM asset_batch_member m JOIN calculation c "
            "ON c.id=m.owner_calculation_id WHERE c.kind='asset_consumption_month' "
            "AND m.asset_id='chair'"
        ).fetchone())
    table, key, ident = {
        "fact": ("fact_revision", "id", member["member_fact_id"]),
        "subject": ("subject", "id", member["member_subject_id"]),
        "calculation_seal": ("calculation_seal", "calculation_id", member["member_calculation_id"]),
        "fact_seal": ("fact_seal", "fact_id", member["member_fact_id"]),
    }[missing]
    damage(engine, table, f"DELETE FROM {table} WHERE {key}=?", (ident,), foreign_keys=False)
    with Dashboard(engine)._snapshot("2026-03") as snap:
        if warm:
            rows = snap.reads.authoritative_close_rows(periods=(snap.month,))
            snap.reads.close_section(rows[0], "asset_batch_adoptions")
        before = set(snap.reads._verified_sql_outcomes)
        with pytest.raises(KernelError):
            snap.queries._selected_asset_member_heads(
                snap.connection, snap.period, asset_ids={"computer"},
            )
        assert snap.reads._verified_sql_outcomes == before
        assert not snap.reads._asset_members
    with pytest.raises(KernelError):
        verify(engine)


@pytest.mark.parametrize("missing", ["current", "publication"])
def test_open_zero_owner_is_located_without_member_or_current_index(metadata_company, missing):
    company, evidence = metadata_company
    engine = company.engine
    month(engine, evidence, "2026-02", "consume-zero")
    events = read_heads(engine, "2026-02")
    assert events == read_heads(engine, "2026-02", complete=True)
    assert not any(event["kind"] == "asset_consumption" for event in events)
    with engine.store.connection(read_only=True) as connection:
        owner = dict(connection.execute(
            "SELECT * FROM calculation WHERE kind='asset_consumption_month'"
        ).fetchone())
    assert json.loads(owner["outcome"])["lines"] == []
    if missing == "current":
        damage(engine, "calculation_current",
               "DELETE FROM calculation_current WHERE calculation_id=?", (owner["id"],),
               foreign_keys=False)
    elif missing == "publication":
        damage(engine, "calculation_publication",
               "DELETE FROM calculation_publication WHERE calculation_id=?", (owner["id"],),
               foreign_keys=False)
    with Dashboard(engine)._snapshot("2026-02") as snap:
        with pytest.raises(KernelError):
            snap.queries._selected_asset_member_heads(
                snap.connection, snap.period, asset_ids={"computer"},
            )
        assert not snap.reads._asset_members
    with pytest.raises(KernelError):
        verify(engine)


@pytest.mark.parametrize("bad", ["digest", "empty", "duplicate"])
def test_bad_frozen_directory_tail_publishes_no_member_or_body_proof(metadata_company, bad):
    company, evidence = metadata_company
    engine = company.engine
    closed_consumption(company, evidence)
    with engine.store.connection(read_only=True) as connection:
        manifest = stored_manifest(connection, "2026-03")
    assert len(manifest["asset_batch_adoptions"]) == 1
    if bad == "digest":
        manifest["asset_batch_adoptions"][-1]["membership_digest"] = "0" * 64
    elif bad == "empty":
        manifest["asset_batch_adoptions"] = []
    else:
        manifest["asset_batch_adoptions"].append(dict(manifest["asset_batch_adoptions"][0]))
    replace_stored_manifest(engine, manifest)
    with Dashboard(engine)._snapshot("2026-03") as snap:
        with pytest.raises(KernelError):
            snap.queries._selected_asset_member_heads(
                snap.connection, snap.period, asset_ids={"computer"},
            )
        assert not snap.reads._asset_members
        assert not snap.reads._verified_sql_outcomes
        assert not snap.reads._verified_source_contents
    with pytest.raises(KernelError):
        verify(engine, include_indexes=False)


@pytest.mark.parametrize("warm", [False, True])
def test_open_member_directory_cannot_hide_a_positive_owner(metadata_company, warm):
    company, evidence = metadata_company
    engine = company.engine
    company.close("2026-02")
    month(engine, evidence, "2026-03", "consume-positive")
    expected = read_heads(engine, "2026-03")
    assert any(event["kind"] == "asset_consumption" for event in expected)
    with engine.store.connection(read_only=True) as connection:
        owner = connection.execute(
            "SELECT id FROM calculation WHERE kind='asset_consumption_month'"
        ).fetchone()[0]
    damage(engine, "asset_batch_member",
           "DELETE FROM asset_batch_member WHERE owner_calculation_id=?", (owner,),
           foreign_keys=False)
    with Dashboard(engine)._snapshot("2026-03") as snap:
        if warm:
            rows = snap.reads.authoritative_close_rows(
                periods=(
                    row[0] for row in snap.connection.execute("SELECT period FROM period_close")
                )
            )
            snap.reads.close_section(rows[0], "asset_batch_adoptions")
        with pytest.raises(KernelError):
            snap.queries._selected_asset_member_heads(
                snap.connection, snap.period, asset_ids={"computer"},
            )
        assert not snap.reads._asset_members
    with pytest.raises(KernelError):
        read_heads(engine, "2026-03", complete=True)
    with pytest.raises(KernelError):
        verify(engine)


@pytest.mark.parametrize("mode", ["withdraw", "closed_correction"])
def test_real_activation_changes_keep_metadata_events_and_frozen_identity(metadata_company, mode):
    company, evidence = metadata_company
    engine = company.engine
    dashboard = Dashboard(engine)
    before = dashboard.assets("2026-02", preparation="deferred")["data"]["collections"]["assets"]
    if mode == "closed_correction":
        company.close("2026-02")
    with engine.store.connection(read_only=True) as connection:
        members = [
            {"subject_id": subject, "expected_revision": 1,
             "data": engine.store.current_fact(connection, subject).fact.model_dump(mode="json")
             | {"benefit_area": "sales"}}
            for subject in ("activate-computer", "activate-chair")
        ]
    options = dict(
        subject_id="activation-batch", period="2026-02",
        members=members[:1] if mode == "withdraw" else members,
        evidence=(evidence,), expected_revision=1,
        **({"posting_period": "2026-03"} if mode == "closed_correction" else {}),
    )
    batches = AssetBatches(engine)
    preview = batches.prepare_activation_batch(**options)
    batches.confirm_activation_batch(
        **options, preview_digest=preview["digest"], epochs=preview["epochs"], request_id=mode,
    )
    period = "2026-02" if mode == "withdraw" else "2026-03"
    if mode == "closed_correction":
        month(engine, evidence, period, "consume-corrected")
        after = dashboard.assets("2026-02", preparation="deferred")["data"]["collections"]["assets"]
        assert before["items"] == after["items"]
    events = read_heads(engine, period)
    assert events == read_heads(engine, period, complete=True)
    if mode == "withdraw":
        assert events == []
        all_events = read_heads(engine, period, complete=True, consumption_only=False)
        assert {event["asset_id"] for event in all_events} == {"computer"}
        items = dashboard.assets(period, preparation="deferred")["data"]["collections"]["assets"][
            "items"
        ]
        assert {item["asset_id"]: item["status"] for item in items} == {
            "computer": "active", "chair": "pending_activation",
        }
    assert verify(engine)["status"] == "verified"


@pytest.mark.parametrize("reader", ["unowned", "fixed_v1"])
def test_non_native_metadata_keeps_actual_complete_body_scope(metadata_company, reader):
    company, evidence = metadata_company
    engine = company.engine
    closed_consumption(company, evidence)
    expected = read_heads(engine, "2026-03", complete=True, consumption_only=False)

    def selected():
        if reader == "unowned":
            with engine.store.connection(read_only=True) as connection:
                connection.execute("BEGIN")
                return BusinessQueries(engine)._selected_asset_member_heads(
                    connection, "2026-03", asset_ids={"computer", "chair"},
                )
        with historical_content(1):
            return read_heads(engine, "2026-03")

    work, actual = measure_work(engine, selected)
    assert actual == expected
    assert work["counters"]["calculation_result_rows_loaded"] > 0
    with engine.store.connection(read_only=True) as connection:
        owner = connection.execute(
            "SELECT id,outcome FROM calculation WHERE kind='asset_consumption_month'"
        ).fetchone()
    malformed = '{"values":{},' + owner["outcome"][1:]
    damage(engine, "calculation", "UPDATE calculation SET outcome=? WHERE id=?",
           (malformed, owner["id"]))
    with historical_content(1) if reader == "fixed_v1" else nullcontext():
        with pytest.raises(KernelError):
            selected()


@pytest.mark.parametrize("consumer", ["metadata", "history", "page", "member_publication"])
def test_missing_open_owner_voucher_pointer_is_not_empty_history(metadata_company, consumer):
    company, evidence = metadata_company
    engine = company.engine
    company.close("2026-02")
    month(engine, evidence, "2026-03", "consume-positive")
    with engine.store.connection(read_only=True) as connection:
        owner = connection.execute(
            "SELECT p.voucher_id,c.outcome FROM calculation c JOIN calculation_publication p "
            "ON p.calculation_id=c.id WHERE c.kind='asset_consumption_month'"
        ).fetchone()
    assert json.loads(owner["outcome"])["lines"]
    assert owner["voucher_id"] is not None
    damage(engine, "voucher_current", "DELETE FROM voucher_current WHERE voucher_id=?",
           (owner["voucher_id"],), foreign_keys=False)
    with pytest.raises(KernelError):
        if consumer == "page":
            Dashboard(engine).assets("2026-03", preparation="deferred")
        elif consumer == "member_publication":
            with QueryReads.snapshot(engine) as reads:
                member = reads.connection.execute(
                    "SELECT m.member_subject_id FROM asset_batch_member m JOIN calculation c "
                    "ON c.id=m.owner_calculation_id WHERE c.kind='asset_consumption_month' "
                    "ORDER BY m.position LIMIT 1",
                ).fetchone()[0]
                BusinessQueries(engine, reads=reads)._current_publication(reads.connection, member)
        else:
            read_heads(engine, "2026-03", complete=consumer == "history")
    with pytest.raises(KernelError):
        verify(engine)


@pytest.mark.parametrize("consumer", ["accounting", "current_publication"])
@pytest.mark.parametrize("domain", ["bank", "payroll", "ordinary"])
def test_missing_current_voucher_pointer_shared_business_consumers(tmp_path, domain, consumer):
    if domain == "bank":
        engine, save, publish, _ = _bank_book.__wrapped__(tmp_path)
        funding(save, publish)
        subject, period = "funding", "2026-09"
    elif domain == "payroll":
        company = _payroll_company.__wrapped__(tmp_path)
        company.publish("january")
        engine, subject, period = company.engine, "january", "2026-01"
    else:
        engine = _simple_engine.__wrapped__(tmp_path)
        save_simple(engine)
        publish_simple(engine)
        subject, period = "charge", "2026-01"

    def consume():
        with QueryReads.snapshot(engine) as reads:
            queries = BusinessQueries(engine, reads=reads)
            if consumer == "current_publication":
                return queries._current_publication(reads.connection, subject)
            return queries._selected_accounting(reads.connection, subject, period)

    baseline = consume()
    if consumer == "accounting":
        assert baseline["through_period"]["voucher_events"]
    else:
        assert baseline["current_voucher_version_id"] is not None
    pages = {}
    if consumer == "accounting" and domain != "ordinary":
        dashboard = Dashboard(engine)
        pages = {
            "funds": lambda: dashboard.funds(period),
            "brief": lambda: dashboard.brief(period),
            "quarterly_report": lambda: dashboard.quarterly_report(
                int(period[:4]), (int(period[5:]) - 1) // 3 + 1, preparation="deferred",
            ),
        }
        for page, read in pages.items():
            assert isinstance(read(), dict), page
    with engine.store.connection(read_only=True) as connection:
        voucher = connection.execute(
            "SELECT p.voucher_id FROM calculation_current a JOIN calculation_publication p "
            "ON p.calculation_id=a.calculation_id WHERE a.subject_id=?", (subject,),
        ).fetchone()[0]
    damage(engine, "voucher_current", "DELETE FROM voucher_current WHERE voucher_id=?",
           (voucher,), foreign_keys=False)
    for page, read in pages.items():
        with pytest.raises(KernelError) as failure:
            read()
        assert failure.value.code == "content_integrity_failed", page
    with pytest.raises(KernelError):
        consume()


@pytest.mark.parametrize("mode", ["cleared", "no_impact", "two_reviews", "future", "closed_review"])
def test_current_voucher_guard_preserves_legal_posting_states(tmp_path, mode):
    engine = _simple_engine.__wrapped__(tmp_path)
    save_simple(engine)
    publish_simple(engine)
    with engine.store.connection(read_only=True) as connection:
        original_version = connection.execute(
            "SELECT version_id FROM voucher_current",
        ).fetchone()[0]
    if mode == "future":
        save_simple(engine, subject="future", period="2026-02", request="future-fact")
        publish_simple(engine, ["future"], request="future-publication")
        with engine.store.connection(read_only=True) as connection:
            future_voucher = connection.execute(
                "SELECT voucher_id FROM calculation_publication WHERE subject_id='future'",
            ).fetchone()[0]
        damage(engine, "voucher_current", "DELETE FROM voucher_current WHERE voucher_id=?",
               (future_voucher,), foreign_keys=False)
    elif mode == "cleared":
        engine.amend_fact(
            "test_charge", "charge",
            {"period": "2026-01", "amount": 100, "suppress_posting": True},
            evidence=(simple_evidence(engine),), expected_revision=1,
            request_id="clear-charge", recording_error_confirmed=True,
        )
        publish_simple(engine, request="clear-publication")
        with engine.store.connection(read_only=True) as connection:
            assert connection.execute("SELECT count(*) FROM voucher_current").fetchone()[0] == 0
            assert connection.execute("SELECT voucher_id FROM calculation_publication "
                                      "ORDER BY sequence DESC LIMIT 1").fetchone()[0] is not None
    elif mode == "two_reviews":
        save_simple(engine, amount=150, revision=1, request="replace-fact")
        publish_simple(engine, request="replace-publication")
        with engine.store.connection(read_only=True) as connection:
            original_version = connection.execute(
                "SELECT version_id FROM voucher_current",
            ).fetchone()[0]
        for revision in (2, 3):
            save_simple(engine, amount=150, revision=revision, request=f"review-fact-{revision}")
            preview, _ = publish_simple(engine, request=f"review-publication-{revision}")
            assert preview["results"][0]["impact"] == "review_no_impact"
    else:
        if mode == "closed_review":
            close_simple(engine)
        save_simple(engine, revision=1, request="review-fact")
        preview, _ = publish_simple(engine, request="review-publication")
        assert preview["results"][0]["impact"] == "review_no_impact"
    with QueryReads.snapshot(engine) as reads:
        queries = BusinessQueries(engine, reads=reads)
        selected = queries._selected_accounting(reads.connection, {"charge", "future"}, "2026-01")
        events = selected["through_period"]["voucher_events"]
        states = selected["through_period"]["state_results"]
        if mode == "cleared":
            assert events == []
            assert len(states) == 1
        else:
            assert len(events) == 1
            assert events[0]["voucher_version_id"] == original_version
        if mode != "future":
            current = queries._current_publication(reads.connection, "charge")
            assert current["current_voucher_version_id"] == (
                None if mode == "cleared" else original_version
            )


def test_missing_open_reversal_pointer_is_rejected(tmp_path):
    engine = _simple_engine.__wrapped__(tmp_path)
    save_simple(engine)
    publish_simple(engine)
    close_simple(engine)
    save_simple(engine, amount=150, revision=1, request="correct-fact")
    publish_simple(engine, request="correct-publication", posting_period="2026-02")

    def read():
        with QueryReads.snapshot(engine) as reads:
            return BusinessQueries(engine, reads=reads)._selected_accounting(
                reads.connection, None, "2026-02",
            )

    before = read()["through_period"]["voucher_events"]
    reversals = [event for event in before if event["direction"] == -1]
    assert len(reversals) == 1
    reversal = reversals[0]
    damage(engine, "voucher_current", "DELETE FROM voucher_current WHERE version_id=?",
           (reversal["voucher_version_id"],), foreign_keys=False)
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT id FROM voucher_version WHERE id=?",
                                  (reversal["voucher_version_id"],)).fetchone() is not None
    try:
        after = read()["through_period"]["voucher_events"]
    except KernelError as failure:
        result = {"error": failure.code}
    else:
        result = {
            "returned_success": True,
            "before_directions": [event["direction"] for event in before],
            "after_directions": [event["direction"] for event in after],
        }
    print(json.dumps({"missing_open_reversal_pointer": result}))
    assert result == {"error": "content_integrity_failed"}


@pytest.mark.parametrize("review_count", [0, 2])
def test_current_normal_voucher_cannot_point_back_before_replacement(tmp_path, review_count):
    engine = _simple_engine.__wrapped__(tmp_path)
    save_simple(engine)
    publish_simple(engine)
    with engine.store.connection(read_only=True) as connection:
        original = connection.execute(
            "SELECT voucher_id,version_id FROM voucher_current",
        ).fetchone()
    save_simple(engine, amount=150, revision=1, request="replace-fact")
    publish_simple(engine, request="replace-publication")
    for revision in range(2, review_count + 2):
        save_simple(engine, amount=150, revision=revision, request=f"review-fact-{revision}")
        preview, _ = publish_simple(engine, request=f"review-publication-{revision}")
        assert preview["results"][0]["impact"] == "review_no_impact"
    with QueryReads.snapshot(engine) as reads:
        before = BusinessQueries(engine, reads=reads)._selected_accounting(
            reads.connection, "charge", "2026-01",
        )["through_period"]["voucher_events"]
        assert len(before) == 1
        assert before[0]["voucher_version_id"] != original["version_id"]
        assert sum(line["debit"] for line in before[0]["lines"]) == 150
    damage(engine, "voucher_current", "UPDATE voucher_current SET version_id=? WHERE voucher_id=?",
           (original["version_id"], original["voucher_id"]))
    with pytest.raises(KernelError) as failure:
        BusinessQueries(engine).business_status("charge", "2026-01", as_of="2026-02-01")
    assert failure.value.code == "content_integrity_failed"


def pointer_source(engine, role):
    save_simple(engine)
    publish_simple(engine)
    if role == "reverse":
        close_simple(engine)
        save_simple(engine, amount=150, revision=1, request="correct-fact")
        publish_simple(engine, request="correct-publication", posting_period="2026-02")
    with engine.store.connection(read_only=True) as connection:
        return connection.execute(
            "SELECT v.id FROM voucher_current h JOIN voucher_version v ON v.id=h.version_id "
            "WHERE v.reverses_id IS " + ("NOT NULL" if role == "reverse" else "NULL")
            + " ORDER BY v.period DESC LIMIT 1",
        ).fetchone()[0]


def database_snapshot(engine):
    with engine.store.connection(read_only=True) as connection:
        return (connection.execute("SELECT read_repair_revision FROM state").fetchone()[0],
                tuple(connection.iterdump()))


@pytest.mark.parametrize("target", ["projections", "read_indexes"])
@pytest.mark.parametrize("role", ["normal", "reverse"])
def test_repair_rejects_missing_current_source_head_without_mutation(tmp_path, target, role):
    engine = _simple_engine.__wrapped__(tmp_path)
    version = pointer_source(engine, role)
    damage(engine, "voucher_current", "DELETE FROM voucher_current WHERE version_id=?",
           (version,), foreign_keys=False)
    before = database_snapshot(engine)
    maintenance = Maintenance(engine)
    operation = (maintenance.rebuild_projections if target == "projections"
                 else maintenance.repair_read_indexes)
    with pytest.raises(KernelError) as failure:
        operation(request_id="reject-damaged-authority")
    assert failure.value.code == "content_integrity_failed"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("role", ["normal", "reverse"])
def test_registered_backup_verifier_rejects_missing_current_source_head(tmp_path, role):
    engine = _simple_engine.__wrapped__(tmp_path)
    version = pointer_source(engine, role)
    baseline = verify_file(engine.store.path, _bundle=engine.store.bundle)
    assert baseline["verification"]["status"] == "verified"
    damage(engine, "voucher_current", "DELETE FROM voucher_current WHERE version_id=?",
           (version,), foreign_keys=False)
    before = database_snapshot(engine)
    with pytest.raises(BackupError) as failure:
        verify_file(engine.store.path, _bundle=engine.store.bundle)
    assert failure.value.code == "backup_content_invalid"
    assert "content_integrity_failed" in str(failure.value)
    assert database_snapshot(engine) == before
