"""Only the current business identity and dependency projections live here."""

from ai_accounting_modular.contracts.public import canonical_json


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

    def active_heads(self, family, period):
        return tuple(
            dict(row)
            for row in self._session.fetchall(
                "SELECT * FROM business_head WHERE state='active' AND family=? AND period=? "
                "ORDER BY business_id",
                (family, period),
            )
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
