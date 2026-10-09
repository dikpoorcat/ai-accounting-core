"""The restored Vue routes share one authenticated SQLite service."""

import json
import sys
from types import SimpleNamespace
from urllib.parse import quote

import test_resident_service as resident_cases
from entity_fixture import seed_entities
from pydantic import TypeAdapter
from test_resident_service import PASSWORD, cookie_header

from ai_accounting.kernel.dashboard import Dashboard
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


def test_dashboard_internal_validation_failure_is_http_500_but_bad_query_is_400(
    resident, monkeypatch
):
    service, _, _, http, _ = resident
    headers, _ = authenticated(resident)
    company = service.catalog.create_company("91310000123456789A", "损坏内容状态码测试企业")["id"]

    def corrupt_brief(*_args, **_kwargs):
        TypeAdapter(int).validate_python("corrupt stored amount")

    monkeypatch.setattr(Dashboard, "brief", corrupt_brief)
    path = f"/api/dashboard/brief?company_id={company}&period=2026-09"
    status, _, _, internal = http.request(path, headers=headers)
    assert status == 500
    assert internal["status"] == "rejected" and internal["code"] == "internal_error"

    status, _, _, invalid = http.request(path + "&limit=invalid", headers=headers)
    assert status == 400
    assert invalid["status"] == "rejected" and invalid["code"] == "invalid_command"


def test_dashboard_explicit_bad_read_parameters_stay_http_400(resident):
    service, _, _, http, _ = resident
    headers, _ = authenticated(resident)
    company = service.catalog.create_company("91310000123456789A", "读参数状态码测试企业")["id"]
    for route, query in (
        ("brief", "period=bad"),
        ("brief", "section=invalid"),
        ("brief", "cursor=unbound"),
        ("brief", "limit=501"),
        ("brief", "voucher_number=0"),
        ("brief", "voucher_number=1&voucher_version_id=other"),
        ("funds", "movement_account_type=bank"),
        ("funds", "movement_account_type=invalid&movement_account_id=account"),
        ("funds", "statement_account_id="),
        ("employees", "section=settlement_events"),
        ("employees", "employee_filter=invalid"),
        ("employees", "employee_filter=payroll"),
        ("employees", "employee_filter=no_payroll"),
        ("employees", "employee_filter=employment_inactive"),
        ("assets", "section=source_history"),
        ("assets", "asset_filter=invalid"),
        ("business-status", "period=2026-09&subject_id=subject&section=invalid"),
        ("quarterly-report", "year=0&quarter=1"),
        ("quarterly-report", "year=2026&quarter=5"),
        ("close-review", "period=bad"),
    ):
        status, _, _, result = http.request(
            f"/api/dashboard/{route}?company_id={company}&{query}", headers=headers
        )
        assert status == 400, (route, query, result)
        assert result["status"] == "rejected" and result["code"] == "invalid_command"

    for route, query in (
        ("overview", "period=bad"),
        ("ledger", "period=bad"),
        ("ledger", "period=2026-09&limit=501"),
        ("trace", ""),
        ("closed_report", "period=bad"),
    ):
        status, _, _, result = http.request(
            f"/api/local/{route}?company_id={company}&{query}", headers=headers
        )
        assert status == 400, (route, query, result)
        assert result["status"] == "rejected" and result["code"] == "invalid_command"


