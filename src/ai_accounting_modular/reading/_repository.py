"""Only derived reading tables are visible to this repository."""

from ai_accounting_modular.contracts.public import canonical_json, content_digest


class ReadingRepository:
    def __init__(self, session):
        self._session = session

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

    def publish(self, scope_key, component, wanted_head, format_version, payload):
        state = self.state(scope_key, component)
        if state is None or (state["wanted_head"], state["format_version"]) != (
            wanted_head,
            format_version,
        ):
            return False
        self._session.execute(
            "INSERT INTO read_component VALUES(?,?,?,?) ON CONFLICT(scope_key,component_key) "
            "DO UPDATE SET head=excluded.head,payload_json=excluded.payload_json",
            (scope_key, component, wanted_head, canonical_json(payload)),
        )
        self._session.execute(
            "UPDATE read_scope_head SET ready_head=?,content_digest=?,"
            "state='ready',error_code=NULL "
            "WHERE scope_key=? AND component_key=?",
            (wanted_head, content_digest(payload), scope_key, component),
        )
        return True

    def fail(self, scope_key, component, wanted_head, code, *, expected_state="preparing"):
        self._session.execute(
            "UPDATE read_scope_head SET state='failed',error_code=? "
            "WHERE scope_key=? AND component_key=? AND wanted_head=? AND state=?",
            (code, scope_key, component, wanted_head, expected_state),
        )

    def retry(self, scope_key, component):
        self._session.execute(
            "UPDATE read_scope_head SET state='preparing',error_code=NULL "
            "WHERE scope_key=? AND component_key=? AND state='failed'",
            (scope_key, component),
        )
