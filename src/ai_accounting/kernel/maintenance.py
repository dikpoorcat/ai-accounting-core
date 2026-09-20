"""Authenticated, transactional repair of only the two declared derived models."""

from .contracts import KernelError
from .read_state import advance_repair_revision, repair_revision
from .types import digest


class Maintenance:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    def verify_integrity(self):
        from .integrity import verify_integrity

        with self.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            return verify_integrity(self.engine, connection) | {
                "read_repair_revision": repair_revision(connection)
            }

    def rebuild_projections(self, *, request_id: str):
        return self._repair("projections", request_id)

    def repair_read_indexes(self, *, request_id: str):
        return self._repair("read_indexes", request_id)

    def _repair(self, target, request_id):
        from .integrity import verify_integrity
        from .projections import repair_projections
        from .read_indexes import repair_read_indexes
        from .settlement_projection import repair_settlement_projection
        from .versions import verify_schema

        def operation(connection):
            # A broken derived target must not prevent its own repair. Authority
            # is checked directly, independently of either derived directory.
            verify_integrity(
                self.engine, connection, include_projections=False, include_indexes=False
            )
            self.engine.fault("repair_verified", connection)
            fault = self.engine.fault
            result = (
                repair_projections(connection, fault=fault)
                if target == "projections"
                else repair_read_indexes(connection, bundle=self.store.bundle, fault=fault)
            )
            if target == "projections":
                settlements = repair_settlement_projection(self.engine, connection)
                result["changed"] = result["changed"] or settlements["changed"]
                result["settlements"] = settlements
            self.engine.fault("repair_applied", connection)
            coverage = verify_integrity(
                self.engine,
                connection,
                include_projections=target == "projections",
                include_indexes=target == "read_indexes",
            )
            verify_schema(connection, bundle=self.store.bundle)
            if connection.execute("PRAGMA foreign_key_check").fetchone():
                raise KernelError("content_integrity_failed", "维修后引用核验未通过")
            revision = (
                advance_repair_revision(connection)
                if result["changed"]
                else repair_revision(connection)
            )
            return {
                "status": "rebuilt" if target == "projections" else "repaired",
                "changed": result["changed"],
                "read_repair_revision": revision,
                "verification": coverage,
            }

        action = "rebuild_projections" if target == "projections" else "repair_read_indexes"
        # Keep the existing rebuild request identity so successful old requests
        # replay instead of accidentally repairing again after an upgrade.
        hashed = digest(["rebuild"] if target == "projections" else ["repair_read_indexes"])
        return self.engine._write(request_id, hashed, None, (), action, operation)
