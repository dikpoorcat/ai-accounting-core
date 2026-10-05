"""Publication-bound, repairable open-report inputs for exact selected lines.

The anchor belongs to the immutable publication chain. The content table is a
derived read projection; it never selects a publication or a voucher on its own.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass

from pydantic_core import SchemaValidator, ValidationError, core_schema, from_json

from .account_definitions import RECLASS
from .contracts import KernelError
from .query_semantics import report_party_needs_ancestry, resolve_calculation_relations
from .report_semantics import compact_line_fields, immutable_line_fields, report_source_fact
from .types import YearMonth, canonical

REPORT_OPEN_CONTRIBUTION_DDL = """
CREATE TABLE report_open_contribution_anchor(
 publication_id TEXT PRIMARY KEY REFERENCES calculation_publication(id),
 calculation_id TEXT NOT NULL UNIQUE REFERENCES calculation(id),
 content_digest BLOB NOT NULL CHECK(length(content_digest)=32)
) STRICT;
CREATE TRIGGER report_open_contribution_anchor_update
 BEFORE UPDATE ON report_open_contribution_anchor
 BEGIN SELECT RAISE(ABORT,'immutable report contribution anchor'); END;
CREATE TRIGGER report_open_contribution_anchor_delete
 BEFORE DELETE ON report_open_contribution_anchor
 BEGIN SELECT RAISE(ABORT,'immutable report contribution anchor'); END;
CREATE TABLE report_open_contribution(
 publication_id TEXT PRIMARY KEY REFERENCES report_open_contribution_anchor(publication_id),
 content TEXT NOT NULL CHECK(json_valid(content)),
 content_digest BLOB NOT NULL CHECK(length(content_digest)=32)
) STRICT;
"""


@dataclass(frozen=True)
class OpenContribution:
    publication_id: str
    calculation_id: str
    content: str
    checksum: bytes


_SOURCE_PROOF_SEAL = object()


@dataclass(frozen=True)
class _AnchoredSourceProof:
    seal: object
    connection: object
    reads: object
    bindings: tuple[tuple[str, str, str, str], ...]


def _require_source_proof(proof, reads):
    if (
        type(proof) is not _AnchoredSourceProof
        or proof.seal is not _SOURCE_PROOF_SEAL
        or proof.connection is not reads.connection
        or proof.reads is not reads
        or not reads._snapshot_active
        or not reads.connection.in_transaction
    ):
        raise ValueError("report source proof belongs to another read snapshot")
    return proof.bindings


def _invalid(record_id, reason):
    raise KernelError(
        "content_integrity_failed",
        "当前报表行贡献与正式来源不一致",
        component="report_open_contribution",
        record_id=record_id,
        reason=reason,
    )


def _checksum(content):
    return hashlib.sha256(content.encode("utf-8")).digest()


def _party_key(value):
    if isinstance(value, list):
        return tuple(_party_key(item) for item in value)
    return value


def _resolution_content(resolution):
    """Retain the unchanged party resolver's actual inputs, not a new rule."""

    return {
        "issues": resolution["issues"],
        "line_relations": resolution["line_relations"],
        "own_party_accounts": sorted(resolution["own_party_accounts"]),
        "party_candidates_by_account": [
            [account, sorted(keys, key=canonical)]
            for account, keys in sorted(resolution["party_candidates_by_account"].items())
        ],
    }


def _resolution_from_content(content):
    return {
        "issues": content["issues"],
        "line_relations": [
            {**item, "party_key": _party_key(item.get("party_key"))}
            for item in content["line_relations"]
        ],
        "own_party_accounts": set(content["own_party_accounts"]),
        "party_candidates_by_account": {
            account: {_party_key(key) for key in keys}
            for account, keys in content["party_candidates_by_account"]
        },
    }


def _publication_rows(connection, calculation_id):
    """Only versions actually produced by this immutable result are stored."""

    return [
        dict(row)
        for row in connection.execute(
            "SELECT v.id version_id,v.calculation_id,v.reverses_id,l.line_no,l.account,"
            "l.debit,l.credit,l.cashflow FROM voucher_version v "
            "INDEXED BY voucher_calculation CROSS JOIN voucher_line l ON l.version_id=v.id "
            "WHERE v.calculation_id=? AND v.reverses_id IS NULL "
            "ORDER BY v.id,l.line_no",
            (calculation_id,),
        )
    ]


