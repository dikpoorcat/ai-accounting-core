"""The restored Vue routes share one authenticated SQLite service."""

import test_resident_service as resident_cases
from test_resident_service import PASSWORD, cookie_header

from ai_accounting.kernel.http import wire_money

resident = resident_cases.resident


def test_statement_check_count_is_not_a_currency_amount():
    assert wire_money({"checks": {"passed": 8, "total": 8}, "voucher": {"total": 123}}) == {
        "checks": {"passed": 8, "total": 8},
        "voucher": {"total": "123"},
    }


def authenticated(resident):
    service, _, _, http, _ = resident
    token = service.security.login("owner", PASSWORD).session_token
    cookies, _ = http.surface(token)
    return {"Cookie": cookie_header(cookies), "Origin": http.origin}, token


def test_restored_routes_and_empty_authenticated_catalog(resident):
    _, _, _, http, _ = resident
    for path in ("/", "/index.html", "/local.html", "/funds", "/employees", "/assets", "/reports"):
        assert http.request(path)[0] == 200
    assert http.request("/api/dashboard/context")[0] == 401
    headers, _ = authenticated(resident)
    status, _, _, context = http.request("/api/dashboard/context", headers=headers)
    assert status == 200 and context["companies"] == []
    assert context["current_company"] is None
    assert http.request("/api/dashboard/missing", headers=headers)[0] == 404
    assert http.request("/missing.js")[0] == 404


def test_browser_money_strings_do_not_change_private_cli_and_mcp_results(resident):
    service, _, capability, http, _ = resident
    headers, token = authenticated(resident)
    company = service.catalog.create_company("91310000123456789A", "金额合同测试企业")["id"]
    engine = service.engine(company)
    proof = engine.register_evidence(
        b"synthetic amount proof", "text/plain", "fixture", request_id="proof"
    )["digest"]
    engine.save_fact(
        "expense",
        "expense",
        {
            "period": "2026-09",
            "amount_fen": 123456,
            "counterparty_id": "supplier",
            "expense_class": "administration",
            "creditor_kind": "supplier",
        },
        evidence=(proof,),
        expected_revision=0,
        request_id="expense",
    )
    plan = engine.preview(["expense"])
    engine.confirm(
        ["expense"], preview_digest=plan["digest"], epochs=plan["epochs"], request_id="publish"
    )
    status, _, _, private = http.request(
        "/api/command",
        {"command": "overview", "payload": {"company_id": company, "period": "2026-09"}},
        headers={
            "X-Local-Capability": capability,
            "Authorization": "Bearer " + token.get_secret_value(),
        },
    )
    assert status == 200 and all(type(row["debit"]) is int for row in private["accounts"])
    assert sum(row["debit"] for row in private["accounts"]) == 123456
    status, _, _, browser = http.request(
        f"/api/dashboard/brief?company_id={company}&period=2026-09",
        headers=headers,
    )
    assert status == 200 and browser["data"]["total_debit_fen"] == "123456"


def test_dashboard_company_selection_query_contract_and_expiration(resident):
    service, _, _, http, _ = resident
    headers, token = authenticated(resident)
    first = service.catalog.create_company("91310000123456789A", "测试甲公司")["id"]
    second = service.catalog.create_company("91310000123456789B", "测试乙公司")["id"]
    for company in (first, second):
        status, _, _, context = http.request(
            f"/api/dashboard/context?company_id={company}",
            headers=headers,
        )
        assert status == 200, context
        assert context["current_company"]["company_id"] == company
        assert {row["company_id"] for row in context["companies"]} == {first, second}
        assert all("path" not in row for row in context["companies"])
    for query in ("company_id=missing", f"company_id={first}&company_id={second}", "org_id=old"):
        assert http.request("/api/dashboard/context?" + query, headers=headers)[0] == 400
    service.security.logout(token)
    assert http.request("/api/dashboard/context", headers=headers)[0] == 401
    for path in (
        f"/api/local/jobs?company_id={first}&job_id=test",
        f"/api/local/trace?company_id={first}&calculation_id=test",
        "/api/local/companies",
    ):
        assert http.request(path, headers=headers)[0] == 401


def test_report_post_is_only_cookie_authenticated_same_origin_typed_export(resident):
    service, _, _, http, _ = resident
    headers, token = authenticated(resident)
    company = service.catalog.create_company("91310000123456789A", "测试企业")["id"]
    payload = {
        "company_id": company,
        "year": 2026,
        "quarter": 1,
        "preview_digest": "0" * 64,
        "epochs": {"accounting": 0, "material": 0, "management": 0},
        "request_id": "browser-export",
    }
    assert http.request("/api/local/report-export", payload)[0] == 403
    assert (
        http.request("/api/local/report-export", payload, headers={"Origin": http.origin})[0] == 401
    )
    assert (
        http.request(
            "/api/local/report-export",
            payload,
            headers={
                "Origin": http.origin,
                "Authorization": "Bearer " + token.get_secret_value(),
            },
        )[0]
        == 401
    )
    assert (
        http.request(
            "/api/local/report-export",
            payload,
            headers={
                **headers,
                "Origin": "https://example.invalid",
            },
        )[0]
        == 403
    )
    for extra in ({"output_directory": "C:/Windows"}, {"command": "confirm"}, {"year": "2026"}):
        status, _, _, error = http.request(
            "/api/local/report-export", {**payload, **extra}, headers=headers
        )
        assert status == 400 and error["code"] == "invalid_command"
    assert (
        http.request("/api/command", {"command": "companies", "payload": {}}, headers=headers)[0]
        == 403
    )
    assert service.engine(company).jobs() == []


def test_download_cannot_read_unknown_or_other_company_jobs(resident):
    service, _, _, http, _ = resident
    headers, _ = authenticated(resident)
    company = service.catalog.create_company("91310000123456789A", "测试企业")["id"]
    base = "/api/local/report-export/not-a-job/download"
    assert http.request(base + f"?company_id={company}")[0] == 401
    assert http.request(base + f"?company_id={company}", headers=headers)[0] == 404
    assert http.request(base + f"?company_id={company}&path=C:/Windows", headers=headers)[0] == 400
    assert http.request(base + "?company_id=foreign", headers=headers)[0] == 400
    status, _, _, jobs = http.request(
        f"/api/local/jobs?company_id={company}&job_id=foreign", headers=headers
    )
    assert status == 200 and jobs == []
