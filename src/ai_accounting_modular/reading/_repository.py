"""Only derived reading tables are visible to this repository."""

from ai_accounting_modular.contracts.public import canonical_json

from ._collections import PreparedModel


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

    def pending(self, limit):
        return tuple(
            dict(row)
            for row in self._session.fetchall(
                "SELECT * FROM read_scope_head WHERE state='preparing' "
                "ORDER BY scope_key,component_key LIMIT ?",
                (limit,),
            )
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
        if not isinstance(model, PreparedModel):
            raise TypeError("model must be prepared before publication")
        state = self.state(scope_key, component)
        if state is None or (state["wanted_head"], state["format_version"]) != (
            wanted_head,
            format_version,
        ):
            return False
        rows = tuple((scope_key, *row, component, wanted_head) for row in model.rows)
        self._session.execute(
            "DELETE FROM read_item WHERE scope_key=? AND component_key=?", (scope_key, component)
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

    def fail(self, scope_key, component, wanted_head, code, *, expected_state="preparing"):
        self._session.execute(
            "UPDATE read_scope_head SET state='failed',error_code=? "
            "WHERE scope_key=? AND component_key=? AND wanted_head=? AND state=?",
            (code, scope_key, component, wanted_head, expected_state),
        )

    def retry(self, scope_key, component, *, format_version=None):
        self._session.execute(
            "UPDATE read_scope_head SET state='preparing',error_code=NULL,"
            "format_version=COALESCE(?,format_version) "
            "WHERE scope_key=? AND component_key=? AND "
            "(state='failed' OR (? IS NOT NULL AND format_version<>?))",
            (format_version, scope_key, component, format_version, format_version),
        )
