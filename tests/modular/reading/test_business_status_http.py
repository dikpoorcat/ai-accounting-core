"""The browser receives native progress and uses its own continuation version."""

from tests.modular.application._working_case import case as case

PATH = "/api/dashboard/business-status"


def test_business_progress_http_is_available_without_month_projection(case):
    expense = case.seed()
    query = {
        "company_id": case.company,
        "period": "2026-01",
        "subject_id": expense["business_id"],
        "settlement_view": "historical",
    }
    with case.kernel.dashboard.get(PATH, query) as response:
        assert response.status == 200, response.body
        first = response.body
    assert first["schema_version"] == 10
    assert first["read_context"]["company_id"] == case.company
    assert first["data"]["settlements"]["obligations"][0]["remaining_fen"] == "100000"
    version = first["snapshot_version"]
    with case.kernel.dashboard.get(
        PATH, query | {"section": "settlement_events", "expected_version": version, "limit": "20"}
    ) as response:
        assert response.status == 200, response.body
        assert response.body["snapshot_version"] == version
    with case.kernel.dashboard.get(PATH, query | {"expected_version": "0" * 64}) as response:
        assert response.status == 409
        assert response.body["code"] == "dashboard_snapshot_changed"


def test_business_progress_http_rejects_ambiguous_or_unpaired_scope(case):
    expense = case.seed()
    query = {"company_id": case.company, "period": "2026-01", "subject_id": expense["business_id"]}
    for extra in (
        {"detail_scope_key": "not-a-real-member"},
        {"voucher_version_id": ""},
        {"limit": "501"},
        {"settlement_view": "invented"},
    ):
        with case.kernel.dashboard.get(PATH, query | extra) as response:
            assert response.status == 400, response.body
    with case.kernel.dashboard.get(
        PATH, [*query.items(), ("company_id", case.company)]
    ) as response:
        assert response.status == 400
