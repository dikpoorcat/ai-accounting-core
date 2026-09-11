"""Public pagination cannot turn an unprocessed large original into a complete one."""

import pytest

from ai_accounting.kernel import materials
from ai_accounting.kernel.catalog import Catalog
from ai_accounting.kernel.command_schema import command_models, validate_command
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.materials import Materials, check_completeness
from ai_accounting.kernel.service import default_registry
from ai_accounting.kernel.types import YearMonth


@pytest.fixture
def source(tmp_path):
    catalog = Catalog(tmp_path / "root", default_registry())
    company = catalog.create_company("91310000123456789A", "Synthetic material pagination")
    engine = Engine(catalog.bind(company["id"]))
    raw = b"reference,amount\n" + b"synthetic,1.23\n" * 249 + b"last,missing\n"
    proof = engine.register_evidence(raw, "text/csv", "synthetic.csv", request_id="proof")["digest"]
    spec = {
        "format": "csv",
        "columns": [{"column": "A", "role": "context"}, {"column": "B", "role": "amount"}],
    }
    return engine, Materials(engine), proof, spec


def test_inspection_pages_are_bounded_and_diagnostics_describe_whole_original(source):
    _, api, proof, spec = source
    first = api.inspect(proof, spec)
    assert len(first["items"]) == len(first["coverage"]) == 100
    assert first["summary"] == {
        "item_count": 250,
        "cell_count": 502,
        "issue_count": 1,
        "control_total_count": 0,
    }
    assert first["status"] == "needs_information"
    assert first["issues"][0]["location"] == "CSV!B251"
    assert all(set(cell) == {"location", "hidden"} for cell in first["coverage"])
    second = api.inspect(proof, spec, after=first["next_cursor"])
    third = api.inspect(proof, spec, after=second["next_cursor"])
    locations = [row["location"] for page in (first, second, third) for row in page["items"]]
    assert len(locations) == len(set(locations)) == 250
    assert third["next_cursor"] is None and len(third["items"]) == 50


def test_inspection_cursor_binds_evidence_mapping_and_parser_build(source, monkeypatch):
    engine, api, proof, spec = source
    cursor = api.inspect(proof, spec, limit=1)["next_cursor"]
    other = engine.register_evidence(b"a\n1.23", "text/csv", "other", request_id="other")["digest"]
    changed = spec | {"header_rows": {"CSV": 0}}
    for digest, mapping in ((other, spec), (proof, changed)):
        with pytest.raises(KernelError) as error:
            api.inspect(digest, mapping, after=cursor)
        assert error.value.code == "material_cursor_stale"
    monkeypatch.setattr(materials, "calculator_build_id", lambda: "synthetic-different-build")
    with pytest.raises(KernelError) as error:
        api.inspect(proof, spec, after=cursor)
    assert error.value.code == "material_cursor_stale"


@pytest.mark.parametrize("limit", [0, 501, True, "100"])
def test_public_page_size_is_strict_and_bounded(source, limit):
    _, api, proof, spec = source
    with pytest.raises(ValueError):
        api.inspect(proof, spec, limit=limit)
    with pytest.raises(ValueError):
        api.check("2026-03", limit=limit)


def test_receive_and_internal_close_checks_keep_all_rows_despite_public_truncation(source):
    engine, api, proof, spec = source
    api.receive(
        "source",
        {
            "period": "2026-03",
            "evidence_digest": proof,
            "category": "bank",
            "purpose": "business",
            "specification": spec,
        },
        evidence=(proof,),
        expected_revision=0,
        request_id="source",
    )
    public = api.check("2026-03", limit=3)
    assert public["status"] == "needs_information"
    assert public["coverage_count"] == 250 and len(public["coverage"]) == 3
    # Every unassigned row remains an unknown period as well as unprocessed.
    assert public["issue_count"] == 501 and len(public["issues"]) == 3
    assert public["coverage_truncated"] and public["issues_truncated"]
    with engine.store.connection(read_only=True) as connection:
        full = check_completeness(connection, YearMonth("2026-03").ordinal, engine.store.registry)
    assert len(full["coverage"]) == 250 and len(full["issues"]) == 501
    assert full["status"] == "needs_information"
    assert full["coverage_digest"] == public["coverage_digest"]


def test_original_row_matching_does_not_rescan_every_prior_row(source, monkeypatch):
    engine, api, proof, spec = source
    api.receive(
        "source",
        {
            "period": "2026-03",
            "evidence_digest": proof,
            "category": "bank",
            "purpose": "business",
            "specification": spec,
        },
        evidence=(proof,),
        expected_revision=0,
        request_id="source",
    )
    original = materials.inspect_bytes
    visits = 0

    class CountedRows(list):
        def __iter__(self):
            nonlocal visits
            for value in super().__iter__():
                visits += 1
                yield value

    def inspect(raw, mapping):
        result = original(raw, mapping)
        result["items"] = CountedRows(result["items"])
        return result

    monkeypatch.setattr(materials, "inspect_bytes", inspect)
    with engine.store.connection(read_only=True) as connection:
        result = check_completeness(connection, YearMonth("2026-03").ordinal, engine.store.registry)
    assert len(result["coverage"]) == 250
    assert visits <= 250 * 10


def test_generated_public_schema_retains_page_limits(source):
    engine, _, proof, spec = source
    models = command_models(engine.store.registry)
    fields = models["inspect_material"].json_schema()["properties"]
    assert fields["limit"]["minimum"] == 1
    assert fields["limit"]["maximum"] == 500
    assert fields["limit"]["default"] == 100
    with pytest.raises(KernelError) as error:
        validate_command(
            models,
            "inspect_material",
            {
                "company_id": engine.store.company_id,
                "evidence_digest": proof,
                "specification": spec,
                "limit": 501,
            },
        )
    assert error.value.code == "invalid_command"