def prepare_open_contribution(
    engine, connection, publication, *, verified_source=None, records=None,
    publication_rows=None,
):
    """Build a compact source contribution using the existing direct resolver."""

    from .query_reads import QueryReads

    publication_id = publication["id"]
    calculation_id = publication["calculation_id"]
    if not calculation_id:
        _invalid(publication_id, "publication_without_calculation")
    if verified_source is None:
        reads = QueryReads(engine, connection)
        calculation = reads.calculation
        parents = reads.parents
    else:
        # Full integrity has already authenticated every saved calculation,
        # fact body and dependency in this transaction. Reuse those exact
        # records instead of decoding them again for each publication.
        records = {} if records is None else records

        def calculation(ident):
            if ident not in records:
                row = verified_source["calculations"].get(ident)
                if row is None:
                    _invalid(publication_id, "verified_calculation_missing")
                fact = verified_source["facts"].get(row["fact_id"])
                if fact is None:
                    _invalid(publication_id, "verified_fact_missing")
                records[ident] = {
                    **row,
                    "period": str(YearMonth.from_ordinal(row["period"])),
                    "result_digest": row["digest"].hex(),
                    # Integrity already decoded and authenticated the saved
                    # shape. Today's validators/defaults must not reinterpret
                    # historical inputs while rebuilding their contribution.
                    "fact_data": fact["data"],
                    "outcome": row["decoded"],
                }
            return records[ident]

        def parents(ident):
            return tuple(sorted(verified_source["dependencies"][ident]))

    root = calculation(calculation_id)
    parent_ids = tuple(sorted(parents(calculation_id)))
    rows = (
        _publication_rows(connection, calculation_id)
        if publication_rows is None else publication_rows
    )
    loaded_sources = set()

    def load_direct(ident):
        loaded_sources.add(ident)
        return calculation(ident)

    relation = resolve_calculation_relations(
        root,
        load_calculation=load_direct,
        load_parents=parents,
        collect_ancestry=False,
    )
    if not loaded_sources.issubset(parent_ids):
        _invalid(publication_id, "direct_source_not_parent")
    used_ids = {calculation_id} | loaded_sources
    if verified_source is None:
        reads.verify_selected_content(used_ids)
        headers = {
            row["id"]: row
            for row in connection.execute(
                "SELECT c.id,c.digest,c.fact_id,f.digest fact_digest "
                "FROM json_each(?) ids CROSS JOIN calculation c ON c.id=ids.value "
                "JOIN fact_revision f ON f.id=c.fact_id",
                (canonical(sorted(used_ids)),),
            )
        }
        if set(headers) != used_ids:
            _invalid(publication_id, "source_header_missing")
        bindings = [
            [
                ident,
                headers[ident]["digest"].hex(),
                headers[ident]["fact_id"],
                headers[ident]["fact_digest"].hex(),
            ]
            for ident in sorted(used_ids)
        ]
    else:
        bindings = [
            [
                ident,
                verified_source["calculations"][ident]["digest"].hex(),
                verified_source["calculations"][ident]["fact_id"],
                verified_source["facts"][
                    verified_source["calculations"][ident]["fact_id"]
                ]["digest"].hex(),
            ]
            for ident in sorted(used_ids)
        ]
    facts = {}

    def source_fact(ident):
        if ident not in facts:
            facts[ident] = report_source_fact(calculation(ident))
        return facts[ident]

    def resolved_calculation(ident):
        record = calculation(ident)
        return record | {"decoded": record["outcome"]}

    projected = []
    usable = True
    for row in rows:
        fields = immutable_line_fields(
            row,
            calculation_id,
            calculation=resolved_calculation,
            source_fact=source_fact,
            relations=lambda _ident: relation,
        )
        projected.append(
            [
                row["version_id"],
                row["line_no"],
                row["account"],
                row["debit"],
                row["credit"],
                row["cashflow"],
                compact_line_fields(fields),
            ]
        )
        if row["account"] in RECLASS and report_party_needs_ancestry(row, relation):
            usable = False
    content = canonical(
        {
            "format": "ai-accounting-kernel/2/report-open-contribution/1",
            "publication_id": publication_id,
            "calculation_id": calculation_id,
            "result_digest": root["result_digest"],
            "fact_id": root["fact_id"],
            "parents": parent_ids,
            "source_bindings": bindings,
            "rows": projected,
            "resolution": _resolution_content(relation),
            "usable": usable,
        }
    )
    return OpenContribution(publication_id, calculation_id, content, _checksum(content))


