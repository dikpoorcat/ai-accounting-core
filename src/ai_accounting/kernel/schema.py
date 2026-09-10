"""Version one of the NEW company format; unrelated to legacy Alembic trees."""

import re
import types
from typing import Annotated, Literal, Union, get_args, get_origin

from pydantic import BaseModel

from .contracts import Registry
from .types import YearMonth

VERSION = 1
DDL = """
CREATE TABLE identity(id INTEGER PRIMARY KEY CHECK(id=1), company_id TEXT NOT NULL,
 taxpayer_id TEXT NOT NULL, database_id TEXT NOT NULL, schema_version INTEGER NOT NULL) STRICT;
CREATE TABLE state(id INTEGER PRIMARY KEY CHECK(id=1), accounting INTEGER NOT NULL,
 material INTEGER NOT NULL, management INTEGER NOT NULL, next_number INTEGER NOT NULL) STRICT;
INSERT INTO state VALUES(1,0,0,0,1);
CREATE TABLE evidence(digest BLOB PRIMARY KEY CHECK(length(digest)=32), content BLOB NOT NULL,
 media_type TEXT NOT NULL, name TEXT NOT NULL) STRICT;
CREATE TABLE subject(id TEXT PRIMARY KEY, kind TEXT NOT NULL) STRICT;
CREATE INDEX subject_kind ON subject(kind,id);
CREATE TABLE fact_revision(id TEXT PRIMARY KEY REFERENCES fact_seal(fact_id)
 DEFERRABLE INITIALLY DEFERRED, subject_id TEXT NOT NULL REFERENCES subject,
 revision INTEGER NOT NULL CHECK(revision>0), period INTEGER NOT NULL CHECK(period BETWEEN 0
 AND 119987),
 digest BLOB NOT NULL CHECK(length(digest)=32), UNIQUE(subject_id,revision)) STRICT;
CREATE INDEX fact_period ON fact_revision(period,subject_id,id);
CREATE TABLE fact_seal(fact_id TEXT PRIMARY KEY REFERENCES fact_revision) STRICT;
CREATE TABLE fact_evidence(fact_id TEXT NOT NULL REFERENCES fact_revision,
 evidence_digest BLOB NOT NULL REFERENCES evidence(digest), PRIMARY
 KEY(fact_id,evidence_digest)) STRICT;
CREATE TABLE fact_current(subject_id TEXT PRIMARY KEY REFERENCES subject,
 fact_id TEXT NOT NULL UNIQUE REFERENCES fact_revision) STRICT;
CREATE TABLE fact_scope(fact_id TEXT NOT NULL REFERENCES fact_revision, kind TEXT NOT NULL,
 scope_key TEXT NOT NULL, PRIMARY KEY(fact_id,scope_key)) STRICT;
CREATE INDEX fact_scope_selection ON fact_scope(kind,scope_key,fact_id);
CREATE INDEX fact_scope_any_kind ON fact_scope(scope_key,kind,fact_id);
CREATE TABLE calculation(id TEXT PRIMARY KEY REFERENCES calculation_seal(calculation_id)
 DEFERRABLE INITIALLY DEFERRED, subject_id TEXT NOT NULL REFERENCES subject,
 fact_id TEXT NOT NULL REFERENCES fact_revision, kind TEXT NOT NULL,
 period INTEGER NOT NULL CHECK(period BETWEEN 0 AND 119987), outcome TEXT NOT NULL
 CHECK(json_valid(outcome)),
 digest BLOB NOT NULL CHECK(length(digest)=32), program_version TEXT NOT NULL) STRICT;
CREATE INDEX calculation_period ON calculation(period,id);
CREATE INDEX calculation_kind_period ON calculation(kind,period,id);
CREATE INDEX calculation_subject ON calculation(subject_id,id);
CREATE TABLE calculation_seal(calculation_id TEXT PRIMARY KEY REFERENCES calculation) STRICT;
CREATE TABLE calculation_current(subject_id TEXT PRIMARY KEY REFERENCES subject,
 calculation_id TEXT NOT NULL UNIQUE REFERENCES calculation) STRICT;
CREATE TABLE calculation_scope(calculation_id TEXT NOT NULL REFERENCES calculation,
 kind TEXT NOT NULL, scope_key TEXT NOT NULL, PRIMARY KEY(calculation_id,scope_key)) STRICT;
CREATE INDEX calculation_scope_selection ON calculation_scope(kind,scope_key,calculation_id);
CREATE INDEX calculation_scope_any_kind ON calculation_scope(scope_key,kind,calculation_id);
CREATE TABLE dependency_scope(calculation_id TEXT NOT NULL REFERENCES calculation,
 source TEXT NOT NULL CHECK(source IN ('fact','calculation')),kind TEXT NOT NULL,scope_key
 TEXT NOT NULL,
 before_period INTEGER NOT NULL DEFAULT 119988,
 PRIMARY KEY(calculation_id,source,kind,scope_key,before_period)) STRICT;
CREATE INDEX dependency_scope_lookup ON dependency_scope(source,kind,scope_key,calculation_id);
CREATE TABLE dependency_fact(calculation_id TEXT NOT NULL REFERENCES calculation,
 fact_id TEXT NOT NULL REFERENCES fact_revision,PRIMARY KEY(calculation_id,fact_id)) STRICT;
CREATE TABLE dependency_calculation(calculation_id TEXT NOT NULL REFERENCES calculation,
 upstream_id TEXT NOT NULL REFERENCES calculation,PRIMARY KEY(calculation_id,upstream_id)) STRICT;
CREATE INDEX dependency_upstream ON dependency_calculation(upstream_id,calculation_id);
CREATE TABLE pending(subject_id TEXT NOT NULL REFERENCES subject, cause_id TEXT NOT NULL
 REFERENCES fact_revision,
 PRIMARY KEY(subject_id,cause_id)) STRICT;
CREATE TABLE disposition(id INTEGER PRIMARY KEY, subject_id TEXT NOT NULL REFERENCES subject,
 cause_id TEXT NOT NULL REFERENCES fact_revision, action TEXT NOT NULL,
 calculation_id TEXT REFERENCES calculation, explanation TEXT NOT NULL) STRICT;
CREATE TABLE voucher(id TEXT PRIMARY KEY, number INTEGER NOT NULL UNIQUE CHECK(number>0)) STRICT;
CREATE TABLE voucher_line(version_id TEXT NOT NULL REFERENCES voucher_version(id)
 DEFERRABLE INITIALLY DEFERRED, line_no INTEGER NOT NULL CHECK(line_no>0), account TEXT NOT NULL,
 debit INTEGER NOT NULL CHECK(debit>=0),credit INTEGER NOT NULL CHECK(credit>=0), cashflow TEXT,
 CHECK((debit>0 AND credit=0) OR (credit>0 AND debit=0)),PRIMARY KEY(version_id,line_no)) STRICT;
CREATE TABLE voucher_version(id TEXT PRIMARY KEY,voucher_id TEXT NOT NULL REFERENCES voucher,
 calculation_id TEXT NOT NULL REFERENCES calculation,
 period INTEGER NOT NULL CHECK(period BETWEEN 0 AND 119987),
 reverses_id TEXT REFERENCES voucher_version, total INTEGER NOT NULL CHECK(total>0)) STRICT;
CREATE INDEX voucher_calculation ON voucher_version(calculation_id);
CREATE INDEX voucher_period ON voucher_version(period,voucher_id,id);
CREATE TABLE voucher_current(voucher_id TEXT PRIMARY KEY REFERENCES voucher,
 version_id TEXT NOT NULL UNIQUE REFERENCES voucher_version) STRICT;
CREATE TABLE calculation_publication(calculation_id TEXT PRIMARY KEY REFERENCES calculation,
 posting_period INTEGER NOT NULL CHECK(posting_period BETWEEN 0 AND 119987),
 voucher_id TEXT REFERENCES voucher) STRICT;
CREATE TABLE period_close(period INTEGER PRIMARY KEY CHECK(period BETWEEN 0 AND 119987),
 manifest TEXT NOT NULL CHECK(json_valid(manifest)),digest BLOB NOT NULL
 CHECK(length(digest)=32)) STRICT;
CREATE TABLE monthly_account(period INTEGER NOT NULL, account TEXT NOT NULL,
 debit INTEGER NOT NULL CHECK(debit>=0),credit INTEGER NOT NULL CHECK(credit>=0),
 PRIMARY KEY(period,account)) STRICT;
CREATE TABLE monthly_cashflow(period INTEGER NOT NULL,category TEXT NOT NULL,
 amount INTEGER NOT NULL,PRIMARY KEY(period,category)) STRICT;
CREATE TABLE balance(category TEXT NOT NULL,balance_key TEXT NOT NULL,amount INTEGER NOT NULL,
 PRIMARY KEY(category,balance_key)) STRICT;
CREATE TABLE management_revision(id INTEGER PRIMARY KEY,subject_id TEXT NOT NULL REFERENCES subject,
 revision INTEGER NOT NULL, note TEXT, payment_period INTEGER, payment_category TEXT,
 UNIQUE(subject_id,revision)) STRICT;
CREATE TABLE payee_revision(id TEXT PRIMARY KEY,party_id TEXT NOT NULL,
 revision INTEGER NOT NULL CHECK(revision>0),name TEXT NOT NULL,account TEXT NOT NULL,
 evidence_digest BLOB NOT NULL REFERENCES evidence(digest),UNIQUE(party_id,revision)) STRICT;
CREATE TABLE material_revision(id INTEGER PRIMARY KEY,period INTEGER NOT NULL,
 category TEXT NOT NULL,expected INTEGER NOT NULL CHECK(expected>=0),
 received INTEGER NOT NULL CHECK(received>=0),processed INTEGER NOT NULL CHECK(processed>=0),
 no_business INTEGER NOT NULL CHECK(no_business IN(0,1)), evidence_digest BLOB NOT NULL
 REFERENCES evidence,
 CHECK(processed<=received),CHECK(no_business=0 OR(expected=0 AND received=0))) STRICT;
CREATE INDEX material_period ON material_revision(period,category,id);
CREATE TABLE material_item(inventory_id INTEGER NOT NULL REFERENCES material_revision,
 evidence_digest BLOB NOT NULL REFERENCES evidence,PRIMARY KEY(inventory_id,evidence_digest))
 STRICT;
CREATE TABLE request(id TEXT PRIMARY KEY,digest BLOB NOT NULL CHECK(length(digest)=32),
 result TEXT NOT NULL CHECK(json_valid(result))) STRICT;
CREATE TABLE audit(id INTEGER PRIMARY KEY,request_id TEXT NOT NULL,action TEXT NOT NULL,
 payload TEXT NOT NULL CHECK(json_valid(payload)),created_at TEXT NOT NULL
 DEFAULT(strftime('%Y-%m-%dT%H:%M:%fZ','now'))) STRICT;
CREATE TABLE jobs(id TEXT PRIMARY KEY,kind TEXT NOT NULL,payload TEXT NOT NULL
 CHECK(json_valid(payload)),
 status TEXT NOT NULL CHECK(status IN('pending','running','succeeded','failed')),
 attempts INTEGER NOT NULL DEFAULT 0,last_error TEXT,result TEXT) STRICT;
CREATE TRIGGER frozen_job_payload BEFORE UPDATE OF id,kind,payload ON jobs
 BEGIN SELECT RAISE(ABORT,'immutable job payload'); END;
CREATE TRIGGER retained_job BEFORE DELETE ON jobs
 BEGIN SELECT RAISE(ABORT,'immutable job history'); END;
CREATE TRIGGER seal_voucher BEFORE INSERT ON voucher_version BEGIN
 SELECT CASE WHEN (SELECT count(*) FROM voucher_line WHERE version_id=NEW.id)<2
 OR (SELECT sum(debit) FROM voucher_line WHERE version_id=NEW.id)!=NEW.total
 OR (SELECT sum(credit) FROM voucher_line WHERE version_id=NEW.id)!=NEW.total
 THEN RAISE(ABORT,'unbalanced voucher') END;
END;
CREATE TRIGGER sealed_line BEFORE INSERT ON voucher_line WHEN EXISTS(
 SELECT 1 FROM voucher_version WHERE id=NEW.version_id)
 BEGIN SELECT RAISE(ABORT,'sealed voucher'); END;
CREATE TRIGGER current_voucher_insert BEFORE INSERT ON voucher_current BEGIN
 SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM voucher_version WHERE id=NEW.version_id
 AND voucher_id=NEW.voucher_id) THEN RAISE(ABORT,'voucher identity mismatch') END;
 SELECT CASE WHEN EXISTS(SELECT 1 FROM voucher_version v JOIN period_close p ON p.period>=v.period
 WHERE v.id=NEW.version_id) THEN RAISE(ABORT,'closed period') END;
END;
CREATE TRIGGER current_voucher_update BEFORE UPDATE ON voucher_current BEGIN
 SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM voucher_version WHERE id=NEW.version_id
 AND voucher_id=NEW.voucher_id) THEN RAISE(ABORT,'voucher identity mismatch') END;
 SELECT CASE WHEN EXISTS(SELECT 1 FROM voucher_version v JOIN period_close p ON p.period>=v.period
 WHERE v.id IN(OLD.version_id,NEW.version_id)) THEN RAISE(ABORT,'closed period') END;
END;
CREATE TRIGGER current_voucher_delete BEFORE DELETE ON voucher_current WHEN EXISTS(
 SELECT 1 FROM voucher_version v JOIN period_close p ON p.period>=v.period WHERE
 v.id=OLD.version_id)
 BEGIN SELECT RAISE(ABORT,'closed period'); END;
CREATE TRIGGER current_fact_insert BEFORE INSERT ON fact_current WHEN NOT EXISTS(
 SELECT 1 FROM fact_revision f JOIN fact_seal s ON s.fact_id=f.id
 WHERE f.id=NEW.fact_id AND f.subject_id=NEW.subject_id)
 BEGIN SELECT RAISE(ABORT,'fact identity mismatch'); END;
CREATE TRIGGER current_fact_update BEFORE UPDATE ON fact_current WHEN NOT EXISTS(
 SELECT 1 FROM fact_revision f JOIN fact_seal s ON s.fact_id=f.id
 WHERE f.id=NEW.fact_id AND f.subject_id=NEW.subject_id)
 BEGIN SELECT RAISE(ABORT,'fact identity mismatch'); END;
CREATE TRIGGER current_calc_insert BEFORE INSERT ON calculation_current WHEN NOT EXISTS(
 SELECT 1 FROM calculation c JOIN calculation_seal s ON s.calculation_id=c.id
 WHERE c.id=NEW.calculation_id AND c.subject_id=NEW.subject_id)
 BEGIN SELECT RAISE(ABORT,'calculation identity mismatch'); END;
CREATE TRIGGER current_calc_update BEFORE UPDATE ON calculation_current WHEN NOT EXISTS(
 SELECT 1 FROM calculation c JOIN calculation_seal s ON s.calculation_id=c.id
 WHERE c.id=NEW.calculation_id AND c.subject_id=NEW.subject_id)
 BEGIN SELECT RAISE(ABORT,'calculation identity mismatch'); END;
"""

