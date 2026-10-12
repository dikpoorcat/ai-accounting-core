"""Filtered labor people have exact monthly totals and read source details on demand."""

import pytest
from test_dashboard_model_integrity import raw_read
from test_integrity_content import damage
from test_labor_accrual import accrual
from test_labor_assets import cost
from test_payroll_corrections import Company
from test_workforce_monthly_roster import change, ids, worker

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard, _Snapshot
from ai_accounting.kernel.dashboard_async import _produce
from ai_accounting.kernel.dashboard_model_filters import directory_matches
from ai_accounting.kernel.dashboard_model_materializer import _page_sources
from ai_accounting.kernel.dashboard_model_runtime import stable_payload
from ai_accounting.kernel.dashboard_read_model import LoadedModel
from ai_accounting.kernel.domains.labor_assets import LaborProjectCost
from ai_accounting.kernel.domains.opening import CATEGORIES, OpeningPackage
from ai_accounting.kernel.response_contracts import http_response, validate_response


def ensure_labor_opening(company):
    with company.engine.store.connection(read_only=True) as connection:
        initialized = connection.execute(
            "SELECT 1 FROM calculation_publication p JOIN calculation c "
            "ON c.id=p.calculation_id WHERE c.kind=? LIMIT 1", (OpeningPackage.kind,),
        ).fetchone()
    if initialized is None:
        company.save(OpeningPackage(
            period="2026-01", package_id="labor-test-opening",
            counts=dict.fromkeys(CATEGORIES.values(), 0), members=(),
            completeness_confirmed=True,
        ), "labor-test-opening")
        company.publish("labor-test-opening")
    return company


def company_with_labor_opening(path):
    return ensure_labor_opening(Company(path))


def labor_company(tmp_path):
    company = company_with_labor_opening(tmp_path / "labor-people.sqlite")
    paid = worker(company.engine, "有报酬人员", "start_labor", "2026-01-01")
    zero = worker(company.engine, "零报酬人员", "start_labor", "2026-01")
    ident = paid["entity_id"]
    company.save(accrual(person_id=ident, gross_fee_fen=101), "labor-first")
    company.save(accrual(person_id=ident, gross_fee_fen=202), "labor-second")
    company.save(LaborProjectCost(**cost(period="2026-01", person_id=ident, gross_fee_fen=303)),
                 "labor-capital")
    company.publish("labor-first", "labor-second", "labor-capital")
    return company, paid, zero


@pytest.mark.parametrize("closed", [False, True])
def test_each_labor_person_aggregates_all_sources_and_zero_remains_real(tmp_path, closed):
    company, paid, zero = labor_company(tmp_path)
    if closed:
        company.close("2026-01")
    dashboard = Dashboard(company.engine)
    response = dashboard.employees("2026-01")
    validate_response("dashboard_employees", response)
    assert response["schema_version"] == 13
    assert set(response["data"]["collections"]) == {"employees", "labor_workers"}
    people = {item["person_id"]: item
              for item in response["data"]["collections"]["labor_workers"]["items"]}
    amounts = ("gross_fen", "net_fen", "booked_tax_fen", "personal_labor_fen",
               "capitalized_labor_fen", "source_count")
    assert tuple(people[paid["entity_id"]][field] for field in amounts) == (
        606, 606, 0, 303, 303, 3,
    )
    assert tuple(people[zero["entity_id"]][field] for field in amounts) == (0, 0, 0, 0, 0, 0)
    assert not people[zero["entity_id"]]["has_labor_activity"]
    wire = http_response("dashboard_employees", response)
    assert all(type(item["gross_fen"]) is str
               for item in wire["data"]["collections"]["labor_workers"]["items"])
    sources, cursor = [], None
    while True:
        detail = dashboard.employees(
            "2026-01", section="labor_sources", employee_id=paid["entity_id"], limit=1,
            employee_filter="employment_unpaid_leave", cursor=cursor,
            expected_version=response["snapshot_version"],
        )["data"]["collections"]["labor_sources"]
        sources.extend(detail["items"])
        if not detail["page"]["has_more"]:
            break
        cursor = detail["page"]["next_cursor"]
    assert {source["source_id"] for source in sources} == {
        "labor-first", "labor-second", "labor-capital",
    }
    assert sum(source["gross_fen"] for source in sources) == people[paid["entity_id"]]["gross_fen"]
    with raw_read():
        assert dashboard.employees("2026-01") == response


