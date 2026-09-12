"""Append-only display metadata, separate from accounting facts and publications."""

from __future__ import annotations

import json
import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, WithJsonSchema, model_validator

from .contracts import KernelError, NeedsInformation
from .types import ActualDate, YearMonth, canonical, digest

CONTENT_CONTRACT = "commentary-content-v1"
LEGACY_CONTRACT = "legacy-context-v8"

DisplayDate = Annotated[
    YearMonth | ActualDate,
    WithJsonSchema(
        {
            "type": "string",
            "anyOf": [
                {"pattern": r"^[0-9]{4}-(0[1-9]|1[0-2])$"},
                {"pattern": r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"},
            ],
            "x-accounting-fact": {
                "role": "management",
                "meaning": "explicitly_provided_employment_date",
                "allowed_precision": ["month", "day"],
                "reusable_sources": ["owner_confirmation", "employment_document"],
            },
        }
    ),
]
ShortText = Annotated[str, Field(min_length=1, max_length=200)]
NoteText = Annotated[str, Field(max_length=50000)]
EvidenceDigest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class _Profile(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "x-accounting-fact": {
                "role": "management",
                "meaning": "explicit_display_information_only",
                "reusable_sources": ["evidence_digest", "owner_confirmation", "display_profiles"],
            }
        },
    )

    entity_id: ShortText
    display_name: ShortText | None = None
    display_number: ShortText | None = None
    purpose: NoteText | None = None
    note: NoteText | None = None
    source: Annotated[str, Field(min_length=1, max_length=2000)]
    evidence_digest: EvidenceDigest | None = None


class EmployeeProfile(_Profile):
    kind: Literal["employee"]
    employment_start: DisplayDate | None = None
    employment_end: DisplayDate | None = None
    employment_status: Literal["active", "inactive", "unknown"] = "unknown"

    @model_validator(mode="after")
    def ordered_employment(self):
        if self.employment_start and self.employment_end:
            start, end = self.employment_start, self.employment_end
            if start[:7] > end[:7] or (len(start) == len(end) == 10 and start > end):
                raise ValueError("employment end cannot precede employment start")
        return self


class CounterpartyProfile(_Profile):
    kind: Literal["counterparty"]


class FundAccountProfile(_Profile):
    kind: Literal["fund_account"]
    active: bool | None = None


class AssetProfile(_Profile):
    kind: Literal["asset"]
    category_label: ShortText | None = None
    rights_description: NoteText | None = None
    useful_life_basis: NoteText | None = None


class BusinessProfile(_Profile):
    kind: Literal["business"]
    counterparty_id: ShortText | None = None
    beneficiary_id: ShortText | None = None
    handler_id: ShortText | None = None


DisplayProfile = Annotated[
    EmployeeProfile | CounterpartyProfile | FundAccountProfile | AssetProfile | BusinessProfile,
    Field(discriminator="kind"),
]
_PROFILE_ADAPTER = TypeAdapter(DisplayProfile)
_KINDS = ("employee", "counterparty", "fund_account", "asset", "business")

DISPLAY_DDL = """
CREATE TABLE display_profile_revision(id TEXT PRIMARY KEY, kind TEXT NOT NULL
 CHECK(kind IN('employee','counterparty','fund_account','asset','business')),
 entity_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0),
 display_name TEXT,display_number TEXT,purpose TEXT,note TEXT,
 employment_start TEXT,employment_end TEXT,employment_status TEXT
 CHECK(employment_status IN('active','inactive','unknown')),
 active INTEGER CHECK(active IN(0,1)),category_label TEXT,rights_description TEXT,
 useful_life_basis TEXT,counterparty_id TEXT,beneficiary_id TEXT,handler_id TEXT,
 source TEXT NOT NULL,evidence_digest BLOB REFERENCES evidence(digest),
 digest BLOB NOT NULL CHECK(length(digest)=32),
 CHECK(kind='employee' OR (employment_start IS NULL AND employment_end IS NULL
 AND employment_status IS NULL)),CHECK(kind='fund_account' OR active IS NULL),
 CHECK(kind='asset' OR (category_label IS NULL AND rights_description IS NULL
 AND useful_life_basis IS NULL)),CHECK(kind='business' OR (counterparty_id IS NULL
 AND beneficiary_id IS NULL AND handler_id IS NULL)),UNIQUE(kind,entity_id,revision)) STRICT;
CREATE TABLE period_commentary_revision(id TEXT PRIMARY KEY,
 period INTEGER NOT NULL CHECK(period BETWEEN 0 AND 119987),
 revision INTEGER NOT NULL CHECK(revision>0),text TEXT NOT NULL,
 context_digest BLOB NOT NULL CHECK(length(context_digest)=32),
 close_digest BLOB CHECK(close_digest IS NULL OR length(close_digest)=32),
 source TEXT NOT NULL,evidence_digest BLOB REFERENCES evidence(digest),
 digest BLOB NOT NULL CHECK(length(digest)=32),UNIQUE(period,revision)) STRICT;
"""