IMMUTABLE = (
    "identity",
    "evidence",
    "subject",
    "fact_revision",
    "fact_seal",
    "fact_evidence",
    "fact_scope",
    "calculation",
    "calculation_seal",
    "calculation_publication",
    "calculation_scope",
    "dependency_scope",
    "dependency_fact",
    "dependency_calculation",
    "disposition",
    "voucher",
    "voucher_line",
    "voucher_version",
    "period_close",
    "management_revision",
    "payee_revision",
    "material_revision",
    "material_item",
    "request",
    "audit",
)


def immutable_sql(table: str) -> str:
    return "\n".join(
        f"CREATE TRIGGER immutable_{table}_{op} BEFORE {op} ON {table} "
        f"BEGIN SELECT RAISE(ABORT,'immutable {table}'); END;"
        for op in ("UPDATE", "DELETE")
    )


def sealed_child_sql(table: str, column: str, seal: str, seal_column: str) -> str:
    return (
        f"CREATE TRIGGER sealed_{table}_insert BEFORE INSERT ON {table} WHEN EXISTS("
        f"SELECT 1 FROM {seal} WHERE {seal_column}=NEW.{column}) "
        "BEGIN SELECT RAISE(ABORT,'sealed revision'); END;"
    )


def ownership_sql(registry: Registry) -> str:
    """Protect generic identity/type links; business rules remain in Python."""
    root_checks = " ".join(
        f"WHEN '{kind}' THEN EXISTS(SELECT 1 FROM {table_name(kind)} t WHERE t.revision_id=f.id)"
        for kind in registry.models
    )
    shape = f"CASE s.kind {root_checks} ELSE 0 END" if root_checks else "0"
    return f"""
CREATE TRIGGER fact_seal_shape BEFORE INSERT ON fact_seal WHEN NOT EXISTS(
 SELECT 1 FROM fact_revision f JOIN subject s ON s.id=f.subject_id
 WHERE f.id=NEW.fact_id AND ({shape}))
 BEGIN SELECT RAISE(ABORT,'missing typed fact'); END;
CREATE TRIGGER calculation_fact_owner BEFORE INSERT ON calculation WHEN NOT EXISTS(
 SELECT 1 FROM fact_revision f JOIN subject s ON s.id=f.subject_id JOIN fact_seal z ON
 z.fact_id=f.id
 WHERE f.id=NEW.fact_id AND f.subject_id=NEW.subject_id AND s.kind=NEW.kind AND f.period=NEW.period)
 BEGIN SELECT RAISE(ABORT,'calculation fact ownership mismatch'); END;
CREATE TRIGGER fact_scope_owner BEFORE INSERT ON fact_scope WHEN NOT EXISTS(
 SELECT 1 FROM fact_revision f JOIN subject s ON s.id=f.subject_id
 WHERE f.id=NEW.fact_id AND s.kind=NEW.kind)
 BEGIN SELECT RAISE(ABORT,'fact scope ownership mismatch'); END;
CREATE TRIGGER calculation_scope_owner BEFORE INSERT ON calculation_scope WHEN NOT EXISTS(
 SELECT 1 FROM calculation c WHERE c.id=NEW.calculation_id AND c.kind=NEW.kind)
 BEGIN SELECT RAISE(ABORT,'calculation scope ownership mismatch'); END;
"""


