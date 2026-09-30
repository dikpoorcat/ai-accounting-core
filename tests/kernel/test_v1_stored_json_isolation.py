"""A registered v1 verifier keeps its own saved JSON interpretation."""

import json
import sys

import pytest
from schema_fixture import TEST_FAMILY, full_contract, write_contract
from stage9_book import MixedBook

from ai_accounting.kernel import content_v1, service, storage, stored_json
from ai_accounting.kernel.backup import verify_file
from ai_accounting.kernel.catalog import catalog_sql
from ai_accounting.kernel.content_history_context import historical_content, source_json_loads
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.schema import schema_sql
from ai_accounting.kernel.schema_bundle import APPLICATION_ID, load_bundle


@pytest.fixture
def released_book(tmp_path, monkeypatch):
    registry = service.default_registry()
    directory = tmp_path / "schema_contracts"
    write_contract(
        directory,
        full_contract(schema_sql(registry), kind="company", status="released", version=1),
    )
    write_contract(
        directory,
        full_contract(catalog_sql(), kind="catalog", status="released", version=1),
    )
    (directory / "content-v1.json").write_text(
        json.dumps(content_v1.content_contract(registry), ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setattr(content_v1, "__file__", str(tmp_path / "content_v1.py"))
    content_v1.v1_registry.cache_clear()
    bundle = load_bundle(
        registry,
        directory,
        family=TEST_FAMILY,
        application_id=APPLICATION_ID,
        status="released",
        current_versions={"company": 1, "catalog": 1},
        company_verifiers={1: content_v1.verify_v1_company},
    )
    monkeypatch.setattr("stage9_book.production_bundle", lambda: bundle)
    try:
        book = MixedBook(tmp_path / "stage9-v1-json", employees=1, businesses=26)
        book.add_month(0)
        yield book, bundle
    finally:
        content_v1.v1_registry.cache_clear()


def _saved_content(connection):
    return {
        "facts": tuple(connection.execute("SELECT id,digest FROM fact_revision ORDER BY id")),
        "calculation": tuple(connection.execute("SELECT id,outcome FROM calculation ORDER BY id")),
        "close": tuple(
            connection.execute("SELECT period,manifest FROM period_close ORDER BY period")
        ),
        "duplicate": tuple(
            connection.execute("SELECT id,manifest FROM business_duplicate_check ORDER BY id")
        ),
    }


def _future_decoder(*_args, **_kwargs):
    raise AssertionError("the released v1 verifier called a changed current JSON decoder")


def _block_current_decoders(patch):
    """Replace exact current functions, including aliases imported before this test."""
    originals = (stored_json.loads_unique, storage.decode_fields)
    replaced = [0, 0]
    for module in tuple(sys.modules.values()):
        if not getattr(module, "__name__", "").startswith("ai_accounting.kernel"):
            continue
        for name, value in tuple(vars(module).items()):
            for index, original in enumerate(originals):
                if value is original:
                    patch.setattr(module, name, _future_decoder)
                    replaced[index] += 1
                    break
    assert all(replaced), "both current decoder identities must have loaded callers"
    with pytest.raises(AssertionError, match="changed current JSON decoder"):
        source_json_loads("{}")
    with historical_content(1):
        assert source_json_loads("{}") == {}


def _replace_immutable(connection, table, column, identity, value, trigger, raw):
    trigger_sql = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type='trigger' AND name=?", (trigger,)
    ).fetchone()[0]
    connection.execute("DROP TRIGGER " + trigger)
    connection.execute(f"UPDATE {table} SET {column}=? WHERE {identity}=?", (raw, value))
    connection.execute(trigger_sql)


def test_registered_v1_verifier_ignores_future_current_decoder(released_book, monkeypatch):
    book, bundle = released_book
    with book.engine.store.connection(read_only=True) as connection:
        before = _saved_content(connection)
    assert all(before.values()), "facts, results, frozen adoption and checks must be nonempty"
    with book.engine.store.connection() as connection:
        ident, original = connection.execute(
            "SELECT id,outcome FROM calculation ORDER BY id LIMIT 1"
        ).fetchone()
        noncanonical = json.dumps(
            json.loads(original), ensure_ascii=False, sort_keys=True, indent=1
        )
        assert noncanonical != original
        assert json.loads(noncanonical) == json.loads(original)
        _replace_immutable(
            connection, "calculation", "outcome", "id", ident,
            "immutable_calculation_UPDATE", noncanonical,
        )
    with book.engine.store.connection(read_only=True) as connection:
        accepted = _saved_content(connection)
    assert accepted["facts"] == before["facts"]
    assert accepted["close"] == before["close"]
    assert accepted["duplicate"] == before["duplicate"]
    assert accepted["calculation"] != before["calculation"]

    with monkeypatch.context() as future:
        _block_current_decoders(future)
        checked = verify_file(book.engine.store.path, _bundle=bundle)
    assert checked["database_format"]["version"] == 1
    assert checked["verification"]["status"] == "verified"
    assert checked["verification"]["counts"]["facts"] > 0
    with book.engine.store.connection(read_only=True) as connection:
        assert _saved_content(connection) == accepted


@pytest.mark.parametrize(
    ("table", "column", "identity", "trigger", "expected_code"),
    [
        (
            "calculation", "outcome", "id", "immutable_calculation_UPDATE",
            "content_integrity_failed",
        ),
        (
            "period_close", "manifest", "period", "immutable_period_close_UPDATE",
            "content_integrity_failed",
        ),
        (
            "business_duplicate_check", "manifest", "id",
            "business_duplicate_check_no_update", "duplicate_review_corrupt",
        ),
    ],
)
def test_registered_v1_verifier_rejects_duplicate_decoded_keys(
    released_book, monkeypatch, table, column, identity, trigger, expected_code
):
    book, bundle = released_book
    with book.engine.store.connection() as connection:
        row = connection.execute(
            f"SELECT {identity},{column} FROM {table} ORDER BY {identity} LIMIT 1"
        ).fetchone()
        assert row is not None
        key = next(iter(json.loads(row[column])))
        raw = '{"' + key + '":null,' + row[column][1:]
        assert json.loads(raw) == json.loads(row[column])
        _replace_immutable(connection, table, column, identity, row[identity], trigger, raw)

    with monkeypatch.context() as future:
        _block_current_decoders(future)
        with book.engine.store.connection(read_only=True) as connection:
            with pytest.raises(KernelError) as failed:
                bundle.company_verifiers[1](connection, bundle)
    assert failed.value.code == expected_code
