from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy.engine import make_url

from ai_accounting import replay_cli


def test_replay_orders_non_primary_company_before_primary() -> None:
    companies = [
        {"org_id": "primary", "display_name": "魂道", "is_primary": True},
        {"org_id": "secondary", "display_name": "屋舍心声", "is_primary": False},
    ]

    assert [
        company["org_id"] for company in replay_cli._ordered_replay_companies(companies)
    ] == ["secondary", "primary"]


def test_financial_statement_normalization_format_is_typed(tmp_path) -> None:
    path = tmp_path / "normalizations.json"
    path.write_text(
        json.dumps(
            {
                "format_version": "ai-accounting-replay-normalizations-v1",
                "companies": [
                    {
                        "org_id": "74299243-c333-43d9-9807-4f2336cd984c",
                        "controls": [
                            {
                                "kind": "financial_statement_classification",
                                "period_month": "2026-07",
                                "source_replay_key": "petty-expense-v1",
                                "line_number": 1,
                                "account_code": "5602",
                                "debit_fen": 500000,
                                "credit_fen": 0,
                                "allocations": [
                                    {
                                        "detail_code": "management_other",
                                        "amount_fen": 500000,
                                    }
                                ],
                                "source_assertion": "负责人已确认计入管理费用",
                                "confirmation_note": "按源业务事实补齐报表明细分类。",
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    controls = replay_cli._load_export_normalizations(path)

    assert controls["74299243-c333-43d9-9807-4f2336cd984c"][0]["kind"] == (
        "financial_statement_classification"
    )


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

    with pytest.raises(replay_cli.ReplayError) as error:
        replay_cli.replay_system(package, state_file)

    assert error.value.code == "REPLAY_AUTHENTICATION_REQUIRED"