def test_labor_filters_use_the_selected_month_and_keep_missing_relationship_unknown(tmp_path):
    company = company_with_labor_opening(tmp_path / "labor-filter.sqlite")
    active = worker(company.engine, "正常劳务", "start_labor", "2026-01-01")
    departed = worker(company.engine, "结束劳务", "start_labor", "2026-01-01")
    change(company.engine, departed, "end_labor", "2026-02-01", "ended")
    converted = worker(company.engine, "转为员工", "start_labor", "2026-01-01")
    change(company.engine, converted, "to_employee", "2026-02-15", "converted")
    future = worker(company.engine, "后月开始", "start_labor", "2026-03-01")
    company.save(accrual(period="2026-02", person_id="source-only"), "without-relationship")
    company.publish("without-relationship")
    dashboard = Dashboard(company.engine)
    assert ids(dashboard.employees("2026-02"), "labor_workers") == {
        active["entity_id"], converted["entity_id"],
    }
    assert ids(dashboard.employees("2026-02", employee_filter="employment_departed"),
               "labor_workers") == {departed["entity_id"], converted["entity_id"]}
    assert ids(dashboard.employees("2026-02", employee_filter="in_period"),
               "labor_workers") == {active["entity_id"], converted["entity_id"]}
    assert ids(dashboard.employees("2026-02", employee_filter="ended"),
               "labor_workers") == {departed["entity_id"]}
    assert ids(dashboard.employees("2026-02", employee_filter="employment_unknown"),
               "labor_workers") == {"source-only"}
    assert ids(dashboard.employees("2026-02", employee_filter="employment_unpaid_leave"),
               "labor_workers") == set()
    everyone = dashboard.employees("2026-02", employee_filter="all")
    assert ids(everyone, "labor_workers") == {
        active["entity_id"], departed["entity_id"], converted["entity_id"], "source-only",
    }
    assert future["entity_id"] not in ids(everyone, "labor_workers")
    assert everyone["data"]["employees"]["labor_worker_count"] == 2
    assert ids(dashboard.employees("2026-01"), "labor_workers") == {
        active["entity_id"], departed["entity_id"], converted["entity_id"],
    }


def test_labor_person_default_read_does_not_prepare_source_settlements(tmp_path, monkeypatch):
    company, _, _ = labor_company(tmp_path)

    def forbid_labor_summary(snapshot, **options):
        assert not (options.get("subject_ids", set()) & {
            "labor-first", "labor-second", "labor-capital",
        }), "labor details were preloaded on the default person page"
        return original(snapshot, **options)

    original = _Snapshot.settlement_summary
    monkeypatch.setattr(_Snapshot, "settlement_summary", forbid_labor_summary)
    with raw_read():
        response = Dashboard(company.engine).employees("2026-01")
    assert response["data"]["collections"]["labor_workers"]["page"]["total_count"] == 2


@pytest.mark.parametrize("closed", [False, True])
def test_labor_person_model_binds_every_source_and_corruption_fails(tmp_path, closed):
    company, paid, _ = labor_company(tmp_path)
    if closed:
        company.close("2026-01")
    dashboard = Dashboard(company.engine)
    with dashboard._snapshot("2026-01") as snap:
        from ai_accounting.kernel.dashboard import _employees

        data = _employees(snap)
        item = next(person for person in data["collections"]["labor_workers"]["items"]
                    if person["person_id"] == paid["entity_id"])
        bindings, _ = _page_sources(snap, "employees", "labor_workers", [item])
        assert len(bindings) == 3
    damage(company.engine, "calculation",
           "UPDATE calculation SET outcome=json_set(outcome,'$.values.gross_fen',999) "
           "WHERE subject_id='labor-second'")
    with pytest.raises(KernelError) as failure:
        dashboard.employees("2026-01")
    assert failure.value.code == "content_integrity_failed"


def test_active_labor_person_with_only_earlier_sources_has_zero_current_month(tmp_path):
    company, paid, zero = labor_company(tmp_path)
    company.save(accrual(period="2026-02", person_id="other-person", gross_fee_fen=100),
                 "other-current-source")
    company.publish("other-current-source")
    people = Dashboard(company.engine).employees("2026-02")["data"]["collections"][
        "labor_workers"
    ]["items"]
    assert {item["person_id"] for item in people} == {paid["entity_id"], zero["entity_id"]}
    assert all(item["gross_fen"] == item["net_fen"] == item["booked_tax_fen"] == 0
               and item["source_count"] == 0 and not item["has_labor_activity"] for item in people)