class Display:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    @staticmethod
    def _revision(value):
        if type(value) is not int or value < 0:
            raise ValueError("expected_revision must be a nonnegative integer")

    @staticmethod
    def _evidence(connection, value):
        if value is None:
            return None
        raw = bytes.fromhex(value)
        if len(raw) != 32:
            raise ValueError("evidence digest must be 32 bytes")
        if connection.execute("SELECT 1 FROM evidence WHERE digest=?", (raw,)).fetchone() is None:
            raise NeedsInformation("evidence_digest", "展示资料引用的依据尚未登记")
        return raw

    @staticmethod
    def _record(row):
        if row is None:
            return None
        result = dict(row)
        if "active" in result and result["active"] is not None:
            result["active"] = bool(result["active"])
        for key in ("digest", "evidence_digest", "context_digest", "close_digest"):
            if key in result and result[key] is not None:
                result[key] = result[key].hex()
        if "period" in result:
            result["period"] = str(YearMonth.from_ordinal(result["period"]))
        if "close_digest" in result:
            result["supplementary"] = result["close_digest"] is not None
        return result

    @staticmethod
    def _closed(connection, period):
        return connection.execute(
            "SELECT manifest,digest FROM period_close WHERE period=?", (YearMonth(period).ordinal,)
        ).fetchone()

    @staticmethod
    def profiles(connection, period: str | None = None):
        """Use the caller's transaction; old closes never acquire newly entered profiles."""
        closed = Display._closed(connection, period) if period is not None else None
        if closed is not None:
            snapshot = json.loads(closed["manifest"]).get("management_snapshot", {})
            identifiers = [item["id"] for item in snapshot.get("profiles", [])]
            rows = connection.execute(
                "SELECT p.* FROM display_profile_revision p JOIN json_each(?) i ON p.id=i.value "
                "ORDER BY p.kind,p.entity_id",
                (json.dumps(identifiers),),
            )
        else:
            rows = connection.execute(
                "SELECT p.* FROM display_profile_revision p WHERE revision=(SELECT max(q.revision) "
                "FROM display_profile_revision q WHERE q.kind=p.kind AND q.entity_id=p.entity_id) "
                "ORDER BY p.kind,p.entity_id"
            )
        result = {kind: {} for kind in _KINDS}
        for row in rows:
            result[row["kind"]][row["entity_id"]] = Display._record(row)
        return result

    @staticmethod
    def _metadata_snapshot(connection, period, *, registry=None):
        if registry is None:
            from .service import default_registry

            registry = default_registry()
        kinds = sorted(
            kind for kind, model in registry.models.items() if model.lane == "management"
        )
        profiles = Display.profiles(connection)
        return {
            "typed_facts": [
                Display._record(row)
                for row in connection.execute(
                    "SELECT f.id,f.subject_id,s.kind,f.revision,f.digest FROM fact_current c "
                    "JOIN fact_revision f ON f.id=c.fact_id JOIN subject s ON s.id=f.subject_id "
                    "JOIN json_each(?) k ON k.value=s.kind WHERE f.period<=? ORDER BY s.kind,s.id",
                    (json.dumps(kinds), YearMonth(period).ordinal),
                )
            ],
            "profiles": [
                {key: item[key] for key in ("id", "kind", "entity_id", "revision", "digest")}
                for kind in _KINDS
                for item in profiles[kind].values()
            ],
            "management": [
                dict(row)
                for row in connection.execute(
                    "SELECT p.id,p.subject_id,p.revision FROM management_revision p "
                    "WHERE revision=(SELECT max(q.revision) FROM management_revision q "
                    "WHERE q.subject_id=p.subject_id) ORDER BY p.subject_id"
                )
            ],
            "payees": [
                dict(row)
                for row in connection.execute(
                    "SELECT p.id,p.party_id,p.revision FROM payee_revision p "
                    "WHERE revision=(SELECT max(q.revision) FROM payee_revision q "
                    "WHERE q.party_id=p.party_id) ORDER BY p.party_id"
                )
            ],
            "company_note": Display._record(
                connection.execute(
                    "SELECT id,revision,digest FROM company_note_revision "
                    "ORDER BY revision DESC LIMIT 1"
                ).fetchone()
            ),
        }

    @staticmethod
    def snapshot(connection, period: str, *, registry=None):
        result = Display._metadata_snapshot(connection, period, registry=registry)
        commentary = connection.execute(
            "SELECT * FROM period_commentary_revision "
            "WHERE period=? "
            "AND close_digest IS NULL ORDER BY revision DESC LIMIT 1",
            (YearMonth(period).ordinal,),
        ).fetchone()
        record = Display._record(commentary)
        latest = (
            {key: record[key] for key in ("id", "revision", "digest", "context_digest")}
            if record
            else None
        )
        # With no commentary there is no stored basis to validate. Avoid loading
        # accounting history merely to discard its digest for an empty record.
        validity = (
            Display._validity(
                connection,
                record,
                Display._context(connection, period, metadata=result, registry=registry),
                registry=registry,
            )
            if record
            else None
        )
        valid = validity is not None and validity["status"] == "current"
        if latest is not None:
            latest["content_validity"] = validity
        result["commentary"] = latest if valid else None
        result["commentary_latest"] = latest
        result["commentary_status"] = (
            "current" if valid else ("stale" if latest else "not_provided")
        )
        return {**result, "digest": digest(result).hex()}

    @staticmethod
    def _accounting_context(connection, period):
        """Summarize actual published journal versions; never synthesize missing business facts."""
        from .reports import PROFIT_ACCOUNTS

        month = YearMonth(period).ordinal
        selection = (
            "WITH selected(id) AS ("
            "SELECT json_extract(j.value,'$.id') FROM period_close p,"
            "json_each(p.manifest,'$.vouchers') j WHERE p.period<=? UNION "
            "SELECT v.id FROM voucher_current c JOIN voucher_version v ON v.id=c.version_id "
            "WHERE v.period<=? AND NOT EXISTS(SELECT 1 FROM period_close p "
            "WHERE p.period=v.period)) "
        )
        balances, movement, businesses = {}, {}, {}
        for row in connection.execute(
            selection + "SELECT v.period,l.account,l.debit,l.credit FROM selected s "
            "JOIN voucher_version v ON v.id=s.id JOIN voucher_line l ON l.version_id=v.id",
            (month, month),
        ):
            account = row["account"]
            amount = row["debit"] - row["credit"]
            balances[account] = balances.get(account, 0) + amount
            if row["period"] == month:
                movement[account] = movement.get(account, 0) + amount
        for row in connection.execute("SELECT * FROM opening_account WHERE period<=?", (month,)):
            balances[row["account"]] = (
                balances.get(row["account"], 0) + row["debit"] - row["credit"]
            )
        for row in connection.execute(
            selection + "SELECT c.kind,v.total,v.reverses_id FROM selected s "
            "JOIN voucher_version v ON v.id=s.id JOIN calculation c ON c.id=v.calculation_id "
            "WHERE v.period=? ORDER BY c.kind",
            (month, month, month),
        ):
            key = (row["kind"], row["reverses_id"] is not None)
            item = businesses.setdefault(
                key, {"kind": key[0], "reversal": key[1], "count": 0, "total_fen": 0}
            )
            item["count"] += 1
            item["total_fen"] += row["total"] * (-1 if key[1] else 1)
        revenue = -sum(
            value
            for account, value in movement.items()
            if PROFIT_ACCOUNTS.get(account, (0, 0))[1] == -1
        )
        expense = sum(
            value
            for account, value in movement.items()
            if PROFIT_ACCOUNTS.get(account, (0, 0))[1] == 1
        )
        return {
            "month_revenue_fen": str(revenue),
            "month_expense_fen": str(expense),
            "month_result_fen": str(revenue - expense),
            "funds": {
                key: str(balances.get(account, 0))
                for key, account in (
                    ("cash_fen", "1001"),
                    ("bank_fen", "1002"),
                    ("platform_fen", "1012"),
                )
            },
            "business_summary": [
                {**item, "total_fen": str(item["total_fen"])} for item in businesses.values()
            ],
            "detail_command": "dashboard_brief",
            "semantics": "已发布凭证按入账月份汇总；收入费用含冲正。空汇总不证明资料完整。",
        }

    @staticmethod
    def _legacy_context(connection, period, *, metadata=None, registry=None):
        """The v8 validation algorithm. Never redefine it using new content selectors."""
        closed = Display._closed(connection, period)
        identity = dict(
            connection.execute("SELECT company_id,database_id FROM identity WHERE id=1").fetchone()
        )
        close_digest = closed["digest"].hex() if closed else None
        if closed:
            basis = {"period": period, "close_digest": close_digest}
        else:
            state = connection.execute(
                "SELECT accounting,material FROM state WHERE id=1"
            ).fetchone()
            snapshot = (
                metadata
                if metadata is not None
                else Display._metadata_snapshot(connection, period, registry=registry)
            )
            basis = {
                "period": period,
                "accounting": state["accounting"],
                "material": state["material"],
                **{
                    key: snapshot[key]
                    for key in ("profiles", "management", "payees", "company_note", "typed_facts")
                },
            }
        basis["accounting_summary"] = Display._accounting_context(connection, period)
        basis["identity"] = identity
        return {"context_digest": digest(basis).hex(), "close_digest": close_digest, "basis": basis}

    @staticmethod
    def _content_basis(connection, period, *, metadata=None, registry=None):
        """Exact adopted content, independent of global write counters.

        References and full stored result digests suffice for tracing. Do not
        re-evaluate accounting or recursively copy historical outcome bodies.
        """
        from .materials import check_completeness
        from .payroll_tax_declarations import declaration_scope, disbursement_scope
        from .schema import table_name

        if registry is None:
            from .service import default_registry

            registry = default_registry()
        month = YearMonth(period).ordinal
        identity = dict(
            connection.execute("SELECT company_id,database_id FROM identity WHERE id=1").fetchone()
        )
        closes, calculations, fact_ids, vouchers = [], set(), set(), set()
        close_digest = None
        for row in connection.execute(
            "SELECT period,manifest,digest FROM period_close WHERE period<=? ORDER BY period",
            (month,),
        ):
            manifest = json.loads(row["manifest"])
            closes.append(
                {
                    "period": str(YearMonth.from_ordinal(row["period"])),
                    "digest": row["digest"].hex(),
                }
            )
            if row["period"] == month:
                close_digest = row["digest"].hex()
            calculations.update(manifest.get("calculations", ()))
            fact_ids.update(manifest.get("facts", ()))
            vouchers.update(item["id"] for item in manifest.get("vouchers", ()))
        calculations.update(
            row[0]
            for row in connection.execute(
                "SELECT c.id FROM calculation_current a "
                "JOIN calculation c ON c.id=a.calculation_id "
                "JOIN calculation_publication p ON p.calculation_id=c.id WHERE p.posting_period<=? "
                "AND NOT EXISTS(SELECT 1 FROM period_close z WHERE z.period=p.posting_period)",
                (month,),
            )
        )
        vouchers.update(
            row[0]
            for row in connection.execute(
                "SELECT v.id FROM voucher_current a JOIN voucher_version v ON v.id=a.version_id "
                "WHERE v.period<=? "
                "AND NOT EXISTS(SELECT 1 FROM period_close z WHERE z.period=v.period)",
                (month,),
            )
        )
        voucher_rows = [
            Display._record(row)
            for row in connection.execute(
                "SELECT v.*,n.number FROM voucher_version v JOIN voucher n ON n.id=v.voucher_id "
                "JOIN json_each(?) ids ON ids.value=v.id ORDER BY v.period,n.number,v.id",
                (canonical(sorted(vouchers)),),
            )
        ]
        calculations.update(row["calculation_id"] for row in voucher_rows)
        calculation_rows = [
            Display._record(row)
            for row in connection.execute(
                "SELECT c.id,c.subject_id,c.fact_id,c.kind,c.period,c.digest,p.posting_period "
                "FROM calculation c JOIN json_each(?) ids ON ids.value=c.id "
                "JOIN calculation_publication p ON p.calculation_id=c.id ORDER BY c.id",
                (canonical(sorted(calculations)),),
            )
        ]
        for row in calculation_rows:
            row["posting_period"] = str(YearMonth.from_ordinal(row["posting_period"]))
        fact_ids.update(row["fact_id"] for row in calculation_rows)
        dependencies = [
            dict(row)
            for row in connection.execute(
                "SELECT d.calculation_id,d.fact_id FROM dependency_fact d JOIN json_each(?) ids "
                "ON ids.value=d.calculation_id ORDER BY d.calculation_id,d.fact_id",
                (canonical(sorted(calculations)),),
            )
        ]
        fact_ids.update(row["fact_id"] for row in dependencies)
        upstream = [
            dict(row)
            for row in connection.execute(
                "SELECT d.calculation_id,d.upstream_id FROM dependency_calculation d "
                "JOIN json_each(?) ids ON ids.value=d.calculation_id "
                "ORDER BY d.calculation_id,d.upstream_id",
                (canonical(sorted(calculations)),),
            )
        ]
        kinds = sorted(kind for kind, model in registry.models.items() if model.lane != "material")
        current_facts = [
            Display._record(row)
            for row in connection.execute(
                "SELECT f.id,f.subject_id,s.kind,f.revision,f.digest FROM fact_current a "
                "JOIN fact_revision f ON f.id=a.fact_id JOIN subject s ON s.id=f.subject_id "
                "JOIN json_each(?) k ON k.value=s.kind WHERE f.period<=? ORDER BY s.kind,s.id",
                (canonical(kinds), month),
            )
        ]
        related_facts = fact_ids | {row["id"] for row in current_facts}
        scopes = set()
        for kind in ("payroll", "payroll_bounded"):
            if kind not in registry.models:
                continue
            for row in connection.execute(
                f"SELECT f.subject_id,f.period,p.employee_id FROM {table_name(kind)} p "
                "JOIN fact_revision f ON f.id=p.revision_id "
                "JOIN json_each(?) ids ON ids.value=f.id",
                (canonical(sorted(related_facts)),),
            ):
                scopes.add(
                    declaration_scope(row["employee_id"], YearMonth.from_ordinal(row["period"]))
                )
                scopes.add(disbursement_scope(kind, row["subject_id"]))
        supplementary = [
            Display._record(row)
            for row in connection.execute(
                "SELECT DISTINCT f.id,f.subject_id,s.kind,f.revision,f.digest FROM fact_scope x "
                "JOIN json_each(?) k ON k.value=x.scope_key "
                "JOIN fact_current a ON a.fact_id=x.fact_id "
                "JOIN fact_revision f ON f.id=a.fact_id JOIN subject s ON s.id=f.subject_id "
                "WHERE s.kind IN ('payroll_tax_declaration_actual','payroll_disbursement_basis') "
                "ORDER BY s.kind,f.subject_id",
                (canonical(sorted(scopes)),),
            )
        ]
        subjects = sorted(
            {row["subject_id"] for row in current_facts + supplementary + calculation_rows}
        )
        heads = [
            Display._record(row)
            for row in connection.execute(
                "SELECT ids.value AS subject_id,f.fact_id,c.id AS calculation_id,"
                "c.fact_id AS calculated_fact_id,"
                "c.digest FROM json_each(?) ids LEFT JOIN fact_current f ON f.subject_id=ids.value "
                "LEFT JOIN calculation_current a ON a.subject_id=ids.value "
                "LEFT JOIN calculation c ON c.id=a.calculation_id ORDER BY ids.value",
                (canonical(subjects),),
            )
        ]
        pending = [
            dict(row)
            for row in connection.execute(
                "SELECT p.subject_id,p.cause_id FROM pending p JOIN json_each(?) ids "
                "ON ids.value=p.subject_id ORDER BY p.subject_id,p.cause_id",
                (canonical(subjects),),
            )
        ]
        inventories = [
            Display._record(row)
            for row in connection.execute(
                "SELECT m.* FROM material_revision m WHERE m.period=? AND m.id=(SELECT max(n.id) "
                "FROM material_revision n WHERE n.period=m.period AND n.category=m.category) "
                "ORDER BY m.category",
                (month,),
            )
        ]
        items = [
            dict(inventory_id=row["inventory_id"], evidence_digest=row["evidence_digest"].hex())
            for row in connection.execute(
                "SELECT i.* FROM material_item i JOIN json_each(?) ids ON ids.value=i.inventory_id "
                "ORDER BY i.inventory_id,i.evidence_digest",
                (canonical([row["id"] for row in inventories]),),
            )
        ]
        coverage = check_completeness(connection, month, registry)
        accounting_summary = Display._accounting_context(connection, period)
        accounting_summary["business_summary"].sort(
            key=lambda item: (item["kind"], item["reversal"])
        )
        return {
            "contract": CONTENT_CONTRACT,
            "identity": identity,
            "period": period,
            "close_digest": close_digest,
            **(
                metadata
                if metadata is not None
                else Display._metadata_snapshot(connection, period, registry=registry)
            ),
            "accounting_summary": accounting_summary,
            "accounting_sources": {
                "closes": closes,
                "vouchers": voucher_rows,
                "calculations": calculation_rows,
                "facts": sorted(fact_ids),
                "dependencies": dependencies,
                "upstream": upstream,
            },
            "current_facts": current_facts,
            "current_heads": heads,
            "pending": pending,
            "supplementary_sources": supplementary,
            "materials": {
                "inventories": inventories,
                "items": items,
                "coverage_digest": coverage["coverage_digest"],
                "fact_ids": coverage["fact_ids"],
                "status": coverage["status"],
                "issues": coverage["issues"],
            },
        }

    @staticmethod
    def _context(connection, period, *, metadata=None, registry=None):
        basis = Display._content_basis(connection, period, metadata=metadata, registry=registry)
        content_digest = digest(basis).hex()
        epochs = dict(
            connection.execute(
                "SELECT accounting,material,management FROM state WHERE id=1"
            ).fetchone()
        )
        return {
            "context_digest": digest(["commentary-submit-v1", basis, epochs]).hex(),
            "content_contract": CONTENT_CONTRACT,
            "content_digest": content_digest,
            "content_basis": basis,
            "basis": basis,
            "close_digest": basis["close_digest"],
        }

    @staticmethod
    def _validity(
        connection, record, context, *, registry=None, legacy_context=None, frozen_digest=None
    ):
        row = connection.execute(
            "SELECT * FROM period_commentary_basis WHERE commentary_id=?", (record["id"],)
        ).fetchone()
        result = {"status": "unverifiable", "contract": row["contract"] if row else None}
        if row is None:
            return {**result, "reason": "content_basis_missing"}
        if frozen_digest is not None and frozen_digest != record["digest"]:
            return {**result, "reason": "frozen_commentary_mismatch"}
        if row["contract"] not in {LEGACY_CONTRACT, CONTENT_CONTRACT}:
            return {**result, "reason": "unsupported_content_contract"}
        # Both versions use the original commentary digest algorithm. A matching
        # context or manifest digest does not establish integrity of the body read.
        try:
            data = [
                record["period"],
                record["text"],
                record["context_digest"],
                record["source"],
                record["revision"] - 1,
                record["evidence_digest"],
            ]
            if digest([data, record["close_digest"]]).hex() != record["digest"]:
                return {**result, "reason": "commentary_digest_mismatch"}
        except (ValueError, TypeError, KeyError):
            return {**result, "reason": "commentary_digest_mismatch"}
        if row["contract"] == LEGACY_CONTRACT:
            if row["basis"] is not None or row["content_digest"] is not None:
                return {**result, "reason": "invalid_legacy_marker"}
            if frozen_digest is not None:
                return {**result, "status": "frozen", "method": "close_manifest"}
            legacy = (
                legacy_context
                if legacy_context is not None
                else Display._legacy_context(connection, record["period"], registry=registry)
            )
            return {
                **result,
                "status": "current"
                if record["context_digest"] == legacy["context_digest"]
                else "unverifiable",
                "method": "legacy_strict",
            }
        try:
            envelope = json.loads(row["basis"])
            content = envelope["content"]
            adoption = {
                "commentary_id": record["id"],
                "revision": record["revision"],
                "context_digest": record["context_digest"],
                "commentary_digest": record["digest"],
            }
            if (
                envelope["adoption"] != adoption
                or content["contract"] != CONTENT_CONTRACT
                or content["period"] != record["period"]
                or content["close_digest"] != record["close_digest"]
                or content["identity"] != context["basis"]["identity"]
                or digest(content) != row["content_digest"]
            ):
                return {**result, "reason": "content_basis_mismatch"}
        except (ValueError, TypeError, KeyError):
            return {**result, "reason": "invalid_content_basis"}
        if frozen_digest is not None:
            return {**result, "status": "frozen", "method": "close_manifest"}
        return {
            **result,
            "status": "current"
            if row["content_digest"].hex() == context["content_digest"]
            else "stale",
        }

    @staticmethod
    def commentary(connection, period: str, *, registry=None, include_context=False):
        period = str(YearMonth(period))
        closed = Display._closed(connection, period)
        rows = [
            Display._record(row)
            for row in connection.execute(
                "SELECT * FROM period_commentary_revision WHERE period=? ORDER BY revision",
                (YearMonth(period).ordinal,),
            )
        ]
        frozen_id = None
        frozen_digest = None
        if closed:
            snapshot = json.loads(closed["manifest"]).get("management_snapshot", {})
            frozen_id = (snapshot.get("commentary") or {}).get("id")
            frozen_digest = (snapshot.get("commentary") or {}).get("digest")
        frozen = next((item for item in rows if item["id"] == frozen_id), None)
        context = (
            Display._context(connection, period, registry=registry)
            if rows or include_context
            else {}
        )
        legacy = None
        if (
            rows
            and connection.execute(
                "SELECT 1 FROM period_commentary_basis b "
                "JOIN period_commentary_revision r ON r.id=b.commentary_id "
                "WHERE r.period=? AND b.contract=? LIMIT 1",
                (YearMonth(period).ordinal, LEGACY_CONTRACT),
            ).fetchone()
        ):
            legacy = Display._legacy_context(connection, period, registry=registry)
        for item in rows:
            item["content_validity"] = Display._validity(
                connection,
                item,
                context,
                registry=registry,
                legacy_context=legacy,
                frozen_digest=frozen_digest if item["id"] == frozen_id else None,
            )
        latest = rows[-1] if rows else None
        valid = latest is not None and latest["content_validity"]["status"] == "current"
        current = (
            (frozen if frozen and frozen["content_validity"]["status"] == "frozen" else None)
            if closed
            else (latest if valid else None)
        )
        return {
            "revision": rows[-1]["revision"] if rows else 0,
            "frozen": frozen,
            "current": current,
            "latest": latest,
            "status": "frozen"
            if closed and current
            else (
                "current"
                if current
                else "stale"
                if (frozen if closed else latest)
                else "not_provided"
            ),
            "supplements": [item for item in rows if item["supplementary"]],
            **context,
        }

    def save_display_profile(
        self, profile: DisplayProfile, *, expected_revision: int, request_id: str
    ):
        self._revision(expected_revision)
        data = _PROFILE_ADAPTER.validate_python(profile).model_dump(mode="json")

        def operation(connection):
            current = connection.execute(
                "SELECT coalesce(max(revision),0) FROM display_profile_revision "
                "WHERE kind=? AND entity_id=?",
                (data["kind"], data["entity_id"]),
            ).fetchone()[0]
            if current != expected_revision:
                raise KernelError("display_profile_conflict", "展示档案已变化", revision=current)
            evidence = self._evidence(connection, data["evidence_digest"])
            identifier = uuid.uuid4().hex
            fields = (
                "display_name",
                "display_number",
                "purpose",
                "note",
                "employment_start",
                "employment_end",
                "employment_status",
                "active",
                "category_label",
                "rights_description",
                "useful_life_basis",
                "counterparty_id",
                "beneficiary_id",
                "handler_id",
            )
            connection.execute(
                "INSERT INTO display_profile_revision VALUES("
                + ",".join("?" for _ in range(7 + len(fields)))
                + ")",
                (
                    identifier,
                    data["kind"],
                    data["entity_id"],
                    current + 1,
                    *(
                        int(data[field])
                        if field == "active" and data.get(field) is not None
                        else data.get(field)
                        for field in fields
                    ),
                    data["source"],
                    evidence,
                    digest(data),
                ),
            )
            row = connection.execute(
                "SELECT * FROM display_profile_revision WHERE id=?", (identifier,)
            ).fetchone()
            return {"status": "saved", **self._record(row)}

        return self.engine._write(
            request_id,
            digest(["display_profile", data, expected_revision]),
            None,
            ("management",),
            "save_display_profile",
            operation,
        )

    def preview_period_commentary(self, period: str):
        period = str(YearMonth(period))
        with self.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            return {
                "status": "preview",
                "period": period,
                **self.commentary(
                    connection, period, registry=self.store.registry, include_context=True
                ),
            }

    def display_profiles(self, *, period: str | None = None):
        if period is not None:
            period = str(YearMonth(period))
        with self.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            return {
                "period": period,
                "profiles": self.profiles(connection, period),
                "epochs": self.store.epochs(connection),
            }

    def update_period_commentary(
        self,
        period: str,
        text: str,
        *,
        context_digest: str,
        source: str,
        expected_revision: int,
        request_id: str,
        evidence_digest: str | None = None,
    ):
        period = str(YearMonth(period))
        self._revision(expected_revision)
        if not isinstance(text, str) or not text.strip() or len(text) > 50000:
            raise ValueError("commentary must be nonempty text of at most 50000 characters")
        if not isinstance(source, str) or not source.strip() or len(source) > 2000:
            raise ValueError("commentary source must be nonempty text of at most 2000 characters")
        data = [period, text, context_digest, source, expected_revision, evidence_digest]

        def operation(connection):
            current = self.commentary(
                connection, period, registry=self.store.registry, include_context=True
            )
            if current["revision"] != expected_revision:
                raise KernelError(
                    "period_commentary_conflict", "月度经营结论已变化", revision=current["revision"]
                )
            if current["context_digest"] != context_digest:
                raise KernelError("preview_expired", "经营结论所依据的资料已变化，请重新核对")
            evidence = self._evidence(connection, evidence_digest)
            identifier = uuid.uuid4().hex
            close_digest = current["close_digest"]
            record_digest = digest([data, close_digest])
            envelope = {
                "content": current["content_basis"],
                "adoption": {
                    "commentary_id": identifier,
                    "revision": expected_revision + 1,
                    "context_digest": context_digest,
                    "commentary_digest": record_digest.hex(),
                },
            }
            # The deferred FK and insert guards seal the association in this
            # transaction; existing commentary can never acquire a new basis.
            connection.execute(
                "INSERT INTO period_commentary_basis VALUES(?,?,?,?)",
                (
                    identifier,
                    CONTENT_CONTRACT,
                    canonical(envelope),
                    bytes.fromhex(current["content_digest"]),
                ),
            )
            connection.execute(
                "INSERT INTO period_commentary_revision VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    YearMonth(period).ordinal,
                    expected_revision + 1,
                    text,
                    bytes.fromhex(context_digest),
                    bytes.fromhex(close_digest) if close_digest else None,
                    source,
                    evidence,
                    record_digest,
                ),
            )
            row = connection.execute(
                "SELECT * FROM period_commentary_revision WHERE id=?", (identifier,)
            ).fetchone()
            return {"status": "saved", **self._record(row)}

        return self.engine._write(
            request_id,
            digest(["period_commentary", data]),
            None,
            ("management",),
            "update_period_commentary",
            operation,
        )
