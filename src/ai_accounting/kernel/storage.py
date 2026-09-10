"""Short-lived bound SQLite connections and typed fact persistence."""

from __future__ import annotations

import json
from contextlib import closing, contextmanager
from typing import get_origin

from pydantic import BaseModel

from .contracts import Calculation, FactVersion, KernelError, Read, Registry
from .runtime import connect, require_local_database
from .schema import VERSION, base_type, initialize, sequence_model, table_name
from .types import YearMonth, canonical


def composite(annotation):
    typ = base_type(annotation)
    return get_origin(typ) in (list, tuple, dict, set) or (
        isinstance(typ, type) and issubclass(typ, BaseModel)
    )


def encode_fields(model, data):
    result = {}
    for name, info in model.model_fields.items():
        if sequence_model(info.annotation):
            continue
        value = data[name]
        if value is not None:
            if base_type(info.annotation) is YearMonth:
                value = YearMonth(value).ordinal
            elif base_type(info.annotation) is bool:
                value = int(value)
            elif composite(info.annotation):
                value = canonical(value)
        result[name] = value
    return result


def decode_fields(model, data):
    for name, info in model.model_fields.items():
        if sequence_model(info.annotation):
            continue
        if data[name] is not None:
            if base_type(info.annotation) is YearMonth:
                data[name] = str(YearMonth.from_ordinal(data[name]))
            elif base_type(info.annotation) is bool:
                data[name] = bool(data[name])
            elif composite(info.annotation):
                data[name] = json.loads(data[name])
    return data


