"""Short-lived bound SQLite connections and typed fact persistence."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from functools import cache
from typing import get_args, get_origin

from pydantic import BaseModel
from pydantic_core import from_json, to_json

from .contracts import Calculation, FactVersion, KernelError, Read
from .dependencies import NO_PERIOD_LIMIT, scope_keys, validate_read
from .runtime import connect, initialize_file, require_local_database
from .schema import base_type, initialize, sequence_model, table_name
from .stored_json import DuplicateStoredKey, loads_unique
from .text_sort import pinyin_key
from .types import YearMonth, canonical
from .versions import verify_schema

# Only QueryReads.snapshot binds this pointer. The raw values themselves live
# on that read transaction's QueryReads and are discarded when it exits.
_active_fact_reads: ContextVar[object | None] = ContextVar("active_fact_reads", default=None)


def _snapshot_fact_raws(store, connection, identifiers):
    """Decode exact pre-validation JSON, never a normalized model dump or proof."""
    reads = _active_fact_reads.get()
    if (
        reads is None
        or not reads._snapshot_active
        or reads.store is not store
        or reads.connection is not connection
        or not connection.in_transaction
    ):
        return {}
    return {
        ident: from_json(reads._raw_fact_data[ident])
        for ident in identifiers
        if ident in reads._raw_fact_data
    }


def _snapshot_fact_hashes(store, connection, identifiers):
    """Hash exact read bytes; callers still check identity, order and seals.

    Equality is a fast path only for a stored canonical representation. A
    different encoding must fall back to hashing the decoded original value.
    """
    raw = _snapshot_fact_raw_sink(store, connection)
    return (
        {ident: hashlib.sha256(raw[ident]).digest() for ident in identifiers if ident in raw}
        if raw is not None else {}
    )


def _snapshot_fact_raw_sink(store, connection):
    reads = _active_fact_reads.get()
    if (
        reads is not None
        and reads._snapshot_active
        and reads.store is store
        and reads.connection is connection
        and connection.in_transaction
    ):
        return reads._raw_fact_data
    return None


def composite(annotation):
    typ = base_type(annotation)
    return get_origin(typ) in (list, tuple, dict, set) or (
        isinstance(typ, type) and issubclass(typ, BaseModel)
    )


@cache
def _field_codecs(model):
    """Compile immutable model metadata, never business values or read results."""
    fields = []
    for name, info in model.model_fields.items():
        if sequence_model(info.annotation):
            continue
        typ = base_type(info.annotation)
        codec = (
            "month"
            if typ is YearMonth
            else "bool"
            if typ is bool
            else ("json" if composite(info.annotation) else "scalar")
        )
        fields.append((name, codec))
    return tuple(fields)


def encode_fields(model, data):
    result = {}
    for name, codec in _field_codecs(model):
        value = data[name]
        if value is not None:
            if codec == "month":
                value = YearMonth(value).ordinal
            elif codec == "bool":
                value = int(value)
            elif codec == "json":
                value = canonical(value)
        result[name] = value
    return result


def decode_fields(model, data):
    if hasattr(model, "_v1_fields"):
        from .content_v1 import decode_v1_fields

        return decode_v1_fields(model, data)
    for name, codec in _field_codecs(model):
        if data[name] is not None:
            if codec == "month":
                data[name] = str(YearMonth.from_ordinal(data[name]))
            elif codec == "bool":
                data[name] = bool(data[name])
            elif codec == "json":
                try:
                    data[name] = loads_unique(data[name])
                except DuplicateStoredKey as exc:
                    raise KernelError(
                        "content_integrity_failed", "已保存的事实 JSON 有重复字段"
                    ) from exc
    return data


def _stored_sequence(model, name, info):
    fields = getattr(model, "_v1_fields", None)
    if fields is not None:
        return (
            get_args(base_type(info.annotation))[0]
            if fields[name]["kind"] == "sequence" else None
        )
    return sequence_model(info.annotation)


def _validation_json(data):
    """Validation needs JSON mode, not sorted keys; persisted digests stay canonical."""
    encoded = to_json(data)
    if b"NaN" in encoded or b"Infinity" in encoded:
        # Retain canonical()'s rejection of non-finite stored values. Text with
        # these words is legal and simply takes the slower serialization path.
        return canonical(data)
    return encoded


@cache
def _scalar_fact_json_sql(model, kind):
    """Build a C-side JSON read for facts whose stored fields need no JSON parsing.

    Composite fields retain the Python decoder, which rejects non-finite and
    non-standard embedded JSON before validation. This cache contains SQL
    metadata only, never fact values or read results.
    """
    if hasattr(model, "_v1_fields"):
        return None
    sequences = []
    for name, info in model.model_fields.items():
        item = _stored_sequence(model, name, info)
        if item is not None:
            sequences.append((name, item))
    if any(codec == "json" for _, codec in _field_codecs(model)) or any(
        codec == "json" for _, item in sequences for _, codec in _field_codecs(item)
    ):
        return None

    def value_sql(alias, name, codec):
        column = f'{alias}."{name}"'
        if codec == "month":
            return (
                f"CASE WHEN {column} IS NULL THEN NULL ELSE "
                f"CASE WHEN typeof({column})='integer' AND "
                f"{column} BETWEEN 0 AND 119987 THEN "
                f"printf('%04d-%02d', {column}/12+1, {column}%12+1) "
                "ELSE 0 END END"
            )
        if codec == "bool":
            return (
                f"CASE WHEN {column} IS NULL THEN NULL ELSE "
                f"json(CASE WHEN (typeof({column}) IN ('text','blob') "
                f"AND length({column})>0) OR "
                f"(typeof({column}) NOT IN ('text','blob') AND {column}!=0) "
                "THEN 'true' ELSE 'false' END) END"
            )
        return column

    def object_sql(alias, item_model, children=None):
        fields = []
        values = {
            name: value_sql(alias, name, codec) for name, codec in _field_codecs(item_model)
        }
        values.update(children or {})
        for name, value in sorted(values.items()):
            fields.extend((f"'{name}'", value))
        return "json_object(" + ",".join(fields) + ")"

    arrays = {}
    for name, item in sequences:
        child = object_sql("c", item)
        child_table = f"{table_name(kind)}_{name}"
        children = (
            f"SELECT json_group_array({child} ORDER BY c.item_no) "
            f"FROM {child_table} c WHERE c.revision_id=f.revision_id"
        )
        arrays[name] = f"json(COALESCE(({children}), '[]'))"
    document = object_sql("f", model, arrays)
    return (
        f"SELECT f.revision_id,{document} FROM json_each(?) ids "
        f"CROSS JOIN {table_name(kind)} f ON f.revision_id=ids.value"
    )


def _scalar_fact_hashes(store, connection, kinds):
    """Hash the existing C-side JSON encoding for selected scalar facts.

    This is only an equality fast path. A missing row, unsupported field codec,
    or nonmatching hash must still use the original decoded-value check.
    Historical v1 reads retain their independent content rules.
    """
    if getattr(store.registry, "content_version", None) == 1:
        return {}
    result = {}
    for kind, identifiers in kinds.items():
        if not identifiers:
            continue
        model = store.registry.models.get(kind)
        if model is None:
            continue
        sql = _scalar_fact_json_sql(model, kind)
        if sql is None:
            continue
        for ident, raw in connection.execute(sql, (canonical(sorted(identifiers)),)):
            result[ident] = hashlib.sha256(raw.encode("utf-8")).digest()
    return result


class Store:
    def __init__(
        self, path, bundle, company_id: str, database_id: str,
        *, taxpayer_id: str | None = None, read_pool=None,
    ):
        self.path = require_local_database(path)
        self.bundle = bundle
        self.registry = bundle.registry
        self.company_id, self.database_id = company_id, database_id
        self.taxpayer_id, self.read_pool = taxpayer_id, read_pool

    @staticmethod
    def evidence_metadata(connection, digests):
        """Read names from this company's immutable evidence without loading file bodies."""
        result = []
        for row in connection.execute(
            "SELECT e.digest,e.name,e.media_type FROM json_each(?) j "
            "JOIN evidence e ON e.digest=unhex(j.value)",
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
        return sorted(result, key=lambda item: (pinyin_key(item["name"]), item["digest"]))

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

    def validate_connection(self, connection):
        verify_schema(connection, bundle=self.bundle)
        identity = connection.execute("SELECT * FROM identity WHERE id=1").fetchone()
        if (
            not identity
            or identity["company_id"] != self.company_id
            or identity["database_id"] != self.database_id
            or (
                self.taxpayer_id is not None
                and identity["taxpayer_id"] != self.taxpayer_id
            )
        ):
            raise KernelError("company_mismatch", "database does not match bound company")

    @contextmanager
    def _snapshot_connection(self):
        """Borrow the current reader's private resident snapshot connection."""
        from . import close_storage, publication
        from .content_history_context import close_reader, publication_reader

        if (
            self.read_pool is not None
            and getattr(self.registry, "content_version", None) != 1
            and close_reader() is close_storage
            and publication_reader() is publication
        ):
            if not self.path.is_file():
                raise KernelError("company_missing", "company database is missing")
            with self.read_pool.borrow(self, _owned_snapshot=True) as connection:
                yield connection
        else:
            with self.connection(read_only=True) as connection:
                yield connection

    @contextmanager
    def connection(self, *, read_only=False):
        if not self.path.is_file():
            raise KernelError("company_missing", "company database is missing")
        if read_only and self.read_pool is not None:
            with self.read_pool.borrow(self) as connection:
                yield connection
            return

        connection = connect(self.path, read_only=read_only, validator=self.validate_connection)
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
            model.model_validate_json(_validation_json(data)),
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
        if getattr(self.registry, "content_version", None) == 1:
            from .content_v1 import load_v1_fact_data_many

            return load_v1_fact_data_many(connection, self.registry, fact_ids)
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
        return self._fact_data_many_from_headers(connection, rows)

    def _fact_data_many_from_headers(self, connection, rows) -> dict[str, dict]:
        """Decode current typed storage using this batch's exact source headers.

        Internal callers already selected and checked the requested fact set.
        Released storage retains its separate fact_data_many reader.
        """
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
                item = _stored_sequence(model, name, info)
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
        if len(result) != len(rows):
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
            item = _stored_sequence(model, name, info)
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
        if getattr(self.registry, "content_version", None) == 1:
            from .content_v1 import load_v1_fact_versions

            return load_v1_fact_versions(connection, self.registry, fact_ids)
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
        raw_sink = _snapshot_fact_raw_sink(self, connection)
        raw_values = {} if raw_sink is not None else None
        for kind, revisions in by_kind.items():
            model = self.registry.models[kind]
            keys = canonical([row["id"] for row in revisions])
            scalar_sql = _scalar_fact_json_sql(model, kind)
            if scalar_sql is not None:
                raw_by_id = {
                    row[0]: row[1].encode("utf-8")
                    for row in connection.execute(scalar_sql, (keys,))
                }
                for row in revisions:
                    raw_json = raw_by_id[row["id"]]
                    result[row["id"]] = FactVersion(
                        row["id"],
                        row["subject_id"],
                        row["revision"],
                        model.model_validate_json(raw_json),
                        tuple(evidence[row["id"]]),
                    )
                    if raw_values is not None:
                        raw_values[row["id"]] = raw_json
                continue
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
                item = _stored_sequence(model, name, info)
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
                raw_json = _validation_json(data[row["id"]])
                result[row["id"]] = FactVersion(
                    row["id"],
                    row["subject_id"],
                    row["revision"],
                    model.model_validate_json(raw_json),
                    tuple(evidence[row["id"]]),
                )
                if raw_values is not None:
                    raw_values[row["id"]] = raw_json
        # A failed batch must not leave partly decoded values in the snapshot.
        if raw_sink is not None:
            raw_sink.update(raw_values)
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
        validate_entity_references(connection, fact, version.subject_id, store=self)
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
    def _verify_calculation_identity(row):
        """Check consumed scalar headers against their original fact identity."""
        if (
            row["id"] is None
            or row["subject_id"] != row["original_subject_id"]
            or row["kind"] != row["original_kind"]
            or row["period"] != row["original_period"]
        ):
            raise KernelError(
                "content_integrity_failed", "核算来源身份与原始事实不匹配",
                component="calculation", record_id=row["id"],
            )

    @staticmethod
    def calculation(row):
        try:
            values = loads_unique(row["outcome"])["values"]
        except DuplicateStoredKey as exc:
            raise KernelError(
                "content_integrity_failed", "已保存的核算结果 JSON 有重复字段"
            ) from exc
        return Calculation(
            row["id"],
            row["subject_id"],
            row["kind"],
            YearMonth.from_ordinal(row["period"]),
            values,
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
                "WITH requests AS MATERIALIZED (SELECT json_extract(value,'$[0]') AS slot,"
                "json_extract(value,'$[1]') AS kind,json_extract(value,'$[2]') AS key,"
                "json_extract(value,'$[3]') AS cutoff FROM json_each(?)), ids AS ("
            )
            if source == "fact":
                query += (
                    "SELECT q.slot,f.id FROM requests q CROSS JOIN fact_revision f "
                    "ON f.id=substr(q.key,2) CROSS JOIN subject s ON s.id=f.subject_id "
                    "CROSS JOIN fact_seal z ON z.fact_id=f.id WHERE substr(q.key,1,1)='#' "
                    "AND (q.kind='*' OR q.kind=s.kind) AND f.period<q.cutoff UNION ALL "
                    "SELECT q.slot,f.id FROM requests q CROSS JOIN subject s ON s.kind=q.kind "
                    "CROSS JOIN fact_current a ON a.subject_id=s.id CROSS JOIN fact_revision f "
                    "ON f.id=a.fact_id WHERE q.key='*' AND f.period<q.cutoff UNION ALL "
                    "SELECT q.slot,f.id FROM requests q CROSS JOIN fact_scope x "
                    "ON x.kind=q.kind AND x.scope_key=q.key "
                    "CROSS JOIN fact_current a ON a.fact_id=x.fact_id "
                    "CROSS JOIN fact_revision f ON f.id=a.fact_id "
                    "WHERE q.key<>'*' AND substr(q.key,1,1)<>'#' "
                    "AND q.kind<>'*' AND f.period<q.cutoff UNION ALL "
                    "SELECT q.slot,f.id FROM requests q CROSS JOIN fact_scope x "
                    "ON x.scope_key=q.key CROSS JOIN fact_current a ON a.fact_id=x.fact_id "
                    "CROSS JOIN fact_revision f ON f.id=a.fact_id "
                    "WHERE q.key<>'*' AND substr(q.key,1,1)<>'#' "
                    "AND q.kind='*' AND f.period<q.cutoff) "
                    "SELECT ids.slot,f.id,f.subject_id FROM ids "
                    "CROSS JOIN fact_revision f ON f.id=ids.id ORDER BY ids.slot,f.subject_id"
                )
            else:
                # Authenticate candidate headers before any kind/cutoff can
                # hide a damaged exact reference or current scope owner. Only
                # scalar identities are read here; result bodies remain in
                # the existing filtered selection below.
                header_query = query[:-len(", ids AS (")] + (
                    " SELECT c.id,c.subject_id,c.kind,c.period,"
                    "f.subject_id original_subject_id,s.kind original_kind,"
                    "f.period original_period,selected.head_subject_id "
                    "FROM (SELECT c.id,NULL head_subject_id,1 exact_reference FROM requests q "
                    "CROSS JOIN calculation c ON c.id=substr(q.key,2) "
                    "WHERE substr(q.key,1,1)='#' UNION ALL "
                    "SELECT a.calculation_id,a.subject_id,0 FROM requests q "
                    "CROSS JOIN subject own ON own.kind=q.kind "
                    "CROSS JOIN calculation_current a ON a.subject_id=own.id "
                    "WHERE q.key='*' UNION ALL "
                    "SELECT a.calculation_id,a.subject_id,0 FROM requests q "
                    "CROSS JOIN calculation_scope x "
                    "ON x.kind=q.kind AND x.scope_key=q.key "
                    "CROSS JOIN calculation_current a ON a.calculation_id=x.calculation_id "
                    "WHERE q.key<>'*' AND substr(q.key,1,1)<>'#' AND q.kind<>'*' UNION ALL "
                    "SELECT a.calculation_id,a.subject_id,0 FROM requests q "
                    "CROSS JOIN calculation_scope x ON x.scope_key=q.key "
                    "CROSS JOIN calculation_current a ON a.calculation_id=x.calculation_id "
                    "WHERE q.key<>'*' AND substr(q.key,1,1)<>'#' AND q.kind='*') selected "
                    "LEFT JOIN calculation c ON c.id=selected.id "
                    "LEFT JOIN fact_revision f ON f.id=c.fact_id "
                    "LEFT JOIN subject s ON s.id=f.subject_id "
                    "WHERE selected.exact_reference OR NOT EXISTS("
                    "SELECT 1 FROM identity_correction_item i "
                    "WHERE i.subject_id=selected.head_subject_id AND i.action='supersede' "
                    "AND i.rowid=(SELECT max(j.rowid) FROM identity_correction_item j "
                    "WHERE j.subject_id=i.subject_id))"
                )
                for header in connection.execute(header_query, (canonical(specifications),)):
                    self._verify_calculation_identity(header)
                    if (header["head_subject_id"] is not None
                            and header["subject_id"] != header["head_subject_id"]):
                        raise KernelError(
                            "content_integrity_failed", "当前核算头与业务身份不匹配",
                            component="calculation", record_id=header["id"],
                        )
                query += (
                    "SELECT q.slot,c.id FROM requests q CROSS JOIN calculation c "
                    "ON c.id=substr(q.key,2) CROSS JOIN fact_revision f ON f.id=c.fact_id "
                    "CROSS JOIN subject s ON s.id=f.subject_id CROSS JOIN calculation_seal z "
                    "ON z.calculation_id=c.id WHERE substr(q.key,1,1)='#' "
                    "AND (q.kind='*' OR q.kind=s.kind) AND f.period<q.cutoff UNION ALL "
                    "SELECT q.slot,c.id FROM requests q CROSS JOIN subject s ON s.kind=q.kind "
                    "CROSS JOIN calculation_current a ON a.subject_id=s.id "
                    "CROSS JOIN calculation c ON c.id=a.calculation_id "
                    "CROSS JOIN fact_revision f ON f.id=c.fact_id "
                    "WHERE q.key='*' AND f.period<q.cutoff UNION ALL "
                    "SELECT q.slot,c.id FROM requests q "
                    "CROSS JOIN calculation_scope x ON x.kind=q.kind AND x.scope_key=q.key "
                    "CROSS JOIN calculation_current a ON a.calculation_id=x.calculation_id "
                    "CROSS JOIN calculation c ON c.id=a.calculation_id "
                    "CROSS JOIN fact_revision f ON f.id=c.fact_id WHERE q.key<>'*' "
                    "AND substr(q.key,1,1)<>'#' AND q.kind<>'*' "
                    "AND f.period<q.cutoff UNION ALL SELECT q.slot,c.id FROM requests q "
                    "CROSS JOIN calculation_scope x ON x.scope_key=q.key "
                    "CROSS JOIN calculation_current a ON a.calculation_id=x.calculation_id "
                    "CROSS JOIN calculation c ON c.id=a.calculation_id "
                    "CROSS JOIN fact_revision f ON f.id=c.fact_id WHERE q.key<>'*' "
                    "AND substr(q.key,1,1)<>'#' AND q.kind='*' "
                    "AND f.period<q.cutoff) SELECT ids.slot,c.*,"
                    "f.subject_id original_subject_id,s.kind original_kind,"
                    "f.period original_period FROM ids "
                    "CROSS JOIN calculation c ON c.id=ids.id "
                    "LEFT JOIN fact_revision f ON f.id=c.fact_id "
                    "LEFT JOIN subject s ON s.id=f.subject_id "
                    "ORDER BY ids.slot,c.period,c.subject_id"
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
                # Authenticate the whole selected batch before exposing any
                # Calculation values, including named-scope/member reads.
                for row in rows:
                    self._verify_calculation_identity(row)
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