def sync_open_contributions(engine, connection, publication_highwater):
    """Mint anchors only for publications inserted by this write transaction.

    An older missing anchor is damage, never a request to backfill authority.
    """

    publications = list(
        connection.execute(
            "SELECT p.id,p.calculation_id FROM calculation_publication p "
            "WHERE p.sequence>? AND p.calculation_id IS NOT NULL ORDER BY p.sequence",
            (publication_highwater,),
        )
    )
    for publication in publications:
        prepared = prepare_open_contribution(engine, connection, publication)
        connection.execute(
            "INSERT INTO report_open_contribution_anchor VALUES(?,?,?)",
            (prepared.publication_id, prepared.calculation_id, prepared.checksum),
        )
        connection.execute(
            "INSERT INTO report_open_contribution VALUES(?,?,?)",
            (prepared.publication_id, prepared.content, prepared.checksum),
        )
    return len(publications)


_CONTENT_KEYS = frozenset(
    {
        "format",
        "publication_id",
        "calculation_id",
        "result_digest",
        "fact_id",
        "parents",
        "source_bindings",
        "rows",
        "resolution",
        "usable",
    }
)


def _decode(content, publication_id):
    try:
        value = from_json(content)
        if (
            type(value) is not dict
            or set(value) != _CONTENT_KEYS
            or value["format"] != "ai-accounting-kernel/2/report-open-contribution/1"
            or value["publication_id"] != publication_id
            or type(value["calculation_id"]) is not str
            or type(value["result_digest"]) is not str
            or len(value["result_digest"]) != 64
            or type(value["fact_id"]) is not str
            or type(value["parents"]) is not list
            or any(type(item) is not str for item in value["parents"])
            or value["parents"] != sorted(set(value["parents"]))
            or type(value["source_bindings"]) is not list
            or any(
                type(item) is not list
                or len(item) != 4
                or any(type(part) is not str for part in item)
                or len(item[1]) != 64
                or len(item[3]) != 64
                for item in value["source_bindings"]
            )
            or type(value["rows"]) is not list
            or any(
                type(item) is not list
                or len(item) != 7
                or type(item[0]) is not str
                or type(item[1]) is not int
                or type(item[2]) is not str
                or type(item[3]) is not int
                or type(item[4]) is not int
                or item[5] is not None
                and type(item[5]) is not str
                or type(item[6]) is not dict
                for item in value["rows"]
            )
            or type(value["resolution"]) is not dict
            or set(value["resolution"])
            != {"issues", "line_relations", "own_party_accounts", "party_candidates_by_account"}
            or type(value["resolution"]["issues"]) is not list
            or type(value["resolution"]["line_relations"]) is not list
            or type(value["resolution"]["own_party_accounts"]) is not list
            or type(value["resolution"]["party_candidates_by_account"]) is not list
            or type(value["usable"]) is not bool
        ):
            raise ValueError("invalid contribution shape")
        return value
    except (TypeError, ValueError, KeyError) as exc:
        raise KernelError(
            "content_integrity_failed", "当前报表行贡献格式不一致"
        ) from exc


def _source_bindings(connection, identifiers):
    return [
        [row["id"], row["digest"].hex(), row["fact_id"], row["fact_digest"].hex()]
        for row in connection.execute(
            "SELECT c.id,c.digest,c.fact_id,f.digest fact_digest "
            "FROM json_each(?) ids CROSS JOIN calculation c ON c.id=ids.value "
            "JOIN fact_revision f ON f.id=c.fact_id ORDER BY c.id",
            (canonical(sorted(identifiers)),),
        )
    ]


