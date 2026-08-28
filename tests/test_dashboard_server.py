from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import date
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from ai_accounting import dashboard_server
from ai_accounting.dashboard_server import build_quarterly_report_view, make_dashboard_handler
from ai_accounting.models import AccountingPeriod


def _calculated_result(*, calculation_hash: str = "a" * 64) -> dict[str, object]:
    return {
        "status": "calculated",
        "calculation_hash": calculation_hash,
        "missing_information": [],
        "errors": [],
        "data": {
            "organization": {
                "name": "报表测试企业",
                "taxpayer_identification_number": "91330106MA1234567T",
            },
            "statements": {
                "balance_sheet": {
                    "1": {"name": "货币资金", "ending_fen": 70_000, "beginning_fen": 0},
                    "30": {"name": "资产合计", "ending_fen": 140_000, "beginning_fen": 0},
                    "53": {
                        "name": "负债和所有者权益总计",
                        "ending_fen": 140_000,
                        "beginning_fen": 0,
                    },
                },
                "profit_statement": {
                    "32": {
                        "name": "净利润",
                        "current_fen": 40_000,
                        "year_to_date_fen": 40_000,
                    }
                },
                "cash_flow_statement": {
                    "20": {
                        "name": "现金净增加额",
                        "current_fen": 70_000,
                        "year_to_date_fen": 70_000,
                    },
                    "22": {
                        "name": "期末现金余额",
                        "current_fen": 70_000,
                        "year_to_date_fen": 70_000,
                    },
                },
            },
            "checks": [
                {
                    "code": "FINANCIAL_STATEMENT_BALANCE_SHEET_ENDING_FEN",
                    "passed": True,
                    "left_fen": 140_000,
                    "right_fen": 140_000,
                }
            ],
            "template": {
                "profile": "test-template-v1",
                "sha256": "b" * 64,
                "file_name": "财务报表月季报.xlsx",
            },
            "rule": {"version": "test-rule-v1"},
            "source_close_hashes": ["c" * 64, "d" * 64, "e" * 64],
            "classification_ids": [str(uuid.uuid4())],
            "enterprise_income_tax_confirmation_ids": [str(uuid.uuid4())],
        },
    }


def test_quarterly_report_view_exposes_ready_summary_and_chinese_checks() -> None:
    view = build_quarterly_report_view(
        _calculated_result(),
        year=2026,
        quarter=1,
    )

    assert view["status"] == "ready"
    assert view["headline"] == "三表已生成且勾稽通过，可以导出。"
    assert view["summary"] == {
        "assets_total_fen": 140_000,
        "liabilities_equity_total_fen": 140_000,
        "current_net_profit_fen": 40_000,
        "year_to_date_net_profit_fen": 40_000,
        "current_cash_change_fen": 70_000,
        "ending_cash_fen": 70_000,
    }
    assert view["checks"]["items"][0]["label"] == "期末资产与负债及所有者权益相等"
    assert view["organization"] == {
        "name": "报表测试企业",
        "taxpayer_identification_number": "91330106MA1234567T",
    }
    assert view["period"]["quarter_start"] == "2026-01-01"
    assert view["export"]["available"] is True
    assert "2026Q1.xlsx" in view["export"]["file_name"]
    balance = view["statements"][0]
    assert balance["label"] == "资产负债表"
    assert next(row for row in balance["rows"] if row["line"] == 30)["is_total"] is True