def base_type(annotation):
    if get_origin(annotation) is Annotated:
        return base_type(get_args(annotation)[0])
    if get_origin(annotation) in (types.UnionType, Union):
        args = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(args) == 1:
            return base_type(args[0])
    if get_origin(annotation) is Literal:
        value_types = {type(value) for value in get_args(annotation)}
        if len(value_types) == 1:
            return next(iter(value_types))
    return annotation


def table_name(kind: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_]*", kind):
        raise ValueError("fact kind must be a safe SQL identifier")
    return "fact_" + kind


def sequence_model(annotation):
    typ = base_type(annotation)
    if get_origin(typ) in (tuple, list):
        item = base_type(get_args(typ)[0])
        if isinstance(item, type) and issubclass(item, BaseModel):
            return item
    return None


def fact_columns(model):
    columns = []
    for name, info in model.model_fields.items():
        if sequence_model(info.annotation):
            continue
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name) or name in {"revision_id", "item_no"}:
            raise ValueError("invalid fact field")
        typ = base_type(info.annotation)
        storage = "INTEGER" if typ in (int, bool, YearMonth) else "TEXT"
        required = " NOT NULL" if type(None) not in get_args(info.annotation) else ""
        constraint = ""
        if typ is YearMonth:
            constraint = f' CHECK("{name}" BETWEEN 0 AND 119987)'
        if typ is bool:
            constraint = f' CHECK("{name}" IN(0,1))'
        columns.append(f'"{name}" {storage}{required}{constraint}')
    return columns


