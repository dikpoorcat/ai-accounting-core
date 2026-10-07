"""Small mock transport tests; these do not constitute packaged AI acceptance."""

import asyncio
import copy
from types import SimpleNamespace

import pytest
import stage9_package_agent_harness as harness

from ai_accounting.kernel.build import calculator_build_id
from ai_accounting.kernel.schema_bundle import production_bundle

ACTUAL_BUILD_ID = calculator_build_id()


def seed_fixture(tmp_path):
    formats = {kind: production_bundle().database_format(kind) for kind in ("company", "catalog")}
    companies = []
    for label, period in (("A", "2026-09"), ("B", "2026-08")):
        taxpayer = "91310000123456789" + label
        companies.append({
            "company": {"id": "company-" + label, "database_id": "database-" + label,
                        "taxpayer_id": taxpayer, "name": "合成" + label,
                        "path": str(tmp_path / "data" / taxpayer / "company.sqlite")},
            "open_periods": [period], "state": [1, 0, 0, 0, 1, 0],
        })
    return formats, {
        "seed_schema_version": 2, "status": "complete",
        "completion_scope": "isolated_draft_seed_construction_not_ai_acceptance",
        "root": str(tmp_path / "data"), "database_formats": formats, "companies": companies,
        "owner_provisioned": False, "ai_acceptance_status": "not_started",
        "source_snapshot_sha256": "a" * 64, "source_build_id": ACTUAL_BUILD_ID,
    }


def test_explicit_two_company_draft_seed_and_catalog_order(tmp_path):
    formats, seed = seed_fixture(tmp_path)
    cases = harness.validate_draft_seed(tmp_path, formats, seed)
    actual = [case["company"] for case in reversed(cases)]
    harness.assert_seed_companies(cases, actual)
    actual[0] = {**actual[0], "database_id": "other"}
    with pytest.raises(ValueError, match="exact seed company"):
        harness.assert_seed_companies(cases, actual)


@pytest.mark.parametrize("damage", [
    "old_unversioned", "old_one_company", "bool_version", "released", "format",
    "duplicate_id", "duplicate_database", "duplicate_taxpayer", "path", "month", "state",
    "owner", "already_run", "source_hash",
])
def test_seed_guard_rejects_scope_format_and_unqualified_old_shapes(tmp_path, damage):
    formats, seed = seed_fixture(tmp_path)
    if damage == "old_unversioned":
        del seed["seed_schema_version"]
    elif damage == "old_one_company":
        seed["companies"].pop()
    elif damage == "bool_version":
        seed["seed_schema_version"] = True
    elif damage == "released":
        formats["company"] = {**formats["company"], "status": "released", "version": 1}
    elif damage == "format":
        seed["database_formats"] = copy.deepcopy(formats)
        seed["database_formats"]["company"]["fingerprint"] = "c" * 64
    elif damage.startswith("duplicate_"):
        key = {"duplicate_id": "id", "duplicate_database": "database_id",
               "duplicate_taxpayer": "taxpayer_id"}[damage]
        seed["companies"][1]["company"][key] = seed["companies"][0]["company"][key]
        if key == "taxpayer_id":
            seed["companies"][1]["company"]["path"] = seed["companies"][0]["company"]["path"]
    elif damage == "path":
        seed["companies"][0]["company"]["path"] = str(tmp_path / "outside.sqlite")
    elif damage == "month":
        seed["companies"][0]["open_periods"] = []
    elif damage == "state":
        seed["companies"][0]["state"][0] = True
    elif damage == "owner":
        seed["owner_provisioned"] = True
    elif damage == "already_run":
        seed["ai_acceptance_status"] = "passed"
    else:
        seed["source_snapshot_sha256"] = "unbound"
    with pytest.raises(ValueError):
        harness.validate_draft_seed(tmp_path, formats, seed)


