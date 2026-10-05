"""Complete frozen owner authority bounds reads without hiding damaged sources."""

import json

import pytest
from close_storage_fixture import replace_stored_manifest, stored_manifest
from stage9_metrics import measure_work
from test_asset_batch_reads import prepare_batch_assets
from test_asset_batches import month
from test_integrity_content import damage
from test_payroll_corrections import Company

from ai_accounting.kernel import close_storage
from ai_accounting.kernel.asset_batches import AssetBatches
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.query_reads import QueryReads


@pytest.fixture
def owner_book(tmp_path):
    company = Company(tmp_path / "owner-read-scopes.sqlite")
    prepare_batch_assets(company)
    with company.engine.store.connection(read_only=True) as connection:
        evidence = company.engine.store.current_fact(connection, "computer").evidence[0]
    company.close("2026-02")
    month(company.engine, evidence, "2026-03", "scope-march")
    company.close("2026-03")
    return company, evidence


def scope_inputs(reads):
    headers = tuple(reads.close_header(row) for row in reads.connection.execute(
        "SELECT * FROM period_close ORDER BY period"
    ))
    subjects = {row[0] for row in reads.connection.execute(
        "SELECT id FROM subject WHERE kind IN "
        "('asset_activation_batch','asset_consumption_month') UNION "
        "SELECT subject_id FROM calculation WHERE kind IN "
        "('asset_activation_batch','asset_consumption_month')"
    )}
    return headers, subjects


def read_scopes(reads, *, parts=None, positions=None):
    headers, subjects = scope_inputs(reads)
    return close_storage.read_asset_owner_accounting_many(
        reads.connection, headers, subjects,
        _verified_parts=parts, _positions_cache=positions,
    )


def test_owner_scopes_reduce_filter_inputs_and_preserve_complete_slices(owner_book, monkeypatch):
    company, evidence = owner_book
    inputs = []
    original = close_storage._accounting_subject_buckets

    def counted(header, subroot, subjects, parts, positions):
        inputs.append((header.period, frozenset(subjects)))
        return original(header, subroot, subjects, parts, positions)

    monkeypatch.setattr(close_storage, "_accounting_subject_buckets", counted)
    def consume(*, complete=False):
        with QueryReads.snapshot(company.engine) as reads:
            headers, subjects = scope_inputs(reads)
            # Requested negative identities remain in authority/output scope.
            subjects.update(f"absent-owner-{index}" for index in range(200))
            reader = (close_storage.read_accounting_many if complete
                      else close_storage.read_asset_owner_accounting_many)
            result = reader(reads.connection, headers, subjects)
            assert all(part.subjects == subjects for part in result)
            return result

    full_metrics, expected = measure_work(company.engine, lambda: consume(complete=True))
    full_work = sum(len(scope) for _, scope in inputs)
    inputs.clear()
    bounded_metrics, actual = measure_work(company.engine, consume)
    assert actual == expected
    assert sum(len(scope) for _, scope in inputs) < full_work // 20
    assert all(len(scope) == 1 for _, scope in inputs)

    def physical(metrics):
        statements = [row for row in metrics["sql"] if (
            "close_storage_block b" in row["statement"]
            or "close_storage_directory d" in row["statement"]
            or "close_storage_subroot s" in row["statement"]
        )]
        return {field: sum(row[field] for row in statements)
                for field in ("returned_rows", "returned_value_bytes")}

    assert physical(bounded_metrics)["returned_rows"] <= physical(full_metrics)["returned_rows"]
    assert physical(bounded_metrics)["returned_value_bytes"] <= (
        physical(full_metrics)["returned_value_bytes"]
    )
    assert bounded_metrics["counters"]["stdlib_json_loads"] <= (
        full_metrics["counters"]["stdlib_json_loads"]
    )
    # These are real typed future owners with actual publications, members and
    # vouchers. They enlarge the requested universe, while closed-month source
    # leaves and physical block work must remain identical.
    for number in (4, 5, 6):
        month(company.engine, evidence, f"2026-{number:02}", f"scope-future-{number}")
    inputs.clear()
    grown_metrics, grown = measure_work(company.engine, consume)
    assert [(part.adopted_results, part.vouchers) for part in grown] == [
        (part.adopted_results, part.vouchers) for part in actual
    ]
    assert physical(grown_metrics) == physical(bounded_metrics)
    assert grown_metrics["counters"]["stdlib_json_loads"] == (
        bounded_metrics["counters"]["stdlib_json_loads"]
    )
    assert all(len(scope) == 1 for _, scope in inputs)
    print(json.dumps({
        "full": full_metrics["counters"], "owner": bounded_metrics["counters"],
        "future_owner_growth": grown_metrics["counters"],
        "physical_full": physical(full_metrics), "physical_owner": physical(bounded_metrics),
        "physical_growth": physical(grown_metrics),
    }))


