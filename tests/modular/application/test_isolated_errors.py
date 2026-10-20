"""Complete offline diagnostics remain valid even for company-sized failures."""

import pytest

from ai_accounting_modular.application import _isolated
from ai_accounting_modular.application._target_paths import beneath
from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.host.public import create_offline_application


def run_failure(tmp_path, monkeypatch, error):
    app = create_offline_application()

    def fail(_):
        raise error

    monkeypatch.setattr(_isolated, "open_standard_bundle", fail)
    target = tmp_path / "not-created"
    result = app.accounting_isolated(
        {
            "action": "rebuild",
            "mode": "run",
            "source_path": str(tmp_path / "standard/current.json"),
            "new_target_directory": str(target),
        }
    )
    assert result["state"] == "failed"
    assert not target.exists()
    return result


def test_more_than_ten_thousand_missing_facts_preserve_every_field_and_diagnostic(
    tmp_path, monkeypatch
):
    issues = [
        {
            "code": "missing_value",
            "field": f"/facts/{index}/data/creditor_id",
            "meaning": "明确的债权人",
            "message": f"缺项编号{index}。",
            "allowed_precision": ["identity"],
            "reusable_sources": [],
            "blocking_scope": "business",
        }
        for index in range(10001)
    ]
    result = run_failure(
        tmp_path,
        monkeypatch,
        ContractError("needs_information", "尚有明确业务缺项。", fact_issues=issues),
    )
    assert len(result["checks"]) == 10001
    for index, check in enumerate(result["checks"]):
        assert check["message"] == f"字段：/facts/{index}/data/creditor_id\n缺项编号{index}。"


def test_long_error_message_is_complete_in_bounded_parts_and_top_message_is_short(
    tmp_path, monkeypatch
):
    text = "错误资料：" + "逐项保全甲乙丙。" * 1000
    result = run_failure(tmp_path, monkeypatch, ContractError("dependency_conflict", text))
    assert len(result["message"]) <= 2000
    assert len(result["checks"]) > 1
    assert all(len(item["message"]) <= 2000 for item in result["checks"])
    assert "".join(item["message"].split("：", 1)[1] for item in result["checks"]) == text


def test_long_issue_preserves_business_and_field_context_without_losing_message(
    tmp_path, monkeypatch
):
    message = "明确信息" * 499
    business = "00000000-0000-4000-8000-000000000001"
    issue = {
        "code": "missing_value",
        "business_id": business,
        "field": "/data/creditor_id",
        "meaning": "债权人",
        "message": message,
        "allowed_precision": ["identity"],
        "reusable_sources": [],
        "blocking_scope": "business",
    }
    result = run_failure(
        tmp_path,
        monkeypatch,
        ContractError("needs_information", "缺少真实资料。", fact_issues=[issue]),
    )
    assert len(result["checks"]) == 2
    assert (
        "".join(item["message"].split("：", 1)[1] for item in result["checks"])
        == f"业务：{business}\n字段：/data/creditor_id\n{message}"
    )


@pytest.mark.parametrize("field", ["source_path", "new_target_directory"])
def test_null_character_paths_return_a_structured_rejection(tmp_path, field):
    app = create_offline_application()
    request = {
        "action": "rebuild",
        "mode": "run",
        "source_path": str(tmp_path / "standard/current.json"),
        "new_target_directory": str(tmp_path / "target"),
    }
    request[field] += "\x00hidden"
    result = app.accounting_isolated(request)
    assert result["state"] == "failed"
    assert result["error"] == "invalid_path"
    assert not (tmp_path / "target").exists()


@pytest.mark.parametrize(
    "relative",
    ["\x00", "standard/name\x00.json", "standard/../outside", "standard//double", ".", ""],
)
def test_controlled_material_paths_reject_ambiguous_or_nonchild_locations(tmp_path, relative):
    with pytest.raises(ContractError, match="规范相对路径"):
        beneath(tmp_path, relative)
