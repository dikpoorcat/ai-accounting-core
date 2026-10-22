"""Reading-owned storage and CAS; opaque financial JSON belongs to Ledger."""

from dataclasses import dataclass
from hashlib import sha256

from ai_accounting_modular.contracts.public import ContractError, canonical_json, loads_json
from ai_accounting_modular.ledger.public import (
    FINANCIAL_PROJECTION_FORMAT,
    FinancialProjectionPlan,
)

COMPONENT = "projection"
CONTROL = "_financial_control"
OWNER_PREFIX = "_financial/"


def digest(raw):
    return sha256(raw.encode("utf-8")).hexdigest()


def corrupt():
    raise ContractError("storage_corrupt", "财务准备模型的保存内容或技术状态不完整。")


def cache_corruption(error):
    # Authorization/operation errors are programming failures, not corrupt cached JSON.
    return not isinstance(error, ContractError) or error.code in {
        "invalid_input",
        "invalid_contract",
        "storage_corrupt",
        "needs_information",
        "unsupported_business",
    }


@dataclass(frozen=True)
class ProducerToken:
    wanted: int
    format: int
    ready: int
    base_digest: str | None
    work_digest: str | None


@dataclass(frozen=True)
class ConsumerToken:
    descriptor_key: tuple[str, str]
    descriptor_digest: str | None
    row_digest: str | None
    format: int = FINANCIAL_PROJECTION_FORMAT


def empty_work():
    return {"registered": {}, "pending": [], "changes": [], "closures": [], "rebuild": False}


