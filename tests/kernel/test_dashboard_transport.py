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


def test_bounded_business_page_is_authenticated_typed_and_version_bound(resident):
    service, _, _, http, _ = resident
    headers, _ = authenticated(resident)
    company = service.catalog.create_company("91310000123456789A", "T4 合成业务查询企业")["id"]
    engine = service.engine(company)
    evidence = engine.register_evidence(
        b"T4 synthetic business page", "text/plain", "fixture", request_id="t4-proof"
    )["digest"]
    engine.save_fact(
        "expense",
        "expense",
        {
            "period": "2026-09",
            "amount_fen": 12500,
            "counterparty_id": "supplier",
            "expense_class": "administration",
            "creditor_kind": "supplier",
        },
        evidence=(evidence,),
        expected_revision=0,
        request_id="t4-expense",
    )
    plan = engine.preview(["expense"])
    engine.confirm(
        ["expense"], preview_digest=plan["digest"], epochs=plan["epochs"], request_id="t4-publish"
    )
    path = (
        f"/api/dashboard/business-status?company_id={company}&period=2026-09"
        "&subject_id=expense&section=events&limit=1"
    )
    assert http.request(path)[0] == 401
    status, _, _, response = http.request(path, headers=headers)
    assert status == 200, response
    assert response["schema_version"] == 1
    assert response["data"]["identity"]["subject_id"] == "expense"
    assert response["data"]["settlements"]["obligations"][0]["remaining_fen"] == "12500"
    assert response["data"]["collections"]["events"]["page"]["returned_count"] == 1
    assert http.request(path + "&expected_version=stale", headers=headers)[0] == 409
    assert http.request(path + "&unexpected=field", headers=headers)[0] == 400
    assert (
        http.request(path.replace("section=events", "section=arbitrary"), headers=headers)[0] == 400
    )
    assert http.request(path.replace("limit=1", "limit=501"), headers=headers)[0] == 400
    assert http.request(path + "&settlement_view=historical", headers=headers)[0] == 200
    assert http.request(path + "&settlement_view=unknown", headers=headers)[0] == 400


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


def _publish_expense(engine, subject, amount, revision=0):
    proof = engine.register_evidence(
        b"synthetic HTTP adapter evidence", "text/plain", "fixture", request_id="adapter-proof"
    )["digest"]
    engine.save_fact(
        "expense",
        subject,
        {
            "period": "2026-09",
            "amount_fen": amount,
            "counterparty_id": subject + "-supplier",
            "expense_class": "administration",
            "creditor_kind": "supplier",
        },
        evidence=(proof,),
        expected_revision=revision,
        request_id=f"save-{subject}-{revision}",
    )
    plan = engine.preview([subject])
    engine.confirm(
        [subject],
        preview_digest=plan["digest"],
        epochs=plan["epochs"],
        request_id=f"publish-{subject}-{revision}",
    )
    return engine.ledger("2026-09")[-1]


def test_http_voucher_trace_is_company_bound_and_keeps_exact_money_strings(resident):
    service, _, _, http, _ = resident
    headers, token = authenticated(resident)
    first = service.catalog.create_company("91310000123456789A", "追溯甲公司")["id"]
    second = service.catalog.create_company("91310000123456789B", "追溯乙公司")["id"]
    engine = service.engine(first)
    amount = 9007199254740993
    voucher = _publish_expense(engine, "original", amount)
    unrelated = _publish_expense(engine, "unrelated", 200)
    _publish_expense(service.engine(second), "original", 300)
    query = f"/api/local/trace?company_id={first}&voucher_version_id={voucher['id']}"
    assert http.request(query)[0] == 401
    status, _, _, trace = http.request(query, headers=headers)
    assert status == 200, trace
    assert trace["voucher"]["id"] == voucher["id"]
    assert trace["voucher"]["total"] == str(amount)
    assert trace["calculation"]["id"] == voucher["calculation_id"]
    assert trace["facts"][0]["subject_id"] == "original"
    assert trace["facts"][0]["data"]["amount_fen"] == str(amount)
    for line in trace["voucher"]["lines"] + trace["calculation"]["outcome"]["lines"]:
        assert isinstance(line["debit"], str) and isinstance(line["credit"], str)
    assert trace["related_vouchers"] == []
    assert trace["upstream"] == []
    legacy = http.request(
        f"/api/local/trace?company_id={first}&calculation_id={voucher['calculation_id']}",
        headers=headers,
    )
    assert legacy[0] == 200 and legacy[3]["calculation"] == trace["calculation"]
    for field, ident, error_code in (
        ("voucher_version_id", voucher["id"], "unknown_voucher"),
        ("calculation_id", voucher["calculation_id"], "unknown_calculation"),
    ):
        status, _, _, error = http.request(
            f"/api/local/trace?company_id={second}&{field}={ident}", headers=headers
        )
        assert status == 400 and error["code"] == error_code
        assert "calculation" not in error and "facts" not in error
    status, _, _, error = http.request(
        query + f"&calculation_id={unrelated['calculation_id']}", headers=headers
    )
    assert status == 400 and error["code"] == "voucher_trace_mismatch"
    service.security.logout(token)
    assert http.request(query, headers=headers)[0] == 401