def fact_ddl(registry: Registry) -> str:
    statements = []
    for kind, model in registry.models.items():
        columns = [
            "revision_id TEXT PRIMARY KEY REFERENCES fact_revision(id)",
            *fact_columns(model),
        ]
        table = table_name(kind)
        statements.append(f"CREATE TABLE {table}({','.join(columns)}) STRICT;")
        statements.append(immutable_sql(table))
        statements.append(sealed_child_sql(table, "revision_id", "fact_seal", "fact_id"))
        statements.append(
            f"CREATE TRIGGER owner_{table} BEFORE INSERT ON {table} WHEN NOT EXISTS("
            "SELECT 1 FROM fact_revision f JOIN subject s ON s.id=f.subject_id "
            f"WHERE f.id=NEW.revision_id AND s.kind='{kind}' AND f.period=NEW.period) "
            "BEGIN SELECT RAISE(ABORT,'typed fact ownership mismatch'); END;"
        )
        for name, info in model.model_fields.items():
            item = sequence_model(info.annotation)
            if item is None:
                continue
            if any(sequence_model(field.annotation) for field in item.model_fields.values()):
                raise ValueError(
                    "nested repeated fact rows need a separately identified typed fact"
                )
            child = f"{table}_{name}"
            columns = [
                f"revision_id TEXT NOT NULL REFERENCES {table}(revision_id)",
                "item_no INTEGER NOT NULL CHECK(item_no>=0)",
                *fact_columns(item),
                "PRIMARY KEY(revision_id,item_no)",
            ]
            statements.append(f"CREATE TABLE {child}({','.join(columns)}) STRICT;")
            statements.append(immutable_sql(child))
            statements.append(sealed_child_sql(child, "revision_id", "fact_seal", "fact_id"))
    return "\n".join(statements)


def initialize(connection, registry: Registry, company_id: str, taxpayer_id: str, database_id: str):
    if connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
        raise ValueError("new company database must be empty")
    script = DDL + "\n" + "\n".join(immutable_sql(t) for t in IMMUTABLE) + fact_ddl(registry)
    script += ownership_sql(registry)
    script += "\n".join(
        sealed_child_sql(t, "fact_id", "fact_seal", "fact_id")
        for t in ("fact_evidence", "fact_scope")
    )
    script += "\n".join(
        sealed_child_sql(t, "calculation_id", "calculation_seal", "calculation_id")
        for t in (
            "calculation_publication",
            "calculation_scope",
            "dependency_scope",
            "dependency_fact",
            "dependency_calculation",
        )
    )
    try:
        connection.executescript("BEGIN IMMEDIATE;\n" + script)
        connection.execute(
            "INSERT INTO identity VALUES(1,?,?,?,?)",
            (company_id, taxpayer_id, database_id, VERSION),
        )
        connection.execute(f"PRAGMA user_version={VERSION}")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
