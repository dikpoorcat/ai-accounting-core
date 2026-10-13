"""Discover bank-payment sources without requiring a complete export preview.

Amounts are saved payment rights and current unpaid balances, not guessed net
disbursements. Pending sources remain visible so the fourth workflow step can
finish their accounting before requesting the bank file.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Annotated

from pydantic import Field

from .contracts import KernelError
from .discovery import Discovery
from .exports import (
    DEFAULT_CATEGORIES,
    EXPORT_KINDS,
    _export_eligible,
    _export_obligations,
    resolve_export_values,
)
from .integrity import verify_sources
from .read_state import repair_revision
from .types import YearMonth, canonical, digest


class ExportSources:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    @staticmethod
    def _cursor_position(cursor, scope):
        if cursor is None:
            return None
        if not isinstance(cursor, str) or not cursor or len(cursor) > 2048:
            raise KernelError("export_cursor_invalid", "银行代发来源的分页位置无效，请重新读取首页")
        try:
            value = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
            if not isinstance(value, dict) or set(value) != {"version", "scope", "subject_id"}:
                raise ValueError("shape")
            if type(value["version"]) is not int or value["version"] != 1:
                raise ValueError("version")
            if not isinstance(value["subject_id"], str) or not value["subject_id"]:
                raise ValueError("position")
            if value["scope"] != scope:
                raise KernelError(
                    "export_cursor_stale",
                    "公司、归集月份或数据版本已变化，请重新读取银行代发来源首页",
                )
            return value["subject_id"]
        except KernelError:
            raise
        except (ValueError, TypeError, KeyError, UnicodeError, binascii.Error) as exc:
            raise KernelError(
                "export_cursor_invalid", "银行代发来源的分页位置无效，请重新读取首页"
            ) from exc

    @staticmethod
    def _seal_cursor(scope, subject_id):
        return base64.urlsafe_b64encode(
            canonical({"version": 1, "scope": scope, "subject_id": subject_id}).encode()
        ).decode()

    def query(
        self,
        period: str,
        *,
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
        cursor: str | None = None,
    ):
        """List all pages before confirming this payment scope; this is not a net-amount check."""
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("银行代发来源每页数量必须为 1 至 500")
        month = YearMonth(period)
        with self.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            epochs = self.store.epochs(connection)
            revision = repair_revision(connection)
            scope = digest(
                {
                    "contract": "export-sources/1",
                    "company_id": self.store.company_id,
                    "database_id": self.store.database_id,
                    "period": str(month),
                    "epochs": epochs,
                    "read_repair_revision": revision,
                }
            ).hex()
            position = self._cursor_position(cursor, scope)
            items = []
            batch_size = max(100, limit + 1)
            superseded = Discovery(self.engine)._superseded_subject("head")
            while len(items) <= limit:
                rows = connection.execute(
                    "SELECT head.subject_id,s.kind,f.id fact_id,f.revision,f.period current_period,"
                    "c.id calculation_id,c.subject_id calculation_subject,c.kind calculation_kind,"
                    "c.period calculation_period,c.fact_id adopted_fact_id,c.outcome,"
                    "a.subject_id adopted_subject,a.period adopted_period,"
                    "m.id management_id,m.payment_period,m.payment_category,"
                    "EXISTS(SELECT 1 FROM pending p WHERE p.subject_id=head.subject_id) pending,"
                    "cc.calculation_id stored_calculation_id "
                    "FROM fact_current head JOIN subject s ON s.id=head.subject_id "
                    "JOIN fact_revision f ON f.id=head.fact_id "
                    "LEFT JOIN calculation_current cc ON cc.subject_id=head.subject_id "
                    "LEFT JOIN calculation c ON c.id=cc.calculation_id "
                    "LEFT JOIN fact_revision a ON a.id=c.fact_id "
                    "LEFT JOIN management_revision m ON m.id=(SELECT n.id "
                    "FROM management_revision n "
                    "WHERE n.subject_id=head.subject_id ORDER BY n.revision DESC LIMIT 1) "
                    "WHERE s.kind IN(SELECT value FROM json_each(?)) "
                    "AND (coalesce(m.payment_period,a.period,f.period)=? "
                    "OR coalesce(m.payment_period,f.period)=?) "
                    "AND (? IS NULL OR head.subject_id>?) AND NOT "
                    + superseded
                    + " ORDER BY head.subject_id LIMIT ?",
                    (
                        canonical(EXPORT_KINDS),
                        month.ordinal,
                        month.ordinal,
                        position,
                        position,
                        batch_size,
                    ),
                ).fetchall()
                if not rows:
                    break
                items.extend(self._items(connection, rows, month.ordinal))
                if len(rows) < batch_size:
                    break
                position = rows[-1]["subject_id"]
            more = len(items) > limit
            selected = items[:limit]
            return {
                "schema_version": 1,
                "company_id": self.store.company_id,
                "database_id": self.store.database_id,
                "period": str(month),
                "epochs": epochs,
                "read_repair_revision": revision,
                "sort": "subject_id_asc",
                "items": selected,
                "next_cursor": self._seal_cursor(scope, selected[-1]["subject_id"])
                if more
                else None,
            }

    def _items(self, connection, rows, payment_period):
        current_ids = {row["fact_id"] for row in rows}
        adopted_ids = {row["adopted_fact_id"] for row in rows if row["adopted_fact_id"]}
        calculation_ids = {row["calculation_id"] for row in rows if row["calculation_id"]}
        # Pending is not an integrity failure. Authenticate the saved adoption,
        # while preserving the separate pending/unpublished discovery status.
        checked = verify_sources(
            self.engine,
            connection,
            fact_ids=current_ids | adopted_ids,
            calculation_ids=calculation_ids,
            _return_facts=True,
        )
        versions = (
            self.store.facts(connection, current_ids | adopted_ids)
            if getattr(self.store.registry, "content_version", None) == 1
            else self.store.fact_versions_from_checked(connection, checked)
        )
        adopted_values, bindings = resolve_export_values(
            self.engine,
            connection,
            [
                {
                    "id": row["calculation_id"],
                    "subject_id": row["subject_id"],
                    "kind": row["kind"],
                    "fact_id": row["adopted_fact_id"],
                    "outcome": row["outcome"],
                }
                for row in rows
                if row["calculation_id"] is not None
            ],
        )
        selected = {}
        for row in rows:
            current = versions[row["fact_id"]]
            if current.subject_id != row["subject_id"] or current.fact.kind != row["kind"]:
                raise KernelError("content_integrity_failed", "银行代发来源身份与当前事实不一致")
            adopted = versions.get(row["adopted_fact_id"])
            if row["stored_calculation_id"] is not None and (
                adopted is None
                or row["calculation_subject"] != row["subject_id"]
                or row["calculation_kind"] != row["kind"]
                or row["adopted_subject"] != row["subject_id"]
                or row["calculation_period"] != row["adopted_period"]
            ):
                raise KernelError(
                    "content_integrity_failed", "银行代发核算来源与正式采用事实不一致"
                )
            current_eligible = _export_eligible(row["kind"], current.fact)
            adopted_eligible = adopted is not None and _export_eligible(row["kind"], adopted.fact)
            if bindings.get(row["calculation_id"], {}).get("superseded"):
                continue
            if not current_eligible and not adopted_eligible:
                continue
            obligations = (
                _export_obligations(
                    row["kind"], adopted_values[row["calculation_id"]], adopted.fact
                )
                if adopted_eligible
                else None
            )
            selected[row["subject_id"]] = obligations
        keys = sorted({item["key"] for group in selected.values() if group for item in group})
        balances = {
            row["balance_key"]: row["amount"]
            for row in connection.execute(
                "SELECT balance_key,amount FROM balance WHERE category='payable' "
                "AND balance_key IN(SELECT value FROM json_each(?))",
                (canonical(keys),),
            )
        }
        result = []
        for row in rows:
            if row["subject_id"] not in selected:
                continue
            pending = bool(
                row["pending"]
                or (row["calculation_id"] and row["adopted_fact_id"] != row["fact_id"])
                or bindings.get(row["calculation_id"], {}).get("pending")
            )
            status = (
                "unpublished"
                if row["calculation_id"] is None
                else "pending"
                if pending
                else "published"
            )
            obligations = selected[row["subject_id"]]
            unpaid = None
            if obligations is not None:
                unpaid = []
                for item in obligations:
                    remaining = balances.get(item["key"], 0)
                    if remaining < 0 or remaining > item["amount_fen"]:
                        raise KernelError(
                            "invalid_payable_balance", "应付款余额与正式采用的付款义务不一致"
                        )
                    if remaining:
                        unpaid.append(
                            {
                                "name": item["name"],
                                "key": item["key"],
                                "party_id": item.get("counterparty_id"),
                                "amount_fen": item["amount_fen"],
                                "remaining_fen": remaining,
                            }
                        )
            if status == "published" and not unpaid:
                continue
            accounting_period = (
                row["adopted_period"] if row["calculation_id"] else row["current_period"]
            )
            result.append(
                {
                    "subject_id": row["subject_id"],
                    "kind": row["kind"],
                    "fact_id": row["fact_id"],
                    "revision": row["revision"],
                    "accounting_period": str(YearMonth.from_ordinal(accounting_period)),
                    "current_accounting_period": str(YearMonth.from_ordinal(row["current_period"])),
                    "payment_period": str(YearMonth.from_ordinal(payment_period)),
                    "payment_category": row["payment_category"] or DEFAULT_CATEGORIES[row["kind"]],
                    "management_revision_id": row["management_id"],
                    "calculation_id": row["calculation_id"],
                    "adopted_fact_id": row["adopted_fact_id"],
                    "opening_binding_calculation_id": bindings.get(row["calculation_id"], {}).get(
                        "calculation_id"
                    ),
                    "opening_binding_fact_id": bindings.get(row["calculation_id"], {}).get(
                        "fact_id"
                    ),
                    "current_opening_binding_fact_id": bindings.get(row["calculation_id"], {}).get(
                        "current_fact_id"
                    ),
                    "status": status,
                    "obligations": unpaid,
                }
            )
        return result
