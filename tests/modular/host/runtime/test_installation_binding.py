"""Installed catalogs select the same whole runtime before any command executes."""

import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.host.runtime import _client, _installation, cli
from ai_accounting_modular.storage.public import OwnerProfile, Storage, create_catalog


@pytest.fixture
def installation(tmp_path):
    catalog = tmp_path / "合成目录𠮷.sqlite3"
    create_catalog(catalog, OwnerProfile(str(uuid4()), "合成负责人", "synthetic-hash"))
    python = tmp_path / "installed-python.exe"
    python.write_bytes(b"synthetic executable placeholder")
    binding = catalog.with_name(catalog.name + ".runtime.json")
    value = {
        "format": _installation.FORMAT,
        "catalog_path": str(catalog),
        "catalog_id": Storage(catalog).catalog_identity(),
        "python_path": str(python),
    }
    binding.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return catalog, python, binding, value


def test_binding_is_optional_and_never_creates_a_catalog(tmp_path):
    assert _installation.bound_python(tmp_path / "absent.sqlite3") is None
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(ContractError) as failed:
        _installation.bound_python(Path("relative.sqlite3"))
    assert failed.value.code == "invalid_path"


def test_binding_selects_canonical_installed_python_and_preserves_catalog(installation):
    catalog, python, binding, _ = installation
    before = catalog.read_bytes(), binding.read_bytes()
    assert _installation.bound_python(catalog) == python.resolve()
    assert (catalog.read_bytes(), binding.read_bytes()) == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("format", "unknown-runtime/1"),
        ("catalog_id", str(uuid4())),
        ("catalog_path", "relative.sqlite3"),
        ("python_path", "relative-python.exe"),
        ("python_path", None),
        ("unexpected", "unused"),
    ],
)
def test_binding_rejects_invalid_fields_without_falling_back(installation, field, value):
    catalog, _, binding, document = installation
    document[field] = value
    binding.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ContractError) as failed:
        _installation.bound_python(catalog)
    assert failed.value.code == "invalid_runtime_binding"


def test_binding_rejects_another_catalog_path_and_missing_python(installation, tmp_path):
    catalog, python, binding, document = installation
    document["catalog_path"] = str(tmp_path / "other.sqlite3")
    binding.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ContractError, match="运行包绑定"):
        _installation.bound_python(catalog)
    document["catalog_path"] = str(catalog)
    binding.write_text(json.dumps(document), encoding="utf-8")
    python.unlink()
    with pytest.raises(ContractError, match="运行包绑定"):
        _installation.bound_python(catalog)


@pytest.mark.parametrize("raw", [b"[", b"[]", b"{}", b" " * 8193, b' {"format":1,"format":2}'])
def test_binding_rejects_damaged_or_oversized_documents(installation, raw):
    catalog, _, binding, _ = installation
    binding.write_bytes(raw)
    with pytest.raises(ContractError) as failed:
        _installation.bound_python(catalog)
    assert failed.value.code == "invalid_runtime_binding"


def test_forwarding_preserves_arguments_streams_and_exit_status(installation, monkeypatch):
    catalog, python, _, _ = installation
    arguments = ["finance-modular", "--catalog", str(catalog), "mcp"]
    monkeypatch.setattr(sys, "argv", arguments)
    calls = []

    def launch(command, **options):
        calls.append((command, options))
        return 17

    monkeypatch.setattr(_installation.subprocess, "call", launch)
    assert _installation.forward_bound_runtime(catalog) == 17
    assert calls == [
        (
            [
                str(python.resolve()),
                "-I",
                "-B",
                "-X",
                "utf8",
                "-m",
                "ai_accounting_modular.host.runtime.cli",
                *arguments[1:],
            ],
            {
                "stdin": sys.stdin,
                "stdout": sys.stdout,
                "stderr": sys.stderr,
                "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
            },
        )
    ]


def test_bound_interpreter_continues_without_recursion(installation, monkeypatch):
    catalog, python, _, _ = installation
    monkeypatch.setattr(sys, "executable", str(python))

    def forbidden(*args, **kwargs):
        raise AssertionError("the bound interpreter must continue directly")

    monkeypatch.setattr(_installation.subprocess, "call", forbidden)
    assert _installation.forward_bound_runtime(catalog) is None


