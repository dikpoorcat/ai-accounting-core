"""Real Application, Ledger persistence, Reading and dashboard composition.

Classic Selection/calculations independently check amounts, issues and Sources;
the new full public reads separately check the full/light publication versions.
No owner replacement or prepared values enter these tests.
"""

from copy import deepcopy
from dataclasses import asdict
from uuid import uuid4

import pytest

from ai_accounting_modular.host.public import create_kernel, create_offline_application
from ai_accounting_modular.ledger._closing import closed_report
from ai_accounting_modular.ledger._financial import normalize
from ai_accounting_modular.ledger._financial_calculation import calculate_snapshot, summary_values
from ai_accounting_modular.ledger._financial_cells import Issues
from ai_accounting_modular.ledger._financial_selection import Selection
from ai_accounting_modular.reading import _financial_projection, _monthly
from ai_accounting_modular.storage.public import CompanySpec, OwnerProfile, Tree, create_catalog
from tests.modular.acceptance.performance.ledger_variant import MODES, install_ledger_implementation
from tests.modular.application._working_case import Case


@pytest.fixture(params=MODES)
def live_case(request, tmp_path):
    with install_ledger_implementation(request.param):
        catalog = tmp_path / "catalog.sqlite3"
        create_catalog(catalog, OwnerProfile(str(uuid4()), "真实集成合成负责人", "synthetic-hash"))
        kernel = create_kernel(catalog)
        company = str(uuid4())
        kernel.storage.create_company(
            CompanySpec(
                company,
                str(uuid4()),
                "913100000000000001",
                "真实金融集成合成公司",
                "2026-01",
                tmp_path / "company",
            )
        )
        try:
            yield Case(kernel, company, str(uuid4()), str(uuid4()))
        finally:
            kernel.close()


def month_scope(case, month="2026-01"):
    return _monthly.normalize(
        {"company_id": case.company, "period_from": month, "period_to": month, "view": "as_posted"}
    )


def quarter_scope(case):
    return {
        "company_id": case.company,
        "period_from": "2026-01",
        "period_to": "2026-03",
        "view": "as_posted",
    }


def month_page(case, month="2026-01"):
    return _monthly.deliver(
        case.kernel.reading,
        month_scope(case, month),
        (_monthly.FINANCIAL,),
        lambda snapshot, read: snapshot.components["financial"],
    )


def quarter_page(case):
    return case.kernel.reading.page("quarterly_report", quarter_scope(case), {})


def settle(case):
    for _ in range(8):
        completed = case.kernel.reading.run_pending(case.company)
        with case.kernel.storage.read_unit(case.company) as read:
            states = read.repository("reading").scopes()
        assert not [value for value in states if value["state"] == "failed"], states
        if not completed:
            assert all(value["state"] == "ready" for value in states), states
            return
    pytest.fail("真实金融生产与消费者未能收敛。")


def classic_selection(scope, read):
    return Selection(
        read.repository("ledger"), scope["view"], scope["period_to"], read.start_period, Issues()
    )


def classic_summary(scope, read):
    scope = normalize(scope, read)
    selection = classic_selection(scope, read)
    saved = (
        closed_report(scope, read)
        if read.repository("ledger").is_closed(scope["period_to"], start_period=read.start_period)
        else None
    )
    values = summary_values(scope, selection, saved=saved)
    sources = tuple(
        sorted(
            (selection.source(selection.heads[key]) for key in selection.used),
            key=lambda value: (value["business_id"], value["result_slot"], value["result_id"]),
        )
    )
    return {"scope": scope, **values, "issues": selection.issues.values}, sources


