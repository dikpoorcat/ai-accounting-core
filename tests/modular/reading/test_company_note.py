"""Company prose has a direct current-company read, separate from monthly models."""

from contextlib import contextmanager
from hashlib import sha256
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.dashboard.public import create_dashboard
from ai_accounting_modular.reading._repository import ReadingRepository
from tests.modular.application._working_case import case as case

ROUTE = "/api/dashboard/company-note"


def save_note(case, text, *, text_format=None, seq=0):
    value = {"scope": "company", "expected_seq": seq, "text": text, "evidence_ids": []}
    if text_format is not None:
        value["text_format"] = text_format
    result = case.app.accounting_apply(case.request(notes=[value]))
    assert result["completion"] == "complete", result
    return result


def get(case, values=None, *, path=ROUTE):
    adapter = create_dashboard(case.kernel.reading)
    with adapter.get(path, [("company_id", case.company)] if values is None else values) as result:
        return result


def state(case):
    with case.kernel.storage.read_unit(case.company) as read:
        path = case.kernel.storage.company_metadata(read).database_path
        return read.head, read.repository("reading").scopes(), sha256(path.read_bytes()).hexdigest()


def five_pages(case):
    return [
        get(case, path="/api/dashboard/" + page)
        for page in ("brief", "funds", "assets", "employees")
    ] + [
        get(
            case,
            [("company_id", case.company), ("year", "2026"), ("quarter", "1")],
            path="/api/dashboard/quarterly-report",
        )
    ]


def test_current_plain_text_is_complete_and_read_only_without_any_business_month(case):
    text = "  *原样* _文字_ 1. 项目 <说明>\n第二行\n\n第三段。  "
    save_note(case, text)
    before = state(case)
    response = get(case)
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    with case.kernel.storage.read_unit(case.company) as read:
        assert response.body["company"] == {
            "company_id": case.company,
            "database_id": read.database_id,
            "credit_code": "913100000000000001",
            "name": "合成应用公司",
        }
    assert response.body["description"] == {"text_format": "plain_text", "text": text}
    assert get(case).body == response.body
    assert case.kernel.reading.page("context", {"company_id": case.company}, {})["periods"] == []
    assert state(case) == before


@pytest.mark.parametrize("text", (None, " \n\t "))
def test_only_absent_or_whitespace_current_description_is_null(case, text):
    if text is not None:
        save_note(case, text)
    before = state(case)
    response = get(case)
    assert response.status == 200 and response.body["description"] is None
    assert state(case) == before


def test_markdown_is_explicit_and_text_is_not_truncated(case):
    text = (
        "# 公司背景\n\n**完整正文**\n\n| 项目 | 说明 |\n| --- | --- |\n"
        + "| 工作 | 原样保存 |\n" * 300
    )
    save_note(case, text, text_format="markdown")
    response = get(case)
    assert response.status == 200
    assert response.body["description"] == {"text_format": "markdown", "text": text}


@pytest.mark.parametrize(
    "values",
    (
        [],
        [("company_id", "")],
        [("company_id", "not-a-company-id")],
        [("company_id", "00000000-0000-4000-8000-000000000101"), ("company_id", "duplicate")],
        [("period", "2026-01")],
        [("text", "不能写入")],
        [("view", "current")],
        [("limit", "20")],
        [("company_id", 123)],
    ),
)
def test_invalid_http_parameters_are_400_and_do_not_open_a_company(case, monkeypatch, values):
    def forbidden(*args, **kwargs):
        raise AssertionError("非法参数不能打开公司数据库。")

    monkeypatch.setattr(case.kernel.storage, "read_unit", forbidden)
    response = get(case, values)
    assert response.status == 400 and response.body["code"] == "invalid_command"
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("name", ("period", "text", "view", "limit", "cursor"))
def test_extra_parameters_are_rejected_even_with_a_valid_company(case, monkeypatch, name):
    def forbidden(*args, **kwargs):
        raise AssertionError("多余参数不能打开公司数据库。")

    monkeypatch.setattr(case.kernel.storage, "read_unit", forbidden)
    response = get(case, [("company_id", case.company), (name, "value")])
    assert response.status == 400


@pytest.mark.parametrize("scope, query", (({}, {}), ({"period": "2026-01"}, {}), (None, {})))
def test_detail_requires_a_company_scope(case, scope, query):
    with pytest.raises(ContractError):
        case.kernel.reading.detail("company_note", scope, query)


@pytest.mark.parametrize("query", ({"period": "2026-01"}, None, []))
def test_detail_does_not_accept_a_month_or_an_ambiguous_query(case, query):
    with pytest.raises(ContractError):
        case.kernel.reading.detail("company_note", {"company_id": case.company}, query)


def test_unknown_company_is_404_and_post_does_not_create_a_write_entry(case):
    response = get(case, [("company_id", str(uuid4()))])
    assert response.status == 404 and response.body["code"] == "not_found"
    response = create_dashboard(case.kernel.reading).post(
        ROUTE, {"company_id": case.company, "text": "不能写入"}
    )
    assert response.status == 404
    assert get(case).body["description"] is None


