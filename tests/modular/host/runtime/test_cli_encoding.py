"""Machine JSON is UTF-8 even when the local text stream uses strict GBK."""

import json
import os
import subprocess
import sys
from hashlib import sha256
from io import StringIO
from pathlib import Path

import pytest

from ai_accounting_modular.host.runtime import cli

from .conftest import business_request


@pytest.fixture
def backup(local):
    runtime, company, _ = local
    assert (
        runtime.kernel.application.accounting_apply(business_request(company.company_id))[
            "completion"
        ]
        == "complete"
    )
    receipt = runtime.kernel.application.accounting_backup(
        {"company_id": company.company_id, "expected_database_id": company.database_id}
    )
    result = runtime.kernel.files.run(company.company_id, receipt["task_id"])
    assert result["state"] == "succeeded", result
    source = Path(result["path"])
    return source, sha256(source.read_bytes()).hexdigest()


def run_gbk_cli(tmp_path, *arguments):
    source = Path(cli.__file__).resolve()
    environment = os.environ.copy()
    environment.pop("PYTHONUTF8", None)
    environment.pop("PYTHONHOME", None)
    environment["PYTHONIOENCODING"] = "gbk:strict"
    environment["PYTHONPATH"] = str(source.parents[3])
    # The real CLI and real command execute under the requested checkout, without
    # -I/-X utf8 or a UTF-8 text wrapper concealing the local encoding boundary.
    bootstrap = """
import sys
from pathlib import Path
from ai_accounting_modular.host.runtime import cli
assert Path(cli.__file__).resolve() == Path(sys.argv.pop(1)).resolve()
assert sys.stdout.encoding.lower() == 'gbk'
cli.main()
"""
    return subprocess.run(
        [sys.executable, "-c", bootstrap, str(source), *arguments],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        timeout=30,
    )


def json_output(process, exit_code):
    assert process.returncode == exit_code, process.stderr
    assert process.stderr == b""
    text = process.stdout.decode("utf-8")
    value = json.loads(text)
    assert text == json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    return value


@pytest.mark.parametrize("name", ("中文核验目标", "生僻字𠮷和表情🙂目标"))
def test_real_offline_check_emits_complete_utf8_under_gbk_stdout(tmp_path, backup, name):
    source, digest = backup
    target = tmp_path / name
    request = tmp_path / "restore.json"
    request.write_text(
        json.dumps(
            {
                "action": "restore",
                "source_path": str(source),
                "new_target_directory": str(target),
                "mode": "check",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    value = json_output(run_gbk_cli(tmp_path, "restore", "--input", str(request)), 0)
    assert value["state"] == "checked" and value["action"] == "restore"
    assert value["new_target_directory"] == str(target)
    assert value["source_digest"] == digest
    assert value["checks"] == [
        {"code": "complete_target", "state": "pass", "message": "完整资料及正式账务核验通过。"}
    ]
    assert not target.exists()
    assert sha256(source.read_bytes()).hexdigest() == digest


def test_real_rejected_response_is_utf8_and_preserves_exit_one(tmp_path):
    value = json_output(run_gbk_cli(tmp_path, "ids", "--count", "0"), 1)
    assert value["status"] == "rejected" and value["code"] == "invalid_input"
    assert "数量" in value["message"]
    assert list(tmp_path.iterdir()) == []


def test_real_failed_offline_response_is_utf8_and_preserves_exit_one(tmp_path):
    target = tmp_path / "未创建𠮷🙂目标"
    request = tmp_path / "restore.json"
    request.write_text(
        json.dumps(
            {
                "action": "restore",
                "source_path": str(tmp_path / "missing.zip"),
                "new_target_directory": str(target),
                "mode": "check",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    value = json_output(run_gbk_cli(tmp_path, "restore", "--input", str(request)), 1)
    assert value["state"] == "failed" and value["error"] == "source_unavailable"
    assert value["new_target_directory"] == str(target)
    assert value["message"] == "离线资料读取或目标交付未完成。"
    assert not target.exists()


@pytest.mark.parametrize("count", (2, 0))
def test_programmatic_text_only_capture_keeps_json_and_exit_semantics(monkeypatch, count):
    output = StringIO()
    assert not hasattr(output, "buffer")
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(sys, "argv", ["finance-modular", "ids", "--count", str(count)])
    if count:
        cli.main()
    else:
        with pytest.raises(SystemExit) as failed:
            cli.main()
        assert failed.value.code == 1
    value = json.loads(output.getvalue())
    assert output.getvalue() == json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if count:
        assert len(value["ids"]) == count
    else:
        assert value["status"] == "rejected" and value["code"] == "invalid_input"
