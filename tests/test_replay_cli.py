from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.engine import make_url

from ai_accounting import replay_cli


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
    assert replay_cli._BUSINESS_REVISION == "0001_business_baseline_v3"
    assert replay_cli._current_schema_revision(catalog=False) == "0003_essential_accounting"
    assert replay_cli._current_schema_revision(catalog=True) == "0001_catalog_baseline_v2"


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
