"""Only the current business identity and dependency projections live here."""

import json

from ai_accounting_modular.contracts.public import (
    ContractError,
    canonical_json,
    resource_components,
    resource_use_affects,
)

from ._relations import identity as relation_identity


class ApplicationRepository:
    def __init__(self, session):
        self._session = session

    def heads(self, ids):
        if not ids:
            return {}
        return {
            row["business_id"]: dict(row)
            for row in self._session.fetchall(
                "SELECT * FROM business_head WHERE business_id IN (SELECT value FROM json_each(?))",
                (canonical_json(list(ids)),),
            )
        }

    def latest_period(self):
        return self._session.fetchone("SELECT MAX(period) FROM business_head WHERE state='active'")[
            0
        ]

    def _search_filter(self, query, *, filtered=True):
        status = query.get("status", "active")
        period = "h.period" if status == "active" else "COALESCE(h.period,h.archive_month)"
        conditions = ["h.family=?", period + " BETWEEN ? AND ?"]
        parameters = [query["family"], query["period_from"], query["period_to"]]
        if status != "all":
            conditions.append("h.state=?")
            parameters.append(status)
        if filtered and query.get("kind"):
            conditions.append("h.kind=?")
            parameters.append(query["kind"])
        if filtered and query.get("object_id"):
            conditions.append(
                "h.state='active' AND EXISTS(SELECT 1 FROM current_dependency d "
                "WHERE d.consumer_business_id=h.business_id AND d.producer_kind='object' "
                "AND d.producer_id=?)"
            )
            parameters.append(query["object_id"])
        return " AND ".join(conditions), tuple(parameters)

    def search_summary(self, query):
        base, base_args = self._search_filter(query, filtered=False)
        filtered, args = self._search_filter(query)
        total = self._session.fetchone(
            "SELECT COUNT(*) FROM business_head h WHERE " + base, base_args
        )[0]

        row = self._session.fetchone(
            "SELECT COUNT(*),COALESCE(MAX(h.search_revision),0) FROM business_head h WHERE "
            + filtered,
            args,
        )
        return total, row[0], row[1]

    def snapshot_heads(self):
        """Complete identity projection for explicit offline integrity checks only."""
        return tuple(
            dict(row)
            for row in self._session.fetchall("SELECT * FROM business_head ORDER BY business_id")
        )

    def search_page(self, query, *, after=None):
        conditions, parameters = self._search_filter(query)
        if after is not None:
            conditions += " AND h.business_id>?"
            parameters += (after,)
        rows = self._session.fetchall(
            "SELECT h.business_id,h.kind,h.seq,h.state,h.current_digest,"
            "COALESCE(h.period,h.archive_month) AS search_period FROM business_head h WHERE "
            + conditions
            + " ORDER BY h.business_id LIMIT ?",
            (*parameters, query["limit"] + 1),
        )
        return [dict(row) for row in rows]

    def working_period_to(self):
        row = self._session.fetchone(
            "SELECT working_period_to FROM application_state WHERE singleton=1"
        )
        return row[0] if row is not None else None

    def advance_working_period(self, period, start_period):
        if period == start_period and self.working_period_to() is None:
            return
        self._session.execute(
            "INSERT INTO application_state VALUES(1,?) ON CONFLICT(singleton) DO UPDATE SET "
            "working_period_to=excluded.working_period_to "
            "WHERE excluded.working_period_to>application_state.working_period_to",
            (period,),
        )

    def update_natural_key(self, business_id, key):
        self._session.execute(
            "UPDATE business_head SET natural_key=? WHERE business_id=? AND natural_key IS NOT ?",
            (key, business_id, key),
        )

    def calendar_observers(self, before, after):
        return tuple(
            _dependency(row)
            for row in self._session.fetchall(
                "SELECT * FROM current_dependency "
                "WHERE producer_kind='calendar' AND producer_id='work' "
                "AND period_from<=? AND period_to>?",
                (after, before),
            )
        )

    def horizon_inputs(self, before, after):
        pending = {edge["consumer_business_id"] for edge in self.calendar_observers(before, after)}
        result = {}
        while pending:
            batch = sorted(pending - result.keys())
            if not batch:
                break
            heads = self.heads(batch)
            if set(heads) != set(batch):
                raise ContractError("storage_corrupt", "工作月份依赖缺少明确业务身份。")
            result.update(heads)
            pending = {
                edge["producer_id"]
                for edge in self.dependencies(batch)
                if edge["producer_kind"] == "business"
            }
        return [
            {key: head[key] for key in ("business_id", "kind", "seq", "state", "current_digest")}
            for _, head in sorted(result.items())
        ]

    def active_heads(self, family, period):
        return tuple(
            dict(row)
            for row in self._session.fetchall(
                "SELECT * FROM business_head WHERE state='active' AND family=? AND period=? "
                "ORDER BY business_id",
                (family, period),
            )
        )

    def scope_heads(self, kinds, period_from, period_to):
        return tuple(
            dict(row)
            for row in self._session.fetchall(
                "SELECT * FROM business_head WHERE state='active' AND kind IN "
                "(SELECT value FROM json_each(?)) AND period>=? AND period<=? ORDER BY business_id",
                (canonical_json(list(kinds)), period_from, period_to),
            )
        )

    def dependencies(self, business_ids, *, direct_only=False):
        if not business_ids:
            return ()
        return tuple(
            _dependency(row)
            for row in self._session.fetchall(
                "SELECT * FROM current_dependency WHERE consumer_business_id IN "
                "(SELECT value FROM json_each(?)) "
                + ("AND is_direct=1 " if direct_only else "")
                + "ORDER BY consumer_business_id,consumer_result_slot",
                (canonical_json(list(business_ids)),),
            )
        )

    def publications(self, business_ids):
        if not business_ids:
            return ()
        return tuple(
            dict(row)
            for row in self._session.fetchall(
                "SELECT * FROM current_publication WHERE business_id IN "
                "(SELECT value FROM json_each(?)) "
                "ORDER BY business_id,result_slot,collection",
                (canonical_json(list(business_ids)),),
            )
        )

    def aggregate_observers(self, publications):
        if not publications:
            return ()
        return tuple(
            _dependency(row)
            for row in self._session.fetchall(
                "SELECT DISTINCT d.* FROM json_each(?) p "
                "JOIN current_dependency d ON d.producer_kind='aggregate' "
                "AND d.producer_id=json_extract(p.value,'$.collection') "
                "AND d.read_mode='result' "
                "WHERE d.period_from<=json_extract(p.value,'$.period_to') "
                "AND d.period_to>=json_extract(p.value,'$.period_from') "
                "ORDER BY d.consumer_business_id",
                (canonical_json(list(publications)),),
            )
        )

    def replace_publications(self, business_ids, publications):
        fields = ("business_id", "result_slot", "collection", "period_from", "period_to")
        before = {tuple(row[field] for field in fields) for row in self.publications(business_ids)}
        after = {tuple(row[field] for field in fields) for row in publications}
        if before == after:
            return False
        self._session.execute(
            "DELETE FROM current_publication WHERE business_id IN (SELECT value FROM json_each(?))",
            (canonical_json(list(business_ids)),),
        )
        if after:
            self._session.executemany(
                "INSERT INTO current_publication VALUES(?,?,?,?,?)", sorted(after)
            )
        return True

    def resource_observers(self, uses):
        indexed = {}
        for edge in uses:
            if edge["read_mode"] == "use":
                key = edge["producer_id"], edge["producer_public_slot"], edge["relation_kind"]
                indexed.setdefault(key, []).append(edge)
        if not indexed:
            return ()
        rows = self._session.fetchall(
            "SELECT * FROM current_dependency WHERE producer_kind='business' "
            "AND read_mode='balance' AND producer_id IN (SELECT value FROM json_each(?))",
            (canonical_json(sorted({key[0] for key in indexed})),),
        )
        return tuple(
            balance
            for row in rows
            for balance in (_dependency(row),)
            if any(
                resource_use_affects(use, balance)
                for use in indexed.get(
                    (
                        balance["producer_id"],
                        balance["producer_public_slot"],
                        balance["relation_kind"],
                    ),
                    (),
                )
            )
        )

    def related_ids(
        self, producer_kind, producer_ids, relation_kind, *, period_from=None, period_to=None
    ):
        sql = (
            "SELECT DISTINCT d.consumer_business_id FROM current_dependency d "
            "JOIN business_head h ON h.business_id=d.consumer_business_id "
            "WHERE d.producer_kind=? AND d.producer_id IN (SELECT value FROM json_each(?)) "
            "AND d.relation_kind=? AND d.is_direct=1 AND h.state='active'"
        )
        parameters = [producer_kind, canonical_json(sorted(producer_ids)), relation_kind]
        for period, comparison in ((period_from, ">="), (period_to, "<=")):
            if period is not None:
                sql += " AND h.period" + comparison + "?"
                parameters.append(period)
        return tuple(
            row["consumer_business_id"]
            for row in self._session.fetchall(
                sql + " ORDER BY d.consumer_business_id", tuple(parameters)
            )
        )

    def observers(self, producer_kind, producer_id, relation_kind, periods):
        rows = self._session.fetchall(
            "SELECT * FROM current_dependency "
            "WHERE producer_kind=? AND producer_id=? AND relation_kind=? "
            "AND read_mode='collection'",
            (producer_kind, producer_id, relation_kind),
        )
        return tuple(
            _dependency(row)
            for row in rows
            if any(
                (row["period_from"] is None or period >= row["period_from"])
                and (row["period_to"] is None or period <= row["period_to"])
                for period in periods
            )
        )

    def scope_observers(self, kind, periods):
        rows = self._session.fetchall(
            "SELECT * FROM current_dependency "
            "WHERE producer_kind='scope' AND producer_id=? AND read_mode='collection'",
            (kind,),
        )
        return tuple(
            _dependency(row)
            for row in rows
            if any(row["period_from"] <= period <= row["period_to"] for period in periods)
        )

    def validate_natural_keys(self, proposed):
        """Check the final set, so swapping keys in one unit is valid."""
        keys = {}
        for business_id, kind, natural_key in proposed:
            if natural_key is None:
                continue
            key = kind, natural_key
            if key in keys and keys[key] != business_id:
                raise ContractError("conflict", "本单两笔业务占用了同一自然唯一身份。")
            keys[key] = business_id
        if not keys:
            return
        rows = self._session.fetchall(
            "SELECT h.business_id,h.kind,h.natural_key FROM json_each(?) k "
            "JOIN business_head h ON h.kind=json_extract(k.value,'$[0]') "
            "AND h.natural_key=json_extract(k.value,'$[1]') WHERE h.state='active' "
            "AND h.natural_key IS NOT NULL "
            "AND h.business_id NOT IN (SELECT value FROM json_each(?))",
            (
                canonical_json([list(key) for key in keys]),
                canonical_json([row[0] for row in proposed]),
            ),
        )
        if any((row["kind"], row["natural_key"]) in keys for row in rows):
            raise ContractError("conflict", "该自然唯一业务已经存在，请修改原业务。")

    def clear_natural_keys(self, business_ids):
        self._session.execute(
            "UPDATE business_head SET natural_key=NULL WHERE natural_key IS NOT NULL "
            "AND business_id IN (SELECT value FROM json_each(?))",
            (canonical_json(list(business_ids)),),
        )

    def save(self, row, *, complete_result_slots, search_revision):
        row = {
            **row,
            "result_slots_json": canonical_json(sorted(complete_result_slots)),
            "search_revision": search_revision,
        }
        columns = tuple(row)
        self._session.execute(
            "INSERT INTO business_head("
            + ",".join(columns)
            + ") VALUES("
            + ",".join("?" for _ in columns)
            + ") ON CONFLICT(business_id) DO UPDATE SET "
            + ",".join(f"{key}=excluded.{key}" for key in columns if key != "business_id"),
            tuple(row.values()),
        )

    def replace_result_slots(self, responsibilities):
        """Keep the successful owner's current complete commitment, including an empty one."""
        current = self.heads(responsibilities)
        if set(current) != set(responsibilities):
            raise ContractError("storage_corrupt", "完整结果责任缺少对应当前业务身份。")
        updates = []
        for business_id, item in responsibilities.items():
            encoded = canonical_json(sorted(item["complete_result_slots"]))
            if current[business_id]["result_slots_json"] != encoded:
                updates.append((encoded, business_id))
        if updates:
            self._session.executemany(
                "UPDATE business_head SET result_slots_json=? WHERE business_id=?", updates
            )
        return bool(updates)

    def consumers(self, producer_kind, producer_id, *, read_mode):
        return tuple(
            _dependency(row)
            for row in self._session.fetchall(
                "SELECT * FROM current_dependency WHERE producer_kind=? AND producer_id=? "
                "AND read_mode=? ORDER BY consumer_business_id,consumer_result_slot",
                (producer_kind, producer_id, read_mode),
            )
        )

    def result_consumers(self, slots):
        if not slots:
            return ()
        return tuple(
            _dependency(row)
            for row in self._session.fetchall(
                "SELECT DISTINCT d.* FROM json_each(?) p JOIN current_dependency d "
                "ON d.producer_kind='business' AND d.read_mode='result' "
                "AND d.producer_id=json_extract(p.value,'$[0]') "
                "AND d.producer_public_slot=json_extract(p.value,'$[1]') "
                "ORDER BY d.consumer_business_id,d.consumer_result_slot",
                (canonical_json([list(key) for key in slots]),),
            )
        )

    def replace_dependencies(self, business_ids, dependencies, *, relations, search_revision):
        def key(edge):
            return tuple(
                edge.get(name)
                for name in (
                    "consumer_business_id",
                    "consumer_result_slot",
                    "producer_kind",
                    "producer_id",
                    "producer_public_slot",
                    "read_mode",
                    "relation_kind",
                    "period_from",
                    "period_to",
                    "effective_on",
                )
            ) + (
                canonical_json(edge.get("excluded_result_slots", [])),
                canonical_json(list(resource_components(edge.get("resource_components")))),
                edge.get("notify_effect", "result"),
            )

        previous = self.dependencies(business_ids)
        old_direct_keys = {key(edge) for edge in self.dependencies(business_ids, direct_only=True)}
        new_direct = {relation_identity(edge) for edge in relations}

        def direct(edge):
            return (
                edge["producer_kind"] in {"business", "object"}
                and edge["read_mode"] in {"fact", "result"}
                and relation_identity(edge) in new_direct
            )

        if {(key(edge), key(edge) in old_direct_keys) for edge in previous} == {
            (key(edge), direct(edge)) for edge in dependencies
        }:
            return False

        def objects(edges):
            grouped = {business_id: set() for business_id in business_ids}
            for edge in edges:
                if edge["producer_kind"] == "object":
                    grouped[edge["consumer_business_id"]].add(edge["producer_id"])
            return grouped

        old_objects, new_objects = objects(previous), objects(dependencies)
        changed_objects = [key for key in business_ids if old_objects[key] != new_objects[key]]
        if changed_objects:
            self._session.execute(
                "UPDATE business_head SET search_revision=? WHERE business_id IN "
                "(SELECT value FROM json_each(?))",
                (search_revision, canonical_json(changed_objects)),
            )
        self._session.execute(
            "DELETE FROM current_dependency WHERE consumer_business_id IN "
            "(SELECT value FROM json_each(?))",
            (canonical_json(list(business_ids)),),
        )
        for edge in dependencies:
            self._session.execute(
                "INSERT INTO current_dependency(consumer_business_id,consumer_result_slot,"
                "producer_kind,producer_id,producer_public_slot,read_mode,relation_kind,"
                "period_from,period_to,effective_on,excluded_result_slots_json,"
                "resource_components_json,notify_effect,is_direct) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(
                    edge.get(key)
                    for key in (
                        "consumer_business_id",
                        "consumer_result_slot",
                        "producer_kind",
                        "producer_id",
                        "producer_public_slot",
                        "read_mode",
                        "relation_kind",
                        "period_from",
                        "period_to",
                        "effective_on",
                    )
                )
                + (
                    canonical_json(edge.get("excluded_result_slots", [])),
                    canonical_json(edge["resource_components"])
                    if "resource_components" in edge
                    else None,
                    edge.get("notify_effect"),
                    int(direct(edge)),
                ),
            )
        return True


def _dependency(row):
    value = dict(row)
    value.pop("is_direct")
    excluded = value.pop("excluded_result_slots_json")
    components = value.pop("resource_components_json")
    if components is not None:
        value["resource_components"] = list(resource_components(json.loads(components)))
    if excluded is not None and json.loads(excluded):
        value["excluded_result_slots"] = json.loads(excluded)
    return {key: item for key, item in value.items() if item is not None}
