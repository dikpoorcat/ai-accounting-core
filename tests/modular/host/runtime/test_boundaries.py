"""Catalog residents and browser cookies stay independent of other local services."""

import json
import os
import time
from http.cookiejar import Cookie, CookieJar
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.host.runtime import _client, _state
from ai_accounting_modular.host.runtime._client import Client
from ai_accounting_modular.host.runtime.public import create_runtime
from ai_accounting_modular.storage.public import OwnerProfile, create_catalog

from .conftest import native_login


def independent(runtime, name, *, port=0):
    catalog = runtime.catalog_path.with_name(name + ".sqlite3")
    create_catalog(
        catalog,
        OwnerProfile(
            str(uuid4()), "另一合成负责人", runtime.kernel.storage.owner_profile().password_hash
        ),
    )
    return create_runtime(catalog, port=port, window_opener=lambda _: None)


def legacy_cookie(name, value):
    return Cookie(
        version=0,
        name=name,
        value=value,
        port=None,
        port_specified=False,
        domain="127.0.0.1",
        domain_specified=False,
        domain_initial_dot=False,
        path="/",
        path_specified=True,
        secure=False,
        expires=None,
        discard=True,
        comment=None,
        comment_url=None,
        rest={"HttpOnly": None, "SameSite": "Strict"},
    )


def post(browser, runtime, path, payload):
    origin = f"http://127.0.0.1:{runtime.server.server_port}"
    request = Request(
        origin + path,
        json.dumps(payload).encode(),
        {"Content-Type": "application/json", "Origin": origin},
    )
    try:
        with browser.open(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        return error.code, json.loads(error.read())


def launch(browser, runtime):
    ticket = parse_qs(urlsplit(runtime.browser_url()).fragment)["ticket"][0]
    status, response = post(browser, runtime, "/api/browser-session", {"ticket": ticket})
    assert status == 200 and response["authenticated"], response


def test_one_browser_preserves_legacy_cookies_and_both_catalog_sessions(local):
    first, _, _ = local
    first.start()
    native_login(first)
    jar = CookieJar()
    legacy = {
        "finance_session": "synthetic-legacy-session",
        "finance_surface": "synthetic-legacy-surface",
    }
    for name, value in legacy.items():
        jar.set_cookie(legacy_cookie(name, value))
    browser = build_opener(HTTPCookieProcessor(jar))
    launch(browser, first)
    with independent(first, "second-catalog") as second:
        native_login(second)
        launch(browser, second)
        for runtime in (first, second, first):
            status, response = post(
                browser,
                runtime,
                "/api/security-request",
                {"operation": "session_status", "payload": {}},
            )
            assert status == 200 and response["authenticated"], response
            assert response["catalog_instance_id"] == runtime.catalog_id
    first.close()
    with create_runtime(first.catalog_path, window_opener=lambda _: None) as restarted:
        native_login(restarted)
        launch(browser, restarted)
    cookies = {cookie.name: cookie.value for cookie in jar}
    assert {key: cookies[key] for key in legacy} == legacy
    assert len(cookies) == 6


def test_stale_port_reused_by_another_catalog_starts_only_the_original_new_resident(local):
    first, _, _ = local
    first.start()
    stale = first.metadata
    first.close()
    with independent(first, "port-reuser", port=stale["port"]) as second:
        # A crashed process leaves precisely this valid, protected connection document.
        _state.publish(first.catalog_path, stale)
        client = Client(first.catalog_path)
        try:
            current = client.info()
            assert current["catalog_id"] == first.catalog_id
            assert current["port"] != second.server.server_port
            assert (
                "people.remuneration" in client.dispatch("accounting_schema", {})["supported_kinds"]
            )
            assert not client.security("session_status", {})["authenticated"]
            assert (
                Client(second.catalog_path, autostart=False).info()["catalog_id"]
                == second.catalog_id
            )
        finally:
            client.stop()
        deadline = time.monotonic() + 10
        while _state.state_path(first.catalog_path).exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _state.state_path(first.catalog_path).exists()


def test_unreachable_address_does_not_replace_a_resident_still_holding_its_catalog_lock(
    local, monkeypatch
):
    first, _, _ = local
    first.start()
    original = first.metadata
    with independent(first, "wrong-address") as second:
        _state.publish(first.catalog_path, original | {"port": second.server.server_port})

        def no_spawn(*args, **kwargs):
            pytest.fail("a living catalog lock must prevent a replacement resident")

        monkeypatch.setattr(_client.subprocess, "Popen", no_spawn)
        try:
            with pytest.raises(ContractError) as failed:
                _client.ensure_service(first.catalog_path, timeout=0.2)
            assert failed.value.code == "service_start_failed"
        finally:
            _state.publish(first.catalog_path, original)
    assert Client(first.catalog_path, autostart=False).info()["catalog_id"] == first.catalog_id


def test_catalog_hardlink_alias_is_rejected_before_service_or_connection(local):
    runtime, _, _ = local
    alias = runtime.catalog_path.with_name("catalog-alias.sqlite3")
    os.link(runtime.catalog_path, alias)
    try:
        assert os.path.samefile(runtime.catalog_path, alias)
        for operation in (
            lambda: create_runtime(alias, window_opener=lambda _: None),
            lambda: Client(alias),
            lambda: Client(alias, autostart=False),
        ):
            with pytest.raises(ContractError) as failed:
                operation()
            assert failed.value.code == "invalid_path"
        with pytest.raises(ContractError) as failed:
            with _state.instance_lock(runtime.catalog_path):
                pass
        assert failed.value.code == "invalid_path"
        assert not _state.state_path(alias).exists()
    finally:
        alias.unlink()
    runtime.start()
    assert Client(runtime.catalog_path, autostart=False).info()["catalog_id"] == runtime.catalog_id


def test_a_connection_document_for_another_catalog_is_not_replaced(local, monkeypatch):
    runtime, _, _ = local
    _state.publish(runtime.catalog_path, runtime.metadata | {"catalog_id": str(uuid4())})
    original = _state.state_path(runtime.catalog_path).read_bytes()

    def no_spawn(*args, **kwargs):
        pytest.fail("unverified catalog metadata must not trigger startup")

    monkeypatch.setattr(_client.subprocess, "Popen", no_spawn)
    with pytest.raises(ContractError) as failed:
        _client.ensure_service(runtime.catalog_path)
    assert failed.value.code == "service_identity_mismatch"
    assert _state.state_path(runtime.catalog_path).read_bytes() == original
