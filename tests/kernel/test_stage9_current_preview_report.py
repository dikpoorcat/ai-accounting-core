"""A current-source preview attests an old synthetic checkpoint without rewriting it."""

import json
import weakref
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from stage9_book import MixedBook
from test_asset_readiness_source_scope import _former_reads, _former_work

from ai_accounting.kernel.periods import Periods
from scripts import stage9_verified_open_preview
from scripts.benchmark_stage9_browser import validate_book_report
from scripts.stage9_verified_open_preview import verify_book_open_preview


def _closed_storage_bytes(book, month):
    period = 12 * (int(month[:4]) - 1) + int(month[5:7]) - 1
    queries = {
        "period_close": (
            "SELECT period,CAST(manifest AS BLOB),digest FROM period_close WHERE period=?"
        ),
        "close_storage_root": "SELECT period,storage_digest FROM close_storage_root WHERE period=?",
        "close_storage_subroot": (
            "SELECT period,family,CAST(content AS BLOB),digest "
            "FROM close_storage_subroot WHERE period=? ORDER BY family"
        ),
        "close_storage_directory": (
            "SELECT period,field,bucket,CAST(content AS BLOB),digest "
            "FROM close_storage_directory WHERE period=? ORDER BY field,bucket"
        ),
        "close_storage_block": (
            "SELECT period,field,bucket,part,CAST(content AS BLOB),digest "
            "FROM close_storage_block WHERE period=? ORDER BY field,bucket,part"
        ),
    }
    with book.engine.store.connection(read_only=True) as connection:
        return {
            table: tuple(map(tuple, connection.execute(query, (period,))))
            for table, query in queries.items()
        }


