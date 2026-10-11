"""Material links bind authenticated exact calculations and share economic capacity."""

import pytest
from pydantic import ValidationError
from test_integrity_content import damage
from test_materials import Company, codes

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.materials import MaterialLink, resolve_material_links
from ai_accounting.kernel.types import canonical


@pytest.mark.parametrize("field", ("subject_id", "fact_kind", "fact_id", "recognition_period"))
def test_material_link_rejects_repeated_source_identity(field):
    data = dict(calculation_id="calculation", amount_field="fact.amount_fen", amount_fen=100)
    assert set(MaterialLink.model_fields) == set(data)
    with pytest.raises(ValidationError) as rejected:
        MaterialLink.model_validate_json(canonical(data | {field: "duplicate"}))
    assert rejected.value.errors()[0]["type"] == "extra_forbidden"


def test_exact_source_binding_survives_recalculation_and_loads_once(tmp_path):
    company = Company(tmp_path)
    old = company.expense("expense", 1000)
    current = company.expense("expense", 1200, "2026-02", revision=1)
    links = [MaterialLink.model_validate_json(canonical(data)) for data in (old, current, old)]
    cache, statements = {}, []
    with company.engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        connection.set_trace_callback(statements.append)
        resolved = resolve_material_links(
            connection, links, registry=company.engine.store.registry, _bindings=cache
        )
        assert [str(link.recognition_period) for link in resolved] == [
            "2026-01",
            "2026-02",
            "2026-01",
        ]
        assert {link.subject_id for link in resolved} == {"expense"}
        assert {link.fact_kind for link in resolved} == {"expense"}
        assert resolved[0].fact_id != resolved[1].fact_id
        assert resolved[0].binding is resolved[2].binding
        before = len(statements)
        assert (
            resolve_material_links(
                connection, links, registry=company.engine.store.registry, _bindings=cache
            )
            == resolved
        )
        assert len(statements) == before
    assert sum("LEFT JOIN calculation c ON c.id=ids.value" in sql for sql in statements) == 1


@pytest.mark.parametrize(
    "component", ("fact_seal", "calculation_seal", "fact", "outcome", "identity")
)
def test_exact_link_rejects_unsealed_or_changed_source(tmp_path, component):
    company = Company(tmp_path)
    data = company.expense("expense", 1000)
    ident = data["calculation_id"]
    with company.engine.store.connection(read_only=True) as connection:
        fact_id = connection.execute(
            "SELECT fact_id FROM calculation WHERE id=?", (ident,)
        ).fetchone()[0]
    if component == "fact_seal":
        damage(
            company.engine,
            component,
            "DELETE FROM fact_seal WHERE fact_id=?",
            (fact_id,),
            foreign_keys=False,
        )
    elif component == "calculation_seal":
        damage(
            company.engine,
            component,
            "DELETE FROM calculation_seal WHERE calculation_id=?",
            (ident,),
            foreign_keys=False,
        )
    elif component == "fact":
        damage(
            company.engine,
            "fact_expense",
            "UPDATE fact_expense SET amount_fen=1001 WHERE revision_id=?",
            (fact_id,),
        )
    elif component == "outcome":
        damage(
            company.engine,
            "calculation",
            "UPDATE calculation SET outcome=json_set(outcome,'$.values.amount_fen',1001) "
            "WHERE id=?",
            (ident,),
        )
    else:
        damage(
            company.engine,
            "calculation",
            "UPDATE calculation SET period=period+1 WHERE id=?",
            (ident,),
        )
    with company.engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        with pytest.raises(KernelError) as rejected:
            resolve_material_links(
                connection,
                [MaterialLink.model_validate_json(canonical(data))],
                registry=company.engine.store.registry,
            )
    assert rejected.value.code == "content_integrity_failed"


def test_unknown_calculation_rejects_resolution_atomically(tmp_path):
    company = Company(tmp_path)
    source, _ = company.source(b"name,amount,period\na,10,2026-01\n")
    with company.engine.store.connection(read_only=True) as connection:
        before = connection.execute("SELECT count(*) FROM fact_revision").fetchone()[0]
    with pytest.raises(KernelError) as rejected:
        company.resolve(
            source,
            "CSV!B2",
            [dict(calculation_id="missing", amount_field="fact.amount_fen", amount_fen=1000)],
        )
    assert rejected.value.code == "needs_information"
    assert rejected.value.details["fact_issues"][0]["field"] == "links.calculation_id"
    with company.engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM fact_revision").fetchone()[0] == before
        assert (
            connection.execute(
                "SELECT count(*) FROM subject WHERE id='resolution-CSV!B2'"
            ).fetchone()[0]
            == 0
        )


def test_recalculation_does_not_reset_material_allocation_capacity(tmp_path):
    company = Company(tmp_path)
    original, _ = company.source(b"name,amount,period\na,10.00,2026-01\n", "original")
    later, _ = company.source(b"name,amount,period\nb,10.01,2026-01\n", "later")
    old = company.expense("expense", 1000)
    company.resolve(original, "CSV!B2", [old], subject="old-resolution")
    current = company.expense("expense", 1001, revision=1)
    company.resolve(later, "CSV!B2", [current], subject="current-resolution")
    assert {"material_result_stale", "material_business_overallocated"} <= codes(
        company.materials.check("2026-01")
    )
