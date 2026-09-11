"""Public boundaries, atomic source batches and the loopback read adapter."""

import http.client
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import partial

import pytest

from ai_accounting.kernel.backup import create_portable
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.http import create_server
from ai_accounting.kernel.security.primitives import IdentityError
from ai_accounting.kernel.service import LocalService


def test_calculation_does_not_block_reads_and_revocation_wins_before_commit(service):
    app, company = service
    engine = app.engine(company)
    engine.save_facts(records(engine)[:1], request_id="source")
    preview = engine.preview(["expense-0"])
    entered, release = threading.Event(), threading.Event()
    original = app.registry.evaluators["expense"]

    def slow_calculation(version, context):
        entered.set()
        assert release.wait(10)
        return original(version, context)

    app.registry.evaluators["expense"] = slow_calculation
    with ThreadPoolExecutor(max_workers=3) as pool:
        confirmation = pool.submit(
            app.dispatch,
            "confirm",
            {
                "company_id": company,
                "subjects": ["expense-0"],
                "preview_digest": preview["digest"],
                "epochs": preview["epochs"],
                "request_id": "publication",
            },
        )
        try:
            assert entered.wait(5)
            overview = pool.submit(
                app.dispatch, "overview", {"company_id": company, "period": "2026-01"}
            )
            assert overview.result(timeout=3)["accounts"] == []
            logout = pool.submit(app.security.logout, app._test_session_token)
            assert logout.result(timeout=3)["status"] == "logged_out"
        finally:
            release.set()
        with pytest.raises(IdentityError):
            confirmation.result(timeout=5)
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM voucher_current").fetchone()[0] == 0


def test_publication_audit_records_authority_without_session_secret(service):
    app, company = service
    item = records(app.engine(company))[0]
    app.dispatch("save_fact", {**item, "company_id": company, "request_id": "public-save"})
    with app.engine(company).store.connection(read_only=True) as connection:
        payload = connection.execute(
            "SELECT payload FROM audit WHERE request_id='public-save'"
        ).fetchone()[0]
    audit = json.loads(payload)
    assert audit["actor"]["catalog_id"] == app.security.catalog_instance_id
    assert audit["actor"]["owner_id"]
    assert app._test_session_token not in payload


@pytest.fixture
def service(tmp_path):
    result = LocalService(tmp_path)
    result.security.provision("owner", "test-password-123")
    token = result.security.login("owner", "test-password-123").session_token
    result._test_session_token = token.get_secret_value()
    result.dispatch = partial(result.dispatch, session_token=token)
    company = result.dispatch(
        "create_company",
        {
            "taxpayer_id": "91310000123456789A",
            "name": "测试企业",
        },
    )
    return result, company["id"]


def records(engine):
    evidence = engine.register_evidence(b"confirmed batch", "text/plain", "batch", request_id="e")
    return [
        {
            "kind": "expense",
            "subject_id": f"expense-{i}",
            "data": {
                "period": "2026-01",
                "amount_fen": 1200,
                "counterparty_id": "supplier",
                "expense_class": "administration",
                "creditor_kind": "supplier",
            },
            "evidence": [evidence["digest"]],
            "expected_revision": 0,
        }
        for i in range(2)
    ]


def test_source_batch_rolls_back_late_failure_and_replays_once(service):
    app, company = service
    engine = app.engine(company)
    batch = records(engine)
    batch[1]["expected_revision"] = 1
    with pytest.raises(KernelError, match="版本"):
        engine.save_facts(batch, request_id="batch")
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM fact_revision").fetchone()[0] == 0
        assert connection.execute("SELECT accounting FROM state").fetchone()[0] == 0
    batch[1]["expected_revision"] = 0
    saved = engine.save_facts(batch, request_id="batch")
    assert engine.save_facts(batch, request_id="batch") == saved
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT accounting FROM state").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM fact_revision").fetchone()[0] == 2
    batch[0]["data"]["amount_fen"] = 1300
    with pytest.raises(KernelError) as error:
        engine.save_facts(batch, request_id="batch")
    assert error.value.code == "idempotency_conflict"


