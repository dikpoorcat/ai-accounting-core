"""Deferred dashboard reads retain amounts and validate preparation in one snapshot."""

from urllib.parse import urlencode

import pytest
import test_reports as report_cases
from entity_fixture import seed_entities
from test_dashboard_transport import _publish_expense, authenticated
from test_integrity_content import damage
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
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.types import YearMonth

book = report_cases.book
resident = resident_fixture
DAY = "2026-09-13"
CHECK_KEYS = {"materials", "accounting", "close_requirements"}
SECTIONS = ("activity", "open_items")


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
    assert {"period_preparation", "material_completeness", "validation"}.isdisjoint(data)
    assert all(item["status"] == "ai_reviewing" for item in data["risks"])


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
    forbid_preparation(monkeypatch)
    complete = dashboard.brief("2026-03", limit=1, preparation="complete")
    assert complete["data"]["activity_count"] == 2
    assert complete["data"]["position"]["month_expense_fen"] == 2000
    assert complete["data"]["funds_overview"]["outflow_fen"] == 10000
    # The earlier month's cost is paid this month; it does not become this month's expense.
    posted = [
        event
        for subject in ("payment", "second-march-voucher")
        for event in BusinessQueries(engine).business_status(subject, "2026-03")["as_posted"][
            "voucher_events"
        ]
        if event["posting_period"] == "2026-03"
    ]
    assert sum(line["debit"] for event in posted for line in event["lines"]) == 12000
    group = complete["data"]["collections"]["activity"]["items"][0]
    assert group["group_key"] and group["member_count"] == 1
    member = dashboard.brief_group(
        "2026-03", section="activity", group_key=group["group_key"],
        expected_version=complete["snapshot_version"],
    )["data"]["collections"]["members"]["items"][0]
    assert member["subject_id"]
    deferred = dashboard.brief("2026-03", limit=1, preparation="deferred")
    assert deferred["schema_version"] == complete["schema_version"] == 17
    context = assert_context(deferred, engine)
    assert context["read_version"] != deferred["snapshot_version"]
    assert_pending_checks(deferred)
    for key in ("snapshot_version", "selected_period", "read_semantics"):
        assert deferred[key] == complete[key]
    assert without_clock(deferred["data"]) == without_clock(complete["data"])
    for section in SECTIONS:
        page = dashboard.brief("2026-03", section=section, limit=1, preparation="deferred")
        assert page["read_context"] == context
        assert_pending_checks(page)
        assert section in page["data"]["collections"]
        assert page["data"]["position"]["month_expense_fen"] == 2000
        assert page["data"]["funds_overview"]["outflow_fen"] == 10000
        assert page["data"]["position"] == deferred["data"]["position"]
        if section == "activity":
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
            assert len(next_page["data"]["collections"]["activity"]["items"]) == 1
            assert (
                next_page["data"]["collections"]["activity"]["items"][0]["key"]
                != page["data"]["collections"]["activity"]["items"][0]["key"]
            )

    damage(
        engine,
        "monthly_account",
        "UPDATE monthly_account SET debit=debit+1 WHERE period=? AND account=("
        "SELECT min(account) FROM monthly_account WHERE period=?)",
        (YearMonth("2026-03").ordinal, YearMonth("2026-03").ordinal),
    )
    with pytest.raises(KernelError) as failure:
        dashboard.brief("2026-03", preparation="deferred")
    assert failure.value.code == "content_integrity_failed"


def test_deferred_report_keeps_open_and_closed_sources_and_export_contract(book, monkeypatch):
    report_cases.scenario(book)
    dashboard = Dashboard(book[0])
    for closed in (False, True):
        if closed:
            report_cases.close_quarter(book)
        with monkeypatch.context() as guard:
            forbid_preparation(guard)
            complete = dashboard.quarterly_report(2026, 1, preparation="complete")
            deferred = dashboard.quarterly_report(2026, 1, preparation="deferred")
        assert complete["export"]["available"] is closed
        assert deferred["schema_version"] == complete["schema_version"] == 5
        assert deferred["projection"] == "dashboard_quarterly_report_deferred"
        assert_context(deferred, book[0])
        assert "period_preparations" not in deferred and "period_preparations" not in complete
        assert without_clock(
            {
                k: v
                for k, v in deferred.items()
                if k not in {"projection", "read_context", "period_preparations"}
            }
        ) == without_clock(
            {k: v for k, v in complete.items() if k not in {"read_context", "period_preparations"}}
        )


def test_preparation_restores_original_checks_inside_the_validated_read_snapshot(book, monkeypatch):
    report_cases.scenario(book)
    engine = book[0]
    dashboard = Dashboard(engine)
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
    assert result["schema_version"] == 4
    assert result["projection"] == "dashboard_period_preparation_result"
    assert result["read_context"] == context and result["period"] == "2026-03"
    checks = result["data"]["brief_checks"]
    core = BusinessQueries(engine).period_readiness("2026-03", as_of=DAY)
    assert (
        result["data"]["period_preparation"]["readiness"]
        == dashboard_module.preparation_view(core)["readiness"]
    )
    expected_checks = dashboard_module._brief_checks(core)
    assert checks["issues"] == expected_checks["issues"]
    assert checks["items"] == expected_checks["items"]
    assert (
        checks["material_completeness"]["satisfied"]
        == expected_checks["material_completeness"]["satisfied"]
    )
    assert (
        checks["material_completeness"]["issues"]
        == expected_checks["material_completeness"]["issues"]
    )
    assert len(checks["material_completeness"]["coverage_digest"]) == 64
    assert {item["key"] for item in checks["items"]} == CHECK_KEYS
    assert checks["attention_count"] == len(checks["material_completeness"]["issues"]) + len(
        checks["issues"]
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
                tmp_path / filename, production_bundle(), company, "911100000000000001", database
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
            tmp_path / "empty.sqlite",
            production_bundle(),
            "empty",
            "911100000000000001",
            "empty-db",
        )
    )
    forbid_preparation(monkeypatch)
    result = Dashboard(engine).brief(preparation="deferred")
    assert result["schema_version"] == 17
    assert result["read_context"]["company_id"] == "empty"
    assert result["read_context"]["database_id"] == "empty-db"
    assert result["data"] is None


def test_deferred_commands_and_authenticated_http_are_registered_and_strict(resident, monkeypatch):
    service, _, _, http, _ = resident
    headers, token = authenticated(resident)
    company = service.catalog.create_company("91310000123456789A", "分段合成公司")["id"]
    company_engine = service.engine(company)
    seed_entities(company_engine, (("expense-supplier", "organization", None),))
    _publish_expense(company_engine, "expense", 12345)
    schema = service.dispatch("schema", {"view": "full"})["command_schemas"]
    command = "dashboard_period_preparation"
    assert {"company_id", "period", "as_of", "expected_read_version"} <= set(
        schema[command]["required"]
    )
    for name, parameters in (
        ("dashboard_brief", {"period": "2026-09"}),
        ("dashboard_quarterly_report", {"year": 2026, "quarter": 3}),
    ):
        assert validate_command(
            service.command_models, name, {"company_id": company, **parameters}
        )["preparation"] == ("deferred" if name == "dashboard_brief" else "complete")
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