def test_quarterly_report_view_groups_actionable_blockers() -> None:
    open_february = AccountingPeriod(
        org_id=uuid.uuid4(),
        calendar_id=uuid.uuid4(),
        generation_action_id=uuid.uuid4(),
        calendar_year=2026,
        calendar_month=2,
        start_date="2026-02-01",
        end_date="2026-02-28",
        status="open",
    )
    result = _calculated_result()
    result.update(
        {
            "status": "needs_information",
            "missing_information": [
                {
                    "code": "FINANCIAL_STATEMENT_PERIOD_NOT_CLOSED",
                    "message": "当年年初至季度末的全部自然月必须存在且已结账。",
                    "data": {},
                },
                {
                    "code": "FINANCIAL_STATEMENT_CLASSIFICATION_REQUIRED",
                    "message": "该费用凭证行需要明确报表明细分类。",
                    "data": {
                        "voucher_number": "202603-0008",
                        "posting_date": "2026-03-20",
                        "amount_fen": 12_345,
                    },
                },
                {
                    "code": "ENTERPRISE_INCOME_TAX_QUARTER_CONFIRMATION_REQUIRED",
                    "message": "必须明确确认该季度企业所得税处理，零元也需确认。",
                    "data": {"year": 2026, "quarter": 1},
                },
            ],
        }
    )

    view = build_quarterly_report_view(
        result,
        year=2026,
        quarter=1,
        periods=[open_february],
        current_date=date(2026, 8, 26),
    )

    assert view["status"] == "blocked"
    assert view["status_label"] == "还差 3 项"
    assert view["draft"] is True
    assert view["export"]["available"] is False
    readiness = {item["key"]: item for item in view["readiness"]}
    assert readiness["period"]["state"] == "attention"
    assert {item["primary"] for item in readiness["period"]["details"]} >= {
        "2026 年 1 月",
        "2026 年 2 月",
        "2026 年 3 月",
    }
    assert readiness["classification"]["details"][0] == {
        "primary": "凭证 202603-0008",
        "secondary": "2026-03-20 · 该费用凭证行需要明确报表明细分类。",
        "amount_fen": 12_345,
    }
    assert readiness["income_tax"]["details"][0]["primary"] == "2026 年第 1 季度"


def test_quarterly_report_view_distinguishes_in_progress_and_not_applicable() -> None:
    in_progress = _calculated_result()
    in_progress.update(
        {
            "status": "needs_information",
            "missing_information": [
                {
                    "code": "FINANCIAL_STATEMENT_PERIOD_NOT_CLOSED",
                    "message": "当年年初至季度末的全部自然月必须存在且已结账。",
                    "data": {},
                }
            ],
        }
    )
    progress_view = build_quarterly_report_view(
        in_progress,
        year=2026,
        quarter=3,
        current_date=date(2026, 8, 27),
    )
    assert progress_view["status"] == "in_progress"
    assert progress_view["status_label"] == "季度进行中"
    assert progress_view["draft"] is True
    assert progress_view["export"]["available"] is False

    not_applicable_view = build_quarterly_report_view(
        {
            "status": "rejected",
            "missing_information": [],
            "errors": ["FINANCIAL_STATEMENT_ACCOUNTING_STANDARD_UNSUPPORTED"],
            "data": {},
        },
        year=2026,
        quarter=1,
        current_date=date(2026, 8, 27),
    )
    assert not_applicable_view["status"] == "not_applicable"
    assert not_applicable_view["readiness"] == []
    assert not_applicable_view["export"]["available"] is False


@pytest.fixture
def dashboard_http_server(monkeypatch: pytest.MonkeyPatch):
    calculation_hash = "a" * 64

    def fake_loader(*_args, **_kwargs):
        return _calculated_result(calculation_hash=calculation_hash), b"xlsx-bytes"

    def fake_context_loader(*_args, **_kwargs):
        return {
            "schema_version": 1,
            "company": "测试服务公司",
            "generated_at": "2026-08-27T00:00:00+00:00",
            "default_period": "2026-03",
            "periods": [
                {
                    "key": "2026-03",
                    "year": 2026,
                    "month": 3,
                    "label": "2026 年 3 月",
                    "short_label": "3 月",
                    "status": "closed",
                    "start_date": "2026-03-01",
                    "end_date": "2026-03-31",
                    "closed_at": "2026-04-01T00:00:00+00:00",
                }
            ],
            "default_quarter": "2026-Q1",
            "quarters": [
                {
                    "key": "2026-Q1",
                    "year": 2026,
                    "quarter": 1,
                    "label": "2026 年第 1 季度",
                    "complete": True,
                }
            ],
            "disclaimer": "私有试用",
        }

    def fake_page_loader(*_args, period_key=None, **_kwargs):
        return {
            "schema_version": 1,
            "selected_period": {"key": period_key or "2026-03"},
            "data": {"total_fen": 9_007_199_254_740_993},
        }

    monkeypatch.setattr(dashboard_server, "load_quarterly_financial_statement", fake_loader)
    monkeypatch.setattr(dashboard_server, "load_dashboard_context", fake_context_loader)
    for path, (page_name, _loader) in dashboard_server._DASHBOARD_PAGE_LOADERS.items():
        monkeypatch.setitem(
            dashboard_server._DASHBOARD_PAGE_LOADERS,
            path,
            (page_name, fake_page_loader),
        )
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        make_dashboard_handler(object()),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", calculation_hash
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _http_error_json(url: str) -> tuple[int, dict[str, object]]:
    with pytest.raises(HTTPError) as caught:
        urlopen(url)  # noqa: S310 - local ephemeral test server
    return caught.value.code, json.loads(caught.value.read())