def test_unusable_bound_interpreter_returns_a_location_error(installation, monkeypatch):
    catalog, _, _, _ = installation

    def unavailable(*args, **kwargs):
        raise OSError("synthetic unavailable interpreter")

    monkeypatch.setattr(_installation.subprocess, "call", unavailable)
    with pytest.raises(ContractError) as failed:
        _installation.forward_bound_runtime(catalog)
    assert failed.value.code == "invalid_runtime_binding"


@pytest.mark.parametrize(
    "command",
    [
        ["serve"],
        ["daemon"],
        ["mcp"],
        ["call", "accounting_context"],
        ["company-create"],
        ["company-register"],
        ["service-info"],
        ["stop"],
        ["security", "status"],
        ["dev-proxy-info"],
    ],
)
def test_cli_forwards_before_reading_input_or_creating_clients(
    tmp_path, monkeypatch, capsys, command
):
    catalog = tmp_path / "catalog.sqlite3"
    monkeypatch.setattr(sys, "argv", ["finance-modular", "--catalog", str(catalog), *command])
    calls = []

    def forward(selected):
        calls.append(selected)
        return 17

    def forbidden(*args, **kwargs):
        raise AssertionError("the source runtime must not execute the bound command")

    monkeypatch.setattr(_installation, "forward_bound_runtime", forward)
    monkeypatch.setattr(_client, "Client", forbidden)
    monkeypatch.setattr(cli, "_input", forbidden)
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 17
    assert calls == [catalog]
    assert capsys.readouterr().out == ""
    assert list(tmp_path.iterdir()) == []


def test_actual_cli_rejects_broken_binding_without_starting_service(installation):
    catalog, _, binding, _ = installation
    binding.write_text("{}", encoding="utf-8")
    before = {path.name: path.read_bytes() for path in catalog.parent.iterdir()}
    process = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-X",
            "utf8",
            "-m",
            "ai_accounting_modular.host.runtime.cli",
            "--catalog",
            str(catalog),
            "service-info",
        ],
        capture_output=True,
        timeout=20,
    )
    assert process.returncode == 1 and process.stderr == b""
    assert json.loads(process.stdout)["code"] == "invalid_runtime_binding"
    assert {path.name: path.read_bytes() for path in catalog.parent.iterdir()} == before


def test_actual_forwarded_process_preserves_stdio_and_nonzero_exit(installation, tmp_path):
    catalog, _, binding, document = installation
    environment = tmp_path / "independent-runtime"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(environment)],
        check=True,
        capture_output=True,
        timeout=30,
    )
    python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    location = subprocess.run(
        [str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        check=True,
        capture_output=True,
        timeout=20,
    )
    package = Path(location.stdout.decode().strip()) / "ai_accounting_modular/host/runtime"
    package.mkdir(parents=True)
    for directory in (package, package.parent, package.parent.parent):
        (directory / "__init__.py").write_text("", encoding="utf-8")
    (package / "cli.py").write_text(
        "import json, sys\n"
        "print(json.dumps({'argv':sys.argv[1:], 'input':sys.stdin.read(), "
        "'python':sys.executable}, ensure_ascii=False))\n"
        "sys.stderr.buffer.write(b'synthetic forwarded stderr\\n')\n"
        "raise SystemExit(17)\n",
        encoding="utf-8",
    )
    document["python_path"] = str(python)
    binding.write_text(json.dumps(document), encoding="utf-8")
    arguments = ["--catalog", str(catalog), "call", "accounting_context"]
    body = '{"合成请求":"原样传递"}\n'
    process = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-X",
            "utf8",
            "-m",
            "ai_accounting_modular.host.runtime.cli",
            *arguments,
        ],
        input=body.encode("utf-8"),
        capture_output=True,
        timeout=30,
    )
    assert process.returncode == 17
    assert json.loads(process.stdout) == {"argv": arguments, "input": body, "python": str(python)}
    assert process.stderr == b"synthetic forwarded stderr\n"