@pytest.mark.parametrize("code, status", (("storage_corrupt", 500), ("temporary_failure", 503)))
def test_read_failure_never_becomes_a_successful_missing_description(
    case, monkeypatch, code, status
):
    def failed(*args, **kwargs):
        raise ContractError(code, "合成说明读取失败。")

    monkeypatch.setattr(case.kernel.records, "current_notes", failed)
    response = get(case)
    assert response.status == status and response.body["code"] == code
    assert "description" not in response.body
    assert response.headers["Cache-Control"] == "no-store"


def test_one_snapshot_reads_only_selected_company_metadata_and_company_note(case, monkeypatch):
    save_note(case, "当前公司说明。")
    observations = []
    original_read = case.kernel.storage.read_unit
    original_metadata = case.kernel.storage.company_metadata
    original_notes = case.kernel.records.current_notes

    @contextmanager
    def snapshot(company_id):
        assert company_id == case.company
        with original_read(company_id) as read:
            observations.append(("snapshot", id(read)))
            yield read

    def metadata(read):
        observations.append(("metadata", id(read)))
        return original_metadata(read)

    def notes(scopes, read):
        assert scopes == ("company",)
        observations.append(("notes", id(read)))
        return original_notes(scopes, read)

    def forbidden(*args, **kwargs):
        raise AssertionError("公司说明不依赖月份、业务、凭证、目录扫描或标准文件。")

    monkeypatch.setattr(case.kernel.storage, "read_unit", snapshot)
    monkeypatch.setattr(case.kernel.storage, "company_metadata", metadata)
    monkeypatch.setattr(case.kernel.records, "current_notes", notes)
    monkeypatch.setattr(case.kernel.storage, "list_companies", forbidden)
    monkeypatch.setattr(case.kernel.reading.businesses, "work_scope", forbidden)
    monkeypatch.setattr(case.kernel.records, "lease_document_set", forbidden)
    monkeypatch.setattr(case.kernel.ledger, "financial_snapshot", forbidden)
    monkeypatch.setattr(ReadingRepository, "scopes", forbidden)
    assert get(case).status == 200
    assert [name for name, _ in observations] == ["snapshot", "metadata", "notes"]
    assert len({identity for _, identity in observations}) == 1


@pytest.mark.parametrize("already_ready", (False, True))
def test_company_note_update_does_not_scan_or_invalidate_five_pages(
    case, monkeypatch, already_ready
):
    case.seed()
    initial = five_pages(case)
    assert all(response.status == 200 for response in initial)
    if already_ready:
        case.kernel.reading.run_pending(case.company)
        assert all(response.body["state"] == "ready" for response in five_pages(case))
    before = state(case)[1]

    def forbidden(*args, **kwargs):
        raise AssertionError("只修改公司说明不能枚举、失效或重算月度读取目标。")

    with monkeypatch.context() as patch:
        patch.setattr(ReadingRepository, "scopes", forbidden)
        patch.setattr(case.kernel.ledger, "financial_snapshot", forbidden)
        result = save_note(case, "新的公司背景。", text_format="markdown")
        assert result["accounting"] == result["reading"] == "unchanged"
        assert get(case).body["description"]["text"] == "新的公司背景。"
    assert state(case)[1] == before


def test_five_pages_never_load_the_company_note_as_part_of_their_default_reads(case, monkeypatch):
    case.seed()
    save_note(case, "不应混进月页的公司背景。", text_format="markdown")
    scopes = []
    original = case.kernel.records.current_notes

    def notes(selected, read):
        scopes.append(tuple(selected))
        assert "company" not in selected
        return original(selected, read)

    monkeypatch.setattr(case.kernel.records, "current_notes", notes)
    assert all(response.status == 200 for response in five_pages(case))
    case.kernel.reading.run_pending(case.company)
    responses = five_pages(case)
    assert all(
        response.status == 200 and response.body["state"] == "ready" for response in responses
    )
    assert all("不应混进月页" not in str(response.body) for response in responses)
    assert scopes == [("period:2026-01",)]


def test_company_note_in_a_mixed_business_update_does_not_hide_the_real_business_impact(case):
    case.seed()
    five_pages(case)
    case.kernel.reading.run_pending(case.company)
    assert all(response.body["state"] == "ready" for response in five_pages(case))
    result = case.app.accounting_apply(
        case.request(
            case.expense("30000"),
            notes=[
                {
                    "scope": "company",
                    "expected_seq": 0,
                    "text": "同批补充的当前背景。",
                    "text_format": "markdown",
                    "evidence_ids": [],
                }
            ],
        )
    )
    assert result["completion"] == "complete" and result["reading"] == "preparing", result
    assert get(case, path="/api/dashboard/brief").body["state"] == "preparing"
    assert (
        get(
            case,
            [("company_id", case.company), ("year", "2026"), ("quarter", "1")],
            path="/api/dashboard/quarterly-report",
        ).body["state"]
        == "preparing"
    )
    assert get(case).body["description"] == {
        "text_format": "markdown",
        "text": "同批补充的当前背景。",
    }