def test_complete_brief_exposes_earlier_open_period_as_typed_issue(resident):
    service, _, _, http, _ = resident
    headers, _ = authenticated(resident)
    company = service.catalog.create_company("91310000123456789A", "跨月简报合同测试企业")["id"]
    engine = service.engine(company)
    seed_entities(engine, [("supplier", "organization", None)])
    proof = engine.register_evidence(
        b"synthetic earlier-month proof", "text/plain", "fixture", request_id="proof"
    )["digest"]
    for period in ("2026-01", "2026-03"):
        engine.save_fact(
            "expense",
            f"expense-{period}",
            {
                "period": period,
                "amount_fen": 10000,
                "counterparty_id": "supplier",
                "expense_class": "administration",
                "creditor_kind": "supplier",
            },
            evidence=(proof,),
            expected_revision=0,
            request_id=f"save-{period}",
        )

    status, _, _, context = http.request(
        f"/api/dashboard/context?company_id={company}", headers=headers
    )
    assert status == 200 and context["default_period"] == "2026-03"
    status, _, _, brief = http.request(
        f"/api/dashboard/brief?company_id={company}&period=2026-03", headers=headers
    )
    assert status == 200, brief
    assert "validation" not in brief["data"]
    assert "period_preparation" not in brief["data"]
    preparation = Dashboard(engine).period_preparation(
        "2026-03",
        expected_read_version=brief["read_context"]["read_version"],
        as_of=brief["read_context"]["as_of"],
    )
    issues = preparation["data"]["brief_checks"]["issues"]
    assert any(issue.get("code") == "earlier_period_open" for issue in issues)


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
    seed_entities(engine, [("supplier", "organization", None)])
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
    assert status == 200 and browser["data"]["position"]["month_expense_fen"] == "123456"


def test_brief_group_is_exposed_through_private_cli_and_mcp_commands(
    resident, tmp_path, monkeypatch, capsys
):
    from ai_accounting.kernel import cli, mcp

    service, _, capability, http, _ = resident
    _, token = authenticated(resident)
    company = service.catalog.create_company("91310000123456789A", "组命令传输测试企业")["id"]
    engine = service.engine(company)
    amount = 9007199254740993
    _publish_expense(engine, "expense", amount)
    summary = Dashboard(engine).brief("2026-09")["data"]["collections"]["activity"]["items"][0]
    payload = {
        "company_id": company,
        "period": "2026-09",
        "section": "activity",
        "group_key": summary["group_key"],
    }

    def dispatch(command, data):
        status, _, _, response = http.request(
            "/api/command",
            {"command": command, "payload": data},
            headers={
                "X-Local-Capability": capability,
                "Authorization": "Bearer " + token.get_secret_value(),
            },
        )
        assert status == 200, response
        return response

    client = SimpleNamespace(dispatch=dispatch)
    monkeypatch.setattr(cli, "ServiceClient", lambda _: client)
    request = tmp_path / "group.json"
    request.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "finance-local",
            "--root",
            str(tmp_path),
            "call",
            "dashboard_brief_group",
            "--input",
            str(request),
        ],
    )
    cli.main()
    cli_response = json.loads(capsys.readouterr().out)
    tools = {}

    class StdioServer:
        def __init__(self, *args, **kwargs):
            pass

        def tool(self):
            def register(function):
                tools[function.__name__] = function
                return function

            return register

        def run(self, **kwargs):
            assert kwargs == {"transport": "stdio"}

    monkeypatch.setattr(mcp, "ServiceClient", lambda _: client)
    monkeypatch.setattr(mcp, "FastMCP", StdioServer)
    mcp.serve(tmp_path)
    mcp_response = tools["finance_local_command"]("dashboard_brief_group", payload)
    for response in (cli_response, mcp_response):
        assert response["schema_version"] == 3
        members = response["data"]["collections"]["members"]["items"]
        vouchers = response["data"]["collections"]["vouchers"]["items"]
        assert type(members[0]["amount_fen"]) is int and members[0]["amount_fen"] == amount
        assert members[0]["voucher_version_id"] == vouchers[0]["voucher_version_id"]


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
        f"/api/local/report-export/test/status?company_id={first}",
        f"/api/local/trace?company_id={first}&calculation_id=test",
        "/api/local/companies",
    ):
        assert http.request(path, headers=headers)[0] == 401


