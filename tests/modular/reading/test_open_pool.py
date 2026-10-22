"""A real company transaction owns the shared Open graph and its publication token."""

from uuid import UUID

import pytest

from ai_accounting_modular.contracts.public import ContractError, PageQuery
from ai_accounting_modular.reading import _brief_open_inputs, _monthly, _open_store
from ai_accounting_modular.storage.public import Entry, Tree, TreeStore

from ._support import assemble, expense, uid


class Count:
    def leaf(self, entries):
        return len(entries)

    def branch(self, children):
        return sum(child.summary for child in children)

    def encode_summary(self, value):
        return value

    def decode_summary(self, value):
        assert type(value) is int and value >= 0
        return value


@pytest.fixture
def book(tmp_path):
    return assemble(tmp_path, established=False)


def scope(book, month):
    return _monthly.key(
        _monthly.normalize(
            {
                "company_id": book.company,
                "period_from": month,
                "period_to": month,
                "view": "as_posted",
            }
        )
    )


def graph(repository):
    pool = _open_store.OpenPool(repository)
    store = TreeStore(pool, namespace=_open_store.NAMESPACE)
    return pool, store, Tree("reading-open-pool-test/1", Count(), store, max_items=2)


def prepare(book, name, *, retained=(), copies=1):
    with book.storage.read_unit(book.company) as read:
        pool, store, tree = graph(read.repository("reading"))
        root = tree.full(
            Entry(f"{name}/{number}", {"text": f"合成条目{number}"}) for number in range(6)
        )
        old = tuple(root.digest for root in retained)
        return pool.token, root, store.finish(old, (*old, *((root.digest,) * copies)))


def publish(book, token, plan):
    with book.storage.maintenance_unit(book.company) as unit:
        return _open_store.publish(unit.repository("reading"), token, plan)


def rows(book):
    with book.storage.read_unit(book.company) as read:
        session = read.repository("reading")._session
        return read.head, tuple(
            tuple(tuple(row) for row in session.fetchall(sql))
            for sql in (
                "SELECT * FROM read_scope_head ORDER BY scope_key,component_key",
                "SELECT * FROM read_component ORDER BY scope_key,component_key",
                "SELECT * FROM read_item ORDER BY scope_key,collection_key,item_id",
            )
        )


def other_components(repository):
    return tuple(
        tuple(
            tuple(row)
            for row in repository._session.fetchall(
                "SELECT * FROM "
                + table
                + " WHERE component_key<>? ORDER BY scope_key,component_key",
                (_open_store.COMPONENT,),
            )
        )
        for table in ("read_scope_head", "read_component", "read_item")
    )


def test_two_month_candidates_compete_for_one_actual_pool_token(book):
    with book.storage.maintenance_unit(book.company) as unit:
        for month in ("2026-01", "2026-02"):
            unit.repository("reading").register(
                scope(book, month), _open_store.COMPONENT, unit.head, 8
            )
    token_a, root_a, plan_a = prepare(book, "january")
    token_b, root_b, plan_b = prepare(book, "february")
    assert token_a is token_b is None
    assert publish(book, token_a, plan_a)
    before = rows(book)
    assert not publish(book, token_b, plan_b)
    assert rows(book) == before
    with book.storage.read_unit(book.company) as read:
        pool, _, tree = graph(read.repository("reading"))
        assert pool.revision == 1 and str(UUID(pool.generation)) == pool.generation
        assert tree.root(root_a.wire()).count == 6
        assert pool.get_blocks(((_open_store.NODES, root_b.digest),)) == (None,)
    token_b, root_b, plan_b = prepare(book, "february", retained=(root_a,))
    assert publish(book, token_b, plan_b)
    with book.storage.read_unit(book.company) as read:
        pool, _, tree = graph(read.repository("reading"))
        assert pool.revision == 2
        assert tree.root(root_a.wire()).count == tree.root(root_b.wire()).count == 6


def test_reset_rejects_old_token_even_when_both_revisions_are_zero(book):
    original, _, old_plan = prepare(book, "before-initial-reset")
    with book.storage.maintenance_unit(book.company) as unit:
        _open_store.reset(unit.repository("reading"), unit.head, 8)
    assert not publish(book, original, old_plan)
    token, _, plan = prepare(book, "before-second-reset")
    with book.storage.maintenance_unit(book.company) as unit:
        repository = unit.repository("reading")
        previous = _open_store.OpenPool(repository)
        assert previous.revision == 0 and previous.token == token
        _open_store.reset(repository, unit.head, 8)
        current = _open_store.OpenPool(repository)
        assert current.revision == previous.revision == 0
        assert current.generation != previous.generation and current.token != previous.token
    before = rows(book)
    assert not publish(book, token, plan)
    assert rows(book) == before


