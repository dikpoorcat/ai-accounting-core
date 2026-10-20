"""Only the current business identity and dependency projections live here."""

from ai_accounting_modular.contracts.public import ContractError, canonical_json


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
            row[0]
            for row in self._session.fetchall(
                "SELECT DISTINCT consumer_business_id FROM current_dependency "
                "WHERE producer_kind='calendar' AND producer_id='work' "
                "AND period_from<=? AND period_to>?",
                (after, before),
            )
        )

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

    def dependencies(self, business_ids):
        if not business_ids:
            return ()
        return tuple(
            dict(row)
            for row in self._session.fetchall(
                "SELECT * FROM current_dependency WHERE consumer_business_id IN "
                "(SELECT value FROM json_each(?)) ORDER BY consumer_business_id",
                (canonical_json(list(business_ids)),),
            )
        )

    def related_ids(self, producer_kind, producer_ids, relation_kind):
        return tuple(
            row["consumer_business_id"]
            for row in self._session.fetchall(
                "SELECT DISTINCT consumer_business_id FROM current_dependency "
                "WHERE producer_kind=? AND producer_id IN (SELECT value FROM json_each(?)) "
                "AND relation_kind=? "
                "AND read_mode!='collection' ORDER BY consumer_business_id",
                (producer_kind, canonical_json(sorted(producer_ids)), relation_kind),
            )
        )

    def observers(self, producer_kind, producer_id, relation_kind, periods):
        rows = self._session.fetchall(
            "SELECT consumer_business_id,period_from,period_to FROM current_dependency "
            "WHERE producer_kind=? AND producer_id=? AND relation_kind=? "
            "AND read_mode='collection'",
            (producer_kind, producer_id, relation_kind),
        )
        return {
            row["consumer_business_id"]
            for row in rows
            if any(
                (row["period_from"] is None or period >= row["period_from"])
                and (row["period_to"] is None or period <= row["period_to"])
                for period in periods
            )
        }

    def scope_observers(self, kind, periods):
        rows = self._session.fetchall(
            "SELECT consumer_business_id,period_from,period_to FROM current_dependency "
            "WHERE producer_kind='scope' AND producer_id=? AND read_mode='collection'",
            (kind,),
        )
        return {
            row["consumer_business_id"]
            for row in rows
            if any(row["period_from"] <= period <= row["period_to"] for period in periods)
        }

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

    def save(self, row):
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

    def consumers(self, producer_kind, producer_id):
        return tuple(
            row["consumer_business_id"]
            for row in self._session.fetchall(
                "SELECT DISTINCT consumer_business_id FROM current_dependency "
                "WHERE producer_kind=? AND producer_id=? ORDER BY consumer_business_id",
                (producer_kind, producer_id),
            )
        )

    def replace_dependencies(self, business_ids, dependencies):
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
                )
            )

        if {key(edge) for edge in self.dependencies(business_ids)} == {
            key(edge) for edge in dependencies
        }:
            return
        self._session.execute(
            "DELETE FROM current_dependency WHERE consumer_business_id IN "
            "(SELECT value FROM json_each(?))",
            (canonical_json(list(business_ids)),),
        )
        for edge in dependencies:
            self._session.execute(
                "INSERT INTO current_dependency VALUES(?,?,?,?,?,?,?,?,?)",
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
                    )
                ),
            )