@pytest.mark.parametrize("missing", [
    "calculation", "publication", "fact", "subject", "calculation_seal", "fact_seal",
    "voucher", "voucher_current", "wrong_kind",
])
def test_declared_owner_scope_rejects_missing_or_wrong_source_without_publishing(
    owner_book, missing,
):
    company, _ = owner_book
    engine = company.engine
    with engine.store.connection(read_only=True) as connection:
        owner = dict(connection.execute(
            "SELECT c.*,v.id version_id,v.voucher_id FROM calculation c "
            "JOIN voucher_version v ON v.calculation_id=c.id "
            "WHERE c.kind='asset_consumption_month'"
        ).fetchone())
    if missing == "wrong_kind":
        damage(engine, "calculation", "UPDATE calculation SET kind='asset_activation_batch' "
               "WHERE id=?", (owner["id"],))
    else:
        table, key, ident = {
            "calculation": ("calculation", "id", owner["id"]),
            "publication": ("calculation_publication", "calculation_id", owner["id"]),
            "fact": ("fact_revision", "id", owner["fact_id"]),
            "subject": ("subject", "id", owner["subject_id"]),
            "calculation_seal": ("calculation_seal", "calculation_id", owner["id"]),
            "fact_seal": ("fact_seal", "fact_id", owner["fact_id"]),
            "voucher": ("voucher_version", "id", owner["version_id"]),
            "voucher_current": ("voucher_current", "voucher_id", owner["voucher_id"]),
        }[missing]
        damage(engine, table, f"DELETE FROM {table} WHERE {key}=?", (ident,), foreign_keys=False)
    parts, positions = {"already_verified": object()}, {"already_verified": object()}
    before_parts, before_positions = parts.copy(), positions.copy()
    with QueryReads.snapshot(engine) as reads:
        with pytest.raises(KernelError):
            read_scopes(reads, parts=parts, positions=positions)
    assert parts == before_parts
    assert positions == before_positions


def test_actual_publication_keeps_omitted_declaration_visible(owner_book):
    company, _ = owner_book
    with company.engine.store.connection(read_only=True) as connection:
        manifest = stored_manifest(connection, "2026-03")
    manifest["asset_batch_adoptions"] = []
    replace_stored_manifest(company.engine, manifest)
    with QueryReads.snapshot(company.engine) as reads:
        selections = read_scopes(reads)
        # Read scopes cannot turn this invalid root into empty history: the
        # metadata selector's independent owner-set comparison rejects it.
        last = selections[-1]
        assert {item["calculation_id"] for item in last.adopted_results
                if item["role"] == "asset_batch_owner"}
        assert reads.close_header(reads.connection.execute(
            "SELECT * FROM period_close ORDER BY period DESC LIMIT 1"
        ).fetchone()).root["small"]["asset_batch_adoptions"] == []


def amend_activation(company, evidence, *, revision, posting_period, benefit_area):
    engine = company.engine
    with engine.store.connection(read_only=True) as connection:
        owner_revision = engine.store.current_fact(connection, "activation-batch").revision
        members = [{
            "subject_id": subject, "expected_revision": revision,
            "data": engine.store.current_fact(connection, subject).fact.model_dump(mode="json")
            | {"benefit_area": benefit_area},
        } for subject in ("activate-computer", "activate-chair")]
    options = {
        "subject_id": "activation-batch", "period": "2026-02", "members": members,
        "evidence": (evidence,), "expected_revision": owner_revision,
        "posting_period": posting_period,
    }
    batches = AssetBatches(engine)
    preview = batches.prepare_activation_batch(**options)
    return batches.confirm_activation_batch(
        **options, preview_digest=preview["digest"], epochs=preview["epochs"],
        request_id=f"scope-amend-{revision}",
    )


def assert_complete_equivalence(engine):
    with QueryReads.snapshot(engine) as reads:
        headers, subjects = scope_inputs(reads)
        assert close_storage.read_asset_owner_accounting_many(
            reads.connection, headers, subjects,
        ) == close_storage.read_accounting_many(reads.connection, headers, subjects)


def test_owner_scopes_keep_continuous_closed_corrections(owner_book):
    company, evidence = owner_book
    amend_activation(company, evidence, revision=1, posting_period="2026-04", benefit_area="sales")
    month(company.engine, evidence, "2026-04", "scope-april")
    company.close("2026-04")
    assert_complete_equivalence(company.engine)
    amend_activation(
        company, evidence, revision=2, posting_period="2026-05", benefit_area="administration",
    )
    month(company.engine, evidence, "2026-05", "scope-may")
    company.close("2026-05")
    assert_complete_equivalence(company.engine)
    with QueryReads.snapshot(company.engine) as reads:
        assert sum(voucher["reverses_id"] is not None for part in read_scopes(reads)
                   for voucher in part.vouchers) >= 2