def test_dashboard_handler_ignores_expected_client_disconnect() -> None:
    class AbortedWriter:
        def write(self, _body: bytes) -> None:
            raise ConnectionAbortedError

    handler_type = make_dashboard_handler(object())
    handler = handler_type.__new__(handler_type)
    handler.wfile = AbortedWriter()

    handler._write_body(b"obsolete response")


def test_dashboard_http_serves_vue_routes_and_assets(
    dashboard_http_server,
) -> None:
    base_url, _calculation_hash = dashboard_http_server

    with urlopen(base_url + "/funds") as response:  # noqa: S310 - local test server
        assert response.status == 200
        document = response.read().decode("utf-8")
        assert '<div id="app"></div>' in document
        content_security_policy = response.headers["Content-Security-Policy"]
        assert "script-src 'self'" in content_security_policy
        assert "frame-src 'self'" in content_security_policy
        assert "'unsafe-inline'" not in content_security_policy

    script_match = re.search(r'src="(/assets/[^"]+\.js)"', document)
    assert script_match is not None
    with urlopen(base_url + script_match.group(1)) as response:  # noqa: S310
        assert response.status == 200
        assert "javascript" in response.headers["Content-Type"]
        assert response.headers["Cache-Control"] == "public, max-age=31536000, immutable"
        assert response.read()

    with pytest.raises(HTTPError) as caught:
        urlopen(base_url + "/legacy")  # noqa: S310 - removed compatibility route
    assert caught.value.code == 404


def test_dashboard_http_serves_shared_context(dashboard_http_server) -> None:
    base_url, _calculation_hash = dashboard_http_server

    with urlopen(base_url + "/api/dashboard/context") as response:  # noqa: S310
        assert response.status == 200
        assert response.headers["Cache-Control"] == "no-store"
        payload = json.loads(response.read())

    assert payload["company"] == "测试服务公司"
    assert payload["default_period"] == "2026-03"
    assert payload["quarters"][0]["complete"] is True


@pytest.mark.parametrize("page", ["brief", "funds", "employees", "assets"])
def test_dashboard_http_serves_one_selected_page_with_string_fen(
    dashboard_http_server,
    page: str,
) -> None:
    base_url, _calculation_hash = dashboard_http_server

    with urlopen(  # noqa: S310
        f"{base_url}/api/dashboard/{page}?period=2026-03"
    ) as response:
        assert response.status == 200
        assert response.headers["Cache-Control"] == "no-store"
        payload = json.loads(response.read())

    assert payload["selected_period"]["key"] == "2026-03"
    assert payload["data"]["total_fen"] == "9007199254740993"


def test_dashboard_http_rejects_duplicate_page_period(dashboard_http_server) -> None:
    base_url, _calculation_hash = dashboard_http_server

    status, payload = _http_error_json(
        base_url + "/api/dashboard/funds?period=2026-02&period=2026-03"
    )

    assert status == 400
    assert payload["errors"] == ["DASHBOARD_PERIOD_INVALID"]


def test_quarterly_export_requires_matching_preview_hash(dashboard_http_server) -> None:
    base_url, calculation_hash = dashboard_http_server
    endpoint = f"{base_url}/financial-reports/quarterly.xlsx?year=2026&quarter=1"

    status, missing_hash = _http_error_json(endpoint)
    assert status == 400
    assert missing_hash["errors"] == ["REPORT_PREVIEW_HASH_REQUIRED"]

    status, stale = _http_error_json(endpoint + "&calculation_hash=" + "b" * 64)
    assert status == 409
    assert stale["errors"] == ["REPORT_PREVIEW_STALE"]

    with urlopen(  # noqa: S310 - local ephemeral test server
        endpoint + "&calculation_hash=" + calculation_hash
    ) as response:
        assert response.status == 200
        assert response.read() == b"xlsx-bytes"
        assert response.headers["X-Report-Calculation-Hash"] == calculation_hash