class FinancialStore:
    def __init__(self, repository, scope_key):
        self.repository, self.scope_key = repository, scope_key
        self._session = repository._session

    def state(self):
        return self.repository.state(self.scope_key, COMPONENT)

    def _row(self, row, collection, key, state):
        if (
            state is None
            or row["scope_key"] != self.scope_key
            or row["component_key"] != COMPONENT
            or row["collection_key"] != collection
            or row["item_id"] != key
            or row["sort_key"] != key
            or row["head"] > state["wanted_head"]
            or any(
                row[field] is not None
                for field in ("business_id", "object_id", "group_key", "category")
            )
        ):
            corrupt()
        return row["payload_json"]

    @property
    def header_json(self):
        state = self.state()
        if state is None:
            return None
        if state["format_version"] != FINANCIAL_PROJECTION_FORMAT:
            corrupt()
        row = self.repository.payload(self.scope_key, COMPONENT)
        if row is None:
            if state["ready_head"] != 0 or state["content_digest"] is not None:
                corrupt()
            return None
        raw = row["payload_json"]
        if row["head"] != state["ready_head"] or digest(raw) != state["content_digest"]:
            corrupt()
        return raw

    def get_blocks(self, keys):
        if not keys:
            return ()
        rows = self._session.fetchall(
            "SELECT * FROM read_item WHERE scope_key=? AND component_key=? AND "
            "(collection_key,item_id) IN (SELECT ?||json_extract(value,'$[0]'),"
            "json_extract(value,'$[1]') FROM json_each(?))",
            (self.scope_key, COMPONENT, OWNER_PREFIX, canonical_json([list(key) for key in keys])),
        )
        values = {}
        state = self.state()
        for row in rows:
            identity = row["collection_key"][len(OWNER_PREFIX) :], row["item_id"]
            raw = self._row(row, OWNER_PREFIX + identity[0], identity[1], state)
            if state is None or row["head"] > state["ready_head"]:
                corrupt()
            values[identity] = raw
        return tuple(values.get(identity) for identity in keys)

    def work(self):
        rows = self.repository.item_get(self.scope_key, COMPONENT, CONTROL, ("work",))
        if not rows:
            if self.state() is not None:
                corrupt()
            return empty_work()
        raw = self._row(rows[0], CONTROL, "work", self.state())
        value = loads_json(raw)
        if (
            not isinstance(value, dict)
            or set(value) != set(empty_work())
            or canonical_json(value) != raw
            or not isinstance(value["registered"], dict)
            or not isinstance(value["pending"], list)
            or value["pending"] != sorted(set(value["pending"]))
            or not set(value["pending"]) <= value["registered"].keys()
            or not isinstance(value["changes"], list)
            or not isinstance(value["closures"], list)
            or type(value["rebuild"]) is not bool
        ):
            corrupt()
        return value

    def token(self):
        state = self.state()
        if state is None:
            return None
        rows = self.repository.item_get(self.scope_key, COMPONENT, CONTROL, ("work",))
        return ProducerToken(
            state["wanted_head"],
            state["format_version"],
            state["ready_head"],
            state["content_digest"],
            digest(canonical_json(rows[0])) if rows else None,
        )

    def save_work(self, work, head, *, retry=False):
        self.repository.register(self.scope_key, COMPONENT, head, FINANCIAL_PROJECTION_FORMAT)
        raw = canonical_json(work)
        self._session.execute(
            "INSERT INTO read_item(scope_key,collection_key,item_id,sort_key,component_key,head,"
            "payload_json) VALUES(?,?,'work','work',?,?,?) ON CONFLICT(scope_key,collection_key,"
            "item_id) DO UPDATE SET head=excluded.head,payload_json=excluded.payload_json,"
            "sort_key=excluded.sort_key,component_key=excluded.component_key,"
            "business_id=NULL,object_id=NULL,group_key=NULL,category=NULL",
            (self.scope_key, CONTROL, COMPONENT, head, raw),
        )
        self._session.execute(
            "UPDATE read_scope_head SET wanted_head=MAX(wanted_head,?),format_version=?,"
            "state=CASE WHEN state='failed' AND ?=0 THEN state ELSE 'preparing' END,"
            "error_code=CASE WHEN state='failed' AND ?=0 THEN error_code ELSE NULL END,"
            "revision=revision+1 "
            "WHERE scope_key=? AND component_key=?",
            (head, FINANCIAL_PROJECTION_FORMAT, retry, retry, self.scope_key, COMPONENT),
        )

    def _matches(self, token):
        state = self.state()
        return state is not None and state["state"] == "preparing" and self.token() == token

    def publish(self, token, plan, *, rebuild=False):
        if not isinstance(plan, FinancialProjectionPlan):
            raise TypeError("financial publication requires the owner's prepared plan")
        if not self._matches(token):
            return False
        if token.format != FINANCIAL_PROJECTION_FORMAT:
            corrupt()
        if digest(plan.header_json) != plan.header_digest:
            corrupt()
        writes = {(collection, key) for collection, key, _ in plan.writes}
        if (
            len(writes) != len(plan.writes)
            or len(set(plan.deletes)) != len(plan.deletes)
            or writes & set(plan.deletes)
        ):
            corrupt()
        if rebuild:
            self._session.execute(
                "DELETE FROM read_item WHERE scope_key=? AND component_key=? AND collection_key<>?",
                (self.scope_key, COMPONENT, CONTROL),
            )
        self._session.executemany(
            "DELETE FROM read_item WHERE scope_key=? AND component_key=? AND collection_key=? "
            "AND item_id=?",
            (
                (self.scope_key, COMPONENT, OWNER_PREFIX + collection, key)
                for collection, key in plan.deletes
            ),
        )
        self._session.executemany(
            "INSERT INTO read_item(scope_key,collection_key,item_id,sort_key,component_key,head,"
            "payload_json) VALUES(?,?,?,?,?,?,?) ON CONFLICT(scope_key,collection_key,item_id) "
            "DO UPDATE SET payload_json=excluded.payload_json,head=excluded.head",
            (
                (self.scope_key, OWNER_PREFIX + collection, key, key, COMPONENT, token.wanted, raw)
                for collection, key, raw in plan.writes
            ),
        )
        self._session.execute(
            "INSERT INTO read_component VALUES(?,?,?,?) ON CONFLICT(scope_key,component_key) "
            "DO UPDATE SET head=excluded.head,payload_json=excluded.payload_json",
            (self.scope_key, COMPONENT, token.wanted, plan.header_json),
        )
        work = self.work()
        work.update(pending=[], changes=[], closures=[], rebuild=False)
        self._session.execute(
            "UPDATE read_item SET payload_json=?,head=? WHERE scope_key=? AND component_key=? "
            "AND collection_key=? AND item_id='work'",
            (canonical_json(work), token.wanted, self.scope_key, COMPONENT, CONTROL),
        )
        self._session.execute(
            "UPDATE read_scope_head SET ready_head=?,content_digest=?,"
            "state='ready',error_code=NULL,revision=revision+1 "
            "WHERE scope_key=? AND component_key=?",
            (token.wanted, plan.header_digest, self.scope_key, COMPONENT),
        )
        return True

    def fail(self, token, code, *, expected_revision):
        if not self._matches(token):
            return False
        return self.repository.fail(
            self.scope_key,
            COMPONENT,
            token.wanted,
            code,
            expected_revision=expected_revision,
        )

    def consumer_token(self, descriptor_key):
        collection, identity = descriptor_key
        rows = self.repository.item_get(
            self.scope_key, COMPONENT, OWNER_PREFIX + collection, (identity,)
        )
        if not rows:
            return ConsumerToken(descriptor_key, None, None)
        return ConsumerToken(
            descriptor_key, digest(rows[0]["payload_json"]), digest(canonical_json(rows[0]))
        )

    def matches_consumer(self, token, *, validate=True):
        state = self.state()
        if state is None or state["format_version"] != token.format:
            return False
        current = self.consumer_token(token.descriptor_key)
        if current.descriptor_digest != token.descriptor_digest:
            return False
        if validate:
            if token.descriptor_digest is None:
                return False
            self.get_blocks((token.descriptor_key,))
            return True
        # A failure must also still describe the same physical corruption, if any.
        return current.row_digest == token.row_digest

    def mark_consumer(self, scope_key, head):
        self._session.execute(
            "UPDATE read_scope_head SET wanted_head=MAX(wanted_head,?),"
            "state=CASE WHEN state='failed' THEN state ELSE 'preparing' END,"
            "error_code=CASE WHEN state='failed' THEN error_code ELSE NULL END,"
            "revision=revision+1 "
            "WHERE scope_key=? AND component_key='financial'",
            (head, scope_key),
        )