def test_no_impact_review_keeps_old_voucher_and_new_adopted_basis(tmp_path):
    company = Company(tmp_path / "review-owner-scope.sqlite")
    prepare_batch_assets(company)
    with company.engine.store.connection(read_only=True) as connection:
        original = connection.execute(
            "SELECT calculation_id FROM calculation_current WHERE subject_id='activation-batch'"
        ).fetchone()[0]
    evidence = company.engine.register_evidence(
        b"Explicit additional unchanged activation confirmation", "text/plain",
        "unchanged activation confirmation", request_id="scope-review-evidence",
    )["digest"]
    result = amend_activation(
        company, evidence, revision=1, posting_period="2026-02", benefit_area="administration",
    )
    assert any(item["impact"] == "review_no_impact" for item in result["results"])
    company.close("2026-02")
    assert_complete_equivalence(company.engine)
    with QueryReads.snapshot(company.engine) as reads:
        part = read_scopes(reads)[0]
        new_id = next(item["calculation_id"] for item in part.adopted_results
                      if item["subject_id"] == "activation-batch")
        assert new_id != original
        assert any(item["calculation_id"] == original and item["adopted_calculation_id"] == new_id
                   for item in part.vouchers)


def test_zero_line_owner_retains_frozen_adoption_without_voucher(tmp_path):
    company = Company(tmp_path / "zero-owner-scope.sqlite")
    prepare_batch_assets(company)
    with company.engine.store.connection(read_only=True) as connection:
        evidence = company.engine.store.current_fact(connection, "computer").evidence[0]
    month(company.engine, evidence, "2026-02", "scope-zero-february")
    company.close("2026-02")
    assert_complete_equivalence(company.engine)
    with QueryReads.snapshot(company.engine) as reads:
        part = read_scopes(reads)[0]
        owner = reads.connection.execute(
            "SELECT id FROM calculation WHERE kind='asset_consumption_month'"
        ).fetchone()[0]
        assert any(item["calculation_id"] == owner for item in part.adopted_results)
        assert not any(item["adopted_calculation_id"] == owner for item in part.vouchers)


def test_unowned_asset_owner_reader_preserves_full_scope(owner_book):
    company, _ = owner_book
    with company.engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        headers = close_storage.verified_headers(
            connection, connection.execute("SELECT * FROM period_close ORDER BY period").fetchall()
        )
        subjects = {row[0] for row in connection.execute(
            "SELECT subject_id FROM calculation WHERE kind IN "
            "('asset_activation_batch','asset_consumption_month')"
        )}
        assert close_storage.read_asset_owner_accounting_many(
            connection, headers, subjects,
        ) == close_storage.read_accounting_many(connection, headers, subjects)


def test_query_reads_owner_and_normal_caches_are_distinct_and_snapshot_owned(
    owner_book, monkeypatch,
):
    company, _ = owner_book
    calls = {"owner": 0, "normal": 0}
    owner_reader = close_storage.read_asset_owner_accounting_many
    normal_reader = close_storage.read_accounting_many

    def owner(*args, **kwargs):
        calls["owner"] += 1
        return owner_reader(*args, **kwargs)

    def normal(*args, **kwargs):
        calls["normal"] += 1
        return normal_reader(*args, **kwargs)

    monkeypatch.setattr(close_storage, "read_asset_owner_accounting_many", owner)
    monkeypatch.setattr(close_storage, "read_accounting_many", normal)
    for snapshot in range(2):
        with QueryReads.snapshot(company.engine) as reads:
            assert not reads._close_accounting_slices
            _, subjects = scope_inputs(reads)
            rows = reads.connection.execute("SELECT * FROM period_close ORDER BY period").fetchall()
            actual = reads.close_asset_owner_accounting_many(rows, subjects=subjects)
            assert reads.close_asset_owner_accounting_many(rows, subjects=subjects) == actual
            assert calls["owner"] == snapshot + 1
            assert reads.close_accounting_many(rows, subjects=subjects) == actual
            assert calls["normal"] == snapshot + 1
            for row in rows:
                assert (row["period"], frozenset(subjects)) in reads._close_accounting_slices
                assert (row["period"], frozenset(subjects), "asset_owners") in (
                    reads._close_accounting_slices
                )


def test_query_reads_late_owner_failure_keeps_prior_exact_scope_only(owner_book):
    company, _ = owner_book
    engine = company.engine
    with engine.store.connection(read_only=True) as connection:
        voucher_id = connection.execute(
            "SELECT v.voucher_id FROM voucher_version v "
            "JOIN calculation c ON c.id=v.calculation_id "
            "WHERE c.kind='asset_consumption_month'"
        ).fetchone()[0]
    damage(engine, "voucher_current", "DELETE FROM voucher_current WHERE voucher_id=?",
           (voucher_id,), foreign_keys=False)
    with QueryReads.snapshot(engine) as reads:
        _, subjects = scope_inputs(reads)
        rows = reads.connection.execute("SELECT * FROM period_close ORDER BY period").fetchall()
        prior = reads.close_asset_owner_accounting_many(rows[:1], subjects=subjects)
        slices = reads._close_accounting_slices.copy()
        parts = reads._verified_close_storage_parts.copy()
        positions = reads._close_accounting_positions.copy()
        with pytest.raises(KernelError):
            reads.close_asset_owner_accounting_many(rows, subjects=subjects)
        assert reads._close_accounting_slices == slices
        assert reads._verified_close_storage_parts == parts
        assert reads._close_accounting_positions == positions
        assert reads.close_asset_owner_accounting_many(rows[:1], subjects=subjects) == prior
