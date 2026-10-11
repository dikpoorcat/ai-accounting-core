"""A registered v1 verifier keeps its own saved JSON interpretation."""

import json
import sys
from types import SimpleNamespace

import pytest
from close_storage_fixture import use_v4_close_manifest
from schema_fixture import TEST_FAMILY, full_contract, write_contract

from ai_accounting.kernel import content_v1, service, storage, stored_json
from ai_accounting.kernel.backup import verify_file
from ai_accounting.kernel.catalog import Catalog, catalog_sql
from ai_accounting.kernel.content_history_context import historical_content, source_json_loads
from ai_accounting.kernel.contracts import KernelError, Registry
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.entities import Entities
from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods
from ai_accounting.kernel.schema import schema_sql
from ai_accounting.kernel.schema_bundle import APPLICATION_ID, load_bundle


@pytest.fixture
def released_book(tmp_path, monkeypatch):
    use_v4_close_manifest(monkeypatch)
    # This is a deliberately small synthetic released contract, not a current
    # MixedBook relabelled as historical data. None of its source models contain
    # changed payroll bodies or material links that the fixed v1 SQL cannot read.
    current = service.default_registry()
    registry = Registry()
    for kind in (
        "expense", "bank_opening", "bank_statement", "bank_reconciliation",
    ):
        registry.register(current.models[kind], current.evaluators.get(kind))
        if kind in current.accounting_projectors:
            registry.accounting_projectors[kind] = current.accounting_projectors[kind]
        if kind in current.accounting_consumers:
            registry.accounting_consumers[kind] = current.accounting_consumers[kind]
    registry.readiness["bank_accounts"] = current.readiness["bank_accounts"]
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
    try:
        book = _synthetic_v1_book(tmp_path / "minimal-v1-json", bundle)
        yield book, bundle
    finally:
        content_v1.v1_registry.cache_clear()


def _synthetic_v1_book(root, bundle):
    """One closed idle bank month and one explicit open-month expense proposal.

    The owner explicitly declares the new account and its empty statement range;
    there are no January business rows, payroll records or copied business links.
    Every category still receives a real inventory revision, never a checker bypass.
    """
    catalog = Catalog(root, bundle)
    company = catalog.create_company("91310000123456789S", "固定 v1 合成核验企业")
    engine = Engine(catalog.bind(company["id"]))
    proof = engine.register_evidence(
        b"Synthetic owner explicitly confirms the bank is newly opened with zero balance; "
        b"its complete January statement has no entries and a zero closing balance. "
        b"There are no employees or other business, and no external source rows.",
        "text/plain", "synthetic-owner-confirmation.txt", request_id="proof",
    )["digest"]
    entities = Entities(engine)
    bank = entities.register_entity(
        "fund_account", {
            "bank_name": "合成测试银行",
            "bank_account_number": "1234567890123456",
            "account_holder_name": company["name"],
            "display_name": "合成企业银行",
        },
        account_type="bank", source="explicit synthetic fixture", evidence_digest=proof,
        request_id="bank",
    )["entity_id"]

    def save_and_publish(kind, subject, fields):
        engine.save_fact(
            kind, subject, {"period": "2026-01", **fields}, evidence=(proof,),
            expected_revision=0, request_id="save-" + subject,
        )
        preview = engine.preview([subject])
        engine.confirm(
            [subject], preview_digest=preview["digest"], epochs=preview["epochs"],
            request_id="publish-" + subject,
        )

    save_and_publish("bank_opening", "bank-start", {
        "bank_account_id": bank, "opening_fen": 0, "basis": "new_account",
    })
    save_and_publish("bank_statement", "statement", {
        "bank_account_id": bank, "opening_fen": 0, "closing_fen": 0,
        "entries": [],
    })
    save_and_publish("bank_reconciliation", "reconciliation", {
        "bank_account_id": bank, "statement_id": "statement",
        "matches": [],
    })
    periods = Periods(engine)
    for category in MATERIAL_CATEGORIES:
        periods.inventory(
            "2026-01", category, evidence=[], expected=0,
            no_business=True, confirmation_evidence=proof,
            request_id="inventory-" + category,
        )
    with engine.store.connection(read_only=True) as connection:
        assert periods.check_readiness(connection, "2026-01")["issues"] == []
    preview = periods.preview_close("2026-01", owner_confirmation=proof)
    periods.close(
        "2026-01", owner_confirmation=proof, preview_digest=preview["digest"],
        epochs=preview["epochs"], request_id="close",
    )
    # A real eligible proposed fact supplies a nonempty immutable duplicate
    # registration check. It belongs to February and is intentionally unpublished;
    # it neither contradicts January's idle-account inventory nor needs material
    # association formats that were removed from the current contract.
    supplier = entities.register_entity(
        "organization", {"display_name": "合成供应商"},
        source="explicit synthetic fixture", request_id="supplier",
    )["entity_id"]
    expense_proof = engine.register_evidence(
        b"Synthetic February owner proposal: a 100-fen unpaid administration expense, "
        b"owed to the synthetic supplier. This open-month proposal is not yet published.",
        "text/plain", "synthetic-open-expense.txt", request_id="expense-proof",
    )["digest"]
    engine.save_fact(
        "expense", "open-expense", {
            "period": "2026-02", "counterparty_id": supplier, "amount_fen": 100,
            "expense_class": "administration", "creditor_kind": "supplier",
        }, evidence=(expense_proof,), expected_revision=0, request_id="open-expense",
    )
    return SimpleNamespace(engine=engine, company=company, root=root)


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


def test_released_fixture_contains_only_v1_expressible_sources(released_book):
    book, bundle = released_book
    assert set(bundle.registry.models) == {
        "expense", "bank_opening", "bank_statement", "bank_reconciliation",
    }
    with book.engine.store.connection(read_only=True) as connection:
        assert dict(connection.execute("SELECT kind,count(*) FROM subject GROUP BY kind")) == {
            "expense": 1, "bank_opening": 1, "bank_statement": 1, "bank_reconciliation": 1,
        }
        assert connection.execute("SELECT count(*) FROM calculation_publication").fetchone()[0] == 3
        assert connection.execute("SELECT count(*) FROM period_close").fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM business_duplicate_check"
        ).fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM fund_account_definition").fetchone()[0] == 1
        inventories = connection.execute(
            "SELECT category,expected,received,no_business FROM material_revision"
        ).fetchall()
        assert {row["category"] for row in inventories} == set(MATERIAL_CATEGORIES)
        assert all(tuple(row)[1:] == (0, 0, 1) for row in inventories)
        assert connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE name LIKE 'fact_material_%links'"
        ).fetchone() is None
    checked = verify_file(book.engine.store.path, _bundle=bundle)
    assert checked["verification"]["status"] == "verified"


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
