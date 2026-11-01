"""Management-only channel evidence survives both supported recovery paths."""

from uuid import uuid4

import pytest

from ai_accounting_modular.host.public import create_offline_application

from ._working_case import case as case
from .conftest import source
from .test_isolated_targets import read_target, request, source_bundle


@pytest.mark.parametrize("action", ["rebuild", "restore"])
def test_channel_and_nested_source_survive_standard_rebuild_and_verified_backup(
    case, tmp_path, action
):
    case.seed(expense=False)
    evidence = source("公司自有云钱包未管理余额，本次转回库存现金100元。")
    cash = str(uuid4())
    channel = {"name": "云钱包", "source": {"source_id": evidence["source_id"]}}
    fact = {
        "business_id": str(uuid4()),
        "kind": "money.movement",
        "period": "2026-01",
        "effective_on": "2026-01-10",
        "evidence_ids": [case.source_id],
        "data": {
            "type": "managed_reserve",
            "direction": "inflow",
            "amount_fen": "10000",
            "funds_account_id": cash,
            "own_funds_channel": channel,
        },
    }
    saved = case.app.accounting_apply(
        case.request(
            fact,
            sources=[evidence],
            objects=[
                {
                    "object_id": cash,
                    "kind": "cash_account",
                    "name": "合成库存现金",
                    "expected_seq": 0,
                    "data": {"currency": "CNY"},
                }
            ],
        )
    )
    assert saved["completion"] == "complete", saved
    metadata, bundle = source_bundle(case)
    origin = bundle.manifest_path
    if action == "restore":
        pending = case.app.accounting_backup(
            {
                "company_id": case.company,
                "expected_database_id": metadata.database_id,
            }
        )
        done = case.kernel.files.run(case.company, pending["task_id"])
        assert done["state"] == "succeeded", done
        origin = done["path"]
    offline = create_offline_application()
    target = tmp_path / action
    result = offline.accounting_isolated(request(action, "run", origin, target))
    assert result["state"] == "created", result
    with read_target(offline, target) as read:
        references = offline.ledger.get_result_refs([fact["business_id"]], read)
        assert offline.money.own_funds_channels(references, read) == {
            (references[0]["result_id"], references[0]["generation"]): channel,
        }
        source_rows = offline.records.resolve_sources([evidence["source_id"]], read)
        assert source_rows[0]["text"] == evidence["text"]
        assert offline.records.document_status(read)["state"] == "ready"