_BINDING_ID = core_schema.str_schema(strict=True, min_length=1)
_BINDING_DIGEST = core_schema.str_schema(strict=True, pattern="^[0-9a-f]{64}$")
_OWN_SOURCE_CONTENT = SchemaValidator(core_schema.typed_dict_schema({
    "format": core_schema.typed_dict_field(core_schema.literal_schema([
        "ai-accounting-kernel/2/report-open-contribution/1",
    ])),
    "publication_id": core_schema.typed_dict_field(_BINDING_ID),
    "calculation_id": core_schema.typed_dict_field(_BINDING_ID),
    "result_digest": core_schema.typed_dict_field(_BINDING_DIGEST),
    "fact_id": core_schema.typed_dict_field(_BINDING_ID),
    "source_bindings": core_schema.typed_dict_field(core_schema.list_schema(
        core_schema.tuple_schema([
            _BINDING_ID, _BINDING_DIGEST, _BINDING_ID, _BINDING_DIGEST,
        ]), strict=True,
    )),
}, strict=True, extra_behavior="ignore"))


def verify_published_source_bindings(reads, calculation_ids):
    """Authenticate only exact selected sources through their own publications.

    This proves saved calculation/fact bytes, not dependency inputs, current
    selection or frozen adoption. The caller retains those selection checks.
    Returned IDs have a healthy immutable publication/anchor but no repairable
    contribution body: the caller must use ``verify_saved_input_identity`` for
    them before interpreting their results. Missing publications or anchors
    never fall back, including unpublished asset members on this narrow path.
    """

    connection = reads.connection
    if not reads._snapshot_active or not connection.in_transaction:
        raise ValueError("published source proof requires an owned read snapshot")
    identifiers = set(calculation_ids)
    if not identifiers:
        return frozenset()
    candidates = {
        row["selected_id"]: dict(row)
        for row in connection.execute(
            "SELECT c.id selected_id,c.subject_id selected_subject,p.*,"
            "a.calculation_id anchor_calculation_id,a.content_digest anchor_digest,"
            "d.content,d.content_digest body_digest "
            "FROM json_each(?) ids CROSS JOIN calculation c ON c.id=ids.value "
            "LEFT JOIN calculation_publication p ON p.calculation_id=c.id "
            "LEFT JOIN report_open_contribution_anchor a ON a.publication_id=p.id "
            "LEFT JOIN report_open_contribution d ON d.publication_id=p.id",
            (canonical(sorted(identifiers)),),
        )
    }
    if candidates.keys() != identifiers:
        raise KernelError("content_integrity_failed", "本次读取的核算来源缺失")
    for ident, row in candidates.items():
        if row["id"] is None:
            _invalid(ident, "own_publication_missing")
    reads.verify_publication_records(candidates.values())
    bindings = []
    fallback = set()
    for ident in sorted(identifiers):
        row = candidates[ident]
        if row["subject_id"] != row["selected_subject"]:
            _invalid(row["id"], "publication_subject_mismatch")
        if row["anchor_digest"] is None:
            _invalid(row["id"], "own_anchor_missing")
        if row["anchor_calculation_id"] != ident:
            _invalid(row["id"], "anchor_calculation_mismatch")
        if row["content"] is None:
            # A missing repairable body cannot authenticate even the saved
            # anchor checksum. Rebuild only this exceptional contribution,
            # read-only, before allowing the complete original input proof.
            rebuilt = prepare_open_contribution(reads.engine, connection, row)
            if rebuilt.checksum != row["anchor_digest"]:
                _invalid(row["id"], "missing_body_anchor_digest_mismatch")
            fallback.add(ident)
            continue
        checksum = _checksum(row["content"])
        if checksum != row["anchor_digest"] or checksum != row["body_digest"]:
            _invalid(row["id"], "body_anchor_digest_mismatch")
        try:
            # The native JSON validator skips unrelated contribution rows,
            # relation resolution and parents instead of materializing them.
            content = _OWN_SOURCE_CONTENT.validate_json(row["content"], strict=True)
        except ValidationError as exc:
            raise KernelError(
                "content_integrity_failed", "当前报表来源绑定格式不一致"
            ) from exc
        if (
            content["publication_id"] != row["id"]
            or content["calculation_id"] != ident
        ):
            _invalid(row["id"], "own_content_identity_mismatch")
        own = [item for item in content["source_bindings"] if item[0] == ident]
        if len(own) != 1:
            _invalid(row["id"], "own_binding_not_unique")
        binding = own[0]
        if (content["result_digest"], content["fact_id"]) != (binding[1], binding[2]):
            _invalid(row["id"], "source_header_mismatch")
        bindings.append(binding)
    if bindings:
        reads._verify_anchored_source_bytes(_AnchoredSourceProof(
            _SOURCE_PROOF_SEAL, connection, reads, tuple(bindings),
        ))
    return frozenset(fallback)