class Store:
    def __init__(self, path, registry: Registry, company_id: str, database_id: str):
        self.path = require_local_database(path)
        self.registry = registry
        self.company_id, self.database_id = company_id, database_id

    @classmethod
    def create(cls, path, registry, company_id, taxpayer_id, database_id):
        path = require_local_database(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive create prevents accidentally initializing an existing real company.
        with path.open("xb"):
            pass
        with closing(connect(path)) as connection:
            initialize(connection, registry, company_id, taxpayer_id, database_id)
        return cls(path, registry, company_id, database_id)

    @contextmanager
    def connection(self, *, read_only=False):
        if not self.path.is_file():
            raise KernelError("company_missing", "company database is missing")
        connection = connect(self.path, read_only=read_only)
        try:
            identity = connection.execute("SELECT * FROM identity WHERE id=1").fetchone()
            if (
                not identity
                or identity["company_id"] != self.company_id
                or identity["database_id"] != self.database_id
            ):
                raise KernelError("company_mismatch", "database does not match bound company")
            if identity["schema_version"] != VERSION:
                raise KernelError("schema_mismatch", "unsupported company database version")
            yield connection
        finally:
            if connection.in_transaction:
                connection.rollback()
            connection.close()

    def fact(self, connection, fact_id) -> FactVersion:
        row = connection.execute(
            "SELECT f.*,s.kind FROM fact_revision f JOIN subject s "
            "ON s.id=f.subject_id WHERE f.id=?",
            (fact_id,),
        ).fetchone()
        if row is None:
            raise KernelError("unknown_fact", "fact revision does not exist")
        model = self.registry.models[row["kind"]]
        data = dict(
            connection.execute(
                f"SELECT * FROM {table_name(row['kind'])} WHERE revision_id=?", (fact_id,)
            ).fetchone()
        )
        del data["revision_id"]
        decode_fields(model, data)
        for name, info in model.model_fields.items():
            item = sequence_model(info.annotation)
            if item is not None:
                rows = connection.execute(
                    f"SELECT * FROM {table_name(row['kind'])}_{name} "
                    "WHERE revision_id=? ORDER BY item_no",
                    (fact_id,),
                )
                data[name] = [
                    decode_fields(item, {k: r[k] for k in item.model_fields}) for r in rows
                ]
        evidence = tuple(
            r[0].hex()
            for r in connection.execute(
                "SELECT evidence_digest FROM fact_evidence WHERE "
                "fact_id=? ORDER BY evidence_digest",
                (fact_id,),
            )
        )
        return FactVersion(
            row["id"],
            row["subject_id"],
            row["revision"],
            model.model_validate_json(canonical(data)),
            evidence,
        )

    def current_fact(self, connection, subject_id):
        row = connection.execute(
            "SELECT fact_id FROM fact_current WHERE subject_id=?", (subject_id,)
        ).fetchone()
        if not row:
            raise KernelError("unknown_subject", f"unknown fact {subject_id}")
        return self.fact(connection, row[0])

    def write_fact(self, connection, version: FactVersion, hashed: bytes):
        fact = version.fact
        connection.execute(
            "INSERT INTO subject VALUES(?,?) ON CONFLICT(id) DO NOTHING",
            (version.subject_id, fact.kind),
        )
        connection.execute(
            "INSERT INTO fact_revision VALUES(?,?,?,?,?)",
            (version.id, version.subject_id, version.revision, fact.period.ordinal, hashed),
        )
        raw = fact.model_dump(mode="json")
        data = encode_fields(type(fact), raw)
        columns = ",".join(f'"{name}"' for name in data)
        connection.execute(
            f"INSERT INTO {table_name(fact.kind)}(revision_id,{columns}) "
            f"VALUES({','.join('?' for _ in range(len(data) + 1))})",
            (version.id, *data.values()),
        )
        for name, info in type(fact).model_fields.items():
            item = sequence_model(info.annotation)
            if item is None:
                continue
            for index, entry in enumerate(raw[name]):
                encoded = encode_fields(item, entry)
                fields = ",".join(f'"{key}"' for key in encoded)
                connection.execute(
                    f"INSERT INTO {table_name(fact.kind)}_{name}(revision_id,item_no,{fields}) "
                    f"VALUES({','.join('?' for _ in range(len(encoded) + 2))})",
                    (version.id, index, *encoded.values()),
                )
        connection.executemany(
            "INSERT INTO fact_scope VALUES(?,?,?)",
            [
                (version.id, fact.kind, key)
                for key in sorted(
                    set(fact.scopes())
                    | {"@" + version.subject_id, str(fact.period)}
                    | {claim.key for claim in fact.claims()}
                )
            ],
        )
        connection.executemany(
            "INSERT INTO fact_evidence VALUES(?,?)",
            [(version.id, bytes.fromhex(e)) for e in version.evidence],
        )
        connection.execute("INSERT INTO fact_seal VALUES(?)", (version.id,))
        connection.execute(
            "INSERT INTO fact_current VALUES(?,?) ON CONFLICT(subject_id) "
            "DO UPDATE SET fact_id=excluded.fact_id",
            (version.subject_id, version.id),
        )

    @staticmethod
    def calculation(row):
        return Calculation(
            row["id"],
            row["subject_id"],
            row["kind"],
            YearMonth.from_ordinal(row["period"]),
            json.loads(row["outcome"])["values"],
            row["fact_id"],
            row["digest"].hex(),
        )

    def select(self, connection, read: Read):
        limit = read.before_period.ordinal if read.before_period else 119988
        if read.key.startswith("#"):
            # Published versions are immutable and can be explicitly referenced
            # even after replacement. A reference never means "the latest".
            if read.source == "fact":
                row = connection.execute(
                    "SELECT f.id,s.kind FROM fact_revision f JOIN fact_seal z ON z.fact_id=f.id "
                    "JOIN subject s ON s.id=f.subject_id WHERE f.id=? AND f.period<?",
                    (read.key[1:], limit),
                ).fetchone()
                if row is None or read.kind not in ("*", row["kind"]):
                    return ()
                return (self.fact(connection, row["id"]),)
            row = connection.execute(
                "SELECT c.* FROM calculation c JOIN calculation_seal z ON z.calculation_id=c.id "
                "WHERE c.id=? AND c.period<?",
                (read.key[1:], limit),
            ).fetchone()
            if row is None or read.kind not in ("*", row["kind"]):
                return ()
            return (self.calculation(row),)
        if read.key == "*":
            if read.kind == "*":
                raise KernelError(
                    "unbounded_read", "whole-company wildcard reads are not supported"
                )
            if read.source == "fact":
                ids = connection.execute(
                    "SELECT f.id FROM subject s JOIN fact_current a ON a.subject_id=s.id "
                    "JOIN fact_revision f ON f.id=a.fact_id "
                    "WHERE s.kind=? AND f.period<? ORDER BY s.id",
                    (read.kind, limit),
                ).fetchall()
                return tuple(self.fact(connection, row[0]) for row in ids)
            rows = connection.execute(
                "SELECT c.* FROM calculation c JOIN calculation_current a "
                "ON a.calculation_id=c.id WHERE c.kind=? AND c.period<? "
                "ORDER BY c.period,c.subject_id",
                (read.kind, limit),
            )
            return tuple(self.calculation(row) for row in rows)
        if read.source == "fact":
            where = "s.scope_key=?" if read.kind == "*" else "s.kind=? AND s.scope_key=?"
            parameters = (read.key,) if read.kind == "*" else (read.kind, read.key)
            ids = connection.execute(
                "SELECT s.fact_id FROM fact_scope s JOIN fact_current c ON c.fact_id=s.fact_id "
                f"JOIN fact_revision f ON f.id=s.fact_id WHERE {where} "
                "AND f.period<? ORDER BY c.subject_id",
                (*parameters, read.before_period.ordinal if read.before_period else 119988),
            )
            return tuple(self.fact(connection, r[0]) for r in ids.fetchall())
        where = "s.scope_key=?" if read.kind == "*" else "s.kind=? AND s.scope_key=?"
        parameters = (read.key,) if read.kind == "*" else (read.kind, read.key)
        rows = connection.execute(
            "SELECT c.* FROM calculation_scope s JOIN calculation_current a "
            "ON a.calculation_id=s.calculation_id JOIN calculation c ON c.id=s.calculation_id "
            f"WHERE {where} AND c.period<? ORDER BY c.period,c.subject_id",
            (*parameters, read.before_period.ordinal if read.before_period else 119988),
        )
        return tuple(self.calculation(row) for row in rows)

    @staticmethod
    def epochs(connection):
        row = connection.execute(
            "SELECT accounting,material,management FROM state WHERE id=1"
        ).fetchone()
        return dict(row)
