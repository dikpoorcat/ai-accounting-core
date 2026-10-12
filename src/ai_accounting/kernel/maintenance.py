"""Authenticated, transactional repair of declared derived read structures."""

from .contracts import KernelError
from .read_state import advance_repair_revision, repair_revision
from .types import YearMonth, digest


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

    def rebuild_dashboard_read_model(self, period: YearMonth, *, request_id: str):
        """Explicitly register or repair one period without changing its books."""
        from .dashboard_model_integrity import repair_models
        from .dashboard_model_runtime import model_scope, supports_model
        from .dashboard_read_model import load_model
        from .integrity import verify_integrity

        period = str(YearMonth(period))
        from .dashboard_async import (
            _queue,
            delivery_state,
            reset_after_restore,
            sequence,
            supported,
        )
        with self.store.connection(read_only=True) as connection:
            asynchronous = supported(connection)
        if asynchronous:
            state = delivery_state(self.engine, period)

            def schedule(connection):
                reset = state["state"] == "failed" and state["reason"] == "corrupt"
                if reset:
                    reset_after_restore(connection, reason="verified_repair")
                    revision = advance_repair_revision(connection)
                else:
                    revision = repair_revision(connection)
                _queue(connection, period, sequence(connection), priority=100)
                return {"status": "pending", "period": period, "changed": reset,
                        "read_repair_revision": revision}

            return self.engine._write(
                request_id, digest(["rebuild_dashboard_read_model", period]), None, (),
                "dashboard_projection_repair", schedule,
            )

        def operation(connection):
            if not supports_model(connection):
                raise KernelError("dashboard_model_unavailable", "请先完成明确的前向结构调整")
            month = YearMonth(period).ordinal
            if connection.execute(
                "SELECT 1 FROM (SELECT v.period FROM voucher_version v JOIN voucher_current c "
                "ON c.version_id=v.id WHERE v.period=? UNION ALL "
                "SELECT period FROM period_close WHERE period=? UNION ALL "
                "SELECT f.period FROM fact_revision f JOIN fact_current c ON c.fact_id=f.id "
                "WHERE f.period=? UNION ALL SELECT period FROM material_revision WHERE period=?) "
                "LIMIT 1", (month, month, month, month),
            ).fetchone() is None:
                raise KernelError("dashboard_period_not_found", "没有找到所选会计月份")
            verify_integrity(self.engine, connection, include_dashboard_models=False)
            changed = repair_models(self.engine, connection)
            existing = load_model(connection, model_scope(self.engine, connection, period))
            changed = changed or existing is None
            revision = (
                advance_repair_revision(connection) if changed else repair_revision(connection)
            )
            return {"status": "rebuilt", "period": period, "changed": changed,
                    "read_repair_revision": revision}

        return self.engine._write(
            request_id, digest(["rebuild_dashboard_read_model", period]), None, (),
            "rebuild_dashboard_read_model", operation, _dashboard_periods=(period,),
        )

    def _repair(self, target, request_id):
        from .discovery_indexes import rebuild_discovery_indexes
        from .duplicate_freeze import compare_duplicate_freeze
        from .entity_references import rebuild_entity_references
        from .integrity import verify_integrity
        from .material_watch import repair_material_watch
        from .period_balance_freeze import compare_balance_freeze
        from .projections import repair_projections
        from .read_indexes import repair_read_indexes
        from .report_classification_directory import repair_classification_directory
        from .report_flow import repair_report_flow
        from .report_open_contribution import repair_open_contributions
        from .report_projection import repair_report_projection
        from .report_semantics import repair_report_semantics
        from .settlement_freeze import repair_frozen_settlement_projection
        from .settlement_projection import repair_settlement_projection
        from .versions import verify_schema

        def operation(connection):
            # A broken derived target must not prevent its own repair. Authority
            # is checked directly, independently of either derived directory.
            verify_integrity(
                self.engine, connection, include_projections=False, include_indexes=False,
                include_dashboard_models=False,
            )
            self.engine.fault("repair_verified", connection)
            fault = self.engine.fault
            # Frozen projection readers authenticate the close marker, and the
            # model producer consumes both directories and monetary projections.
            # Restore their complete derived basis before either reader runs.
            indexes = repair_read_indexes(
                connection, bundle=self.store.bundle, fault=fault
            )
            references_changed = rebuild_entity_references(
                connection, registry=self.store.registry
            )
            discovery_changed = rebuild_discovery_indexes(connection)
            result = repair_projections(connection, fault=fault)
            duplicates_changed = compare_duplicate_freeze(
                connection, self.engine, repair=True
            )
            materials_changed = repair_material_watch(self.engine, connection)
            result["changed"] = (
                result["changed"] or indexes["changed"] or references_changed
                or discovery_changed or duplicates_changed or materials_changed
            )
            settlements = repair_settlement_projection(self.engine, connection)
            result["changed"] = result["changed"] or settlements["changed"]
            result["settlements"] = settlements
            frozen_settlements = repair_frozen_settlement_projection(self.engine, connection)
            result["changed"] = result["changed"] or frozen_settlements["changed"]
            result["frozen_settlements"] = frozen_settlements
            reports = repair_report_projection(self.engine, connection)
            result["changed"] = result["changed"] or reports["changed"]
            result["reports"] = reports
            open_contributions = repair_open_contributions(self.engine, connection)
            result["changed"] = result["changed"] or bool(open_contributions)
            result["report_open_contributions"] = open_contributions
            report_semantics = repair_report_semantics(self.engine, connection, fault=fault)
            result["changed"] = result["changed"] or report_semantics["changed"]
            result["report_semantics"] = report_semantics
            report_flow = repair_report_flow(self.engine, connection)
            result["changed"] = result["changed"] or report_flow["changed"]
            result["report_flow"] = report_flow
            classifications = repair_classification_directory(
                self.engine, connection, _expected_flows=report_flow["expected_rows"]
            )
            result["changed"] = result["changed"] or classifications["changed"]
            result["report_classifications"] = classifications
            balance_freeze = compare_balance_freeze(connection, repair=True)
            result["changed"] = result["changed"] or balance_freeze["changed"]
            result["balance_freeze"] = balance_freeze
            self.engine.fault("repair_applied", connection)
            from .dashboard_model_integrity import repair_models

            result["changed"] = repair_models(self.engine, connection) or result["changed"]
            coverage = verify_integrity(self.engine, connection)
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