def test_bounded_business_page_is_authenticated_typed_and_version_bound(resident):
    service, _, _, http, _ = resident
    headers, _ = authenticated(resident)
    company = service.catalog.create_company("91310000123456789A", "T4 合成业务查询企业")["id"]
    engine = service.engine(company)
    seed_entities(engine, [("supplier", "organization", None)])
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
        "&subject_id=expense&limit=1"
    )
    assert http.request(path)[0] == 401
    status, _, _, response = http.request(path, headers=headers)
    assert status == 200, response
    assert response["schema_version"] == 10
    assert response["data"]["identity"]["subject_id"] == "expense"
    assert response["data"]["settlements"]["obligations"][0]["remaining_fen"] == "12500"
    assert "events" not in response["data"]["collections"]
    assert http.request(path + "&expected_version=stale", headers=headers)[0] == 409
    assert http.request(path + "&unexpected=field", headers=headers)[0] == 400
    assert http.request(path + "&section=arbitrary", headers=headers)[0] == 400
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
    assert (
        http.request(f"/api/local/jobs?company_id={company}&job_id=foreign", headers=headers)[0]
        == 404
    )


def _publish_expense(
    engine, subject, amount, revision=0, party=None, expense_class="administration"
):
    party = party or subject + "-supplier"
    seed_entities(engine, [(party, "organization", None)])
    proof = engine.register_evidence(
        b"synthetic HTTP adapter evidence", "text/plain", "fixture", request_id="adapter-proof"
    )["digest"]
    engine.save_fact(
        "expense",
        subject,
        {
            "period": "2026-09",
            "amount_fen": amount,
            "counterparty_id": party,
            "expense_class": expense_class,
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


def test_http_and_command_scope_select_same_category_by_exact_member_key(resident):
    service, _, capability, http, _ = resident
    headers, token = authenticated(resident)
    company = service.catalog.create_company("91310000123456789A", "精确事项接口测试")["id"]
    engine = service.engine(company)
    for subject, nature, amount in (
        ("admin-a", "administration", 1000),
        ("admin-b", "administration", 2000),
        ("sales", "sales", 3000),
    ):
        _publish_expense(engine, subject, amount, party="supplier", expense_class=nature)
    seed_entities(engine, [("bank", "fund_account", "bank")])
    proof = engine.register_evidence(
        b"synthetic exact payment scope",
        "text/plain",
        "fixture",
        request_id="scope-proof",
    )["digest"]
    engine.save_fact(
        "payment",
        "combined",
        {
            "period": "2026-09",
            "actual_date": "2026-09-28",
            "direction": "outflow",
            "bank_account_id": "bank",
            "counterparty_id": None,
            "payment_method": "bank_batch",
            "amount_fen": 1000,
            "allocations": [
                {
                    "source_kind": "expense",
                    "source_id": subject,
                    "obligation": "primary",
                    "amount_fen": amount,
                    "recipient_id": "supplier",
                }
                for subject, amount in (("admin-a", 500), ("admin-b", 200), ("sales", 300))
            ],
        },
        evidence=(proof,),
        expected_revision=0,
        request_id="scope-payment",
    )
    plan = engine.preview(["combined"])
    engine.confirm(
        ["combined"],
        preview_digest=plan["digest"],
        epochs=plan["epochs"],
        request_id="scope-publication",
    )
    dashboard = Dashboard(engine)
    with dashboard._snapshot("2026-09") as snap:
        parts = next(parts for parts in snap.activity_components.values() if len(parts) == 2)
    assert {part["source_category"] for part in parts} == {"expense_supplier"}
    admin = next(part for part in parts if part["amount_fen"] == 700)
    sales = next(part for part in parts if part["amount_fen"] == 300)
    path = (
        f"/api/dashboard/business-status?company_id={company}&period=2026-09"
        f"&subject_id=combined&voucher_version_id={admin['voucher_version_id']}&limit=1"
    )
    status, _, _, response = http.request(
        path + "&detail_scope_key=" + admin["key"], headers=headers
    )
    assert status == 200 and response["schema_version"] == 10
    assert response["data"]["detail_scope"]["key"] == admin["key"]
    assert response["data"]["detail_scope"]["amount_fen"] == "700"
    cursor = response["data"]["collections"]["settlement_events"]["page"]["next_cursor"]
    assert http.request(path + "&detail_scope_category=expense_supplier", headers=headers)[0] == 400
    assert (
        http.request(
            path
            + "&detail_scope_key="
            + sales["key"]
            + "&section=settlement_events&cursor="
            + cursor,
            headers=headers,
        )[0]
        == 409
    )
    payload = {
        "company_id": company,
        "period": "2026-09",
        "subject_id": "combined",
        "voucher_version_id": sales["voucher_version_id"],
        "detail_scope_key": sales["key"],
    }
    status, _, _, native = http.request(
        "/api/command",
        {"command": "dashboard_business_status", "payload": payload},
        headers={
            "X-Local-Capability": capability,
            "Authorization": "Bearer " + token.get_secret_value(),
        },
    )
    assert status == 200 and native["data"]["detail_scope"]["amount_fen"] == 300
    assert native["data"]["detail_scope"]["key"] == sales["key"]


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
    _publish_expense(engine, "first", 100, party="same-supplier")
    _publish_expense(engine, "second", 200, party="same-supplier")
    base = f"/api/dashboard/brief?company_id={company}&period=2026-09&limit=1"
    status, _, _, first = http.request(base, headers=headers)
    assert status == 200
    group = first["data"]["collections"]["activity"]["items"][0]
    assert group["member_count"] == 2
    member_base = (
        f"/api/dashboard/brief-group?company_id={company}&period=2026-09"
        f"&section=activity&group_key={group['group_key']}&limit=1"
    )
    status, _, _, members = http.request(member_base, headers=headers)
    assert status == 200 and members["data"]["collections"]["members"]["page"]["has_more"]
    cursor = quote(members["data"]["collections"]["members"]["page"]["next_cursor"], safe="")
    following = member_base + f"&cursor={cursor}&expected_version={first['snapshot_version']}"
    status, _, _, page = http.request(following, headers=headers)
    assert status == 200 and page["snapshot_version"] == first["snapshot_version"]
    assert len(page["data"]["collections"]["members"]["items"]) == 1
    _publish_expense(engine, "first", 150, revision=1, party="same-supplier")
    status, _, _, error = http.request(following, headers=headers)
    assert status == 409 and error["code"] == "dashboard_snapshot_changed"
    assert "data" not in error
    status, _, _, refreshed = http.request(base, headers=headers)
    assert status == 200 and refreshed["snapshot_version"] != first["snapshot_version"]
    assert refreshed["data"]["position"]["month_expense_fen"] == "350"


def test_numeric_voucher_deep_link_survives_real_http_parsing(resident):
    service, _, _, http, _ = resident
    headers, _ = authenticated(resident)
    company = service.catalog.create_company("91310000123456789A", "数字凭证深链测试企业")["id"]
    engine = service.engine(company)
    _publish_expense(engine, "first", 100)
    target = _publish_expense(engine, "target", 200)
    base = f"/api/dashboard/brief?company_id={company}&period=2026-09&limit=1"
    numeric = base + f"&voucher_number={target['number']}"
    status, _, _, result = http.request(numeric, headers=headers)
    assert status == 200, result
    assert result["data"]["focused_activity"]["subject_id"] == "target"
    assert result["data"]["focused_activity"]["amount_fen"] == "200"
    group = result["data"]["collections"]["activity"]["items"][0]
    assert group["member_count"] == 1
    assert (
        result["data"]["focused_activity_group"]["group_key"]
        == result["data"]["focused_activity"]["group_key"]
    )
    assert result["data"]["focused_activity_group"]["group_key"] != group["group_key"]
    assert result["data"]["collections"]["activity"]["page"]["has_more"]
    status, _, _, exact = http.request(
        base + f"&voucher_version_id={target['id']}", headers=headers
    )
    assert status == 200
    assert exact["data"]["focused_activity"] == result["data"]["focused_activity"]
    status, _, _, absent = http.request(base + "&voucher_number=10009", headers=headers)
    assert status == 400 and absent["code"] == "dashboard_voucher_not_found"
    for suffix in (
        "&voucher_number=2.5",
        "&voucher_number=0",
        "&voucher_number=1&voucher_number=2",
        f"&voucher_number=2&voucher_version_id={target['id']}",
    ):
        assert http.request(base + suffix, headers=headers)[0] == 400
