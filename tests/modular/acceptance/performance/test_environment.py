"""A long measurement must identify the code it started with, not just its last revision."""

import json

import pytest

from . import __main__ as cli
from . import environment as identity
from . import qualification


def put(root, relative, text="synthetic acceptance input\n"):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    for name in identity.INPUT_FILES:
        put(root, name)
    for name in (
        "src/ai_accounting_modular/public.py",
        "frontend/src/main.ts",
        "tests/modular/acceptance/performance/environment.py",
    ):
        put(root, name)
    monkeypatch.setattr(identity, "WORKSPACE", root)
    return root


@pytest.mark.parametrize("changed", (False, True))
def test_qualification_records_start_and_rejects_code_changes(monkeypatch, capsys, changed):
    calls = []
    initial = {"source_sha256": "before", "git_sha": "start-revision"}

    def environment():
        calls.append("before")
        return initial

    def run(root):
        calls.append("measurement")
        return {"status": "passed"}

    def finished():
        calls.append("after")
        return {"source_sha256": "after" if changed else "before"}

    monkeypatch.setattr(cli, "environment", environment)
    monkeypatch.setattr(cli, "code_identity", finished)
    monkeypatch.setattr(qualification, "run", run)
    monkeypatch.setattr("sys.argv", ["performance", "verify", "synthetic-case"])
    if changed:
        with pytest.raises(SystemExit, match="不能作为验收依据"):
            cli.main()
    else:
        cli.main()
    report = json.loads(capsys.readouterr().out)
    assert calls == ["before", "measurement", "after"]
    assert report["environment"] == initial
    assert report["source_unchanged"] is not changed
    assert report["code_after"] == {"source_sha256": "after" if changed else "before"}
    assert report["result"]["status"] == "passed"


@pytest.mark.parametrize("relative", identity.INPUT_FILES)
def test_every_selected_external_input_changes_its_real_fingerprint(workspace, relative):
    before = identity.code_identity()
    path = workspace / relative
    path.write_bytes(path.read_bytes() + b"\n")
    after = identity.code_identity()
    assert after["input_files_sha256"][relative] != before["input_files_sha256"][relative]
    assert {
        name: digest for name, digest in after["input_files_sha256"].items() if name != relative
    } == {name: digest for name, digest in before["input_files_sha256"].items() if name != relative}
    for field in ("source_sha256", "frontend_source_sha256", "measurement_sha256"):
        assert after[field] == before[field]


@pytest.mark.parametrize(
    "relative,field",
    (
        ("src/ai_accounting_modular/public.py", "source_sha256"),
        ("frontend/src/main.ts", "frontend_source_sha256"),
        ("frontend/src/api/generated/dashboardValidators.js", "frontend_source_sha256"),
        ("tests/modular/acceptance/_actions.py", "measurement_sha256"),
        ("tests/modular/acceptance/_files.py", "measurement_sha256"),
        ("tests/modular/acceptance/performance/environment.py", "measurement_sha256"),
    ),
)
def test_actual_source_and_acceptance_support_changes_are_covered(workspace, relative, field):
    path = put(workspace, relative)
    before = identity.code_identity()
    path.write_bytes(path.read_bytes() + b"\n")
    after = identity.code_identity()
    assert after[field] != before[field]
    assert after["input_files_sha256"] == before["input_files_sha256"]


@pytest.mark.parametrize("operation", ("add", "delete", "rename"))
def test_measurement_tree_binds_the_file_set_as_well_as_content(workspace, operation):
    path = put(workspace, "tests/modular/acceptance/_support.py")
    before = identity.code_identity()
    if operation == "add":
        put(workspace, "tests/modular/acceptance/browser_driver.py")
    elif operation == "delete":
        path.unlink()
    else:
        path.rename(path.with_name("_different_support.py"))
    assert identity.code_identity()["measurement_sha256"] != before["measurement_sha256"]


def test_unselected_files_and_python_cache_do_not_expand_the_scope(workspace):
    before = identity.code_identity()
    put(workspace, "README.md")
    put(workspace, "tests/modular/acceptance/__pycache__/generated.pyc")
    put(workspace, "tests/modular/acceptance/generated.pyc")
    assert identity.code_identity() == before


def test_missing_selected_input_cannot_be_an_empty_success(workspace):
    (workspace / "frontend/package-lock.json").unlink()
    with pytest.raises(FileNotFoundError):
        identity.code_identity()


def test_real_helper_change_makes_the_default_verify_command_reject(workspace, monkeypatch, capsys):
    def run(root):
        path = workspace / "frontend/tests/helpers/dashboard-select.cjs"
        path.write_bytes(path.read_bytes() + b"\n")
        return {"status": "passed"}

    monkeypatch.setattr(cli, "environment", identity.code_identity)
    monkeypatch.setattr(cli, "code_identity", identity.code_identity)
    monkeypatch.setattr(qualification, "run", run)
    monkeypatch.setattr("sys.argv", ["performance", "verify", "synthetic-case"])
    with pytest.raises(SystemExit, match="不能作为验收依据"):
        cli.main()
    report = json.loads(capsys.readouterr().out)
    assert report["source_unchanged"] is False
    key = "frontend/tests/helpers/dashboard-select.cjs"
    assert (
        report["environment"]["input_files_sha256"][key]
        != report["code_after"]["input_files_sha256"][key]
    )


def test_explicit_static_digest_includes_every_actual_asset(workspace, tmp_path):
    static = tmp_path / "release"
    put(static, "index.html")
    asset = put(static, "__pycache__/actual-chunk.js")
    before = identity.code_identity(static_directory=static)
    asset.write_bytes(asset.read_bytes() + b"\n")
    after = identity.code_identity(static_directory=static)
    assert after["static_directory"]["sha256"] != before["static_directory"]["sha256"]


@pytest.mark.parametrize("changed", (False, True))
def test_explicit_static_directory_is_resolved_before_start_run_and_finish(
    workspace, tmp_path, monkeypatch, capsys, changed
):
    from . import browser

    static = tmp_path / "release"
    page = put(static, "index.html")
    calls = []

    def before(*, static_directory):
        calls.append(("before", static_directory))
        return identity.code_identity(static_directory=static_directory)

    def run(root, mode, *, static_directory, **options):
        calls.append(("run", static_directory))
        if changed:
            page.write_bytes(page.read_bytes() + b"\n")
        return {"status": "passed"}

    def after(*, static_directory):
        calls.append(("after", static_directory))
        return identity.code_identity(static_directory=static_directory)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "environment", before)
    monkeypatch.setattr(cli, "code_identity", after)
    monkeypatch.setattr(browser, "run", run)
    monkeypatch.setattr(
        "sys.argv",
        [
            "performance",
            "browser",
            "synthetic-case",
            "--node",
            "synthetic-node",
            "--playwright-module",
            "synthetic-playwright",
            "--static-directory",
            "release",
        ],
    )
    if changed:
        with pytest.raises(SystemExit, match="不能作为验收依据"):
            cli.main()
    else:
        cli.main()
    assert calls == [(phase, static.resolve()) for phase in ("before", "run", "after")]
    report = json.loads(capsys.readouterr().out)
    assert report["source_unchanged"] is not changed
    assert report["environment"]["static_directory"]["path"] == str(static.resolve())
    assert report["code_after"]["static_directory"]["path"] == str(static.resolve())
    assert (
        report["environment"]["static_directory"]["sha256"]
        == report["code_after"]["static_directory"]["sha256"]
    ) is not changed
