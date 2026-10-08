"""Reading reuse is disposable and never skips an accounting source check."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from types import SimpleNamespace

import pytest
from openpyxl import Workbook

from ai_accounting.kernel import inspection_cache, materials
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.inspection_cache import CACHE_FORMAT, MAX_CACHE_BYTES, InspectionCache
from ai_accounting.kernel.materials import Materials, Specification, inspect_bytes
from ai_accounting.kernel.permissions import (
    assert_private_directory,
    assert_private_file,
    create_private_file,
)
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.types import canonical


def csv_spec():
    return {
        "format": "csv",
        "columns": [
            {"column": "A", "role": "context"},
            {"column": "B", "role": "amount"},
            {"column": "C", "role": "recognition_period"},
        ],
    }


@pytest.fixture
def company(tmp_path):
    engine = Engine(
        Store.create(
            tmp_path / "company.sqlite",
            production_bundle(),
            "company",
            "91310000123456789A",
            "database",
        )
    )
    cache = InspectionCache(tmp_path, build_id="test-build")
    return SimpleNamespace(
        engine=engine, cache=cache, reader=Materials(engine, inspection_cache=cache), root=tmp_path
    )


def register(company, raw, request="source"):
    return company.engine.register_evidence(
        raw, "application/octet-stream", "synthetic.csv", request_id=request
    )["digest"]


def spy_parser(monkeypatch):
    calls = []
    original = materials.inspect_bytes

    def counted(raw, spec):
        calls.append((raw, spec))
        return original(raw, spec)

    monkeypatch.setattr(materials, "inspect_bytes", counted)
    return calls


def result_files(cache):
    return [
        path
        for path in cache.directory.glob("inspection-*.json")
        if not path.name.endswith(".used.json")
    ]


def stored_bytes(cache):
    return sum(path.stat().st_size for path in cache.directory.glob("inspection-*.json"))


def request(cache, label, *, parse=None, company_id="company", database_id="database", spec=None):
    raw = label.encode()
    return cache.inspect(
        company_id=company_id,
        database_id=database_id,
        evidence_digest=hashlib.sha256(raw).hexdigest(),
        specification=spec or {"format": "text"},
        raw=raw,
        parse=parse
        or (
            lambda: {
                "items": [{"location": label, "amount_fen": 2**53 + 1}],
                "issues": [],
                "coverage": [],
                "control_totals": [],
            }
        ),
    )


def test_constructor_is_lazy_and_default_quota_is_global_512_mib(tmp_path):
    cache = InspectionCache(tmp_path, build_id="build")
    assert cache.max_bytes == MAX_CACHE_BYTES == 512 * 1024 * 1024
    assert not cache.directory.exists()


def test_pages_parse_once_and_copy_only_bounded_response(company, monkeypatch):
    calls = spy_parser(monkeypatch)
    raw = b"name,amount,period\n" + b"a,10.00,2026-01\n" * 300
    proof = register(company, raw)
    copied_sizes = []
    original_copy = materials.deepcopy

    def bounded_copy(response):
        copied_sizes.append((len(response["items"]), len(response["coverage"])))
        return original_copy(response)

    monkeypatch.setattr(materials, "deepcopy", bounded_copy)
    first = company.reader.inspect(proof, csv_spec(), limit=100)
    second = company.reader.inspect(proof, csv_spec(), after=first["next_cursor"], limit=100)
    third = company.reader.inspect(proof, csv_spec(), after=second["next_cursor"], limit=100)
    assert len(calls) == 1
    assert copied_sizes == [(100, 100)] * 3
    assert result_files(company.cache)[0].stat().st_size > 64 * 1024
    assert third["next_cursor"] is None
    assert second["items"][0]["location"] == "CSV!B102"
    first["items"][0]["amount_fen"] = 999
    assert company.reader.inspect(proof, csv_spec(), limit=1)["items"][0]["amount_fen"] == 1000


def test_restart_reuses_complete_file_including_hidden_diagnostics_and_controls(
    company, monkeypatch
):
    book = Workbook()
    sheet = book.active
    sheet.title = "工资"
    sheet.append(["name", "amount", "period"])
    sheet.append(["a", "90071992547409.93", "2026-01"])
    sheet.append(["b", "broken", "invalid"])
    sheet.append(["total", "90071992547409.93", "2026-01"])
    sheet.row_dimensions[2].hidden = True
    stream = BytesIO()
    book.save(stream)
    raw = stream.getvalue()
    spec = csv_spec() | {"format": "xlsx", "total_rows": {"工资": [4]}}
    expected = inspect_bytes(raw, Specification.model_validate_json(canonical(spec)))
    proof = register(company, raw)
    calls = spy_parser(monkeypatch)
    page = company.reader.inspect(proof, spec, limit=1)
    assert page["items"][0]["hidden"]
    assert page["items"][0]["amount_fen"] == 9007199254740993
    path = result_files(company.cache)[0]
    assert_private_directory(company.cache.directory)
    assert_private_file(path)
    persisted = json.loads(path.read_bytes())
    assert persisted["cache_format"] == CACHE_FORMAT
    assert persisted["result"] == expected
    assert any(item["hidden"] for item in persisted["result"]["coverage"])
    assert persisted["result"]["issues"]
    assert persisted["result"]["control_totals"]
    restarted = InspectionCache(company.root, build_id="test-build")
    restarted_reader = Materials(company.engine, inspection_cache=restarted)
    assert restarted_reader.inspect(proof, spec, limit=1) == page
    assert len(calls) == 1


def test_cache_identity_binds_company_database_mapping_source_and_build(tmp_path):
    calls = []

    def parse():
        calls.append(1)
        return {"items": [], "issues": [], "coverage": [], "control_totals": []}

    cache = InspectionCache(tmp_path, build_id="a")
    request(cache, "source", parse=parse, spec={"format": "text", "a": 1, "b": 2})
    request(cache, "source", parse=parse, spec={"b": 2, "a": 1, "format": "text"})
    assert len(calls) == 1  # Canonical mapping order does not affect identity.
    request(cache, "source", parse=parse, company_id="second")
    request(cache, "source", parse=parse, database_id="second")
    request(cache, "source", parse=parse, spec={"format": "image"})
    request(cache, "different-source", parse=parse)
    request(InspectionCache(tmp_path, build_id="b"), "source", parse=parse)
    assert len(calls) == 6
    assert len(result_files(cache)) == 6


@pytest.mark.parametrize("change", ["mapping", "source", "build"])
def test_pagination_rejects_changed_mapping_source_or_build(company, change):
    proof = register(company, b"name,amount,period\na,1.00,2026-01\nb,2.00,2026-01\n")
    first = company.reader.inspect(proof, csv_spec(), limit=1)
    reader, spec = company.reader, csv_spec()
    if change == "mapping":
        spec["columns"][0]["label"] = "changed"
    elif change == "source":
        proof = register(company, b"name,amount,period\nc,3.00,2026-01\n", "another")
    else:
        reader = Materials(
            company.engine, inspection_cache=InspectionCache(company.root, build_id="changed-build")
        )
    with pytest.raises(KernelError) as caught:
        reader.inspect(proof, spec, after=first["next_cursor"], limit=1)
    assert caught.value.code == "material_cursor_stale"


@pytest.mark.parametrize("damage", ["json", "digest", "duplicate", "shape", "identity"])
def test_corrupt_cache_is_rebuilt(company, monkeypatch, damage):
    calls = spy_parser(monkeypatch)
    proof = register(company, b"name,amount,period\na,10.00,2026-01\n")
    expected = company.reader.inspect(proof, csv_spec())
    path = result_files(company.cache)[0]
    envelope = json.loads(path.read_bytes())
    if damage == "json":
        changed = "{"
    elif damage == "duplicate":
        changed = '{"cache_format":"bad",' + path.read_text(encoding="utf-8")[1:]
    elif damage == "digest":
        envelope["result"]["items"][0]["amount_fen"] = 1
        changed = canonical(envelope)
    elif damage == "shape":
        envelope["result"]["items"] = "not-a-list"
        changed = canonical(envelope)
    else:
        envelope["identity"]["company_id"] = "other-company"
        changed = canonical(envelope)
    path.write_text(changed, encoding="utf-8")
    restarted = Materials(
        company.engine, inspection_cache=InspectionCache(company.root, build_id="test-build")
    )
    assert restarted.inspect(proof, csv_spec()) == expected
    assert len(calls) == 2
    assert json.loads(path.read_bytes())["result"]["items"][0]["amount_fen"] == 1000


def test_current_original_digest_failure_is_not_a_cache_miss(company, monkeypatch):
    proof = register(company, b"name,amount,period\na,10.00,2026-01\n")
    calls = spy_parser(monkeypatch)
    company.reader.inspect(proof, csv_spec())
    with company.engine.store.connection() as connection:
        trigger = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name='immutable_evidence_UPDATE'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER immutable_evidence_UPDATE")
        connection.execute(
            "UPDATE evidence SET content=? WHERE digest=?", (b"damaged", bytes.fromhex(proof))
        )
        connection.execute(trigger)
    with pytest.raises(KernelError) as caught:
        company.reader.inspect(proof, csv_spec())
    assert caught.value.code == "content_integrity_failed"
    assert caught.value.details["reason"] == "evidence_digest_mismatch"
    assert len(calls) == 1


def test_memory_retains_only_one_original_and_disk_retains_previous(tmp_path):
    cache = InspectionCache(tmp_path, build_id="test-build")
    first = request(cache, "first")
    second = request(cache, "second")
    assert cache._last_result is second
    assert cache._last_result is not first
    loaded = request(cache, "first", parse=lambda: pytest.fail("must reuse persistent parse"))
    assert loaded == first
    assert cache._last_result is loaded


def test_lru_global_quota_includes_hot_hits_and_survives_restart(tmp_path, monkeypatch):
    timestamps = iter(range(100, 1000))
    monkeypatch.setattr(inspection_cache.time, "time_ns", lambda: next(timestamps))
    cache = InspectionCache(tmp_path, build_id="test-build")
    request(cache, "a", company_id="first")
    one_size = stored_bytes(cache)
    cache.max_bytes = one_size * 2 + 20
    request(cache, "b", company_id="second")
    request(cache, "a", company_id="first")  # Disk hit promotes it.
    request(cache, "a", company_id="first")  # Hot hit also promotes it durably.
    restarted = InspectionCache(tmp_path, build_id="test-build", max_bytes=cache.max_bytes)
    request(restarted, "c", company_id="third")
    identities = [json.loads(path.read_bytes())["identity"] for path in result_files(restarted)]
    assert {item["company_id"] for item in identities} == {"first", "third"}
    assert stored_bytes(restarted) <= restarted.max_bytes
    request(restarted, "a", company_id="first", parse=lambda: pytest.fail("LRU retained a"))
    for path in restarted.directory.glob("*.used.json"):
        usage = json.loads(path.read_bytes())
        assert type(usage["last_used_ns"]) is int


def test_oversize_parse_is_returned_without_persistent_file(tmp_path):
    cache = InspectionCache(tmp_path, build_id="test-build", max_bytes=1)
    expected = request(cache, "a")
    assert expected["items"][0]["amount_fen"] == 2**53 + 1
    assert not cache.directory.exists()
    assert request(cache, "a", parse=lambda: pytest.fail("one memory entry remains")) == expected


def test_failed_cache_write_does_not_refuse_reading(company, monkeypatch):
    calls = spy_parser(monkeypatch)

    def fail(*args):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(company.cache, "_atomic_write", fail)
    proof = register(company, b"name,amount,period\na,10.00,2026-01\n")
    assert company.reader.inspect(proof, csv_spec())["items"][0]["amount_fen"] == 1000
    assert company.reader.inspect(proof, csv_spec())["items"][0]["amount_fen"] == 1000
    assert len(calls) == 1


def test_atomic_replace_failure_cleans_private_temporary(company, monkeypatch):
    def fail(*args):
        raise OSError("synthetic atomic replace failure")

    monkeypatch.setattr(inspection_cache.os, "replace", fail)
    proof = register(company, b"name,amount,period\na,10.00,2026-01\n")
    assert company.reader.inspect(proof, csv_spec())["items"][0]["amount_fen"] == 1000
    assert not result_files(company.cache)
    assert not list(company.cache.directory.glob("*.tmp"))


def test_restart_cleans_only_private_interrupted_cache_writes(tmp_path):
    cache = InspectionCache(tmp_path, build_id="test-build")
    expected = request(cache, "first")
    temporary = create_private_file(cache.directory / ("inspection-write-" + "a" * 32 + ".tmp"))
    temporary.write_bytes(b"interrupted")
    unrelated = cache.directory / "owner-temporary.tmp"
    unrelated.write_bytes(b"preserve")
    restarted = InspectionCache(tmp_path, build_id="test-build")
    assert request(restarted, "first", parse=lambda: pytest.fail("persistent hit")) == expected
    assert not temporary.exists()
    assert unrelated.read_bytes() == b"preserve"


def test_corrupt_usage_metadata_does_not_hide_files_from_quota(tmp_path):
    cache = InspectionCache(tmp_path, build_id="test-build")
    request(cache, "first")
    cache.max_bytes = stored_bytes(cache) + 20
    metadata = next(cache.directory.glob("*.used.json"))
    metadata.write_bytes(b"invalid-json")
    request(cache, "second")
    assert stored_bytes(cache) <= cache.max_bytes
    assert len(result_files(cache)) == 1


def test_multiple_managers_do_not_race_quota_or_atomic_files(tmp_path):
    caches = [InspectionCache(tmp_path, build_id="test-build", max_bytes=1500) for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as executor:
        responses = list(
            executor.map(lambda pair: request(pair[1], str(pair[0])), enumerate(caches))
        )
    assert [item["items"][0]["location"] for item in responses] == [str(i) for i in range(8)]
    assert stored_bytes(caches[0]) <= 1500
    assert all(json.loads(path.read_bytes()) for path in caches[0].directory.glob("*.json"))


def test_concurrent_pages_share_first_parse_and_have_independent_responses(company, monkeypatch):
    calls = spy_parser(monkeypatch)
    proof = register(company, b"name,amount,period\na,10.00,2026-01\n")
    with ThreadPoolExecutor(max_workers=8) as executor:
        responses = list(
            executor.map(lambda _: company.reader.inspect(proof, csv_spec()), range(16))
        )
    assert len(calls) == 1
    assert all(response == responses[0] for response in responses)
    responses[0]["items"][0]["amount_fen"] = 1
    assert responses[1]["items"][0]["amount_fen"] == 1000
    assert all(json.loads(path.read_bytes()) for path in company.cache.directory.glob("*.json"))


def test_eviction_keeps_unrelated_files_and_removes_only_owned_private_cache(tmp_path):
    cache = InspectionCache(tmp_path, build_id="test-build")
    request(cache, "first")
    cache.max_bytes = stored_bytes(cache) + 20
    unrelated = cache.directory / "owner-notes.json"
    unrelated.write_text("preserve", encoding="utf-8")
    request(cache, "second")
    assert unrelated.read_text(encoding="utf-8") == "preserve"
    assert len(result_files(cache)) == 1
    assert stored_bytes(cache) <= cache.max_bytes
    assert not list(cache.directory.glob("*.tmp"))


def test_receive_and_completeness_continue_independent_parsing(company, monkeypatch):
    calls = spy_parser(monkeypatch)
    proof = register(company, b"name,amount,period\na,10.00,2026-01\n")
    company.reader.inspect(proof, csv_spec())
    company.reader.inspect(proof, csv_spec())
    assert len(calls) == 1
    company.reader.receive(
        "source",
        {
            "period": "2026-01",
            "evidence_digest": proof,
            "category": "transactions",
            "purpose": "business",
            "specification": csv_spec(),
        },
        evidence=(proof,),
        expected_revision=0,
        request_id="receive",
    )
    assert len(calls) == 2
    company.reader.check("2026-01")
    assert len(calls) == 3
    company.reader.check("2026-01")
    assert len(calls) == 4
    company.reader.inspect(proof, csv_spec())
    assert len(calls) == 4


def test_plain_materials_has_no_cache_and_still_parses_each_read(company, monkeypatch):
    calls = spy_parser(monkeypatch)
    reader = Materials(company.engine)
    monkeypatch.setattr(materials, "calculator_build_id", lambda: "test-build")
    proof = register(company, b"name,amount,period\na,10.00,2026-01\n")
    reader.inspect(proof, csv_spec())
    reader.inspect(proof, csv_spec())
    assert len(calls) == 2
    assert not company.cache.directory.exists()