def test_actual_blocks_keep_requested_order_duplicates_and_missing_values(book):
    token, root, plan = prepare(book, "ordered")
    assert publish(book, token, plan)
    expected = {(row.collection, row.key): row.payload_json for row in plan.writes}
    keys = tuple(reversed(expected))
    missing = (_open_store.NODES, "0" * 64)
    with book.storage.read_unit(book.company) as read:
        pool = _open_store.OpenPool(read.repository("reading"))
        assert pool.get_blocks((*keys, keys[0], missing)) == (
            *(expected[key] for key in keys),
            expected[keys[0]],
            None,
        )
        assert pool.get_blocks(()) == ()
        with pytest.raises(ContractError) as error:
            pool.get_blocks((("another-owner", root.digest),))
        assert error.value.code == "storage_corrupt"


@pytest.mark.parametrize("collection", [_open_store.NODES, _open_store.REFS, _open_store.CONTROL])
@pytest.mark.parametrize(
    "field,value",
    [
        ("component_key", "other"),
        ("sort_key", "other"),
        ("head", 0),
        ("head", 2),
        ("category", "opaque"),
    ],
)
def test_actual_control_and_block_metadata_cannot_be_reinterpreted(book, collection, field, value):
    token, root, plan = prepare(book, "metadata")
    assert publish(book, token, plan)
    identity = "head" if collection == _open_store.CONTROL else root.digest
    with book.storage.maintenance_unit(book.company) as unit:
        unit.repository("reading")._session.execute(
            f"UPDATE read_item SET {field}=? WHERE scope_key=? AND collection_key=? AND item_id=?",
            (value, _open_store.SCOPE, collection, identity),
        )
    with book.storage.read_unit(book.company) as read, pytest.raises(ContractError) as error:
        pool = _open_store.OpenPool(read.repository("reading"))
        pool.get_blocks(((collection, identity),))
    assert error.value.code == "storage_corrupt"


@pytest.mark.parametrize("remaining", ["nodes", "component"])
def test_missing_control_with_actual_open_data_is_corruption_not_an_empty_pool(book, remaining):
    with book.storage.maintenance_unit(book.company) as unit:
        repository = unit.repository("reading")
        if remaining == "nodes":
            pool, store, tree = graph(repository)
            root = tree.full((Entry("key", {"text": "真实节点"}),))
            assert _open_store.publish(repository, pool.token, store.finish((), (root.digest,)))
            repository._session.execute(
                "DELETE FROM read_item WHERE scope_key=? AND collection_key=?",
                (_open_store.SCOPE, _open_store.CONTROL),
            )
        else:
            key = scope(book, "2026-01")
            repository.register(key, _open_store.COMPONENT, unit.head, 8)
            repository._session.execute(
                "INSERT INTO read_component VALUES(?,?,?,'{}')",
                (key, _open_store.COMPONENT, unit.head),
            )
    with book.storage.read_unit(book.company) as read, pytest.raises(ContractError) as error:
        _open_store.OpenPool(read.repository("reading"))
    assert error.value.code == "storage_corrupt"
    with book.storage.maintenance_unit(book.company) as unit:
        _open_store.reset(unit.repository("reading"), unit.head, 9)
    with book.storage.read_unit(book.company) as read:
        pool = _open_store.OpenPool(read.repository("reading"))
        assert pool.revision == 0 and pool.generation is not None


@pytest.mark.parametrize("boundary", ["delete", "insert", "control"])
def test_node_reference_and_control_changes_roll_back_together(book, monkeypatch, boundary):
    token, root, plan = prepare(book, "original")
    assert publish(book, token, plan)
    with book.storage.read_unit(book.company) as read:
        pool, store, tree = graph(read.repository("reading"))
        changed = tree.full((Entry("replacement", {"text": "替换"}),))
        token, plan = pool.token, store.finish((root.digest,), (changed.digest,))
    assert plan.deletes and plan.writes
    before = rows(book)
    reached = []
    with pytest.raises(RuntimeError, match="real pool publication boundary"):
        with book.storage.maintenance_unit(book.company) as unit:
            repository = unit.repository("reading")
            session = repository._session
            method = "execute" if boundary == "control" else "executemany"
            original = getattr(session, method)

            def interrupt(sql, arguments=()):
                result = original(sql, arguments)
                if boundary == "control":
                    selected = _open_store.CONTROL in arguments
                else:
                    selected = sql.startswith("DELETE" if boundary == "delete" else "INSERT")
                if selected:
                    reached.append(boundary)
                    raise RuntimeError("real pool publication boundary")
                return result

            monkeypatch.setattr(session, method, interrupt)
            _open_store.publish(repository, token, plan)
    assert reached == [boundary] and rows(book) == before
    assert publish(book, token, plan)