def classic_snapshot(scope, read):
    scope = normalize(scope, read)
    selection = classic_selection(scope, read)
    result = calculate_snapshot(scope, read, selection)
    year, last = scope["period_to"][:4] + "-01", scope["period_to"]
    result["selection"] = {
        "closed_periods": read.repository("ledger").closed_periods(
            year, last, start_period=read.start_period
        ),
        "supplement_periods": sorted(
            {
                selection.heads[key]["posting_period"]
                for key in selection.used
                if selection.heads[key].get("baseline_result_id")
                and year <= selection.heads[key]["posting_period"] <= last
            }
        ),
    }
    for name in ("opening", "continuation"):
        head = getattr(selection, name)
        if head:
            result["selection"][name + "_business_id"] = head["business_id"]
    return result


def assert_month(case, month, expense):
    actual = month_page(case, month)
    assert actual.state == "ready", actual
    with case.kernel.storage.read_unit(case.company) as read:
        expected = case.kernel.ledger.financial_summary(month_scope(case, month), read)
        classic, sources = classic_summary(month_scope(case, month), read)
    value = asdict(expected)
    value.pop("sources")
    value["issues"] = list(value["issues"])
    assert actual.payload["financial"] == value
    assert {key: item for key, item in value.items() if key != "version"} == classic
    assert expected.sources == sources
    assert value["expense"] == {"state": "known", "amount_fen": expense}
    return expected


def assert_quarter(case, profit):
    actual = quarter_page(case)
    assert actual.state == "ready", actual
    with case.kernel.storage.read_unit(case.company) as read:
        expected = case.kernel.ledger.financial_snapshot(quarter_scope(case), read)
        classic = classic_snapshot(quarter_scope(case), read)
    assert actual.payload["snapshot"] == expected
    without_version = deepcopy(expected)
    without_version["selection"].pop("selection_digest")
    assert without_version == classic
    assert expected["profit_statement"]["32"]["current"]["amount_fen"] == profit
    return expected


def sources_for(summary, business_id):
    return tuple(value for value in summary.sources if value["business_id"] == business_id)


def replace(case, fact, amount, seq=1, correction_period=None):
    changed = deepcopy(fact)
    changed["data"]["amount_fen"] = amount
    fields = {"correction_period": correction_period} if correction_period else {}
    result = case.app.accounting_apply(
        case.request(mutations=[{"op": "replace", "expected_seq": seq, "fact": changed}], **fields)
    )
    assert result["completion"] == "complete", result
    return changed


def dashboard(case, route, **parameters):
    with case.kernel.dashboard.get(route, {"company_id": case.company, **parameters}) as response:
        assert response.status == 200, response.body
        return response.body


def test_real_cold_month_quarter_and_dashboard_equal_the_formal_oracle(live_case):
    case = live_case
    expense = case.seed()
    assert month_page(case).state == quarter_page(case).state == "preparing"
    assert dashboard(case, "/api/dashboard/brief", period="2026-01")["state"] == "preparing"
    settle(case)
    summary = assert_month(case, "2026-01", "100000")
    assert [
        value["result_generation"] for value in sources_for(summary, expense["business_id"])
    ] == [1]
    assert_quarter(case, "-100000")
    brief = dashboard(case, "/api/dashboard/brief", period="2026-01")
    assert brief["state"] == "ready"
    assert brief["response"]["data"]["position"] == {
        "month_revenue_fen": "0",
        "month_expense_fen": "100000",
        "month_result_fen": "-100000",
        "complete": True,
    }
    report = dashboard(case, "/api/dashboard/quarterly-report", year="2026", quarter="1")
    assert report["state"] == "ready"
    assert report["response"]["summary"]["current_net_profit_fen"] == "-100000"


