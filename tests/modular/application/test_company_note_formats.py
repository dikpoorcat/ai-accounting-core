"""AI writes, offline final-fact rebuild and exact ZIP restore share one note body."""

import pytest

from ai_accounting_modular.host.public import create_offline_application
from ai_accounting_modular.records.public import open_standard_bundle

from ._working_case import case as case
from .test_isolated_targets import read_target
from .test_isolated_targets import request as isolated_request
from .test_restore_proof import package


@pytest.mark.parametrize("text_format", ["plain_text", "markdown"])
def test_company_note_format_survives_real_ai_export_rebuild_and_exact_restore(
    case, tmp_path, text_format
):
    case.seed()
    note = {
        "scope": "company",
        "expected_seq": 0,
        "text": "# 业务安排\n\n- 保留**当前**正文和来源。\n- 未关账错误直接更正。",
        "text_format": text_format,
        "evidence_ids": [case.source_id],
    }
    response = case.app.accounting_apply(case.request(notes=[note]))
    assert response["completion"] == "complete" and response["accounting"] == "unchanged", response
    saved = case.context(note_scopes=["company"], issue_scopes=[])["notes"][0]
    assert saved["text"] == note["text"] and saved["seq"] == 1
    if text_format == "markdown":
        assert saved["text_format"] == "markdown"
    else:
        assert "text_format" not in saved
    with case.kernel.storage.read_unit(case.company) as read:
        identity = (read.company_id, read.database_id)
        root = case.kernel.storage.company_metadata(read).company_root
        before_head = read.head
        before_set = case.kernel.records.document_status(read)["set_id"]
    retry = dict(note)
    if text_format == "plain_text":
        retry.pop("text_format")
    assert case.app.accounting_apply(case.request(notes=[retry]))["changes"] == "none"
    with case.kernel.storage.read_unit(case.company) as read:
        assert read.head == before_head
        assert case.kernel.records.document_status(read)["set_id"] == before_set
    bundle = open_standard_bundle(root / "standard/current.json")
    assert bundle.notes == (
        {
            "scope": "company",
            "text": note["text"],
            "evidence_ids": [case.source_id],
            **({"text_format": "markdown"} if text_format == "markdown" else {}),
        },
    )
    archive = package(case, seed=False)
    offline = create_offline_application()
    for action, source in (("rebuild", bundle.manifest_path), ("restore", archive)):
        target = tmp_path / action
        result = offline.accounting_isolated(isolated_request(action, "run", source, target))
        assert result["state"] == "created", result
        with read_target(offline, target) as read:
            actual = offline.records.current_notes(("company",), read)[0]
            assert actual == saved
            if action == "restore":
                assert (read.company_id, read.database_id) == identity
            else:
                assert read.company_id != identity[0] and read.database_id != identity[1]