def read_open_contributions(engine, connection, calculation_ids, *, reads):
    """Authenticate selected saved contributions without trusting their selection.

    If a saved contribution is unavailable, callers use the complete original
    source reader. Full verification still rejects missing immutable anchors;
    only derived bodies can be repaired. A present malformed or mismatched
    body is damage and must reject the page.
    """

    identifiers = set(calculation_ids)
    if not identifiers:
        return {}
    cache = (
        reads._report_snapshot_cache
        if reads._snapshot_active and reads.connection is connection
        else None
    )
    key = "report_open_contributions"
    stored = cache.setdefault(key, {}) if cache is not None else {}
    missing = {ident for ident in identifiers if ident not in stored}
    if missing:
        candidates = {
            row["calculation_id"]: row
            for row in connection.execute(
                "SELECT p.id publication_id,p.calculation_id,"
                "a.calculation_id anchor_calculation_id,a.content_digest anchor_digest,"
                "d.content,d.content_digest body_digest "
                "FROM json_each(?) ids CROSS JOIN calculation_publication p "
                "ON p.calculation_id=ids.value "
                "LEFT JOIN report_open_contribution_anchor a ON a.publication_id=p.id "
                "LEFT JOIN report_open_contribution d ON d.publication_id=p.id",
                (canonical(sorted(missing)),),
            )
        }
        verified = {}
        selected_sources = set()
        for ident in missing:
            row = candidates.get(ident)
            if row is None or row["anchor_digest"] is None or row["content"] is None:
                continue
            if row["anchor_calculation_id"] != ident:
                _invalid(row["publication_id"], "anchor_calculation_mismatch")
            checksum = _checksum(row["content"])
            if checksum != row["anchor_digest"] or checksum != row["body_digest"]:
                _invalid(row["publication_id"], "body_anchor_digest_mismatch")
            content = _decode(row["content"], row["publication_id"])
            if content["calculation_id"] != ident:
                _invalid(row["publication_id"], "calculation_identity_mismatch")
            source_ids = {item[0] for item in content["source_bindings"]}
            if (
                not source_ids
                or ident not in source_ids
                or len(source_ids) != len(content["source_bindings"])
            ):
                _invalid(row["publication_id"], "source_set_invalid")
            selected_sources.update(source_ids)
            verified[ident] = content
        if selected_sources:
            if (
                reads._snapshot_active
                and reads.connection is connection
                and connection.in_transaction
            ):
                proof = _AnchoredSourceProof(
                    _SOURCE_PROOF_SEAL,
                    connection,
                    reads,
                    tuple(
                        tuple(item)
                        for content in verified.values()
                        for item in content["source_bindings"]
                    ),
                )
                actual_by_id = reads._verify_anchored_source_bytes(proof)
            else:
                reads.verify_selected_content(selected_sources)
                actual_by_id = {
                    item[0]: item for item in _source_bindings(connection, selected_sources)
                }
            parents_by_id = defaultdict(list)
            for row in connection.execute(
                "SELECT d.calculation_id,d.upstream_id FROM json_each(?) ids "
                "CROSS JOIN dependency_calculation d ON d.calculation_id=ids.value "
                "ORDER BY d.calculation_id,d.upstream_id",
                (canonical(sorted(verified)),),
            ):
                parents_by_id[row["calculation_id"]].append(row["upstream_id"])
            for ident, content in verified.items():
                if content["parents"] != parents_by_id[ident]:
                    raise KernelError(
                        "content_integrity_failed", "报表命中来源缺少精确核算依赖"
                    )
                if content["source_bindings"] != [actual_by_id[source_id] for source_id in sorted(
                    item[0] for item in content["source_bindings"]
                )]:
                    _invalid(content["publication_id"], "source_dependency_mismatch")
                root = actual_by_id[ident]
                if content["result_digest"] != root[1] or content["fact_id"] != root[2]:
                    _invalid(content["publication_id"], "source_header_mismatch")
        stored.update(verified)
    return {ident: stored[ident] for ident in identifiers if ident in stored}


