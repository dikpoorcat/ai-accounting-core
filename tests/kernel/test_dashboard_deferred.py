"""Deferred dashboard reads retain amounts and validate preparation in one snapshot."""

from urllib.parse import urlencode

import pytest
import test_reports as report_cases
from test_dashboard_transport import _publish_expense, authenticated
from test_resident_service import resident as resident_fixture

import ai_accounting.kernel.business_queries as business_query_module
import ai_accounting.kernel.dashboard as dashboard_module
import ai_accounting.kernel.engine as engine_module
import ai_accounting.kernel.materials as material_module
from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.command_schema import validate_command
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.http import wire_money
from ai_accounting.kernel.service import default_registry
from ai_accounting.kernel.storage import Store

book = report_cases.book
resident = resident_fixture
DAY = "2026-09-13"
CHECK_KEYS = {"materials", "accounting", "close_requirements"}
SECTIONS = (
    "vouchers",
    "businesses",
    "open_items",
    "settlement_events",
    "external_followups",
    "file_jobs",
)


@pytest.fixture(autouse=True)
def fixed_day(monkeypatch):
    monkeypatch.setattr(business_query_module, "_today_china", lambda: DAY)


def forbid_preparation(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("deferred or stale read reached the preparation checker")

    monkeypatch.setattr(BusinessQueries, "_period_readiness", forbidden)
    monkeypatch.setattr(material_module, "check_completeness", forbidden)


def assert_context(response, engine):
    context = response["read_context"]
    assert set(context) == {"read_version", "company_id", "database_id", "as_of"}
    assert context["company_id"] == engine.store.company_id
    assert context["database_id"] == engine.store.database_id
    assert context["as_of"] == DAY
    assert isinstance(context["read_version"], str) and context["read_version"]
    return context


def assert_pending_checks(response):
    data = response["data"]
    assert data["period_preparation"] is None
    assert data["material_completeness"] is None
    checks = {item["key"]: item for item in data["validation"]["items"]}
    assert CHECK_KEYS <= checks.keys()
    assert all(checks[key]["state"] == "pending" for key in CHECK_KEYS)
    assert data["validation"]["state"] in {"pending", "attention", "error"}


def without_clock(value):
    if isinstance(value, dict):
        return {
            key: without_clock(item)
            for key, item in value.items()
            if key not in {"generated_at", "checked_at"}
        }
    if isinstance(value, list):
        return [without_clock(item) for item in value]
    return value


def test_deferred_brief_and_all_sections_skip_checks_and_keep_main_evidence(book, monkeypatch):
    report_cases.scenario(book)
    engine, save, publish, _ = book
    save(
        "expense",
        "second-march-voucher",
        {
            "period": "2026-03",
            "counterparty_id": "supplier",
            "amount_fen": 2000,
            "expense_class": "administration",
            "creditor_kind": "supplier",
        },
    )
    publish("second-march-voucher")
    dashboard = Dashboard(engine)
    complete = dashboard.brief("2026-03", limit=1)
    assert complete["data"]["voucher_count"] == 2
    assert complete["data"]["total_debit_fen"] == 12000
    assert complete["data"]["vouchers"][0]["components"]

    forbid_preparation(monkeypatch)
    deferred = dashboard.brief("2026-03", limit=1, preparation="deferred")
    assert deferred["schema_version"] == complete["schema_version"] == 2
    assert deferred["projection"] == "dashboard_brief_deferred"
    context = assert_context(deferred, engine)
    assert context["read_version"] != deferred["snapshot_version"]
    assert_pending_checks(deferred)
    for key in ("snapshot_version", "selected_period", "read_semantics"):
        assert deferred[key] == complete[key]
    deferred_only = {"period_preparation", "material_completeness", "validation", "generated_at"}
    assert {k: v for k, v in deferred["data"].items() if k not in deferred_only} == {
        k: v for k, v in complete["data"].items() if k not in deferred_only
    }
    for section in SECTIONS:
        page = dashboard.brief("2026-03", section=section, limit=1, preparation="deferred")
        assert page["read_context"] == context
        assert_pending_checks(page)
        assert section in page["data"]["collections"]
        assert page["data"]["total_debit_fen"] == 12000
        assert page["data"]["position"] == deferred["data"]["position"]
        if section == "vouchers":
            cursor = page["data"]["collections"][section]["page"]["next_cursor"]
            assert cursor
            next_page = dashboard.brief(
                "2026-03",
                section=section,
                limit=1,
                cursor=cursor,
                expected_version=page["snapshot_version"],
                preparation="deferred",
            )
            assert_pending_checks(next_page)
            assert next_page["read_context"] == context
            assert len(next_page["data"]["vouchers"]) == 1
            assert (
                next_page["data"]["vouchers"][0]["number"] != page["data"]["vouchers"][0]["number"]
            )

    original_position = dashboard_module._position

    def known_amount_error(snapshot):
        return {**original_position(snapshot), "equation_valid": False}

    monkeypatch.setattr(dashboard_module, "_position", known_amount_error)
    invalid = dashboard.brief("2026-03", preparation="deferred")
    assert_pending_checks(invalid)
    assert invalid["data"]["validation"]["state"] == "error"
    assert invalid["data"]["validation"]["integrity_valid"] is False
    assert invalid["data"]["validation"]["items"][0]["state"] == "error"


def test_deferred_report_keeps_open_and_closed_sources_and_export_contract(book, monkeypatch):
    report_cases.scenario(book)
    dashboard = Dashboard(book[0])
    for closed in (False, True):
        if closed:
            report_cases.close_quarter(book)
        complete = dashboard.quarterly_report(2026, 1)
        assert complete["export"]["available"] is closed
        assert len(complete["period_preparations"]) == 3
        with monkeypatch.context() as guard:
            forbid_preparation(guard)
            deferred = dashboard.quarterly_report(2026, 1, preparation="deferred")
        assert deferred["schema_version"] == complete["schema_version"] == 1
        assert deferred["projection"] == "dashboard_quarterly_report_deferred"
        assert_context(deferred, book[0])
        assert deferred["period_preparations"] is None
        assert without_clock(
            {
                k: v
                for k, v in deferred.items()
                if k not in {"projection", "read_context", "period_preparations"}
            }
        ) == without_clock({k: v for k, v in complete.items() if k != "period_preparations"})


def test_preparation_restores_original_checks_inside_the_validated_read_snapshot(book, monkeypatch):
    report_cases.scenario(book)
    engine = book[0]
    dashboard = Dashboard(engine)
    complete = dashboard.brief("2026-03")
    deferred = dashboard.brief("2026-03", preparation="deferred")
    context = assert_context(deferred, engine)
    assert dashboard.quarterly_report(2026, 1, preparation="deferred")["read_context"] == context
    original = BusinessQueries._period_readiness
    calls = []

    def checked(queries, connection, period, **kwargs):
        assert connection.in_transaction
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        calls.append((period, kwargs["as_of"]))
        return original(queries, connection, period, **kwargs)

    monkeypatch.setattr(BusinessQueries, "_period_readiness", checked)
    result = dashboard.period_preparation(
        "2026-03", expected_read_version=context["read_version"], as_of=context["as_of"]
    )
    assert calls == [("2026-03", DAY)]
    assert result["schema_version"] == 1
    assert result["projection"] == "dashboard_period_preparation_result"
    assert result["read_context"] == context and result["period"] == "2026-03"
    assert result["data"]["period_preparation"] == complete["data"]["period_preparation"]
    checks = result["data"]["brief_checks"]
    assert checks["material_completeness"] == complete["data"]["material_completeness"]
    assert checks["issues"] == complete["data"]["validation"]["issues"]
    assert checks["items"] == [
        item for item in complete["data"]["validation"]["items"] if item["key"] in CHECK_KEYS
    ]
    assert (
        checks["attention_count"] + deferred["data"]["validation"]["attention_count"]
        == complete["data"]["validation"]["attention_count"]
    )
    forbid_preparation(monkeypatch)
    with pytest.raises(KernelError) as error:
        dashboard.period_preparation(
            "2026-03", expected_read_version=deferred["snapshot_version"], as_of=DAY
        )
    assert error.value.code == "dashboard_snapshot_changed"


@pytest.mark.parametrize(
    "changed",
    ["accounting", "material", "management", "company", "database", "build", "as_of", "new_day"],
)
def test_changed_read_binding_rejects_before_recomputing(changed, tmp_path, monkeypatch):
    def create(filename, company="company-a", database="database-a"):
        return Engine(
            Store.create(
                tmp_path / filename, default_registry(), company, "911100000000000001", database
            )
        )

    engine = create("original.sqlite")
    dashboard = Dashboard(engine)
    context = assert_context(dashboard.quarterly_report(2026, 1, preparation="deferred"), engine)
    as_of = DAY
    if changed in {"accounting", "material", "management"}:
        with engine.store.connection() as connection:
            connection.execute(f"UPDATE state SET {changed}={changed}+1 WHERE id=1")
            connection.commit()
    elif changed in {"company", "database"}:
        other = create(
            "other.sqlite",
            company="company-b" if changed == "company" else "company-a",
            database="database-b" if changed == "database" else "database-a",
        )
        with (
            engine.store.connection(read_only=True) as first,
            other.store.connection(read_only=True) as second,
        ):
            assert engine.store.epochs(first) == other.store.epochs(second)
        dashboard = Dashboard(other)
    elif changed == "build":
        monkeypatch.setattr(engine_module, "PROGRAM_VERSION", "synthetic-new-process-build")
    elif changed == "as_of":
        as_of = "2026-09-12"
    else:
        monkeypatch.setattr(business_query_module, "_today_china", lambda: "2026-09-14")
    forbid_preparation(monkeypatch)
    with pytest.raises(KernelError) as error:
        dashboard.period_preparation(
            "2026-01", expected_read_version=context["read_version"], as_of=as_of
        )
    assert error.value.code == "dashboard_snapshot_changed"


def test_empty_brief_deferred_context_is_explicitly_absent(tmp_path, monkeypatch):
    engine = Engine(
        Store.create(
            tmp_path / "empty.sqlite", default_registry(), "empty", "911100000000000001", "empty-db"
        )
    )
    forbid_preparation(monkeypatch)
    result = Dashboard(engine).brief(preparation="deferred")
    assert result["schema_version"] == 2
    assert result["projection"] == "dashboard_brief_deferred"
    assert result["read_context"] is None and result["data"] is None


def test_deferred_commands_and_authenticated_http_are_registered_and_strict(resident, monkeypatch):
    service, _, _, http, _ = resident
    headers, token = authenticated(resident)
    company = service.catalog.create_company("91310000123456789A", "分段合成公司")["id"]
    _publish_expense(service.engine(company), "expense", 12345)
    schema = service.dispatch("schema", {})["command_schemas"]
    command = "dashboard_period_preparation"
    assert {"company_id", "period", "as_of", "expected_read_version"} <= set(
        schema[command]["required"]
    )
    for name, parameters in (
        ("dashboard_brief", {"period": "2026-09"}),
        ("dashboard_quarterly_report", {"year": 2026, "quarter": 3}),
    ):
        assert (
            validate_command(service.command_models, name, {"company_id": company, **parameters})[
                "preparation"
            ]
            == "complete"
        )
        with pytest.raises(KernelError) as error:
            validate_command(
                service.command_models,
                name,
                {"company_id": company, **parameters, "preparation": "skip"},
            )
        assert error.value.code == "invalid_command"
    with monkeypatch.context() as guard:
        forbid_preparation(guard)
        brief = service.dispatch(
            "dashboard_brief",
            {"company_id": company, "period": "2026-09", "preparation": "deferred"},
            session_token=token,
        )
        for endpoint, parameters in (
            ("brief", {"period": "2026-09"}),
            ("quarterly-report", {"year": 2026, "quarter": 3}),
        ):
            query = urlencode({"company_id": company, **parameters, "preparation": "deferred"})
            status, _, _, response = http.request(
                "/api/dashboard/" + endpoint + "?" + query, headers=headers
            )
            assert status == 200, response
            assert response["read_context"] == brief["read_context"]
    payload = {
        "company_id": company,
        "period": "2026-09",
        "as_of": DAY,
        "expected_read_version": brief["read_context"]["read_version"],
    }
    for invalid in (
        {k: v for k, v in payload.items() if k != "expected_read_version"},
        {**payload, "as_of": "2026-02-30"},
        {**payload, "unexpected": True},
    ):
        with pytest.raises(KernelError) as error:
            validate_command(service.command_models, command, invalid)
        assert error.value.code == "invalid_command"
    expected = service.dispatch(command, payload, session_token=token)
    url = "/api/dashboard/period-preparation?" + urlencode(payload)
    assert http.request(url)[0] == 401
    status, _, _, response = http.request(url, headers=headers)
    assert status == 200, response
    assert response == wire_money(expected)
    assert http.request(url + "&unexpected=true", headers=headers)[0] == 400