def test_releasing_one_shared_root_keeps_the_other_and_final_release_cleans_the_graph(book):
    token, root, plan = prepare(book, "shared", copies=2)
    assert publish(book, token, plan)
    for count in (1, 0):
        with book.storage.read_unit(book.company) as read:
            pool, store, _ = graph(read.repository("reading"))
            token = pool.token
            plan = store.finish((root.digest,) * (count + 1), (root.digest,) * count)
        assert publish(book, token, plan)
        with book.storage.read_unit(book.company) as read:
            pool, _, tree = graph(read.repository("reading"))
            if count:
                assert len(tree.scan(tree.root(root.wire()), limit=20)) == 6
            else:
                assert pool.get_blocks(((_open_store.NODES, root.digest),)) == (None,)
                assert (
                    read.repository("reading")._session.fetchone(
                        "SELECT COUNT(*) FROM read_item WHERE scope_key=? "
                        "AND collection_key IN (?,?)",
                        (_open_store.SCOPE, _open_store.NODES, _open_store.REFS),
                    )[0]
                    == 0
                )


def test_reset_preserves_formal_and_other_components_and_requeues_all_open_scopes(
    tmp_path,
):
    book = assemble(tmp_path)
    cost = expense("reset-preserves-formal", "321")
    book.submit(cost)
    query = {
        "company_id": book.company,
        "business_ids": [uid("opening"), cost["business_id"]],
        "include": ["facts"],
    }
    selected = {
        "company_id": book.company,
        "period_from": "2026-01",
        "period_to": "2026-01",
        "view": "as_posted",
        "as_of": "2026-01",
    }
    business = book.app.accounting_get(query)
    with book.storage.read_unit(book.company) as read:
        formal_head = read.head
        adopted = book.ledger.query_adopted_directory(selected, (), PageQuery(500), read)
        documents = book.records.document_status(read)
    token, _, plan = prepare(book, "before-reset")
    assert publish(book, token, plan)
    open_keys = tuple(scope(book, month) for month in ("2026-01", "2026-02", "2026-03"))
    with book.storage.maintenance_unit(book.company) as unit:
        repository = unit.repository("reading")
        for key, state in zip(open_keys, ("ready", "failed", "preparing"), strict=True):
            repository.register(key, _open_store.COMPONENT, unit.head, 8)
            repository._session.execute(
                "UPDATE read_scope_head SET ready_head=?,content_digest=?,state=?,error_code='old' "
                "WHERE scope_key=? AND component_key=?",
                (unit.head, "a" * 64, state, key, _open_store.COMPONENT),
            )
            repository._session.execute(
                "INSERT INTO read_component VALUES(?,?,?,'{}')",
                (key, _open_store.COMPONENT, unit.head),
            )
            repository._session.execute(
                "INSERT INTO read_item(scope_key,collection_key,item_id,component_key,head,"
                "sort_key,payload_json) VALUES(?,'open_members','obsolete',?,?,'obsolete','{}')",
                (key, _open_store.COMPONENT, unit.head),
            )
            repository.open_mark(key, unit.head, (cost["business_id"],))
        # Opaque sentinels only prove reset's deletion boundary; they are not
        # replacement business calculations or a claim that those pages are ready.
        for component in ("brief_activity", "financial", "projection"):
            repository.register("other/" + component, component, unit.head, 1)
            repository._session.execute(
                "INSERT INTO read_component VALUES(?,?,?,'{}')",
                ("other/" + component, component, unit.head),
            )
            repository._session.execute(
                "INSERT INTO read_item(scope_key,collection_key,item_id,component_key,head,"
                "sort_key,payload_json) "
                "VALUES(?,?,'keep',?,?,'keep','{}')",
                ("other/" + component, "opaque", component, unit.head),
            )
    with book.storage.read_unit(book.company) as read:
        others = other_components(read.repository("reading"))
    with book.storage.maintenance_unit(book.company) as unit:
        _open_store.reset(unit.repository("reading"), unit.head, 9)
    with book.storage.read_unit(book.company) as read:
        repository = read.repository("reading")
        pool = _open_store.OpenPool(repository)
        assert pool.revision == 0 and pool.generation is not None
        assert read.head == formal_head
        assert (
            repository._session.fetchone(
                "SELECT COUNT(*) FROM read_item WHERE scope_key=? AND collection_key<>?",
                (_open_store.SCOPE, _open_store.CONTROL),
            )[0]
            == 0
        )
        for key in open_keys:
            state = repository.state(key, _open_store.COMPONENT)
            assert (
                state["state"],
                state["wanted_head"],
                state["ready_head"],
                state["format_version"],
                state["content_digest"],
                state["error_code"],
            ) == ("preparing", formal_head, 0, 9, None, None)
            assert repository.payload(key, _open_store.COMPONENT) is None
            assert _brief_open_inputs.pending(repository, key) == (True, set())
            assert (
                repository._session.fetchone(
                    "SELECT COUNT(*) FROM read_item WHERE scope_key=? AND component_key=?",
                    (key, _open_store.COMPONENT),
                )[0]
                == 1
            )
        assert other_components(repository) == others
        assert book.records.document_status(read) == documents
        assert book.ledger.query_adopted_directory(selected, (), PageQuery(500), read) == adopted
    assert book.app.accounting_get(query) == business
