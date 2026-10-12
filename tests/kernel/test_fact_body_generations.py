"""One stable business identity can retain several immutable typed body grammars."""

import sqlite3
from typing import ClassVar

import pytest
from pydantic import Field
from schema_fixture import test_bundle

from ai_accounting.kernel.contracts import Fact, FactVersion, KernelError, Registry
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.integrity import verify_sources
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.storage import Store, encode_fields
from ai_accounting.kernel.types import digest
from ai_accounting.kernel.verified_source_lease import (
    verified_calculation_source,
    verified_source_lease,
)
from ai_accounting.kernel.verified_source_reads import VerifiedSourceReads, readonly_verification


class OriginalBody(Fact):
    kind: ClassVar[str] = "body_probe"
    historical_amount_fen: int = Field(ge=0)


class CurrentBody(Fact):
    kind: ClassVar[str] = "body_probe"
    confirmed_amount_fen: int = Field(ge=0)


@pytest.fixture
def bodies(tmp_path):
    registry = Registry()
    registry.register(
        CurrentBody, table="fact_body_probe_confirmed",
        historical=((OriginalBody, "fact_body_probe"),),
    )
    store = Store.create(tmp_path / "probe.sqlite", test_bundle(registry), "co", "tax", "db")
    original = OriginalBody(period="2026-08", historical_amount_fen=30000)
    current = CurrentBody(period="2026-08", confirmed_amount_fen=90000)
    with store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT INTO subject VALUES('wage','body_probe')")
        connection.execute(
            "INSERT INTO fact_revision VALUES('wage:v1','wage',1,?,?)",
            (original.period.ordinal, digest(original.model_dump(mode="json"))),
        )
        data = encode_fields(OriginalBody, original.model_dump(mode="json"))
        connection.execute(
            "INSERT INTO fact_body_probe VALUES('wage:v1',?,?)", tuple(data.values())
        )
        connection.execute("INSERT INTO fact_seal VALUES('wage:v1')")
        store.write_fact(
            connection, FactVersion("wage:v2", "wage", 2, current),
            digest(current.model_dump(mode="json")),
        )
        connection.commit()
    return store, original, current


def test_exact_body_read_preserves_original_fields_and_digest(bodies):
    store, original, current = bodies
    with store.connection(read_only=True) as connection:
        assert type(store.fact(connection, "wage:v1").fact) is OriginalBody
        assert type(store.fact(connection, "wage:v2").fact) is CurrentBody
        raw = store.fact_data_many(connection, ["wage:v1", "wage:v2"])
        assert raw == {
            "wage:v1": original.model_dump(mode="json"),
            "wage:v2": current.model_dump(mode="json"),
        }
        versions = store.facts(connection, raw)
        assert versions["wage:v1"].fact == original
        assert versions["wage:v2"].fact == current
        verify_sources(Engine(store), connection, fact_ids=set(raw))
    with QueryReads.snapshot(Engine(store)) as reads:
        reads.verify_fact_versions(set(raw))
        assert type(reads.fact_version("wage:v1").fact) is OriginalBody
    assert "historical_amount_fen" not in store.registry.schemas()["body_probe"]["properties"]


@pytest.mark.parametrize("body_count", [0, 2])
def test_seal_rejects_missing_or_duplicate_bodies_atomically(bodies, body_count):
    store, original, current = bodies
    with store.connection() as connection:
        before = list(connection.execute("SELECT id FROM fact_revision ORDER BY id"))
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO fact_revision VALUES('wage:v3','wage',3,?,?)",
            (current.period.ordinal, digest(current.model_dump(mode="json"))),
        )
        if body_count == 2:
            connection.execute(
                "INSERT INTO fact_body_probe VALUES('wage:v3',?,?)",
                tuple(encode_fields(OriginalBody, original.model_dump(mode="json")).values()),
            )
            connection.execute(
                "INSERT INTO fact_body_probe_confirmed VALUES('wage:v3',?,?)",
                tuple(encode_fields(CurrentBody, current.model_dump(mode="json")).values()),
            )
            with pytest.raises(KernelError, match="恰有一个合法正文"):
                store.fact(connection, "wage:v3")
        with pytest.raises(sqlite3.IntegrityError, match="missing typed fact"):
            connection.execute("INSERT INTO fact_seal VALUES('wage:v3')")
        connection.rollback()
        assert list(connection.execute("SELECT id FROM fact_revision ORDER BY id")) == before


def test_store_rejects_historical_body_before_writing(bodies):
    store, original, _ = bodies
    with store.connection() as connection:
        with pytest.raises(KernelError) as failure:
            store.write_fact(
                connection, FactVersion("wage:v3", "wage", 3, original),
                digest(original.model_dump(mode="json")),
            )
        assert failure.value.code == "historical_fact_read_only"
        assert not connection.in_transaction


def test_full_verified_source_adapter_uses_each_retained_body_grammar(bodies):
    store, original, current = bodies
    engine = Engine(store)
    with store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        with readonly_verification(connection), verified_source_lease(connection):
            facts = verify_sources(
                engine, connection, fact_ids={"wage:v1", "wage:v2"}, _return_facts=True,
            )
            reads = VerifiedSourceReads(
                engine, connection, verified_calculation_source(connection, {
                    "facts": facts, "calculations": {}, "publications": {},
                }),
            )
            versions = reads.fact_versions({"wage:v1", "wage:v2"})
            assert type(versions["wage:v1"].fact) is OriginalBody
            assert type(versions["wage:v2"].fact) is CurrentBody
            assert reads.facts(versions)["wage:v1"]["data"] == original.model_dump(mode="json")
            assert reads.facts(versions)["wage:v2"]["data"] == current.model_dump(mode="json")


def test_historical_only_kind_is_readable_but_absent_from_input_schema():
    registry = Registry()
    registry.register_history(OriginalBody)
    assert registry.models[OriginalBody.kind] is OriginalBody
    assert registry.input_models == registry.schemas() == {}
    assert registry.evaluators == {}