@pytest.mark.parametrize("closed", [False, True])
def test_persisted_labor_default_is_consumed_and_still_checks_each_source(
    tmp_path, monkeypatch, closed,
):
    import ai_accounting.kernel.dashboard as dashboard_module
    import ai_accounting.kernel.dashboard_model_materializer as materializer

    company, paid, _ = labor_company(tmp_path)
    if closed:
        company.close("2026-01")
    built = []
    original_build = materializer.build_model

    def build(*args, **options):
        assert options["default_only"] is True
        content = original_build(*args, **options)
        built.append(content)
        return content

    monkeypatch.setattr(materializer, "build_model", build)
    published = _produce(company.engine, "2026-01")
    assert published["status"] == "ready" and len(built) == 1
    default = next(block for block in built[0].blocks
                   if block.key == "employees/default/overview/0")
    assert len(default.sources) == 3
    assert set(default.payload["data"]["collections"]) == {"employees", "labor_workers"}
    dashboard = Dashboard(company.engine)
    with raw_read():
        expected = dashboard.employees("2026-01")
    loaded = []
    original_read = LoadedModel.read_page_block

    def read(model, key, **options):
        block = original_read(model, key, **options)
        if key == "employees/default/overview/0":
            loaded.append(block)
        return block

    def no_native(*_args, **_options):
        raise AssertionError("the persisted labor default fell back to native production")

    monkeypatch.setattr(LoadedModel, "read_page_block", read)
    monkeypatch.setattr(dashboard_module, "_employees", no_native)
    actual = dashboard.employees("2026-01")
    assert actual["snapshot_version"] == published["model_version"]
    assert stable_payload(actual) == stable_payload(expected)
    assert loaded and all(len(block.sources) == 3 for block in loaded)
    person = next(item for item in actual["data"]["collections"]["labor_workers"]["items"]
                  if item["person_id"] == paid["entity_id"])
    assert person["gross_fen"] == 606 and person["source_count"] == 3
    loaded.clear()
    damage(company.engine, "calculation",
           "UPDATE calculation SET outcome=json_set(outcome,'$.values.net_fen',999) "
           "WHERE subject_id='labor-second'")
    with pytest.raises(KernelError) as failure:
        dashboard.employees("2026-01")
    assert failure.value.code == "content_integrity_failed"
    assert loaded, "the damaged source must be rejected while consuming the persisted default"


def test_full_model_labor_all_directory_keeps_month_filters_and_person_totals(tmp_path):
    from ai_accounting.kernel.dashboard_model_materializer import build_model

    company, paid, zero = labor_company(tmp_path)
    departed = worker(company.engine, "以前结束劳务", "start_labor", "2025-12-01")
    change(company.engine, departed, "end_labor", "2026-01-01", "before-selected-month")
    with raw_read():
        content = build_model(
            Dashboard(company.engine), "2026-01", default_only=False, block_size=1,
        )
    index = next(item for item in content.collections
                 if item.page == "employees" and item.variant == "all"
                 and item.section == "labor_workers")
    assert index.total_count == index.filtered_count == 3
    assert set(index.keys) == {paid["entity_id"], zero["entity_id"], departed["entity_id"]}
    blocks = [block for block in content.blocks
              if block.key.startswith("employees/all/labor_workers/")]
    entries = [entry for block in blocks for entry in block.keys]
    expected = {
        "all": set(index.keys),
        "in_period": {paid["entity_id"], zero["entity_id"]},
        "employment_active": {paid["entity_id"], zero["entity_id"]},
        "ended": {departed["entity_id"]},
        "employment_departed": {departed["entity_id"]},
        "employment_unknown": set(),
        "employment_unpaid_leave": set(),
    }
    for selected, identifiers in expected.items():
        assert {entry[0] for entry in entries if directory_matches(
            "employees", "labor_workers", entry,
            {"employee_filter": selected, "employee_id": None},
        )} == identifiers
    people = {item["person_id"]: (item, block)
              for block in blocks
              for item in block.payload["data"]["collections"]["labor_workers"]["items"]}
    person, block = people[paid["entity_id"]]
    assert person["gross_fen"] == 606 and person["source_count"] == 3
    assert len(block.sources) == len(block.item_sources[0]) == 3
    assert all(people[ident][0]["gross_fen"] == people[ident][0]["source_count"] == 0
               for ident in (zero["entity_id"], departed["entity_id"]))
    assert not any(item.page == "employees" and item.section == "labor_sources"
                   for item in content.collections)
