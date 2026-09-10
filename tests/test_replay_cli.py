from __future__ import annotations

import subprocess
import sys
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.engine import make_url

from ai_accounting import replay_cli


def test_checkpoint_replacement_failure_preserves_previous_state(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    replay_cli._write_json(state_file, {"completed": ["first"]})

    def fail_replace(*_args):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(replay_cli.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        replay_cli._write_json(state_file, {"completed": ["first", "second"]}, compact=True)
    assert replay_cli._load_json(state_file) == {"completed": ["first"]}
    assert list(tmp_path.iterdir()) == [state_file]


def test_checkpoint_retries_transient_windows_replacement_denial(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    replay_cli._write_json(state_file, {"completed": ["first"]})
    real_replace = replay_cli.os.replace
    attempts = []
    delays = []

    def transient_replace(*args):
        attempts.append(args)
        if len(attempts) < 3:
            raise PermissionError("simulated transient file lock")
        real_replace(*args)

    monkeypatch.setattr(replay_cli.os, "replace", transient_replace)
    monkeypatch.setattr(replay_cli.time, "sleep", delays.append)

    replay_cli._write_json(state_file, {"completed": ["first", "second"]}, compact=True)

    assert len(attempts) == 3
    assert delays == [0.05, 0.1]
    assert replay_cli._load_json(state_file) == {"completed": ["first", "second"]}


def test_preview_confirm_recovers_committed_result_after_checkpoint_failure(monkeypatch):
    org_id = str(uuid.uuid4())
    asset_id = str(uuid.uuid4())
    operation = {
        "key": "amortization",
        "kind": "preview_confirm",
        "preview_tool": "finance_preview_intangible_asset_amortization",
        "preview_request": {
            "org_id": org_id,
            "asset_id": asset_id,
            "amortization_period": "2025-01",
            "posting_date": "2025-01-31",
        },
        "confirm_tool": "finance_confirm_intangible_asset_amortization",
        "confirm_request": {
            "idempotency_key": "amortization-confirm",
            "confirmation_note": "confirmed",
        },
        "allowed_preview_statuses": ["calculated"],
        "allowed_confirm_statuses": ["posted"],
    }
    resolver = SimpleNamespace(
        materialize=lambda value: value,
        existing_calculation_hash=lambda key: (
            "stored-calculation-hash" if key == "amortization-confirm" else None
        ),
    )
    calls = []

    def call_tool(name, request):
        calls.append((name, request))
        if name == "finance_preview_intangible_asset_amortization":
            return {
                "status": "rejected",
                "errors": ["INTANGIBLE_ASSET_AMORTIZATION_OUT_OF_SEQUENCE"],
            }
        return {"status": "posted", "event_id": "event-1", "idempotent_replay": True}

    monkeypatch.setattr(replay_cli, "_call_tool", call_tool)

    result = replay_cli._preview_confirm(operation, resolver)

    assert result["idempotent_replay"] is True
    assert calls[1][0] == "finance_confirm_intangible_asset_amortization"
    assert calls[1][1]["calculation_hash"] == "stored-calculation-hash"


def test_replay_lock_excludes_another_process_and_releases_after_failure(tmp_path):
    state_file = tmp_path / "state.json"
    code = (
        "import sys\nfrom pathlib import Path\n"
        "from ai_accounting.replay_cli import _replay_state_lock, ReplayError\n"
        "try:\n"
        "    with _replay_state_lock(Path(sys.argv[1])): print('acquired')\n"
        "except ReplayError as exc: print(exc.code)\n"
    )

    def attempt():
        return subprocess.run(
            [sys.executable, "-c", code, str(state_file)],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()

    with pytest.raises(RuntimeError, match="interrupted"):
        with replay_cli._replay_state_lock(state_file):
            assert attempt() == "REPLAY_STATE_ALREADY_IN_USE"
            raise RuntimeError("interrupted")
    assert attempt() == "acquired"


def test_replay_checkpoints_progress_and_resumes_only_pending_operations(tmp_path, monkeypatch):
    operations = [
        {"key": key, "kind": "tool", "tool": "test", "request": {}}
        for key in ("first", "second", "third")
    ]
    package, org_id = _resume_package(tmp_path, operations)
    state = _resume_state(package, org_id, [])
    state["companies"][0]["database_name"] = "isolated"
    state_file = tmp_path / "state.json"
    replay_cli._write_json(state_file, state)
    monkeypatch.setattr(
        replay_cli, "_load_state", lambda *_: (state_file, replay_cli._load_json(state_file))
    )
    monkeypatch.setattr(replay_cli, "_validate_replay_target", lambda *_: None)
    monkeypatch.setattr(
        replay_cli,
        "get_settings",
        lambda: SimpleNamespace(
            finance_environment="development",
            finance_migration_database_url="sqlite://",
        ),
    )
    disposed = []
    monkeypatch.setattr(
        replay_cli,
        "create_engine",
        lambda *_: SimpleNamespace(dispose=lambda: disposed.append(True)),
    )
    from ai_accounting import mcp_server

    monkeypatch.setattr(mcp_server, "_initialize_mcp_credential_store", lambda **_: None)
    monkeypatch.setattr(replay_cli, "_call_tool", lambda *_: {"status": "ok"})
    executed = []

    def execute(operation, **_kwargs):
        if operation["key"] == "second" and executed == ["first"]:
            executed.append("failed-second")
            raise replay_cli.ReplayError("TEST_INTERRUPTION")
        executed.append(operation["key"])
        return {"status": "posted"}

    monkeypatch.setattr(replay_cli, "_execute_operation", execute)
    progress = []
    with pytest.raises(replay_cli.ReplayError, match="TEST_INTERRUPTION"):
        replay_cli.replay_system(package, state_file, progress=progress.append)
    assert replay_cli._load_json(state_file)["companies"][0]["completed_operations"] == ["first"]
    result = replay_cli.replay_system(package, state_file, progress=progress.append)
    assert executed == ["first", "failed-second", "second", "third"]
    assert result["completed_operations_this_run"] == 2
    assert result["timing"]["operation_seconds_by_kind"]["tool"]["count"] == 2
    assert len(disposed) == 2
    assert [row["operation_key"] for row in progress if row["status"] == "operation_completed"] == [
        "first",
        "second",
        "third",
    ]
    assert progress[-1]["company_completed"] == progress[-1]["company_total"] == 3
    saved = replay_cli._load_json(state_file)
    assert saved["phase"] == "replayed"
    assert saved["last_run_timing"] == result["timing"]


def test_replay_ignores_intra_operation_preparation_results_when_building_checkpoint(
    tmp_path, monkeypatch
):
    operation = {
        "key": "composite",
        "kind": "composite_event",
        "replay_key": "composite",
        "preparations": [{"key": "preview", "preview_request": {}}],
        "request": {
            "batch_id": {
                "$ref": "operation_result",
                "operation_key": "preview",
                "field": "batch_id",
            }
        },
    }
    package, org_id = _resume_package(tmp_path, [operation])
    state = _resume_state(package, org_id, [])
    state["companies"][0]["database_name"] = "isolated"
    state_file = tmp_path / "state.json"
    replay_cli._write_json(state_file, state)
    monkeypatch.setattr(
        replay_cli, "_load_state", lambda *_: (state_file, replay_cli._load_json(state_file))
    )
    monkeypatch.setattr(replay_cli, "_validate_replay_target", lambda *_: None)
    monkeypatch.setattr(
        replay_cli,
        "get_settings",
        lambda: SimpleNamespace(
            finance_environment="development",
            finance_migration_database_url="sqlite://",
        ),
    )
    monkeypatch.setattr(
        replay_cli, "create_engine", lambda *_: SimpleNamespace(dispose=lambda: None)
    )
    from ai_accounting import mcp_server

    monkeypatch.setattr(mcp_server, "_initialize_mcp_credential_store", lambda **_: None)
    monkeypatch.setattr(replay_cli, "_call_tool", lambda *_: {"status": "ok"})
    monkeypatch.setattr(
        replay_cli,
        "_execute_operation",
        lambda *_args, **_kwargs: {"status": "posted", "event_id": "event-1"},
    )

    result = replay_cli.replay_system(package, state_file)

    assert result["status"] == "replayed"
    saved_results = replay_cli._load_json(state_file)["companies"][0]["operation_results"]
    assert saved_results == {"composite": {"status": "posted", "event_id": "event-1"}}


def _resume_package(tmp_path, operations):
    org_id = str(uuid.uuid4())
    package = tmp_path / "package"
    company = package / "companies" / org_id
    company.mkdir(parents=True)
    replay_cli._write_json(
        package / "system.json",
        {
            "format_version": replay_cli._FORMAT_VERSION,
            "baseline_revisions": {
                "catalog": replay_cli._CATALOG_REVISION,
                "business": replay_cli._BUSINESS_REVISION,
            },
            "companies": [
                {
                    "org_id": org_id,
                    "directory": f"companies/{org_id}",
                    "display_name": "测试公司",
                    "is_primary": True,
                }
            ],
        },
    )
    replay_cli._write_json(
        company / "company.json",
        {
            "org_id": org_id,
            "organization": {"name": "测试公司"},
            "accounts": [{"code": "1001"}],
        },
    )
    replay_cli._write_jsonl(company / "operations.jsonl", operations)
    return package, org_id


def _resume_state(package, org_id, completed):
    snapshot = replay_cli._resume_package_snapshot(package)
    operation_hashes = {
        row["key"]: row["sha256"]
        for row in snapshot["company_operations"][org_id]
        if row["key"] in completed
    }
    return {
        "phase": "replaying",
        "package_manifest_sha256": "a" * 64,
        "package_preparation_sha256": snapshot["preparation_sha256"],
        "companies": [
            {
                "org_id": org_id,
                "completed_operations": completed,
                "completed_operation_sha256": operation_hashes,
                "operation_results": {
                    key: {"status": "posted", "event_id": str(uuid.uuid4())} for key in completed
                },
            }
        ],
    }


def test_replay_distinguishes_baseline_identity_from_current_heads() -> None:
    assert replay_cli._BUSINESS_REVISION == "0008_mybank_payment_metadata"
    assert replay_cli._current_schema_revision(catalog=False) == "0008_mybank_payment_metadata"
    assert replay_cli._current_schema_revision(catalog=True) == "0001_catalog_baseline_v2"


@pytest.mark.parametrize(
    "revision,expected_error",
    [
        ("0007_mybank_payment_sources", "REPLAY_SOURCE_ORGANIZATION_MISSING"),
        ("0008_mybank_payment_metadata", "REPLAY_SOURCE_ORGANIZATION_MISSING"),
        ("unknown_history", "REPLAY_SOURCE_COMPONENT_BASELINE_REQUIRED"),
    ],
)
def test_payment_metadata_upgrade_retains_supported_replay_sources(
    tmp_path, monkeypatch, revision, expected_error
):
    from contextlib import nullcontext

    source = SimpleNamespace(scalar=lambda *_args: revision, get=lambda *_args: None)
    monkeypatch.setattr(replay_cli, "Session", lambda _engine: nullcontext(source))
    with pytest.raises(replay_cli.ReplayError, match=expected_error):
        replay_cli._export_company(
            engine=None, registry={"org_id": str(uuid.uuid4())}, package_root=tmp_path
        )


def test_replay_orders_non_primary_company_before_primary() -> None:
    companies = [
        {"org_id": "primary", "display_name": "魂道", "is_primary": True},
        {"org_id": "secondary", "display_name": "屋舍心声", "is_primary": False},
    ]

    assert [company["org_id"] for company in replay_cli._ordered_replay_companies(companies)] == [
        "secondary",
        "primary",
    ]


def test_catalog_migration_target_uses_migration_credentials() -> None:
    runtime = make_url("postgresql+psycopg://finance_runtime:runtime@127.0.0.1/finance_catalog")
    migration = "postgresql+psycopg://finance_migrator:migrator@127.0.0.1/finance"

    target = replay_cli._migration_url_for_runtime_database(migration, runtime)

    assert target.username == "finance_migrator"
    assert target.password == "migrator"
    assert target.database == "finance_catalog"


def test_old_normalization_command_is_not_supported():
    with pytest.raises(SystemExit):
        replay_cli.main(["export-system", "--output", "unused", "--normalizations", "old.json"])


def test_manifest_rejects_any_file_tampering(tmp_path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    payload = package / "system.json"
    payload.write_text("{}\n", encoding="utf-8")
    replay_cli._seal_package(package)
    payload.write_text('{"tampered":true}\n', encoding="utf-8")

    with pytest.raises(replay_cli.ReplayError) as error:
        replay_cli._parse_manifest(package)

    assert error.value.code == "REPLAY_PACKAGE_MANIFEST_MISMATCH"


def test_manifest_covers_archived_nested_manifest(tmp_path) -> None:
    package = tmp_path / "package"
    archive = package / "archive" / "legacy-package"
    archive.mkdir(parents=True)
    nested_manifest = archive / "MANIFEST.sha256"
    nested_manifest.write_text("legacy manifest\n", encoding="utf-8")
    replay_cli._seal_package(package)
    nested_manifest.write_text("tampered legacy manifest\n", encoding="utf-8")

    with pytest.raises(replay_cli.ReplayError) as error:
        replay_cli._parse_manifest(package)

    assert error.value.code == "REPLAY_PACKAGE_MANIFEST_MISMATCH"


def test_replay_resolves_uniquely_anonymized_legacy_open_item(monkeypatch) -> None:
    source_event_id = uuid.uuid4()
    open_item_id = uuid.uuid4()

    class FakeSession:
        def __init__(self, _engine):
            self.fallback_sql = ""

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def scalar(self, _statement, _parameters):
            return None

        def scalars(self, statement, _parameters):
            self.fallback_sql = str(statement)
            return SimpleNamespace(all=lambda: [open_item_id])

    fake_session = FakeSession(None)
    monkeypatch.setattr(replay_cli, "Session", lambda _engine: fake_session)
    resolver = replay_cli._ReplayResolver(
        engine=object(),
        org_id=uuid.uuid4(),
        results={"source": {"event_id": str(source_event_id)}},
    )

    resolved = resolver.materialize(
        {
            "$ref": "open_item",
            "source_replay_key": "source",
            "item_type": "payable",
            "original_amount_fen": 115_000,
            "payable_category": "employer_social",
            "payable_agency_code": "legacy-agency",
            "insurance_kind": "medical_and_maternity",
            "counterparty_kind": "other",
            "counterparty_name": "旧缴费机构",
            "counterparty_external_ref": "legacy-agency",
        }
    )

    assert resolved == str(open_item_id)
    assert "item.counterparty_id IS NULL" in fake_session.fallback_sql
    assert "item.payable_agency_code IS NULL" in fake_session.fallback_sql


def test_resume_accepts_correction_after_completed_prefix(tmp_path) -> None:
    operations = [
        {"key": "completed", "kind": "tool", "request": {"value": 1}},
        {"key": "pending", "kind": "tool", "request": {"value": 2}},
    ]
    package, org_id = _resume_package(tmp_path, operations)
    state = _resume_state(package, org_id, ["completed"])
    replay_cli._write_jsonl(
        package / "companies" / org_id / "operations.jsonl",
        [operations[0], {"key": "pending", "kind": "tool", "request": {"value": 3}}],
    )

    changed = replay_cli._reconcile_state_package_binding(
        state,
        package_root=package,
        manifest_sha256="b" * 64,
    )

    assert changed is True
    assert state["package_manifest_sha256"] == "b" * 64
    assert state["package_manifest_history"][-1]["decision"] == (
        "preparation_and_completed_prefix_unchanged"
    )


def test_resume_rejects_correction_to_completed_operation(tmp_path) -> None:
    operations = [
        {"key": "completed", "kind": "tool", "request": {"value": 1}},
        {"key": "pending", "kind": "tool", "request": {"value": 2}},
    ]
    package, org_id = _resume_package(tmp_path, operations)
    state = _resume_state(package, org_id, ["completed"])
    replay_cli._write_jsonl(
        package / "companies" / org_id / "operations.jsonl",
        [
            {"key": "completed", "kind": "tool", "request": {"value": 9}},
            operations[1],
        ],
    )

    with pytest.raises(replay_cli.ReplayError) as error:
        replay_cli._reconcile_state_package_binding(
            state,
            package_root=package,
            manifest_sha256="b" * 64,
        )

    assert error.value.code == "REPLAY_STATE_PACKAGE_COMPLETED_OPERATION_IMPACT"


def test_resume_rejects_new_reference_to_unsaved_completed_result(tmp_path) -> None:
    operations = [
        {"key": "completed", "kind": "tool", "request": {"value": 1}},
        {"key": "pending", "kind": "tool", "request": {"value": 2}},
    ]
    package, org_id = _resume_package(tmp_path, operations)
    state = _resume_state(package, org_id, ["completed"])
    replay_cli._write_jsonl(
        package / "companies" / org_id / "operations.jsonl",
        [
            operations[0],
            {
                "key": "pending",
                "kind": "tool",
                "request": {
                    "value": {
                        "$ref": "operation_result",
                        "operation_key": "completed",
                        "field": "data",
                    }
                },
            },
        ],
    )

    with pytest.raises(replay_cli.ReplayError) as error:
        replay_cli._reconcile_state_package_binding(
            state,
            package_root=package,
            manifest_sha256="b" * 64,
        )

    assert error.value.code == "REPLAY_STATE_PACKAGE_COMPLETED_RESULT_MISSING"


def test_resume_rejects_correction_to_preparation_inputs(tmp_path) -> None:
    package, org_id = _resume_package(
        tmp_path, [{"key": "pending", "kind": "tool", "request": {"value": 1}}]
    )
    state = _resume_state(package, org_id, [])
    company_file = package / "companies" / org_id / "company.json"
    replay_cli._write_json(
        company_file,
        {
            "org_id": org_id,
            "organization": {"name": "已改变的公司"},
            "accounts": [{"code": "1001"}],
        },
    )

    with pytest.raises(replay_cli.ReplayError) as error:
        replay_cli._reconcile_state_package_binding(
            state,
            package_root=package,
            manifest_sha256="b" * 64,
        )

    assert error.value.code == "REPLAY_STATE_PACKAGE_PREPARATION_IMPACT"


def test_resume_rejects_new_pending_operation_after_replay_completed(tmp_path) -> None:
    operations = [{"key": "completed", "kind": "tool", "request": {"value": 1}}]
    package, org_id = _resume_package(tmp_path, operations)
    state = _resume_state(package, org_id, ["completed"])
    state["phase"] = "replayed"
    replay_cli._write_jsonl(
        package / "companies" / org_id / "operations.jsonl",
        [*operations, {"key": "new-pending", "kind": "tool", "request": {"value": 2}}],
    )

    with pytest.raises(replay_cli.ReplayError) as error:
        replay_cli._reconcile_state_package_binding(
            state,
            package_root=package,
            manifest_sha256="b" * 64,
        )

    assert error.value.code == "REPLAY_STATE_PACKAGE_PENDING_OPERATION_IMPACT"


def test_resume_backfills_compatibility_metadata_for_unchanged_legacy_state(tmp_path) -> None:
    operations = [{"key": "completed", "kind": "tool", "request": {"value": 1}}]
    package, org_id = _resume_package(tmp_path, operations)
    state = _resume_state(package, org_id, ["completed"])
    state.pop("package_preparation_sha256")
    state["companies"][0].pop("completed_operation_sha256")

    changed = replay_cli._reconcile_state_package_binding(
        state,
        package_root=package,
        manifest_sha256="a" * 64,
    )

    assert changed is True
    assert state["package_preparation_sha256"]
    assert state["companies"][0]["completed_operation_sha256"]["completed"]


def test_operation_reference_must_resolve_to_an_earlier_operation() -> None:
    operations = [
        {
            "key": "dependent",
            "kind": "tool",
            "request": {
                "source": {
                    "$ref": "operation_result",
                    "operation_key": "missing",
                    "field": "event_id",
                }
            },
        }
    ]

    with pytest.raises(replay_cli.ReplayError) as error:
        replay_cli._verify_operation_references(
            operations,
            evidence_hashes=set(),
            org_id="72830c73-b9ee-5fdd-b891-227f506ac8f8",
        )

    assert error.value.code == "REPLAY_PACKAGE_OPERATION_REFERENCE_MISSING"


def test_nonempty_target_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Connection:
        def __enter__(self) -> _Connection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class _Engine:
        def connect(self) -> _Connection:
            return _Connection()

        def dispose(self) -> None:
            return None

    class _Inspector:
        @staticmethod
        def get_table_names(*, schema: str) -> list[str]:
            assert schema == "public"
            return ["existing_business_data"]

    monkeypatch.setattr(replay_cli, "create_engine", lambda _url: _Engine())
    monkeypatch.setattr(replay_cli, "sa_inspect", lambda _connection: _Inspector())

    with pytest.raises(replay_cli.ReplayError) as error:
        replay_cli._assert_database_empty(make_url("postgresql://local/target"))

    assert error.value.code == "REPLAY_TARGET_DATABASE_NOT_EMPTY"


def test_replay_requires_login_before_mutating_state(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "package"
    package.mkdir()
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(
        replay_cli,
        "_load_state",
        lambda _package, _state: (state_file, {"phase": "prepared"}),
    )
    monkeypatch.setattr(
        replay_cli,
        "get_settings",
        lambda: SimpleNamespace(finance_environment="development"),
    )
    monkeypatch.setattr(
        replay_cli,
        "_call_tool",
        lambda _name, _request: {
            "status": "rejected",
            "errors": ["AUTHENTICATION_REQUIRED"],
        },
    )
    from ai_accounting import mcp_server

    monkeypatch.setattr(
        mcp_server,
        "_initialize_mcp_credential_store",
        lambda **_kwargs: None,
    )

    monkeypatch.setattr(replay_cli, "_validate_replay_target", lambda *_: None)
    monkeypatch.setattr(replay_cli, "_request_replay_security", lambda *_: {"status": "starting"})
    result = replay_cli.replay_system(package, state_file)
    assert result["status"] == "waiting_for_owner"
    assert result["error_code"] == "REPLAY_AUTHENTICATION_REQUIRED"
    assert not state_file.exists()


def test_component_replay_rejects_old_event_request() -> None:
    with pytest.raises(replay_cli.ReplayError, match="REPLAY_COMPONENT_REQUEST_INVALID"):
        replay_cli._validate_composed_replay_request(
            {
                "org_id": "${ORG_ID}",
                "idempotency_key": "old",
                "event_type": "expense_cash",
                "amounts": {"gross_amount_fen": 100},
            }
        )


def test_component_replay_validates_funds_allocation_without_database() -> None:
    request = {
        "org_id": "${ORG_ID}",
        "idempotency_key": "mixed",
        "posting_date": "2026-08-01",
        "components": [
            {
                "key": "office",
                "kind": "expense",
                "business_date": "2026-08-01",
                "amount_fen": 100,
                "expense_class": "general_expense",
                "payment_basis": "immediate",
            },
            {
                "key": "fee",
                "kind": "expense",
                "business_date": "2026-08-01",
                "amount_fen": 20,
                "expense_class": "finance_expense",
                "payment_basis": "immediate",
            },
        ],
        "funds": [
            {
                "key": "payment",
                "account_code": "100201",
                "direction": "payment",
                "payment_date": "2026-08-01",
                "amount_fen": 120,
                "allocations": [
                    {"component_key": "office", "amount_fen": 100},
                    {"component_key": "fee", "amount_fen": 20},
                ],
            }
        ],
    }
    replay_cli._validate_composed_replay_request(request)
    request["funds"][0]["amount_fen"] = 121
    with pytest.raises(replay_cli.ReplayError, match="REPLAY_COMPONENT_REQUEST_INVALID"):
        replay_cli._validate_composed_replay_request(request)


def test_component_replay_reference_requires_prior_source() -> None:
    operations = [
        {
            "key": "recovery",
            "kind": "tool",
            "request": {
                "source": {
                    "$ref": "component",
                    "source_replay_key": "expense",
                    "component_key": "office",
                }
            },
        }
    ]
    with pytest.raises(replay_cli.ReplayError, match="REPLAY_PACKAGE_OPERATION_REFERENCE_MISSING"):
        replay_cli._verify_operation_references(operations, evidence_hashes=set(), org_id="org")
    replay_cli._verify_operation_references(
        [{"key": "expense", "kind": "tool", "request": {}}, *operations],
        evidence_hashes=set(),
        org_id="org",
    )


def test_reentry_compares_terminal_balance_not_reversed_journal_volume() -> None:
    source = [
        {
            "account_code": "100201",
            "ending_balance_fen": 100,
            "debit_total_fen": 300,
            "credit_total_fen": 200,
        }
    ]
    replayed = [
        {
            "account_code": "100201",
            "ending_balance_fen": 100,
            "debit_total_fen": 100,
            "credit_total_fen": 0,
        }
    ]
    assert replay_cli._balance_terminal_state(source) == (
        replay_cli._balance_terminal_state(replayed)
    )
    replayed[0]["ending_balance_fen"] = 101
    assert replay_cli._balance_terminal_state(source) != (
        replay_cli._balance_terminal_state(replayed)
    )


def test_export_connection_forces_source_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = {}

    def create(url, **kwargs):
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(replay_cli, "create_engine", create)
    replay_cli._readonly_source_engine("postgresql://local/source")
    assert seen["connect_args"]["options"] == "-c default_transaction_read_only=on"
    assert seen["isolation_level"] == "REPEATABLE READ"


@pytest.mark.parametrize(
    ("legacy", "scope"),
    [("declared", "wage_income"), ("not_declared", "contributions_only")],
)
def test_export_normalizes_legacy_payroll_scope(legacy, scope):
    request = replay_cli._normalize_persisted_payroll_request(
        {
            "employee_items": [
                {
                    "employee_id": "E01",
                    "wage_tax_declaration_state": legacy,
                }
            ]
        }
    )

    assert request["employee_items"] == [{"employee_id": "E01", "wage_tax_scope": scope}]


def test_export_rejects_conflicting_legacy_payroll_scope():
    with pytest.raises(replay_cli.ReplayError, match="REPLAY_SOURCE_PAYROLL_SCOPE_INVALID"):
        replay_cli._normalize_persisted_payroll_request(
            {
                "employee_items": [
                    {
                        "employee_id": "E01",
                        "wage_tax_declaration_state": "declared",
                        "wage_tax_scope": "contributions_only",
                    }
                ]
            }
        )


def test_composite_payroll_event_triggers_policy_timeline_insertion():
    assert replay_cli._operation_replays_payroll(
        {
            "source_event_type": "composite",
            "preparations": [
                {
                    "key": "prepare:payroll",
                    "preview_tool": "finance_preview_payroll",
                }
            ],
        }
    )
    assert not replay_cli._operation_replays_payroll(
        {"source_event_type": "composite", "preparations": []}
    )


def test_export_retains_confirmed_no_wage_plan_with_stable_employee_reference(monkeypatch):
    org_id, employee_id = uuid.uuid4(), uuid.uuid4()
    items = [{"employee_id": str(employee_id), "wage_tax_scope": "contributions_only"}]

    def rows(_session, sql, **_parameters):
        if "FROM owner_period_confirmations" not in sql:
            return []
        return [
            {
                "fact_type": "workforce_review",
                "confirmation_state": "no_change",
                "confirmation_note": "已确认",
                "evidence_snapshot": [],
                "calendar_year": 2026,
                "calendar_month": 7,
                "source_snapshot": {
                    "regular_payroll_plan": {
                        "version": "regular_payroll_plan_v1",
                        "employee_items": items,
                    }
                },
            }
        ]

    monkeypatch.setattr(replay_cli, "_query_rows", rows)
    reference = {"$ref": "employee", "employee_code": "E01"}
    maps = {
        key: {}
        for key in (
            "income_tax",
            "evidence",
            "bank",
            "employee",
            "open_item",
            "component",
            "asset",
            "intangible",
            "labor_person",
            "borrowing",
            "event",
            "counterparty",
        )
    }
    maps["employee"][str(employee_id)] = reference
    operations = replay_cli._owner_control_operations(object(), org_id=org_id, maps=maps)
    assert operations[0]["regular_payroll_items"] == [
        {"employee_id": reference, "wage_tax_scope": "contributions_only"}
    ]


def test_export_migrates_legacy_workforce_wage_difference_with_batch_evidence(monkeypatch):
    org_id, employee_id = uuid.uuid4(), uuid.uuid4()
    evidence_reference = {"$ref": "evidence", "sha256": "a" * 64}

    def rows(_session, sql, **_parameters):
        if "FROM owner_period_confirmations" not in sql:
            return []
        return [
            {
                "fact_type": "workforce_review",
                "confirmation_state": "no_change",
                "confirmation_note": "已确认",
                "evidence_snapshot": [],
                "calendar_year": 2026,
                "calendar_month": 8,
                "source_snapshot": {
                    "regular_payroll_plan": {
                        "employee_items": [
                            {
                                "employee_id": str(employee_id),
                                "wage_tax_scope": "wage_income",
                                "tax_reported_salary_fen": 0,
                                "accounting_gross_salary_fen": 100,
                                "tax_reporting_difference_reason": None,
                            }
                        ]
                    }
                },
            }
        ]

    monkeypatch.setattr(replay_cli, "_query_rows", rows)
    maps = {
        key: {}
        for key in (
            "income_tax",
            "evidence",
            "bank",
            "employee",
            "open_item",
            "component",
            "asset",
            "intangible",
            "labor_person",
            "borrowing",
            "event",
            "counterparty",
        )
    }
    maps["employee"][str(employee_id)] = {
        "$ref": "employee",
        "employee_code": "E01",
    }
    event_operations = [
        {
            "preparations": [
                {
                    "preview_tool": "finance_preview_payroll",
                    "preview_request": {
                        "batch_kind": "regular",
                        "payroll_period": "2026-08",
                        "evidence_references": [evidence_reference],
                    },
                }
            ]
        }
    ]

    operations = replay_cli._owner_control_operations(
        object(),
        org_id=org_id,
        maps=maps,
        event_operations=event_operations,
    )

    assert operations[0]["evidence_references"] == [evidence_reference]
    assert operations[0]["regular_payroll_items"][0]["tax_reporting_difference_reason"].startswith(
        "历史空库重放："
    )


def test_metadata_replay_accounts_for_target_generated_initial_metadata():
    org_id = uuid.uuid4()
    event_id = uuid.uuid4()
    source_event_key = "source-event"
    target_event_key = replay_cli._replay_idempotency(
        replay_cli._semantic_replay_key(source_event_key)
    )
    row = SimpleNamespace(
        event_id=event_id,
        component_key="primary",
        version=1,
        metadata_values={"description": "原管理说明"},
    )
    session = SimpleNamespace(
        execute=lambda *_args, **_kwargs: SimpleNamespace(all=lambda: [(row, source_event_key)])
    )
    event_operations = [
        {
            "request": {
                "idempotency_key": target_event_key,
                "components": [
                    {
                        "key": "primary",
                        "kind": "expense",
                        "payment_basis": "person_advance",
                        "payment_date": "2026-01-02",
                    }
                ],
            }
        }
    ]

    maps = {
        key: {}
        for key in (
            "income_tax",
            "evidence",
            "bank",
            "employee",
            "open_item",
            "component",
            "asset",
            "intangible",
            "labor_person",
            "borrowing",
            "event",
            "counterparty",
        )
    }
    operations = replay_cli._metadata_operations(session, org_id, maps, event_operations)

    assert operations[0]["request"]["expected_version"] == 1
    assert operations[0]["request"]["metadata"] == {
        "advance_payment_date": None,
        "description": "原管理说明",
    }


def test_export_repreviews_specialized_depreciation_batch():
    operation = replay_cli._event_operation(
        object(),
        {
            "id": uuid.uuid4(),
            "idempotency_key": "asset-batch",
            "event_type": "fixed_asset_depreciation_batch",
            "description": "月折旧",
            "business_date": "2026-08-31",
            "posting_date": "2026-08-31",
            "created_at": "2026-09-01",
            "facts": {
                "depreciation_period": "2026-08",
                "posting_date": "2026-08-31",
            },
        },
        org_id=uuid.uuid4(),
        maps={
            key: {}
            for key in (
                "income_tax",
                "evidence",
                "bank",
                "employee",
                "open_item",
                "component",
                "asset",
                "intangible",
                "labor_person",
                "borrowing",
                "event",
                "counterparty",
            )
        },
    )
    assert operation["preview_tool"] == "finance_preview_fixed_asset_depreciation_batch"
    assert operation["confirm_tool"] == "finance_confirm_fixed_asset_depreciation_batch"
    assert operation["preview_request"]["depreciation_period"] == "2026-08"