@pytest.mark.parametrize("build_id", [
    ACTUAL_BUILD_ID.split(":", 1)[1], "a" * 64, None, 1, "local-kernel-1:" + "a" * 64,
])
def test_real_build_descriptor_is_not_a_raw_source_sha(tmp_path, build_id):
    formats, seed = seed_fixture(tmp_path)
    assert seed["source_build_id"] == ACTUAL_BUILD_ID
    assert harness.validate_draft_seed(tmp_path, formats, seed) == seed["companies"]
    seed["source_build_id"] = build_id
    with pytest.raises(ValueError):
        harness.validate_draft_seed(tmp_path, formats, seed)


def mocked_pair(case):
    context = {
        "company_id": case["company"]["id"], "database_id": case["company"]["database_id"],
        "as_of": "2026-10-03", "read_version": case["company"]["id"] + "-read",
    }
    brief = {
        "schema_version": 15, "read_context": context,
        "selected_period": {"key": case["open_periods"][0]},
        "data": {"activity_count": 1, "funds_overview": {"total_fen": 100}, "position": {}},
    }
    funds = {"read_context": copy.deepcopy(context), "data": {"total_fen": 100}}
    return brief, funds


class MockSession:
    def __init__(self, seed, formats, reads, *, damage=None):
        self.seed, self.formats, self.reads = seed, formats, reads
        self.calls = []
        self.damage = damage

    async def call_tool(self, tool, arguments):
        self.calls.append((tool, arguments))
        if tool == "finance_local_schema":
            value = {"database_formats": self.formats}
        elif arguments["command"] == "companies":
            value = {"items": [case["company"] for case in self.seed["companies"]]}
        else:
            payload = arguments["payload"]
            index = int(arguments["command"] == "dashboard_funds")
            value = copy.deepcopy(self.reads[payload["company_id"]][index])
            if self.damage and index:
                value["read_context"][self.damage] = "wrong"
        return SimpleNamespace(isError=False, structuredContent=value)


def mock_owner_checker(brief, funds, *, company_id, period):
    # Only scope/association is modeled here. Production response validation is
    # independently exercised by test_stage9_package_owner_contracts.py.
    assert brief["read_context"]["company_id"] == company_id
    assert funds["read_context"]["company_id"] == company_id
    assert brief["selected_period"]["key"] == period


def test_mcp_preflight_observes_both_exact_companies_and_periods(tmp_path, monkeypatch):
    formats, seed = seed_fixture(tmp_path)
    cases = harness.validate_draft_seed(tmp_path, formats, seed)
    reads = {case["company"]["id"]: mocked_pair(case) for case in cases}
    session = MockSession(seed, formats, reads)
    monkeypatch.setattr(harness, "assert_owner_brief_result", mock_owner_checker)
    result = asyncio.run(harness.preflight_seed_mcp(session, cases, reads, formats))
    assert [row["company"] for row in result] == [case["company"] for case in cases]
    assert [row["period"] for row in result] == ["2026-09", "2026-08"]
    assert [arguments["payload"] for _, arguments in session.calls[2:]] == [
        {"company_id": case["company"]["id"], "period": case["open_periods"][0]}
        for case in cases for _ in range(2)
    ]


@pytest.mark.parametrize("business_changed", [False, True])
def test_mock_preflight_ignores_only_brief_generation_time(tmp_path, monkeypatch, business_changed):
    formats, seed = seed_fixture(tmp_path)
    cases = harness.validate_draft_seed(tmp_path, formats, seed)
    reads = {case["company"]["id"]: mocked_pair(case) for case in cases}
    for brief, _funds in reads.values():
        brief["data"]["generated_at"] = "2026-10-03T00:00:00+00:00"
    before = copy.deepcopy(reads)

    class LaterGenerationSession(MockSession):
        async def call_tool(self, tool, arguments):
            result = await super().call_tool(tool, arguments)
            if arguments.get("command") == "dashboard_brief":
                data = result.structuredContent["data"]
                data["generated_at"] = "2026-10-03T00:00:01+00:00"
                if business_changed:
                    data["activity_count"] += 1
            return result

    monkeypatch.setattr(harness, "assert_owner_brief_result", mock_owner_checker)
    session = LaterGenerationSession(seed, formats, reads)
    if business_changed:
        with pytest.raises(AssertionError):
            asyncio.run(harness.preflight_seed_mcp(session, cases, reads, formats))
    else:
        assert len(asyncio.run(harness.preflight_seed_mcp(session, cases, reads, formats))) == 2
    assert reads == before