def test_real_open_replacement_withdrawal_and_multiple_scope_sources(live_case):
    case = live_case
    expense = case.seed()
    saved = case.app.accounting_apply(case.request(case.expense("70000", "2026-02")))
    assert saved["completion"] == "complete", saved
    month_page(case)
    month_page(case, "2026-02")
    quarter_page(case)
    settle(case)
    before = assert_month(case, "2026-01", "100000")
    old_source = sources_for(before, expense["business_id"])[0]
    replace(case, expense, "120000")
    settle(case)
    after = assert_month(case, "2026-01", "120000")
    actual_sources = sources_for(after, expense["business_id"])
    assert len(actual_sources) == 1 and actual_sources[0]["result_generation"] == 2
    assert old_source not in after.sources
    assert_month(case, "2026-02", "70000")
    assert_quarter(case, "-190000")
    saved = case.app.accounting_apply(
        case.request(
            mutations=[{"op": "withdraw", "business_id": expense["business_id"], "expected_seq": 2}]
        )
    )
    assert saved["completion"] == "complete", saved
    settle(case)
    withdrawn = assert_month(case, "2026-01", "0")
    assert not sources_for(withdrawn, expense["business_id"])
    assert_month(case, "2026-02", "70000")
    assert_quarter(case, "-70000")


def test_real_two_closed_corrections_keep_all_pending_legs_and_frozen_sources(live_case):
    case = live_case
    expense = case.seed()
    month_page(case)
    quarter_page(case)
    settle(case)
    assert case.close(case.check())["state"] == "closed"
    # Publish the closed January artifact before the later, unconsumed changes.
    settle(case)
    january = assert_month(case, "2026-01", "100000")
    january_source = sources_for(january, expense["business_id"])[0]
    replace(case, expense, "120000", correction_period="2026-02")
    february_reference = case.get(expense["business_id"])["businesses"][0]["results"][0][
        "reference"
    ]
    assert february_reference["baseline_result_id"] == january_source["result_id"]
    assert case.close(case.check("2026-02"))["state"] == "closed"
    replace(case, expense, "150000", seq=2, correction_period="2026-03")
    march_reference = case.get(expense["business_id"])["businesses"][0]["results"][0]["reference"]
    assert march_reference["baseline_result_id"] == february_reference["result_id"]
    month_page(case, "2026-02")
    month_page(case, "2026-03")
    with case.kernel.storage.read_unit(case.company) as read:
        dirty = _financial_projection.store(read).work()["changes"]
    changes = [value for value in dirty if value["business_id"] == expense["business_id"]]
    assert len(changes) == 1 and {"2026-02", "2026-03"} <= set(changes[0]["posted_periods"])
    settle(case)
    frozen = assert_month(case, "2026-01", "100000")
    february = assert_month(case, "2026-02", "20000")
    march = assert_month(case, "2026-03", "30000")
    assert frozen == january
    assert {value["result_id"] for value in sources_for(february, expense["business_id"])} == {
        february_reference["result_id"]
    }
    assert {value["result_id"] for value in sources_for(march, expense["business_id"])} == {
        february_reference["result_id"],
        march_reference["result_id"],
    }
    assert_quarter(case, "-150000")


def test_real_future_b_does_not_rewrite_ready_a_and_new_range_catches_up(live_case):
    case = live_case
    case.seed()
    month_page(case)
    settle(case)
    before = month_page(case)
    with case.kernel.storage.read_unit(case.company) as read:
        selected = _financial_projection.request(
            case.kernel.reading, month_scope(case), "summary_values", read
        )
        old_descriptor = _financial_projection.store(read).get_blocks((selected.descriptor_key,))
    saved = case.app.accounting_apply(case.request(case.expense("70000", "2026-07")))
    assert saved["completion"] == "complete", saved
    with case.kernel.storage.read_unit(case.company) as read:
        private = _financial_projection.key(read)
        assert _financial_projection.store(read).work()["pending"] == []
    assert case.kernel.reading.run_pending(case.company) == (private,)
    assert month_page(case) == before
    assert month_page(case, "2026-07").state == "preparing"
    settle(case)
    assert_month(case, "2026-07", "70000")
    assert month_page(case) == before
    with case.kernel.storage.read_unit(case.company) as read:
        assert (
            _financial_projection.store(read).get_blocks((selected.descriptor_key,))
            == old_descriptor
        )


