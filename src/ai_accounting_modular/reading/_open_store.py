"""Reading's shared Open node pool; one graph token protects reference-count plans."""

from uuid import UUID, uuid4

from ai_accounting_modular.contracts.public import ContractError, canonical_json, loads_json

COMPONENT = "brief_open_items"
NAMESPACE = "_open"
SCOPE = "_open/index"
CONTROL = "_open_control"
NODES, REFS = NAMESPACE + "_nodes", NAMESPACE + "_node_refs"
FORMAT = "reading-open-pool/1"


def corrupt():
    raise ContractError("storage_corrupt", "待收待付共享索引的保存内容不完整，请重新生成。")


def _control(generation, revision):
    return canonical_json({"format": FORMAT, "generation": generation, "revision": revision})


class OpenPool:
    def __init__(self, repository):
        self._session = repository._session
        row = self._session.fetchone(
            "SELECT * FROM read_item WHERE scope_key=? AND collection_key=? AND item_id='head'",
            (SCOPE, CONTROL),
        )
        self.token, self.revision, self.generation = None, 0, None
        if row is None:
            if self._session.fetchone(
                "SELECT 1 FROM read_item WHERE scope_key=? LIMIT 1", (SCOPE,)
            ) or self._session.fetchone(
                "SELECT 1 FROM read_component WHERE component_key=? LIMIT 1", (COMPONENT,)
            ):
                corrupt()
            return
        try:
            raw = row["payload_json"]
            value = loads_json(raw)
            revision, generation = value["revision"], value["generation"]
            if (
                set(value) != {"format", "generation", "revision"}
                or value["format"] != FORMAT
                or type(revision) is not int
                or revision < 0
                or type(generation) is not str
                or str(UUID(generation)) != generation
                or raw != _control(generation, revision)
            ):
                raise ValueError
            self._check_row(row, CONTROL, "head", revision, exact=True)
        except (ContractError, KeyError, TypeError, ValueError) as error:
            raise ContractError("storage_corrupt", "待收待付共享索引的发布状态不完整。") from error
        self.token, self.revision, self.generation = raw, revision, generation

    @staticmethod
    def _check_row(row, collection, identity, revision, *, exact=False):
        if (
            row["scope_key"] != SCOPE
            or row["component_key"] != COMPONENT
            or row["collection_key"] != collection
            or row["item_id"] != identity
            or row["sort_key"] != identity
            or type(row["head"]) is not int
            or (row["head"] != revision if exact else not 1 <= row["head"] <= revision)
            or any(
                row[key] is not None
                for key in ("business_id", "object_id", "group_key", "category")
            )
        ):
            corrupt()

    def get_blocks(self, keys):
        keys = tuple(keys)
        if not keys:
            return ()
        if any(collection not in {NODES, REFS} for collection, _ in keys):
            corrupt()
        rows = self._session.fetchall(
            "SELECT * FROM read_item WHERE scope_key=? AND (collection_key,item_id) IN "
            "(SELECT json_extract(value,'$[0]'),json_extract(value,'$[1]') FROM json_each(?))",
            (SCOPE, canonical_json([list(key) for key in keys])),
        )
        found = {}
        for row in rows:
            identity = row["collection_key"], row["item_id"]
            self._check_row(row, *identity, self.revision)
            found[identity] = row["payload_json"]
        return tuple(found.get(key) for key in keys)


def _save_control(repository, generation, revision):
    raw = _control(generation, revision)
    repository._session.execute(
        "INSERT INTO read_item(scope_key,collection_key,item_id,component_key,head,sort_key,"
        "payload_json) VALUES(?,?,'head',?,?,'head',?) "
        "ON CONFLICT(scope_key,collection_key,item_id) DO UPDATE SET "
        "component_key=excluded.component_key,head=excluded.head,sort_key=excluded.sort_key,"
        "business_id=NULL,object_id=NULL,group_key=NULL,category=NULL,payload_json=excluded.payload_json",
        (SCOPE, CONTROL, COMPONENT, revision, raw),
    )


def publish(repository, token, plan):
    """Call only inside the target's maintenance transaction, after its own CAS."""
    current = OpenPool(repository)
    if current.token != token:
        return False
    if any(write.collection not in {NODES, REFS} for write in plan.writes) or any(
        collection not in {NODES, REFS} for collection, _ in plan.deletes
    ):
        corrupt()
    revision = current.revision + 1
    repository._session.executemany(
        "DELETE FROM read_item WHERE scope_key=? AND component_key=? "
        "AND collection_key=? AND item_id=?",
        ((SCOPE, COMPONENT, *key) for key in plan.deletes),
    )
    repository._session.executemany(
        "INSERT INTO read_item(scope_key,collection_key,item_id,component_key,head,sort_key,"
        "payload_json) VALUES(?,?,?,?,?,?,?) ON CONFLICT(scope_key,collection_key,item_id) "
        "DO UPDATE SET component_key=excluded.component_key,head=excluded.head,"
        "sort_key=excluded.sort_key,business_id=NULL,object_id=NULL,group_key=NULL,category=NULL,"
        "payload_json=excluded.payload_json",
        (
            (SCOPE, write.collection, write.key, COMPONENT, revision, write.key, write.payload_json)
            for write in plan.writes
        ),
    )
    _save_control(repository, current.generation or str(uuid4()), revision)
    return True


def reset(repository, head, format_version):
    """Explicit corrupt/obsolete Open retry replaces this graph, never formal data."""
    session = repository._session
    session.execute(
        "DELETE FROM read_item WHERE scope_key=? OR component_key=?", (SCOPE, COMPONENT)
    )
    session.execute("DELETE FROM read_component WHERE component_key=?", (COMPONENT,))
    session.execute(
        "UPDATE read_scope_head SET wanted_head=MAX(wanted_head,?),ready_head=0,"
        "format_version=?,content_digest=NULL,state='preparing',error_code=NULL,"
        "revision=revision+1 "
        "WHERE component_key=?",
        (head, format_version, COMPONENT),
    )
    scopes = session.fetchall(
        "SELECT scope_key FROM read_scope_head WHERE component_key=?", (COMPONENT,)
    )
    for row in scopes:
        repository.open_mark(row["scope_key"], head, full=True)
    _save_control(repository, str(uuid4()), 0)