def selected_open_line(content, row):
    """Match one authoritative selected line to the saved source line."""

    if not content["usable"]:
        return None
    key = row.get("reverses_id") or row["version_id"]
    candidates = [
        item
        for item in content["rows"]
        if item[0] == key and item[1] == row["line_no"]
    ]
    if not candidates:
        return None
    if len(candidates) != 1:
        _invalid(content["publication_id"], "line_duplicated")
    item = candidates[0]
    expected = (item[2], item[4], item[3], item[5]) if row.get("reverses_id") else (
        item[2], item[3], item[4], item[5]
    )
    if (row["account"], row["debit"], row["credit"], row["cashflow"]) != expected:
        _invalid(content["publication_id"], "line_content_mismatch")
    resolved = content.get("_runtime_resolution")
    if resolved is None:
        resolved = _resolution_from_content(content["resolution"])
        content["_runtime_resolution"] = resolved
    return item[6], resolved


def compare_open_contributions(engine, connection, *, check_bodies=True, verified_source=None):
    """Independently rebuild every formal result, including missing anchors."""

    if verified_source is None:
        from .integrity import _check_sources

        verified_source = _check_sources(engine, connection)
    records = {}
    # Source verification already checked every owned voucher and contiguous
    # line number. Reuse those exact rows without rereading all of them or
    # retaining another copy of the complete historical line collection.
    vouchers = defaultdict(list)
    for voucher in verified_source["vouchers"].values():
        if voucher["reverses_id"] is None:
            vouchers[voucher["calculation_id"]].append(voucher)

    def owned_lines(calculation_id):
        for voucher in sorted(vouchers[calculation_id], key=lambda item: item["id"]):
            for number, line in enumerate(voucher["lines"], start=1):
                yield {
                    "version_id": voucher["id"], "calculation_id": calculation_id,
                    "reverses_id": None, "line_no": number, **line,
                }
    expected = {
        row["id"]: prepare_open_contribution(
            engine,
            connection,
            row,
            verified_source=verified_source,
            records=records,
            publication_rows=owned_lines(row["calculation_id"]),
        )
        for row in connection.execute(
            "SELECT id,calculation_id FROM calculation_publication "
            "WHERE calculation_id IS NOT NULL ORDER BY sequence"
        )
    }
    anchors = {
        row["publication_id"]: row
        for row in connection.execute("SELECT * FROM report_open_contribution_anchor")
    }
    if set(anchors) != set(expected):
        _invalid("all", "anchor_coverage_mismatch")
    for ident, prepared in expected.items():
        anchor = anchors[ident]
        if (
            anchor["calculation_id"] != prepared.calculation_id
            or anchor["content_digest"] != prepared.checksum
        ):
            _invalid(ident, "anchor_rebuild_mismatch")
    if check_bodies:
        rows = {row["publication_id"]: row for row in connection.execute(
            "SELECT * FROM report_open_contribution"
        )}
        if set(rows) != set(expected):
            _invalid("all", "body_coverage_mismatch")
        for ident, prepared in expected.items():
            row = rows[ident]
            if row["content"] != prepared.content or row["content_digest"] != prepared.checksum:
                _invalid(ident, "body_rebuild_mismatch")
    return expected


def repair_open_contributions(engine, connection):
    """Restore only the repairable body after its immutable anchor checks."""

    expected = compare_open_contributions(engine, connection, check_bodies=False)
    current = {
        row["publication_id"]: row
        for row in connection.execute("SELECT * FROM report_open_contribution")
    }
    changed = {
        ident: row for ident, row in expected.items()
        if ident not in current
        or current[ident]["content"] != row.content
        or current[ident]["content_digest"] != row.checksum
    }
    if not changed and set(current) == set(expected):
        return 0
    connection.execute("DELETE FROM report_open_contribution")
    connection.executemany(
        "INSERT INTO report_open_contribution VALUES(?,?,?)",
        ((row.publication_id, row.content, row.checksum) for row in expected.values()),
    )
    return len(changed) + len(set(current) - set(expected))
