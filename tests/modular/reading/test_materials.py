from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path

import pytest

from ai_accounting_modular.contracts.public import ContractError, validate_contract

from ._support import BANK, SOURCE, assemble, expense, fact, source, uid


def materials(book, business, **query):
    return book.get(
        "/api/review/materials",
        company_id=book.company,
        business_id=business["business_id"],
        **query,
    )


def content_query(book, business, response, **extra):
    return {
        "company_id": book.company,
        "business_id": business["business_id"],
        "expected_version": response["snapshot_version"],
        **{name: str(value) for name, value in extra.items()},
    }


def test_25_unique_sources_page_20_plus_5_without_opening_original_bytes(tmp_path, monkeypatch):
    book = assemble(tmp_path)
    sources = [source("evidence-" + str(index)) for index in range(25)]
    cost = expense(sources=[item["source_id"] for item in sources])
    book.submit(cost, sources=sources)
    monkeypatch.setattr(book.records, "lease_source", lambda *_: pytest.fail("列表打开了原件字节"))
    status, first = materials(book, cost)
    assert status == 200, first
    assert first["page"]["total_count"] == 25
    assert len(first["sources"]) == 20
    assert first["page"]["has_more"]
    _, last = materials(
        book, cost, cursor=first["page"]["next_cursor"], expected_version=first["snapshot_version"]
    )
    assert len(last["sources"]) == 5
    assert last["page"]["next_cursor"] is None
    combined = first["sources"] + last["sources"]
    assert [item["source_id"] for item in combined] == sorted(item["source_id"] for item in sources)
    assert all(item["available"] and item["unavailable_reason"] is None for item in combined)
    assert all("relative_path" not in item and "text" not in item for item in combined)


def test_statement_content_matches_list_bytes_and_has_no_business_effect(tmp_path):
    book = assemble(tmp_path)
    cost = expense()
    book.submit(cost)
    status, selection = materials(book, cost)
    assert status == 200, selection
    with book.storage.read_unit(book.company) as read:
        before = read.head
    query = content_query(book, cost, selection)
    with book.dashboard.get(f"/api/review/sources/{SOURCE}/content", query) as result:
        assert result.status == 200, result.body
        validate_contract("review-materials.schema.json", "SourceTextContent", result.body)
        raw = result.body["text"].encode("utf-8")
        assert sha256(raw).hexdigest() == result.body["sha256"] == selection["sources"][0]["sha256"]
        assert len(raw) == selection["sources"][0]["byte_size"]
    with book.storage.read_unit(book.company) as read:
        assert read.head == before


def test_open_old_generation_is_409_and_current_uses_new_source_only(tmp_path):
    book = assemble(tmp_path)
    cost = expense()
    receipt = book.submit(cost)
    ref = next(b for b in receipt["businesses"] if b["business_id"] == cost["business_id"])[
        "results"
    ][0]
    replacement = expense(amount="12000", sources=(uid("replacement"),))
    book.submit(
        mutations=[{"op": "replace", "expected_seq": 1, "fact": replacement}],
        sources=[source("replacement")],
    )
    status, old = materials(book, cost, result_id=ref["result_id"], generation=ref["generation"])
    assert status == 409 and old["code"] == "dashboard_snapshot_changed", old
    status, current = materials(book, cost)
    assert status == 200
    assert current["selection"] == {"mode": "current", "fact_seq": 2}
    assert [item["source_id"] for item in current["sources"]] == [uid("replacement")]


def test_frozen_sources_remain_exact_when_current_fact_uses_other_source(tmp_path):
    book = assemble(tmp_path)
    cost = expense()
    receipt = book.submit(cost)
    ref = next(b for b in receipt["businesses"] if b["business_id"] == cost["business_id"])[
        "results"
    ][0]
    with book.storage.write_unit(book.company) as unit:
        check = book.ledger.check_period("2026-01", unit)
        assert check.ready
        book.ledger.close_period("2026-01", check.head, unit)
    replacement = expense(amount="12000", sources=(uid("replacement"),))
    book.submit(
        mutations=[{"op": "replace", "expected_seq": 1, "fact": replacement}],
        correction_period="2026-02",
        sources=[source("replacement")],
    )
    status, frozen = materials(book, cost, result_id=ref["result_id"], generation=ref["generation"])
    assert status == 200, frozen
    assert frozen["selection"]["mode"] == "frozen"
    assert [item["source_id"] for item in frozen["sources"]] == [SOURCE]
    query = content_query(
        book, cost, frozen, result_id=ref["result_id"], generation=ref["generation"]
    )
    with book.dashboard.get(f"/api/review/sources/{SOURCE}/content", query) as result:
        assert result.status == 200, result.body
    _, current = materials(book, cost)
    assert [item["source_id"] for item in current["sources"]] == [uid("replacement")]


def test_wrong_company_or_unreferenced_source_is_404(tmp_path):
    book = assemble(tmp_path)
    other = assemble(tmp_path / "other")
    cost = expense()
    book.submit(cost, sources=[source("unrelated")])
    status, selection = materials(book, cost)
    assert status == 200
    query = content_query(book, cost, selection)
    with book.dashboard.get(f"/api/review/sources/{uid('unrelated')}/content", query) as result:
        assert result.status == 404 and result.body["code"] == "review_source_not_found"
    query["company_id"] = other.company
    # This catalog does not contain that company; it cannot become the original scope.
    with book.dashboard.get(f"/api/review/sources/{SOURCE}/content", query) as result:
        assert result.status == 404, result.body


