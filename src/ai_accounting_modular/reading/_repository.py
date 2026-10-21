"""Only derived reading tables are visible to this repository."""

from ai_accounting_modular.contracts.public import canonical_json, loads_json

from . import _brief_activity_index as activity_index
from . import _brief_open_index as open_index
from ._collections import PreparedModel
from ._financial_projection import PreparedFinancial
from ._financial_store import FinancialStore


class ReadingRepository:
    def __init__(self, session):
        self._session = session

    def reset(self):
        for table in ("read_item", "read_component", "read_scope_head"):
            self._session.execute("DELETE FROM " + table)

    def state(self, scope_key, component):
        row = self._session.fetchone(
            "SELECT * FROM read_scope_head WHERE scope_key=? AND component_key=?",
            (scope_key, component),
        )
        return dict(row) if row else None

    def financial_store(self, scope_key):
        return FinancialStore(self, scope_key)

    def register(self, scope_key, component, head, version):
        self._session.execute(
            "INSERT INTO read_scope_head(scope_key,component_key,wanted_head,format_version,state) "
            "VALUES(?,?,?,?,'preparing') ON CONFLICT(scope_key,component_key) DO NOTHING",
            (scope_key, component, head, version),
        )

    def scopes(self):
        return tuple(dict(row) for row in self._session.fetchall("SELECT * FROM read_scope_head"))

    def mark(self, scope_key, component, head):
        self._session.execute(
            "UPDATE read_scope_head SET wanted_head=?,state='preparing',error_code=NULL "
            "WHERE scope_key=? AND component_key=? AND wanted_head<?",
            (head, scope_key, component, head),
        )

    def payload(self, scope_key, component):
        row = self._session.fetchone(
            "SELECT * FROM read_component WHERE scope_key=? AND component_key=?",
            (scope_key, component),
        )
        return dict(row) if row else None

    def input_dependencies(self, scope_key, component):
        """Read only the declared sparse inputs; never load collection items."""
        row = self._session.fetchone(
            "SELECT json_extract(payload_json,'$._inputs') FROM read_component "
            "WHERE scope_key=? AND component_key=?",
            (scope_key, component),
        )
        return row[0] if row else None

    def publish(self, scope_key, component, wanted_head, format_version, model):
        financial = model if isinstance(model, PreparedFinancial) else None
        if financial is not None:
            if (
                component != "financial"
                or scope_key != financial.consumer_scope
                or not self.financial_store(financial.producer_scope).matches_consumer(
                    financial.guard
                )
            ):
                return False
            model = financial.model
        patch = model if isinstance(model, open_index.PreparedOpenPatch) else None
        if patch is not None:
            model = patch.model
        if not isinstance(model, PreparedModel):
            raise TypeError("model must be prepared before publication")
        state = self.state(scope_key, component)
        if state is None or (state["wanted_head"], state["format_version"]) != (
            wanted_head,
            format_version,
        ):
            return False
        if patch is not None and (
            component != open_index.COMPONENT
            or state["ready_head"] != patch.base_ready
            or state["content_digest"] != patch.base_digest
        ):
            return False
        epoch = (
            open_index.validate(loads_json(model.payload_json))
            if component == open_index.COMPONENT
            else wanted_head
        )
        if patch is not None and epoch != patch.item_epoch:
            return False
        rows = tuple((scope_key, *row, component, epoch) for row in model.rows)
        if patch is None:
            self._session.execute(
                "DELETE FROM read_item WHERE scope_key=? AND component_key=?",
                (scope_key, component),
            )
        else:
            self._session.executemany(
                "DELETE FROM read_item WHERE scope_key=? AND component_key=? "
                "AND collection_key=? AND item_id=?",
                ((scope_key, component, *identity) for identity in patch.deletes),
            )
            self._session.execute(
                "DELETE FROM read_item WHERE scope_key=? AND component_key=? AND collection_key=?",
                (scope_key, component, open_index.DIRTY),
            )
        if rows:
            self._session.executemany(
                "INSERT INTO read_item(scope_key,collection_key,item_id,sort_key,business_id,"
                "object_id,group_key,category,payload_json,component_key,head) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
        self._session.execute(
            "INSERT INTO read_component VALUES(?,?,?,?) ON CONFLICT(scope_key,component_key) "
            "DO UPDATE SET head=excluded.head,payload_json=excluded.payload_json",
            (scope_key, component, wanted_head, model.payload_json),
        )
        self._session.execute(
            "UPDATE read_scope_head SET ready_head=?,content_digest=?,"
            "state='ready',error_code=NULL "
            "WHERE scope_key=? AND component_key=?",
            (wanted_head, model.content_digest, scope_key, component),
        )
        return True

    def open_pending_rows(self, scope_key):
        return tuple(
            dict(row)
            for row in self._session.fetchall(
                "SELECT * FROM read_item WHERE scope_key=? AND component_key=? "
                "AND collection_key=?",
                (scope_key, open_index.COMPONENT, open_index.DIRTY),
            )
        )

    def activity_rows(self, scope_key):
        return tuple(
            dict(row)
            for row in self._session.fetchall(
                "SELECT * FROM read_item WHERE scope_key=? AND component_key=? "
                "AND collection_key<>?",
                (scope_key, activity_index.COMPONENT, activity_index.DIRTY),
            )
        )

    def activity_pending_rows(self, scope_key):
        return tuple(
            dict(row)
            for row in self._session.fetchall(
                "SELECT * FROM read_item WHERE scope_key=? AND component_key=? "
                "AND collection_key=?",
                (scope_key, activity_index.COMPONENT, activity_index.DIRTY),
            )
        )

    def activity_mark(self, scope_key, head, business_ids=(), *, full=False):
        from ._collections import IndexItem, prepare_items

        if full:
            self._session.execute(
                "DELETE FROM read_item WHERE scope_key=? AND component_key=? AND collection_key=?",
                (scope_key, activity_index.COMPONENT, activity_index.DIRTY),
            )
        previous = None if full else activity_index.pending(self, scope_key)
        full = full or bool(previous and previous["full"])
        values = (
            []
            if full
            else sorted(set(business_ids) | set(previous["business_ids"] if previous else ()))
        )
        item = IndexItem(
            activity_index.DIRTY, "current", "current", {"full": full, "business_ids": values}
        )
        rows = prepare_items((item,)).rows
        self._session.executemany(
            "INSERT INTO read_item(scope_key,collection_key,item_id,sort_key,business_id,"
            "object_id,group_key,category,payload_json,component_key,head) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(scope_key,collection_key,item_id) "
            "DO UPDATE SET payload_json=excluded.payload_json,head=excluded.head",
            ((scope_key, *row, activity_index.COMPONENT, head) for row in rows),
        )

    def activity_advance_failed(self, scope_key, head):
        self._session.execute(
            "UPDATE read_scope_head SET wanted_head=MAX(wanted_head,?) WHERE scope_key=? "
            "AND component_key=? AND state='failed'",
            (head, scope_key, activity_index.COMPONENT),
        )

    def open_bucket_count(self, scope_key):
        return self._session.fetchone(
            "SELECT COUNT(*) FROM read_item WHERE scope_key=? AND component_key=? "
            "AND collection_key=?",
            (scope_key, open_index.COMPONENT, open_index.BUCKETS),
        )[0]

    def open_bucket_rows(self, scope_key, bucket_keys):
        return tuple(
            dict(row)
            for row in self._session.fetchall(
                "SELECT * FROM read_item WHERE scope_key=? AND component_key=? "
                "AND collection_key=? AND item_id IN (SELECT value FROM json_each(?))",
                (
                    scope_key,
                    open_index.COMPONENT,
                    open_index.BUCKETS,
                    canonical_json(list(bucket_keys)),
                ),
            )
        )

    def open_mark(self, scope_key, head, business_ids=(), *, full=False):
        from ._collections import prepare_items

        if full:
            self._session.execute(
                "DELETE FROM read_item WHERE scope_key=? AND component_key=? AND collection_key=?",
                (scope_key, open_index.COMPONENT, open_index.DIRTY),
            )
        values = (
            [open_index.item(open_index.DIRTY, "*", {"full": True})]
            if full
            else [
                open_index.item(open_index.DIRTY, identity, {"business_id": identity})
                for identity in sorted(business_ids)
            ]
        )
        rows = prepare_items(values).rows
        self._session.executemany(
            "INSERT INTO read_item(scope_key,collection_key,item_id,sort_key,business_id,"
            "object_id,group_key,category,payload_json,component_key,head) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(scope_key,collection_key,item_id) "
            "DO UPDATE SET payload_json=excluded.payload_json,head=excluded.head",
            ((scope_key, *row, open_index.COMPONENT, head) for row in rows),
        )

    def open_advance_failed(self, scope_key, head):
        self._session.execute(
            "UPDATE read_scope_head SET wanted_head=MAX(wanted_head,?) WHERE scope_key=? "
            "AND component_key=? AND state='failed'",
            (head, scope_key, open_index.COMPONENT),
        )

    def _item_scope(self, scope_key, component, collection, query=None):
        where = "scope_key=? AND component_key=? AND collection_key=?"
        args = [scope_key, component, collection]
        if query is not None:
            for key in ("business_id", "object_id", "group_key", "item_id"):
                value = getattr(query, key)
                if value is not None:
                    where += f" AND {key}=?"
                    args.append(value)
            if query.categories:
                where += " AND category IN (SELECT value FROM json_each(?))"
                args.append(canonical_json(list(query.categories)))
        return where, tuple(args)

    def item_counts(self, scope_key, component, collection, query):
        values = []
        previous = None
        for selected in (None, query):
            where, args = self._item_scope(scope_key, component, collection, selected)
            if (where, args) == previous:
                values.append(values[0])
                continue
            values.append(
                self._session.fetchone("SELECT COUNT(*) FROM read_item WHERE " + where, args)[0]
            )
            previous = where, args
        return tuple(values)

    def item_get(self, scope_key, component, collection, item_ids):
        return tuple(
            dict(row)
            for row in self._session.fetchall(
                "SELECT * FROM read_item WHERE scope_key=? AND component_key=? "
                "AND collection_key=? AND item_id IN (SELECT value FROM json_each(?))",
                (scope_key, component, collection, canonical_json(list(item_ids))),
            )
        )

    def item_page(self, scope_key, component, collection, query, after, limit):
        where, args = self._item_scope(scope_key, component, collection, query)
        if after is not None:
            where += " AND (sort_key,item_id)>(?,?)"
            args = (*args, *after)
        return tuple(
            dict(row)
            for row in self._session.fetchall(
                "SELECT * FROM read_item WHERE " + where + " ORDER BY sort_key,item_id LIMIT ?",
                (*args, limit),
            )
        )

    def fail(
        self,
        scope_key,
        component,
        wanted_head,
        code,
        *,
        expected_state="preparing",
        expected_format=None,
    ):
        self._session.execute(
            "UPDATE read_scope_head SET state='failed',error_code=? "
            "WHERE scope_key=? AND component_key=? AND wanted_head=? AND state=? "
            "AND (? IS NULL OR format_version=?)",
            (
                code,
                scope_key,
                component,
                wanted_head,
                expected_state,
                expected_format,
                expected_format,
            ),
        )

    def retry(self, scope_key, component, *, format_version=None):
        self._session.execute(
            "UPDATE read_scope_head SET state='preparing',error_code=NULL,"
            "format_version=COALESCE(?,format_version) "
            "WHERE scope_key=? AND component_key=? AND "
            "(state='failed' OR (? IS NOT NULL AND format_version<>?))",
            (format_version, scope_key, component, format_version, format_version),
        )