@pytest.mark.parametrize("damage", ["company_id", "database_id", "as_of", "read_version"])
def test_mock_preflight_rejects_cross_company_or_snapshot_response(tmp_path, monkeypatch, damage):
    formats, seed = seed_fixture(tmp_path)
    cases = harness.validate_draft_seed(tmp_path, formats, seed)
    reads = {case["company"]["id"]: mocked_pair(case) for case in cases}
    monkeypatch.setattr(harness, "assert_owner_brief_result", mock_owner_checker)
    with pytest.raises(AssertionError):
        asyncio.run(harness.preflight_seed_mcp(
            MockSession(seed, formats, reads, damage=damage), cases, reads, formats,
        ))


@pytest.mark.parametrize("structured", [None, {"code": "invalid_arguments"}])
def test_mcp_tool_error_preserves_non_json_validation_text(structured):
    value = harness.mcp_result_value(SimpleNamespace(
        isError=True, structuredContent=structured,
        content=[SimpleNamespace(text="Arguments validation error: action is required")],
    ))
    assert value["status"] == "rejected" and value["code"] == "mcp_tool_error"
    assert value["mcp_is_error"] is True
    assert value["structured_content"] == structured
    assert "action is required" in value["message"]


def test_pending_requests_survive_error_and_completed_receipts_are_immutable(tmp_path):
    (tmp_path / "requests").mkdir()
    (tmp_path / "responses").mkdir()
    requests = tmp_path / "requests"
    responses = tmp_path / "responses"
    harness.write_json(requests / "0-done.json", {"tool": "done", "arguments": {}})
    harness.write_json(responses / "0-done.json", {"result": "original receipt"})
    completed = (responses / "0-done.json").read_bytes()
    (requests / "1-invalid.json").write_text("invalid JSON", encoding="utf-8")
    for name, tool in (("2-error", "error"), ("3-unknown", "unknown"), ("4-read", "read")):
        harness.write_json(requests / (name + ".json"), {"tool": tool, "arguments": {}})

    class Session:
        def __init__(self):
            self.calls = []

        async def call_tool(self, tool, arguments):
            self.calls.append(tool)
            if tool == "unknown":
                raise ConnectionError("not a known business outcome")
            return SimpleNamespace(
                isError=tool == "error", structuredContent=None,
                content=[SimpleNamespace(text="invalid action" if tool == "error"
                                         else '{"status":"verified"}')],
            )

    session = Session()
    asyncio.run(harness.process_mcp_requests(tmp_path, session))
    assert session.calls == ["error", "unknown", "read"]
    assert (responses / "0-done.json").read_bytes() == completed
    assert harness.json.loads((responses / "1-invalid.json").read_text())["code"] == (
        "relay_invalid_request"
    )
    assert harness.json.loads((responses / "2-error.json").read_text())["mcp_is_error"] is True
    assert harness.json.loads((responses / "3-unknown.json").read_text())["status"] == "unknown"
    assert harness.json.loads((responses / "4-read.json").read_text())["status"] == "verified"
    receipts = {path.name: path.read_bytes() for path in responses.iterdir()}
    asyncio.run(harness.process_mcp_requests(tmp_path, session))
    assert session.calls == ["error", "unknown", "read"]
    assert receipts == {path.name: path.read_bytes() for path in responses.iterdir()}