def test_old_checkpoint_gets_new_read_only_preview_and_state_guard(tmp_path, monkeypatch):
    monkeypatch.setenv("STAGE9_WORKSPACE_ROOT", str(tmp_path))
    root = tmp_path / ".tmp/stage9-current-preview"
    book = MixedBook(root, employees=1, businesses=26)
    readiness = book.engine.store.registry.readiness
    current_asset_rule = readiness["assets_and_financing"]
    try:
        readiness["assets_and_financing"] = (_former_reads, _former_work)
        book.add_month(0, close=True)
        book.add_month(1, close=False)
    finally:
        readiness["assets_and_financing"] = current_asset_rule
    checkpoint = root / "stage9-builder.json"
    checkpoint_bytes = checkpoint.read_bytes()
    snapshots_before = deepcopy(book.snapshots)
    closed_bytes = _closed_storage_bytes(book, "2016-01")
    assert all(closed_bytes.values())
    created = json.loads(checkpoint_bytes)
    period = "2016-02"
    assert created["snapshots"] == snapshots_before

    # Construction payloads must be released before the registered verifier
    # loads its independent source proof, even when the checkpoint is large.
    class Checkpoint(dict):
        pass

    loaded_checkpoints = []
    def checkpoint_loads(text):
        value = Checkpoint(json.loads(text))
        loaded_checkpoints.append(weakref.ref(value))
        return value

    monkeypatch.setattr(
        stage9_verified_open_preview, "json", SimpleNamespace(loads=checkpoint_loads)
    )
    original_bundle = book.engine.store.bundle
    original_version = original_bundle.current_versions["company"]
    original_verifier = original_bundle.company_verifiers[original_version]

    def check_without_fixture_payload(connection, bundle):
        assert loaded_checkpoints and all(item() is None for item in loaded_checkpoints)
        return original_verifier(connection, bundle)

    book.engine.store.bundle = replace(
        original_bundle,
        company_verifiers={**original_bundle.company_verifiers,
                           original_version: check_without_fixture_payload},
    )

    verified = verify_book_open_preview(
        book.engine,
        checkpoint_path=checkpoint,
        company=book.company,
        snapshots=book.snapshots,
        period=period,
        source=tmp_path,
    )
    current = verified["verified_open_preview"]
    assert verified["integrity"]["status"] == "verified"
    assert current["construction_preview_digest"] == snapshots_before[period]["preview_digest"]
    assert current["digest"] != current["construction_preview_digest"]
    assert current["state"] == created["epochs"]
    assert checkpoint.read_bytes() == checkpoint_bytes
    assert book.snapshots == snapshots_before
    assert _closed_storage_bytes(book, "2016-01") == closed_bytes

    dispatches = []

    def resident_preview(selected_period, *, owner_confirmation):
        dispatches.append((selected_period, owner_confirmation))
        return Periods(book.engine).preview_close(
            selected_period, owner_confirmation=owner_confirmation
        )

    same_process = verify_book_open_preview(
        book.engine, checkpoint_path=checkpoint, company=book.company,
        snapshots=book.snapshots, period=period, source=tmp_path,
        dimensions={"month_stats": book.month_stats, "employees_count": 1, "businesses": 26},
        preview_close=resident_preview,
    )
    assert dispatches == [(period, book.snapshots[period]["owner_confirmation"])]
    assert same_process["verified_open_preview"] == current
    assert same_process["checkpoint_sha256"] == verified["checkpoint_sha256"]
    with pytest.raises(ValueError, match="sample dimensions"):
        verify_book_open_preview(
            book.engine, checkpoint_path=checkpoint, company=book.company,
            snapshots=book.snapshots, period=period, source=tmp_path,
            dimensions={"employees_count": 2}, preview_close=resident_preview,
        )
    assert len(dispatches) == 1
    assert (
        Periods(book.engine).preview_close(
            period, owner_confirmation=book.snapshots[period]["owner_confirmation"]
        )["digest"]
        == current["digest"]
    )

    bundle = book.engine.store.bundle
    version = bundle.current_versions["company"]
    registered = bundle.company_verifiers[version]

    def limited(connection, selected_bundle):
        return {
            **registered(connection, selected_bundle),
            "limitations": ["synthetic incomplete coverage"],
        }

    book.engine.store.bundle = replace(
        bundle, company_verifiers={**bundle.company_verifiers, version: limited}
    )
    try:
        with pytest.raises(ValueError, match="registered content verifier"):
            verify_book_open_preview(
                book.engine,
                checkpoint_path=checkpoint,
                company=book.company,
                snapshots=book.snapshots,
                period=period,
                source=tmp_path,
            )
    finally:
        book.engine.store.bundle = bundle

    report = {
        **book.describe(),
        **verified,
        "status": "complete",
        "source": str(tmp_path),
        "requested_months": 2,
    }
    assert validate_book_report(report, company_name="阶段九合成规模企业") == period
    stale = {key: value for key, value in report.items() if key != "verified_open_preview"}
    with pytest.raises(ValueError, match="Current synthetic open preview"):
        validate_book_report(stale, company_name="阶段九合成规模企业")
    assert (
        validate_book_report(
            stale, company_name="阶段九合成规模企业", require_current_preview=False
        )
        == period
    )
    changed_source = {**report, "source": str(root)}
    with pytest.raises(ValueError, match="Current synthetic open preview"):
        validate_book_report(changed_source, company_name="阶段九合成规模企业")
    changed_company = {**book.company, "database_id": "another-database"}
    with pytest.raises(ValueError, match="checkpoint identity"):
        verify_book_open_preview(
            book.engine,
            checkpoint_path=checkpoint,
            company=changed_company,
            snapshots=book.snapshots,
            period=period,
            source=tmp_path,
        )

    with book.engine.store.connection() as connection:
        connection.execute("UPDATE state SET read_repair_revision=read_repair_revision+1")
    with pytest.raises(ValueError, match="state, repair revision or identity"):
        verify_book_open_preview(
            book.engine,
            checkpoint_path=checkpoint,
            company=book.company,
            snapshots=book.snapshots,
            period=period,
            source=tmp_path,
        )
    assert checkpoint.read_bytes() == checkpoint_bytes
    assert book.snapshots == snapshots_before
    assert _closed_storage_bytes(book, "2016-01") == closed_bytes


@pytest.mark.parametrize("section", (
    "sources", "historical_adoption", "projections", "read_indexes", "limitations",
))
def test_registered_qualification_refuses_missing_proof_before_preview(monkeypatch, section):
    from contextlib import nullcontext

    from ai_accounting.kernel import versions

    integrity = {
        "status": "verified", "limitations": [],
        "coverage": dict.fromkeys(
            ("sources", "historical_adoption", "projections", "read_indexes"), "verified"
        ),
    }
    if section == "limitations":
        integrity.pop("limitations")
    else:
        integrity["coverage"].pop(section)
    calls = []
    connection = SimpleNamespace(execute=lambda _: None)
    engine = SimpleNamespace(store=SimpleNamespace(
        bundle=SimpleNamespace(company_verifiers={1: lambda *_: integrity}),
        connection=lambda **_: nullcontext(connection),
    ))
    monkeypatch.setattr(versions, "database_format", lambda *_, **__: {"version": 1})
    monkeypatch.setattr(stage9_verified_open_preview, "preview_checkpoint", lambda *_, **__: {})
    monkeypatch.setattr(stage9_verified_open_preview, "require_preview_state",
                        lambda *_: calls.append("state"))
    with pytest.raises(ValueError, match="registered content verifier"):
        verify_book_open_preview(engine, checkpoint_path=None, company={}, snapshots={},
                                 period="2016-02", source=None,
                                 preview_close=lambda *_, **__: calls.append("preview"))
    assert calls == ["state", "state"]
