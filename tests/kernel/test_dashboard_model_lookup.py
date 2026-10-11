"""Root locators avoid repeated traversal without caching decoded block proofs."""

import hashlib
import json
from dataclasses import replace

import pytest
from test_dashboard_read_model_storage import anchor, produce
from test_dashboard_read_model_storage import database as database  # noqa: F401
from test_dashboard_read_model_storage import native as native  # noqa: F401

from ai_accounting.kernel import dashboard_read_model
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard_read_model import load_model


class CountedReferences(tuple):
    def __new__(cls, values):
        result = super().__new__(cls, values)
        result.visited = 0
        return result

    def __iter__(self):
        for value in super().__iter__():
            self.visited += 1
            yield value


@pytest.mark.parametrize("unrelated", (0, 20))
def test_same_authenticated_root_keeps_results_and_rehashes_each_hit_without_rescanning(
    database,
    native,
    unrelated,
):
    scope, base = native
    blocks = [
        *base,
        *(
            replace(base[0], key=f"funds/unrelated-{index}/overview/0")
            for index in range(unrelated)
        ),
    ]
    produce(database, scope, blocks)
    database.execute("BEGIN")
    loaded = load_model(database, scope)
    references = CountedReferences(loaded.root.blocks)
    # Instrument only the already authenticated root's immutable references.
    # Their bytes, order, identities and metadata are unchanged.
    indexed = replace(loaded, root=loaded.root.model_copy(update={"blocks": references}))
    assert references.visited == len(blocks)
    references.visited = 0
    statements = []
    database.set_trace_callback(statements.append)
    try:
        for _ in range(3):
            assert indexed.has_block(base[0].key)
            assert not indexed.has_block("funds/missing/overview/0")
            assert tuple(
                ref.key
                for ref in indexed.collection_references(
                    "funds",
                    "default",
                    "accounts",
                )
            ) == (base[1].key,)
            assert indexed.read_block(base[0].key) == base[0].payload
            assert indexed.read_page_block(base[1].key) == base[1]
        with pytest.raises(KernelError) as error:
            indexed.read_page_block("funds/missing/overview/0")
        assert error.value.code == "content_integrity_failed"
    finally:
        database.set_trace_callback(None)
    assert references.visited == 0
    # Every hit still loads and hashes the saved body afresh. A lookup index
    # does not become a successful-proof or decoded-value cache.
    assert (
        len(
            [
                query
                for query in statements
                if query.startswith("SELECT payload FROM dashboard_read_model_block")
            ]
        )
        == 6
    )
    assert len(indexed.page_references("funds")) == len(blocks)
    with pytest.raises(TypeError):
        indexed._blocks_by_key[base[0].key] = loaded.root.blocks[0]


def test_each_hit_decodes_exact_bytes_once_without_canonical_round_trip(
    database, native, monkeypatch,
):
    scope, blocks = native
    produce(database, scope, blocks)
    database.execute("BEGIN")
    loaded = load_model(database, scope)
    decode = dashboard_read_model.loads_finite_unique
    check_digest = dashboard_read_model._is_digest
    decoded, checked_digests = [], []

    def counted(raw):
        decoded.append(raw)
        return decode(raw)

    def unexpected_encoding(_value):
        raise AssertionError("a stored page block was re-encoded")

    def counted_digest(value):
        checked_digests.append(value)
        return check_digest(value)

    monkeypatch.setattr(dashboard_read_model, "loads_finite_unique", counted)
    monkeypatch.setattr(dashboard_read_model, "canonical", unexpected_encoding)
    monkeypatch.setattr(dashboard_read_model, "_is_digest", counted_digest)
    monkeypatch.setattr(dashboard_read_model, "_decode_block_value",
                        lambda *args, **kwargs: pytest.fail("decoded bytes were walked again"))
    assert loaded.read_page_block(blocks[1].key) == blocks[1]
    assert loaded.read_page_block(blocks[1].key) == blocks[1]
    assert len(decoded) == 2  # Every read still authenticates and decodes fresh bytes.
    assert checked_digests == ["1" * 64, "2" * 64, "3" * 64] * 2


@pytest.mark.parametrize("version", (1, 2, 3))
def test_root_validates_its_original_noncanonical_bytes_without_reencoding(
    database, native, monkeypatch, version,
):
    from ai_accounting.kernel.types import canonical

    scope, blocks = native
    with monkeypatch.context() as old:
        old.setattr(dashboard_read_model, "CONTENT_VERSION", version)
        prepared = dashboard_read_model.prepare_model(scope, blocks, request_id="original-root")
    root = json.loads(prepared.root_json)
    raw = json.dumps(root, ensure_ascii=True, indent=2)
    assert raw != prepared.root_json
    root_digest = hashlib.sha256(raw.encode("utf-8")).digest()
    descriptor = prepared.descriptor
    old_digest = bytes.fromhex(descriptor["root_digest"])
    descriptor["root_digest"] = root_digest.hex()
    descriptor["commit_id"] = dashboard_read_model._descriptor_id(descriptor)
    rows = tuple((key, value) for key, value in prepared.block_rows if key != old_digest)
    prepared = dashboard_read_model.PreparedModel(
        canonical(descriptor), raw, (*rows, (root_digest, raw)),
    )
    with database:
        audit = anchor(database, [prepared])
        dashboard_read_model.persist_model(database, prepared, audit_id=audit)
    descriptor = dashboard_read_model.ModelDescriptor.model_validate_json(prepared.descriptor_json)
    calls = []
    validate = dashboard_read_model.ModelRoot.model_validate_json

    def checked(raw):
        calls.append(raw)
        return validate(raw)

    monkeypatch.setattr(dashboard_read_model.ModelRoot, "model_validate_json", checked)
    monkeypatch.setattr(dashboard_read_model, "canonical",
                        lambda _value: pytest.fail("authenticated root was reencoded"))
    loaded = dashboard_read_model._load_root(database, descriptor)
    assert loaded.content_version == version
    assert calls == [raw]
    assert database.execute("SELECT payload FROM dashboard_read_model_block WHERE digest=?",
                            (root_digest,)).fetchone()[0] == raw