def imported_file(book, tmp_path, name="原件脚本.html", content=b"<script>synthetic</script>"):
    with book.storage.read_unit(book.company) as read:
        root = book.storage.company_metadata(read).company_root
    incoming = root / "originals" / name
    incoming.parent.mkdir(parents=True, exist_ok=True)
    incoming.write_bytes(content)
    with book.storage.read_unit(book.company) as read:
        imported = book.records.prepare_source(
            {
                "kind": "file",
                "source_id": uid(name),
                "relative_path": "originals/" + name,
                "archive_month": "2026-01",
            },
            read,
        )
    cost = expense(sources=(imported["source_id"],))
    book.submit(cost, sources=[imported])
    return cost, imported, root / imported["relative_path"], content


def test_registered_file_becoming_unreadable_returns_source_unavailable(tmp_path, monkeypatch):
    book = assemble(tmp_path)
    cost, imported, path, _ = imported_file(book, tmp_path)
    status, selection = materials(book, cost)
    assert status == 200
    original_open = Path.open

    def denied(candidate, *args, **kwargs):
        if candidate == path:
            raise PermissionError("synthetic inaccessible original")
        return original_open(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    query = content_query(book, cost, selection)
    with book.dashboard.get(
        f"/api/review/sources/{imported['source_id']}/content", query
    ) as result:
        assert result.status == 409, result.body
        assert result.body["code"] == "source_unavailable"


def test_file_is_verified_download_with_safe_headers_and_lease_lifetime(tmp_path):
    book = assemble(tmp_path)
    cost, original, _, raw = imported_file(book, tmp_path)
    status, selection = materials(book, cost)
    assert status == 200, selection
    query = content_query(book, cost, selection)
    with book.dashboard.get(
        f"/api/review/sources/{original['source_id']}/content", query
    ) as result:
        assert result.status == 200, result.body
        assert result.headers["Content-Type"] == "application/octet-stream"
        assert result.headers["X-Content-Type-Options"] == "nosniff"
        assert result.headers["Content-Disposition"].startswith("attachment;")
        assert "<script>" not in result.headers["Content-Disposition"]
        assert int(result.headers["Content-Length"]) == len(raw)
        assert result.body.read() == raw
        stream = result.body
    with pytest.raises(ValueError):
        stream.read()


@pytest.mark.parametrize("failure", ["missing", "wrong_size", "same_size"])
def test_missing_and_tampered_file_never_sends_bad_bytes(tmp_path, failure):
    book = assemble(tmp_path)
    cost, original, path, raw = imported_file(book, tmp_path)
    _, selection = materials(book, cost)
    if failure == "missing":
        path.unlink()
    else:
        path.write_bytes(b"x" * (len(raw) + (failure == "wrong_size")))
    status, observed = materials(book, cost)
    assert status == 200, observed
    row = observed["sources"][0]
    if failure == "same_size":
        # Metadata presence is not a claim that a complete content hash was checked.
        assert row["available"]
    else:
        assert not row["available"]
        assert row["unavailable_reason"] == (
            "missing_file" if failure == "missing" else "content_mismatch"
        )
    query = content_query(book, cost, selection)
    with book.dashboard.get(
        f"/api/review/sources/{original['source_id']}/content", query
    ) as result:
        assert result.status == 409
        assert result.body["code"] == (
            "source_unavailable" if failure == "missing" else "source_content_mismatch"
        )


def test_correction_during_original_read_fails_final_fresh_scope_check(tmp_path, monkeypatch):
    book = assemble(tmp_path)
    cost = expense()
    book.submit(cost)
    _, selection = materials(book, cost)
    original = book.records.lease_source

    @contextmanager
    def moving(source_id, read):
        with original(source_id, read) as lease:
            replacement = expense(amount="12000", sources=(uid("replacement"),))
            book.submit(
                mutations=[{"op": "replace", "expected_seq": 1, "fact": replacement}],
                sources=[source("replacement")],
            )
            yield lease

    monkeypatch.setattr(book.records, "lease_source", moving)
    with book.dashboard.get(
        f"/api/review/sources/{SOURCE}/content", content_query(book, cost, selection)
    ) as result:
        assert result.status == 409
        assert result.body["code"] == "dashboard_snapshot_changed"


def test_bad_cursor_and_unpaired_result_or_path_are_rejected(tmp_path):
    book = assemble(tmp_path)
    cost = expense()
    book.submit(cost)
    for query in ({"cursor": "bad"}, {"result_id": uid("x")}, {"path": "../../x"}):
        status, result = materials(book, cost, **query)
        assert status == 400 and result["code"] == "invalid_command"


def test_sender_exception_does_not_get_a_second_http_response(tmp_path):
    book = assemble(tmp_path)
    with pytest.raises(ContractError, match="sender"):
        with book.dashboard.get("/api/dashboard/context"):
            raise ContractError("internal_error", "sender")


def test_legitimate_501_source_locations_are_complete_and_deduplicated(tmp_path):
    book = assemble(tmp_path)
    rows = [
        {
            "row_id": uid("bank-row-" + str(index)),
            "source": {"source_id": SOURCE, "location": f"合成第{index}行"},
            "actual_on": "2026-01-15",
            "direction": "inflow",
            "amount_fen": "1",
        }
        for index in range(501)
    ]
    statement = fact(
        "bank-statement",
        "money.bank_statement",
        {"funds_account_id": BANK, "opening_fen": "0", "closing_fen": "501", "rows": rows},
    )
    book.submit(statement)
    status, selected = materials(book, statement)
    assert status == 200, selected
    assert selected["page"]["total_count"] == 1
    assert len(selected["sources"]) == 1
    assert len(selected["sources"][0]["locations"]) == 501
    assert set(selected["sources"][0]["locations"]) == {f"合成第{index}行" for index in range(501)}