def test_http_continuation_requires_the_same_published_snapshot(resident):
    service, _, _, http, _ = resident
    headers, _ = authenticated(resident)
    company = service.catalog.create_company("91310000123456789A", "分页版本测试企业")["id"]
    engine = service.engine(company)
    _publish_expense(engine, "first", 100)
    _publish_expense(engine, "second", 200)
    base = f"/api/dashboard/brief?company_id={company}&period=2026-09&limit=1"
    status, _, _, first = http.request(base, headers=headers)
    assert status == 200 and first["data"]["voucher_page"]["has_more"]
    cursor = first["data"]["voucher_page"]["next_after_number"]
    following = base + f"&after_number={cursor}&expected_version={first['snapshot_version']}"
    status, _, _, page = http.request(following, headers=headers)
    assert status == 200 and page["snapshot_version"] == first["snapshot_version"]
    assert len(page["data"]["vouchers"]) == 1
    _publish_expense(engine, "first", 150, revision=1)
    status, _, _, error = http.request(following, headers=headers)
    assert status == 409 and error["code"] == "dashboard_snapshot_changed"
    assert "data" not in error
    status, _, _, refreshed = http.request(base, headers=headers)
    assert status == 200 and refreshed["snapshot_version"] != first["snapshot_version"]
    assert refreshed["data"]["total_debit_fen"] == "350"


def test_numeric_voucher_deep_link_survives_real_http_parsing(resident):
    service, _, _, http, _ = resident
    headers, _ = authenticated(resident)
    company = service.catalog.create_company("91310000123456789A", "数字凭证深链测试企业")["id"]
    engine = service.engine(company)
    first = _publish_expense(engine, "first", 100)
    target = _publish_expense(engine, "target", 200)
    base = f"/api/dashboard/brief?company_id={company}&period=2026-09&limit=1"
    numeric = base + f"&voucher_number={target['number']}"
    status, _, _, result = http.request(numeric, headers=headers)
    assert status == 200, result
    assert result["data"]["focused_voucher"]["voucher_version_id"] == target["id"]
    assert result["data"]["focused_voucher"]["business_amount_fen"] == "200"
    assert [item["voucher_version_id"] for item in result["data"]["vouchers"]] == [first["id"]]
    assert result["data"]["voucher_page"]["has_more"]
    status, _, _, exact = http.request(
        base + f"&voucher_version_id={target['id']}", headers=headers
    )
    assert status == 200
    assert exact["data"]["focused_voucher"] == result["data"]["focused_voucher"]
    status, _, _, absent = http.request(base + "&voucher_number=10009", headers=headers)
    assert status == 400 and absent["code"] == "dashboard_voucher_not_found"
    for suffix in (
        "&voucher_number=2.5",
        "&voucher_number=0",
        "&voucher_number=1&voucher_number=2",
        f"&voucher_number=2&voucher_version_id={target['id']}",
    ):
        assert http.request(base + suffix, headers=headers)[0] == 400