def test_loopback_auth_origin_read_only_and_large_money(service, tmp_path):
    app, company = service
    engine = app.engine(company)
    batch = records(engine)[:1]
    batch[0]["data"]["amount_fen"] = 9007199254740993
    engine.save_facts(batch, request_id="batch")
    preview = engine.preview(["expense-0"])
    engine.confirm(
        ["expense-0"],
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="publish",
    )
    static = tmp_path / "static"
    static.mkdir()
    (static / "local.html").write_text("<h1>local</h1>", encoding="utf-8")
    server, token = create_server(app, static_directory=static, token="test-token")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(path, *, headers=None, method="GET"):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        body = response.read()
        result = response.status, dict(response.headers), body
        connection.close()
        return result

    try:
        auth = {"Authorization": "Bearer " + app._test_session_token}
        assert request("/api/local/companies")[0] == 401
        assert (
            request("/api/local/companies", headers=auth | {"Origin": "https://evil.test"})[0]
            == 403
        )
        assert request("/api/local/companies", headers=auth | {"Host": "evil.test"})[0] == 403
        assert request("/api/local/save_fact", headers=auth)[0] == 404
        assert request("/api/local/companies", headers=auth, method="POST")[0] == 413
        assert request("/../catalog.sqlite")[0] == 404
        assert request("/favicon.ico")[0] == 204
        status, headers, body = request(
            f"/api/local/overview?company_id={company}&period=2026-01", headers=auth
        )
        assert status == 200 and headers["Cache-Control"] == "no-store"
        accounts = json.loads(body)["accounts"]
        assert any(row["debit"] == "9007199254740993" for row in accounts)
        assert request("/api/local/companies?period=a&period=b", headers=auth)[0] == 400
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_portable_company_import_binds_verified_identity_and_rejects_overwrite(service, tmp_path):
    app, company = service
    engine = app.engine(company)
    engine.save_facts(records(engine), request_id="batch")
    exported = create_portable(engine.store.path, tmp_path / "portable")
    destination = LocalService(tmp_path / "restored")
    imported = destination.catalog.restore_company(
        exported["path"], taxpayer_id="91310000123456789A", name="恢复企业"
    )
    assert imported["id"] == company
    assert imported["database_id"] == engine.store.database_id
    restored = destination.engine(company)
    outcome = restored.preview(["expense-0"])["results"][0]
    assert sum(line["debit"] for line in outcome["lines"]) == 1200
    assert (
        destination.catalog.restore_company(
            exported["path"], taxpayer_id="91310000123456789A", name="恢复企业"
        )
        == imported
    )
    with pytest.raises(KernelError):
        destination.catalog.restore_company(
            exported["path"], taxpayer_id="91310000123456789A", name="另一企业"
        )


def test_generated_command_schemas_validate_typed_facts_and_exclude_journals(service):
    from jsonschema import Draft202012Validator, ValidationError

    app, company = service
    schemas = app.dispatch("schema", {})["command_schemas"]
    for schema in schemas.values():
        Draft202012Validator.check_schema(schema)
    fact = records(app.engine(company))[0]
    request = {**fact, "company_id": company, "request_id": "schema-test"}
    Draft202012Validator(schemas["save_fact"]).validate(request)
    Draft202012Validator(schemas["save_facts"]).validate(
        {"company_id": company, "request_id": "batch", "facts": [fact]}
    )
    with pytest.raises(ValidationError):
        Draft202012Validator(schemas["save_fact"]).validate(request | {"lines": []})
    assert "source" not in schemas["backup"]["properties"]
    assert "request_id" in schemas["rebuild"]["required"]


def test_explicit_recording_correction_retains_actual_fact_history_and_voucher_number(service):
    app, company = service
    engine = app.engine(company)
    proof = engine.register_evidence(
        b"verified actual cash receipt", "text/plain", "proof", request_id="evidence"
    )["digest"]
    fact = {
        "period": "2026-01",
        "owner_id": "owner",
        "amount_fen": 1000,
        "funding_kind": "capital",
        "actual_date": "2026-01-10",
        "bank_account_id": "bank",
    }
    original = engine.save_fact(
        "funding", "capital", fact, evidence=(proof,), expected_revision=0, request_id="original"
    )

    def publish(request):
        preview = engine.preview(["capital"])
        return engine.confirm(
            ["capital"],
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id=request,
        )["results"][0]

    first = publish("publish")
    corrected = fact | {"amount_fen": 2000}
    with pytest.raises(KernelError) as error:
        engine.save_fact(
            "funding",
            "capital",
            corrected,
            evidence=(proof,),
            expected_revision=1,
            request_id="ordinary-change",
        )
    assert error.value.code == "immutable_fact"
    engine.amend_fact(
        "funding",
        "capital",
        corrected,
        evidence=(proof,),
        expected_revision=1,
        recording_error_confirmed=True,
        request_id="recording-correction",
    )
    second = publish("republish")
    assert first["voucher_number"] == second["voucher_number"]
    with engine.store.connection(read_only=True) as connection:
        assert engine.store.fact(connection, original["fact_id"]).fact.amount_fen == 1000
        assert engine.store.current_fact(connection, "capital").fact.amount_fen == 2000
    with pytest.raises(KernelError):
        engine.preview_delete("capital")
    preview = engine.preview_delete("capital", recording_error_evidence=proof)
    engine.delete(
        "capital",
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="withdraw-recording",
        recording_error_evidence=proof,
    )
    assert engine.overview("2026-01")["accounts"] == []
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM voucher_version").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM fact_revision").fetchone()[0] == 2
