"""Short-lived bound SQLite connections and typed fact persistence."""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import get_origin

from pydantic import BaseModel

from .contracts import Calculation, FactVersion, KernelError, Read
from .dependencies import NO_PERIOD_LIMIT, scope_keys, validate_read
from .runtime import connect, initialize_file, require_local_database
from .schema import base_type, initialize, sequence_model, table_name
from .types import YearMonth, canonical
from .versions import verify_schema


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
    def __init__(self, path, bundle, company_id: str, database_id: str):
        self.path = require_local_database(path)
        self.bundle = bundle
        self.registry = bundle.registry
        self.company_id, self.database_id = company_id, database_id

    @staticmethod
    def evidence_metadata(connection, digests):
        """Read names from this company's immutable evidence without loading file bodies."""
        result = []
        for row in connection.execute(
            "SELECT e.digest,e.name,e.media_type FROM json_each(?) j "
            "JOIN evidence e ON e.digest=unhex(j.value) ORDER BY e.name,e.digest",
            (json.dumps(sorted(set(digests))),),
        ):
            identity = row["digest"].hex()
            name = row["name"].replace("\\", "/").rsplit("/", 1)[-1]
            result.append(
                {
                    "digest": identity,
                    "name": "" if name.lower() == identity else name,
                    "media_type": row["media_type"],
                }
            )
        return sorted(result, key=lambda item: (item["name"], item["digest"]))

    @classmethod
    def create(cls, path, bundle, company_id, taxpayer_id, database_id):
        path = require_local_database(path)

        def validate(connection):
            verify_schema(connection, bundle=bundle)
            identity = connection.execute("SELECT * FROM identity WHERE id=1").fetchone()
            if tuple(identity) != (1, company_id, taxpayer_id, database_id):
                raise KernelError("company_mismatch", "新建数据库身份不匹配")

        initialize_file(
            path,
            lambda conn: initialize(conn, bundle, company_id, taxpayer_id, database_id),
            validate,
        )
        return cls(path, bundle, company_id, database_id)

    @contextmanager
    def connection(self, *, read_only=False):
        if not self.path.is_file():
            raise KernelError("company_missing", "company database is missing")

        def validate(connection):
            verify_schema(connection, bundle=self.bundle)
            identity = connection.execute("SELECT * FROM identity WHERE id=1").fetchone()
            if (
                not identity
                or identity["company_id"] != self.company_id
                or identity["database_id"] != self.database_id
            ):
                raise KernelError("company_mismatch", "database does not match bound company")

        connection = connect(self.path, read_only=read_only, validator=validate)
        try:
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
        data = Store._fact_data(connection, fact_id, model, row["kind"])
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

    def fact_data(self, connection, fact_id) -> dict:
        """Decode the exact stored fact shape without applying today's model defaults."""
        row = connection.execute(
            "SELECT s.kind FROM fact_revision f JOIN subject s ON s.id=f.subject_id WHERE f.id=?",
            (fact_id,),
        ).fetchone()
        if row is None:
            raise KernelError("unknown_fact", "fact revision does not exist")
        model = self.registry.models[row["kind"]]
        return Store._fact_data(connection, fact_id, model, row["kind"])

    def fact_data_many(self, connection, fact_ids) -> dict[str, dict]:
        """Batch raw fact decoding without applying current model validation or defaults."""
        identifiers = sorted(set(fact_ids))
        if not identifiers:
            return {}
        rows = list(
            connection.execute(
                "SELECT f.id,s.kind FROM json_each(?) ids JOIN fact_revision f "
                "ON f.id=ids.value JOIN subject s ON s.id=f.subject_id",
                (canonical(identifiers),),
            )
        )
        if len(rows) != len(identifiers):
            raise KernelError("unknown_fact", "fact revision does not exist")
        by_kind = {}
        for row in rows:
            by_kind.setdefault(row["kind"], []).append(row["id"])
        result = {}
        for kind, revision_ids in by_kind.items():
            model = self.registry.models[kind]
            keys = canonical(revision_ids)
            for row in connection.execute(
                f"SELECT f.* FROM json_each(?) ids JOIN {table_name(kind)} f "
                "ON f.revision_id=ids.value",
                (keys,),
            ):
                values = dict(row)
                ident = values.pop("revision_id")
                result[ident] = decode_fields(model, values)
            for name, info in model.model_fields.items():
                item = sequence_model(info.annotation)
                if item is None:
                    continue
                for ident in revision_ids:
                    result[ident][name] = []
                for row in connection.execute(
                    f"SELECT f.* FROM json_each(?) ids JOIN {table_name(kind)}_{name} f "
                    "ON f.revision_id=ids.value ORDER BY f.revision_id,f.item_no",
                    (keys,),
                ):
                    result[row["revision_id"]][name].append(
                        decode_fields(item, {key: row[key] for key in item.model_fields})
                    )
        if len(result) != len(identifiers):
            raise KernelError("unknown_fact", "typed fact data does not exist")
        return result

    @staticmethod
    def _fact_data(connection, fact_id, model, kind) -> dict:
        row = connection.execute(
            f"SELECT * FROM {table_name(kind)} WHERE revision_id=?", (fact_id,)
        ).fetchone()
        if row is None:
            raise KernelError("unknown_fact", "typed fact data does not exist")
        data = dict(row)
        del data["revision_id"]
        decode_fields(model, data)
        for name, info in model.model_fields.items():
            item = sequence_model(info.annotation)
            if item is not None:
                rows = connection.execute(
                    f"SELECT * FROM {table_name(kind)}_{name} WHERE revision_id=? ORDER BY item_no",
                    (fact_id,),
                )
                data[name] = [
                    decode_fields(item, {k: r[k] for k in item.model_fields}) for r in rows
                ]
        return data

    def facts(self, connection, fact_ids) -> dict[str, FactVersion]:
        """Load exact revisions in batches, including each kind's typed child tables."""
        identifiers = sorted(set(fact_ids))
        if not identifiers:
            return {}
        rows = list(
            connection.execute(
                "SELECT f.*,s.kind FROM json_each(?) ids JOIN fact_revision f ON f.id=ids.value "
                "JOIN subject s ON s.id=f.subject_id",
                (canonical(identifiers),),
            )
        )
        if len(rows) != len(identifiers):
            raise KernelError("unknown_fact", "fact revision does not exist")
        evidence = {ident: [] for ident in identifiers}
        for row in connection.execute(
            "SELECT e.fact_id,e.evidence_digest FROM json_each(?) ids "
            "JOIN fact_evidence e ON e.fact_id=ids.value ORDER BY e.fact_id,e.evidence_digest",
            (canonical(identifiers),),
        ):
            evidence[row["fact_id"]].append(row["evidence_digest"].hex())
        by_kind = {}
        for row in rows:
            by_kind.setdefault(row["kind"], []).append(row)
        result = {}
        for kind, revisions in by_kind.items():
            model = self.registry.models[kind]
            keys = canonical([row["id"] for row in revisions])
            data = {}
            for row in connection.execute(
                f"SELECT f.* FROM json_each(?) ids JOIN {table_name(kind)} f "
                "ON f.revision_id=ids.value",
                (keys,),
            ):
                values = dict(row)
                ident = values.pop("revision_id")
                data[ident] = decode_fields(model, values)
            for name, info in model.model_fields.items():
                item = sequence_model(info.annotation)
                if item is None:
                    continue
                for values in data.values():
                    values[name] = []
                for row in connection.execute(
                    f"SELECT f.* FROM json_each(?) ids JOIN {table_name(kind)}_{name} f "
                    "ON f.revision_id=ids.value ORDER BY f.revision_id,f.item_no",
                    (keys,),
                ):
                    data[row["revision_id"]][name].append(
                        decode_fields(item, {key: row[key] for key in item.model_fields})
                    )
            for row in revisions:
                result[row["id"]] = FactVersion(
                    row["id"],
                    row["subject_id"],
                    row["revision"],
                    model.model_validate_json(canonical(data[row["id"]])),
                    tuple(evidence[row["id"]]),
                )
        return result

    def current_fact(self, connection, subject_id):
        row = connection.execute(
            "SELECT fact_id FROM fact_current WHERE subject_id=?", (subject_id,)
        ).fetchone()
        if not row:
            raise KernelError("unknown_subject", f"unknown fact {subject_id}")
        return self.fact(connection, row[0])

    def write_fact(self, connection, version: FactVersion, hashed: bytes):
        from .discovery_indexes import sync_discovery_fact
        from .entity_references import sync_entity_references, validate_entity_references

        fact = version.fact
        validate_entity_references(connection, fact, version.subject_id)
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
                for key in sorted(scope_keys("fact", fact, version.subject_id))
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
        sync_entity_references(connection, version, hashed)
        sync_discovery_fact(connection, version.id)

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
        return self.select_many(connection, (read,))[read]

    def select_many(self, connection, reads, *, fact_loader=None) -> dict[Read, tuple]:
        """Select declared reads in batches from one shared semantic implementation."""
        requested = list(dict.fromkeys(reads))
        for read in requested:
            validate_read(read)
        selected: dict[Read, tuple] = {}
        for source in ("fact", "calculation"):
            group = [read for read in requested if read.source == source]
            if not group:
                continue
            specifications = [
                [
                    index,
                    read.kind,
                    read.key,
                    read.before_period.ordinal if read.before_period else NO_PERIOD_LIMIT,
                ]
                for index, read in enumerate(group)
            ]
            query = (
                "WITH requests AS (SELECT json_extract(value,'$[0]') AS slot,"
                "json_extract(value,'$[1]') AS kind,json_extract(value,'$[2]') AS key,"
                "json_extract(value,'$[3]') AS cutoff FROM json_each(?)), ids AS ("
            )
            if source == "fact":
                query += (
                    "SELECT q.slot,f.id FROM requests q JOIN fact_revision f "
                    "ON f.id=substr(q.key,2) JOIN subject s ON s.id=f.subject_id "
                    "JOIN fact_seal z ON z.fact_id=f.id WHERE substr(q.key,1,1)='#' "
                    "AND (q.kind='*' OR q.kind=s.kind) AND f.period<q.cutoff UNION ALL "
                    "SELECT q.slot,f.id FROM requests q JOIN subject s ON s.kind=q.kind "
                    "JOIN fact_current a ON a.subject_id=s.id JOIN fact_revision f "
                    "ON f.id=a.fact_id WHERE q.key='*' AND f.period<q.cutoff UNION ALL "
                    "SELECT q.slot,f.id FROM requests q JOIN fact_scope x ON x.scope_key=q.key "
                    "JOIN fact_current a ON a.fact_id=x.fact_id "
                    "JOIN fact_revision f ON f.id=a.fact_id "
                    "WHERE q.key<>'*' AND substr(q.key,1,1)<>'#' "
                    "AND (q.kind='*' OR q.kind=x.kind) AND f.period<q.cutoff) "
                    "SELECT ids.slot,f.id,f.subject_id FROM ids "
                    "JOIN fact_revision f ON f.id=ids.id ORDER BY ids.slot,f.subject_id"
                )
            else:
                query += (
                    "SELECT q.slot,c.id FROM requests q JOIN calculation c ON c.id=substr(q.key,2) "
                    "JOIN calculation_seal z ON z.calculation_id=c.id WHERE substr(q.key,1,1)='#' "
                    "AND (q.kind='*' OR q.kind=c.kind) AND c.period<q.cutoff UNION ALL "
                    "SELECT q.slot,c.id FROM requests q JOIN calculation c ON c.kind=q.kind "
                    "JOIN calculation_current a ON a.calculation_id=c.id "
                    "WHERE q.key='*' AND c.period<q.cutoff UNION ALL "
                    "SELECT q.slot,c.id FROM requests q "
                    "JOIN calculation_scope x ON x.scope_key=q.key "
                    "JOIN calculation_current a ON a.calculation_id=x.calculation_id "
                    "JOIN calculation c ON c.id=a.calculation_id WHERE q.key<>'*' "
                    "AND substr(q.key,1,1)<>'#' AND (q.kind='*' OR q.kind=x.kind) "
                    "AND c.period<q.cutoff) SELECT ids.slot,c.* FROM ids "
                    "JOIN calculation c ON c.id=ids.id ORDER BY ids.slot,c.period,c.subject_id"
                )
            rows = list(connection.execute(query, (canonical(specifications),)))
            superseded = {
                row[0]
                for row in connection.execute(
                    "SELECT i.subject_id FROM identity_correction_item i "
                    "WHERE i.action='supersede' "
                    "AND i.subject_id IN(SELECT value FROM json_each(?)) "
                    "AND i.rowid=(SELECT max(j.rowid) FROM identity_correction_item j "
                    "WHERE j.subject_id=i.subject_id)",
                    (canonical(sorted({row["subject_id"] for row in rows})),),
                )
            }
            rows = [
                row
                for row in rows
                if group[row["slot"]].key.startswith("#") or row["subject_id"] not in superseded
            ]
            if source == "fact":
                identifiers = {row["id"] for row in rows}
                objects = (
                    fact_loader(identifiers)
                    if fact_loader is not None
                    else self.facts(connection, identifiers)
                )
            else:
                objects = {row["id"]: self.calculation(row) for row in rows}
            grouped = [[] for _ in group]
            for row in rows:
                grouped[row["slot"]].append(objects[row["id"]])
            selected.update((read, tuple(grouped[index])) for index, read in enumerate(group))
        return selected

    @staticmethod
    def epochs(connection):
        row = connection.execute(
            "SELECT accounting,material,management FROM state WHERE id=1"
        ).fetchone()
        return dict(row)
