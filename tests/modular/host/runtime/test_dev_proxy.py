"""The development bridge is a read-only CLI contract, not a private Python import."""

import json
import subprocess
import sys
from hashlib import sha256
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.host.runtime import _client, _http, _state
from ai_accounting_modular.storage.public import OwnerProfile, create_catalog


def run_cli(*arguments):
    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-X",
            "utf8",
            "-m",
            "ai_accounting_modular.host.runtime.cli",
            *map(str, arguments),
        ],
        capture_output=True,
        timeout=20,
    )


def rejected(process, code, capability=""):
    assert process.returncode == 1, process.stderr
    assert process.stderr == b""
    result = json.loads(process.stdout)
    assert result["status"] == "rejected" and result["code"] == code
    if capability:
        assert capability.encode() not in process.stdout + process.stderr
    return result


def test_real_cli_exposes_only_verified_development_fields_and_service_info_stays_safe(local):
    runtime, _, _ = local
    runtime.start()
    state_path = _state.state_path(runtime.catalog_path)
    _state.publish(runtime.catalog_path, runtime.metadata | {"unrelated_private": "discarded"})
    state = state_path.read_bytes()
    catalog = runtime.catalog_path.read_bytes()
    process = run_cli("--catalog", runtime.catalog_path, "dev-proxy-info")
    assert process.returncode == 0, process.stderr
    assert process.stderr == b""
    assert json.loads(process.stdout) == {
        **runtime.metadata,
        "state_path": str(state_path),
        "state_fingerprint": sha256(state).hexdigest(),
    }
    assert state_path.read_bytes() == state
    assert runtime.catalog_path.read_bytes() == catalog
    if sys.platform == "win32":
        assert runtime.capability.encode() not in state
    ordinary = run_cli("--catalog", runtime.catalog_path, "service-info")
    assert ordinary.returncode == 0, ordinary.stderr
    assert json.loads(ordinary.stdout) == runtime.info()
    assert runtime.capability.encode() not in ordinary.stdout + ordinary.stderr


def test_real_cli_without_service_never_starts_or_creates_one(tmp_path):
    catalog = tmp_path / "目录𠮷🙂.sqlite3"
    create_catalog(catalog, OwnerProfile(str(uuid4()), "合成负责人", "synthetic-hash"))
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    try:
        rejected(run_cli("--catalog", catalog, "dev-proxy-info"), "service_unavailable")
        assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before
    finally:
        # A regression must not leave an accidentally started synthetic resident running.
        if _state.state_path(catalog).exists():
            _client.Client(catalog, autostart=False).stop()


@pytest.mark.parametrize("arguments", [(), ("--catalog", "relative.sqlite3")])
def test_real_cli_requires_an_explicit_absolute_catalog(arguments):
    rejected(run_cli(*arguments, "dev-proxy-info"), "invalid_path")


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("catalog_id", "another-catalog", "service_identity_mismatch"),
        ("protocol", "another-service/1", "service_state_invalid"),
        ("catalog_path", "another-directory.sqlite3", "service_state_invalid"),
    ],
)
def test_real_cli_rejects_invalid_state_before_contacting_its_endpoint(
    local, monkeypatch, field, value, code
):
    runtime, _, _ = local
    runtime.start()
    calls = []
    private = runtime.private

    def observed(path, body):
        calls.append(path)
        return private(path, body)

    monkeypatch.setattr(runtime, "private", observed)
    _state.publish(runtime.catalog_path, runtime.metadata | {field: value})
    rejected(run_cli("--catalog", runtime.catalog_path, "dev-proxy-info"), code, runtime.capability)
    assert calls == []


@pytest.mark.parametrize("field", ["catalog_id", "protocol"])
def test_real_cli_rejects_a_live_service_with_a_different_identity_or_protocol(
    local, monkeypatch, field
):
    runtime, _, _ = local
    runtime.start()
    if field == "protocol":
        monkeypatch.setattr(_http, "PROTOCOL", "another-live-service/1")
    else:
        monkeypatch.setattr(runtime, "catalog_id", "another-live-catalog")
    rejected(
        run_cli("--catalog", runtime.catalog_path, "dev-proxy-info"),
        "service_unavailable",
        runtime.capability,
    )


@pytest.mark.parametrize("change", ["replace", "delete"])
def test_real_cli_rejects_state_replaced_or_deleted_during_its_single_live_probe(
    local, monkeypatch, change
):
    runtime, _, _ = local
    runtime.start()
    private = runtime.private
    calls = []

    def altered(path, body):
        calls.append(path)
        assert path == "/private/info"
        if change == "replace":
            _state.publish(
                runtime.catalog_path,
                runtime.metadata | {"capability": "replacement-" + runtime.capability},
            )
        else:
            _state.state_path(runtime.catalog_path).unlink()
        return private(path, body)

    monkeypatch.setattr(runtime, "private", altered)
    rejected(
        run_cli("--catalog", runtime.catalog_path, "dev-proxy-info"),
        "service_unavailable",
        runtime.capability,
    )
    assert calls == ["/private/info"]


def test_probe_path_belongs_to_runtime_and_is_not_inferred_from_metadata(local, monkeypatch):
    runtime, _, _ = local
    state_path = runtime.catalog_path.with_name("connection-probe.bin")
    monkeypatch.setattr(_state, "state_path", lambda _: state_path)
    runtime.start()
    _state.publish(runtime.catalog_path, runtime.metadata | {"state_path": "untrusted-path"})
    value = _client.development_proxy_info(runtime.catalog_path)
    assert value["state_path"] == str(state_path)
    assert value["state_fingerprint"] == sha256(state_path.read_bytes()).hexdigest()


def test_state_link_is_rejected_before_the_development_bridge_hashes_its_target(local, monkeypatch):
    runtime, _, _ = local
    target = runtime.catalog_path.with_name("unrelated-private-file")
    target.write_bytes(b"unrelated private content")
    state_path = _state.state_path(runtime.catalog_path)
    try:
        state_path.symlink_to(target)
    except OSError:
        pytest.skip("This host does not permit synthetic file symlinks.")

    def forbidden(_):
        pytest.fail("An unverified state path must never be hashed.")

    monkeypatch.setattr(_client, "sha256", forbidden)
    for operation in (_state.read, _client.development_proxy_info):
        with pytest.raises(ContractError) as failed:
            operation(runtime.catalog_path)
        assert failed.value.code == "invalid_path"
    rejected(run_cli("--catalog", runtime.catalog_path, "dev-proxy-info"), "invalid_path")
    assert target.read_bytes() == b"unrelated private content"


@pytest.mark.parametrize("size", [65536, 65537])
def test_state_size_limit_is_shared_by_the_existing_reader_and_development_probe(
    local, monkeypatch, size
):
    runtime, _, _ = local
    state_path = _state.state_path(runtime.catalog_path)
    raw = b"x" * size
    state_path.write_bytes(raw)
    state_path.chmod(0o600)
    hashed = []

    def observed(value):
        hashed.append(len(value))
        return sha256(value)

    monkeypatch.setattr(_client, "sha256", observed)
    for operation in (_state.read, _client.development_proxy_info):
        with pytest.raises(ContractError) as failed:
            operation(runtime.catalog_path)
        assert failed.value.code == "service_state_invalid"
    assert hashed == ([size] if size == 65536 else [])
    rejected(run_cli("--catalog", runtime.catalog_path, "dev-proxy-info"), "service_state_invalid")
    assert state_path.read_bytes() == raw
