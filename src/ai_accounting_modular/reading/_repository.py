"""Only derived reading tables are visible to this repository."""

from ai_accounting_modular.contracts.public import ContractError, canonical_json, loads_json

from . import _brief_activity_index as activity_index
from . import _brief_open_index as open_index
from . import _brief_open_rollover as open_rollover
from . import _descriptor, _open_store
from ._collections import PreparedModel
from ._financial_projection import PreparedFinancial
from ._financial_store import FinancialStore
from ._publication_guard import CaptureGuard


class ReadingRepository:
    def __init__(self, session):
        self._session = session
        self._component_views = session.snapshot_cache()

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

    def pending(self, *, after=None, limit=100):
        where, args = "state='preparing'", ()
        if after is not None:
            where += " AND (scope_key,component_key)>(?,?)"
            args = tuple(after)
        return tuple(
            dict(row)
            for row in self._session.fetchall(
                "SELECT * FROM read_scope_head WHERE "
                + where
                + " ORDER BY scope_key,component_key LIMIT ?",
                (*args, limit),
            )
        )

    def previous_ready(self, scope_key, component):
        before = scope_key
        while True:
            rows = self._session.fetchall(
                "SELECT * FROM read_scope_head WHERE state='ready' AND scope_key<? "
                "AND component_key=? ORDER BY scope_key DESC LIMIT 50",
                (before, component),
            )
            yield from (dict(row) for row in rows)
            if len(rows) < 50:
                return
            before = rows[-1]["scope_key"]

    def mark(self, scope_key, component, head):
        self._session.execute(
            "UPDATE read_scope_head SET wanted_head=?,state='preparing',error_code=NULL,"
            "revision=revision+1 "
            "WHERE scope_key=? AND component_key=? AND wanted_head<?",
            (head, scope_key, component, head),
        )

    def payload(self, scope_key, component):
        row = self._session.fetchone(
            "SELECT * FROM read_component WHERE scope_key=? AND component_key=?",
            (scope_key, component),
        )
        return dict(row) if row else None

    def component_body(self, scope_key, component, descriptor):
        row = self._session.fetchone(
            "SELECT * FROM read_item WHERE scope_key=? AND collection_key=? AND item_id=?",
            (scope_key, _descriptor.BODY_COLLECTION, component),
        )
        return _descriptor.body(scope_key, component, descriptor, row)

    def publish(
        self, scope_key, component, wanted_head, format_version, model, *, guard, current_head
    ):
        if not isinstance(guard, CaptureGuard):
            raise TypeError("publication requires the captured lifecycle")
        state = self.state(scope_key, component)
        if not guard.matches(state) or (
            guard.scope_key,
            guard.component,
            guard.wanted_head,
            guard.format_version,
        ) != (scope_key, component, wanted_head, format_version):
            return False
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
        open_model = model if isinstance(model, open_index.PreparedOpen) else None
        patch = (
            model
            if isinstance(
                model, (open_index.PreparedOpenPatch, activity_index.PreparedActivityPatch)
            )
            else None
        )
        activity_patch = isinstance(patch, activity_index.PreparedActivityPatch)
        if patch is not None or open_model is not None:
            model = model.model
        if not isinstance(model, PreparedModel):
            raise TypeError("model must be prepared before publication")
        if open_model is not None and (
            component != open_index.COMPONENT or open_model.scope_key != scope_key
        ):
            return False
        fork = isinstance(patch, open_index.PreparedOpenPatch) and patch.base_scope != scope_key
        if patch is not None:
            if component != (activity_index.COMPONENT if activity_patch else open_index.COMPONENT):
                return False
            if activity_patch and patch.scope_key != scope_key:
                return False
            if fork:
                if not open_rollover.initial(self, state):
                    return False
            elif (state["ready_head"], state["content_digest"]) != (
                patch.base_ready,
                patch.base_digest,
            ):
                return False
            if not fork:
                if activity_patch:
                    pending = activity_index.pending(self, scope_key, wanted=wanted_head)
                    if pending is None or pending["full"]:
                        return False
                else:
                    from ._brief_open_inputs import pending as open_pending

                    full, dirty = open_pending(self, scope_key)
                    if full or not dirty:
                        return False
        if component == open_index.COMPONENT:
            if open_model is None:
                raise TypeError("Open publication requires its shared graph plan")
            payload = loads_json(model.payload_json)
            epoch = open_index.validate(payload)
        elif component == activity_index.COMPONENT:
            payload = loads_json(model.payload_json)
            epoch = activity_index.validate(payload, scope_key)
        else:
            payload, epoch = None, wanted_head
        if patch is not None and epoch != patch.item_epoch:
            return False
        if activity_patch:
            current = activity_index.base(self, state)
            if current is None or current.epoch != patch.item_epoch:
                return False
        if fork and not open_rollover.matches(self, patch, scope_key, payload):
            return False
        publication = _descriptor.prepare(
            scope_key, component, wanted_head, format_version, model, payload=payload
        )
        subscriptions, subscription_plan = None, None
        try:
            selection = loads_json(scope_key)
        except ContractError:
            selection = None
        if isinstance(selection, dict) and selection.get("page") in {"monthly", "quarterly_report"}:
            from ._subscriptions import Directory

            subscriptions = Directory(self)
            known = subscriptions.known(scope_key, component)
            subscription_plan = subscriptions.plan(
                scope_key,
                component,
                inputs=model.inputs,
                adds=model.input_adds,
                removes=model.input_removes,
                base_target=(patch.base_scope, component) if fork else None,
            )
            if (
                not known
                or subscription_plan.expanded
                or guard.content_digest is None
                or guard.ready_head == guard.wanted_head
            ) and current_head != guard.captured_head:
                return False
        rows = tuple((scope_key, *row, component, epoch) for row in model.rows)
        if open_model is not None:
            if rows:
                raise TypeError("Open models publish shared nodes rather than scope rows")
            if not _open_store.publish(self, open_model.pool_token, open_model.tree_plan):
                return False
        if open_model is not None or patch is None:
            self._session.execute(
                "DELETE FROM read_item WHERE scope_key=? AND component_key=?",
                (scope_key, component),
            )
        if activity_patch:
            self._session.executemany(
                "DELETE FROM read_item WHERE scope_key=? AND component_key=? "
                "AND collection_key=? AND item_id=?",
                ((scope_key, component, *identity) for identity in patch.deletes),
            )
            self._session.execute(
                "DELETE FROM read_item WHERE scope_key=? AND component_key=? AND collection_key=?",
                (
                    scope_key,
                    component,
                    activity_index.DIRTY,
                ),
            )
        if rows:
            self._session.executemany(
                "INSERT INTO read_item(scope_key,collection_key,item_id,sort_key,business_id,"
                "object_id,group_key,category,payload_json,component_key,head) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
        if subscription_plan is not None and not subscriptions.commit(subscription_plan):
            raise ContractError("storage_corrupt", "同一发布事务中的读取订阅发生变化。")
        self._session.execute(
            "INSERT INTO read_item(scope_key,collection_key,item_id,component_key,head,sort_key,"
            "payload_json) VALUES(?,?,?,?,?,?,?) ON CONFLICT(scope_key,collection_key,item_id) "
            "DO UPDATE SET component_key=excluded.component_key,head=excluded.head,"
            "sort_key=excluded.sort_key,payload_json=excluded.payload_json,"
            "business_id=NULL,object_id=NULL,group_key=NULL,category=NULL",
            (
                scope_key,
                _descriptor.BODY_COLLECTION,
                component,
                component,
                wanted_head,
                component,
                publication.body_json,
            ),
        )
        self._session.execute(
            "INSERT INTO read_component VALUES(?,?,?,?) ON CONFLICT(scope_key,component_key) "
            "DO UPDATE SET head=excluded.head,payload_json=excluded.payload_json",
            (scope_key, component, wanted_head, publication.descriptor_json),
        )
        self._session.execute(
            "UPDATE read_scope_head SET ready_head=?,content_digest=?,"
            "state='ready',error_code=NULL,revision=revision+1 "
            "WHERE scope_key=? AND component_key=?",
            (wanted_head, publication.descriptor_digest, scope_key, component),
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

    def activity_bucket_count(self, scope_key):
        return self._session.fetchone(
            "SELECT COUNT(*) FROM read_item WHERE scope_key=? AND component_key=? "
            "AND collection_key=?",
            (scope_key, activity_index.COMPONENT, activity_index.BUCKETS),
        )[0]

    def activity_bucket_rows(self, scope_key, bucket_keys):
        return tuple(
            dict(row)
            for row in self._session.fetchall(
                "SELECT * FROM read_item WHERE scope_key=? AND component_key=? "
                "AND collection_key=? "
                "AND item_id IN (SELECT value FROM json_each(?))",
                (
                    scope_key,
                    activity_index.COMPONENT,
                    activity_index.BUCKETS,
                    canonical_json(list(bucket_keys)),
                ),
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

        old = self.activity_pending_rows(scope_key)
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
        if len(old) == 1 and old[0]["payload_json"] == rows[0][-1] and old[0]["head"] == head:
            return
        self._session.executemany(
            "INSERT INTO read_item(scope_key,collection_key,item_id,sort_key,business_id,"
            "object_id,group_key,category,payload_json,component_key,head) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(scope_key,collection_key,item_id) "
            "DO UPDATE SET payload_json=excluded.payload_json,head=excluded.head",
            ((scope_key, *row, activity_index.COMPONENT, head) for row in rows),
        )
        self._work_changed(scope_key, activity_index.COMPONENT)

    def activity_advance_failed(self, scope_key, head):
        self._session.execute(
            "UPDATE read_scope_head SET wanted_head=?,revision=revision+1 WHERE scope_key=? "
            "AND component_key=? AND state='failed' AND wanted_head<?",
            (head, scope_key, activity_index.COMPONENT, head),
        )

    def open_mark(self, scope_key, head, business_ids=(), *, full=False):
        from ._collections import prepare_items

        previous = self.open_pending_rows(scope_key)
        full = full or any(row["item_id"] == "*" for row in previous)
        old = {row["item_id"]: row for row in previous}
        values = (
            [open_index.item(open_index.DIRTY, "*", {"full": True})]
            if full
            else [
                open_index.item(open_index.DIRTY, identity, {"business_id": identity})
                for identity in sorted(business_ids)
            ]
        )
        rows = prepare_items(values).rows
        if (
            full
            and len(previous) == 1
            and (previous[0]["payload_json"], previous[0]["head"]) == (rows[0][-1], head)
        ):
            return
        if full:
            self._session.execute(
                "DELETE FROM read_item WHERE scope_key=? AND component_key=? AND collection_key=?",
                (scope_key, open_index.COMPONENT, open_index.DIRTY),
            )
        if not full:
            rows = tuple(
                row
                for row in rows
                if row[1] not in old
                or (old[row[1]]["payload_json"], old[row[1]]["head"]) != (row[-1], head)
            )
        self._session.executemany(
            "INSERT INTO read_item(scope_key,collection_key,item_id,sort_key,business_id,"
            "object_id,group_key,category,payload_json,component_key,head) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(scope_key,collection_key,item_id) "
            "DO UPDATE SET payload_json=excluded.payload_json,head=excluded.head",
            ((scope_key, *row, open_index.COMPONENT, head) for row in rows),
        )
        if rows:
            self._work_changed(scope_key, open_index.COMPONENT)

    def _work_changed(self, scope_key, component):
        self._session.execute(
            "UPDATE read_scope_head SET revision=revision+1 WHERE scope_key=? AND component_key=?",
            (scope_key, component),
        )

    def open_advance_failed(self, scope_key, head):
        self._session.execute(
            "UPDATE read_scope_head SET wanted_head=?,revision=revision+1 WHERE scope_key=? "
            "AND component_key=? AND state='failed' AND wanted_head<?",
            (head, scope_key, open_index.COMPONENT, head),
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

    def item_counts(self, scope_key, component, collection, query, *, total=None):
        values = []
        previous = None
        for selected in (None, query):
            where, args = self._item_scope(scope_key, component, collection, selected)
            if total is not None and (
                selected is None
                or (where, args) == self._item_scope(scope_key, component, collection)
            ):
                values.append(total)
                previous = where, args
                continue
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
        expected_revision,
        expected_state="preparing",
        expected_format=None,
    ):
        return (
            self._session.execute(
                "UPDATE read_scope_head SET state='failed',error_code=?,revision=revision+1 "
                "WHERE scope_key=? AND component_key=? AND wanted_head=? AND state=? "
                "AND revision=? AND (? IS NULL OR format_version=?)",
                (
                    code,
                    scope_key,
                    component,
                    wanted_head,
                    expected_state,
                    expected_revision,
                    expected_format,
                    expected_format,
                ),
            )
            == 1
        )

    def retry(self, scope_key, component, *, format_version=None):
        self._session.execute(
            "UPDATE read_scope_head SET state='preparing',error_code=NULL,"
            "format_version=COALESCE(?,format_version),revision=revision+1 "
            "WHERE scope_key=? AND component_key=? AND "
            "(state='failed' OR (? IS NOT NULL AND format_version<>?))",
            (format_version, scope_key, component, format_version, format_version),
        )