def test_real_verified_backup_restore_rebuilds_from_empty_reading_cache(live_case, tmp_path):
    case = live_case
    expense = case.seed()
    month_page(case)
    quarter_page(case)
    settle(case)
    expected_summary = assert_month(case, "2026-01", "100000")
    expected_report = assert_quarter(case, "-100000")
    with case.kernel.storage.read_unit(case.company) as read:
        metadata = case.kernel.storage.company_metadata(read)
        expected_results = case.kernel.ledger.get_results([expense["business_id"]], read)
    task = case.app.accounting_backup(
        {"company_id": case.company, "expected_database_id": metadata.database_id}
    )
    done = case.kernel.files.run(case.company, task["task_id"])
    assert done["state"] == "succeeded", done
    target = tmp_path / "restored"
    restored = create_offline_application().accounting_isolated(
        {
            "action": "restore",
            "mode": "run",
            "source_path": done["path"],
            "new_target_directory": str(target),
        }
    )
    assert restored["state"] == "created", restored
    assert (
        restored["company_id"] == case.company and restored["database_id"] == metadata.database_id
    )
    catalog = tmp_path / "restored-catalog.sqlite3"
    create_catalog(catalog, OwnerProfile(str(uuid4()), "合成恢复负责人", "synthetic-hash"))
    kernel = create_kernel(catalog)
    try:
        actual_metadata = kernel.storage.register_company(target)
        assert actual_metadata.database_id == metadata.database_id
        actual = Case(kernel, case.company, case.source_id, case.party)
        with kernel.storage.read_unit(case.company) as read:
            repository = read.repository("reading")
            assert repository.scopes() == ()
            for table in ("read_scope_head", "read_component", "read_item"):
                assert repository._session.fetchone(f"SELECT COUNT(*) AS n FROM {table}")["n"] == 0
            assert kernel.ledger.get_results([expense["business_id"]], read) == expected_results
        assert month_page(actual).state == quarter_page(actual).state == "preparing"
        settle(actual)
        assert assert_month(actual, "2026-01", "100000") == expected_summary
        assert assert_quarter(actual, "-100000") == expected_report
    finally:
        kernel.close()


def test_real_hot_one_slot_captures_only_changed_bodies_without_full_source_read(
    live_case, monkeypatch
):
    from ai_accounting_modular.ledger import (
        _financial_projection_bodies,
        _financial_projection_selection,
    )

    case = live_case
    expense = case.seed()
    extra = tuple(case.expense("1000") for _ in range(8))
    saved = case.app.accounting_apply(case.request(*extra))
    assert saved["completion"] == "complete", saved
    month_page(case)
    quarter_page(case)
    settle(case)
    replace(case, expense, "120000")
    capture = _financial_projection_bodies.capture_bodies
    entries = Tree.entries
    captured = []

    def tracked(selection, heads):
        heads = tuple(heads)
        captured.extend((value["business_id"], value["generation"]) for value in heads)
        return capture(selection, heads)

    def forbidden(*args, **kwargs):
        pytest.fail("真实hot生产不应重新展开全公司Sources或回退完整公共财务计算。")

    def bounded_entries(tree, root, *, first=None, last=None):
        if tree.domain == "financial-source-bodies/2" and first is None and last is None:
            pytest.fail("真实hot生产不应遍历完整Source正文目录。")
        return entries(tree, root, first=first, last=last)

    with monkeypatch.context() as patch:
        patch.setattr(_financial_projection_bodies, "capture_bodies", tracked)
        patch.setattr(Tree, "entries", bounded_entries)
        patch.setattr(_financial_projection_selection.ProjectionSelection, "all_sources", forbidden)
        patch.setattr(case.kernel.ledger, "financial_summary", forbidden)
        patch.setattr(case.kernel.ledger, "financial_snapshot", forbidden)
        settle(case)
    assert captured and set(captured) == {(expense["business_id"], 2)}
    assert_month(case, "2026-01", "128000")
    assert_quarter(case, "-128000")
