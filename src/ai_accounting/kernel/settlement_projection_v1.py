"""Retained v1 read-only settlement contribution and seal verification."""

import hashlib
import json
from dataclasses import dataclass

from .contracts import KernelError
from .history_reads_v1 import V1Reads as QueryReads
from .query_relations_v1 import SETTLEMENT_SOURCE_SLOTS, resolve_calculation_relations

_STANDARD_QUERY_READS = QueryReads

_SETTLEMENT_KINDS = frozenset(
    {*SETTLEMENT_SOURCE_SLOTS, "settlement", "overpayment", "opening_package"}
)


@dataclass(frozen=True)
class _VerifiedProjection:
    connection: object
    expected: tuple[tuple, ...]
    lease: object


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).digest()


def checked(value):
    if type(value) is not int or not -(2**63) <= value < 2**63:
        raise ValueError("v1 settlement amount outside signed 64-bit cents")
    return value


def _active_tranches(connection, *, subject_ids=None):
    restriction = (
        " WHERE subject_id IN (SELECT value FROM json_each(?))" if subject_ids is not None else ""
    )
    parameters = (canonical(sorted(subject_ids)),) if subject_ids is not None else ()
    rows = [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM calculation_publication" + restriction + " ORDER BY sequence",
            parameters,
        )
    ]
    active = {}
    for row in rows:
        segments = active.setdefault(row["subject_id"], [])
        if row["mode"] in ("initial", "closed_correction"):
            segments.append(row)
        elif row["mode"] in ("open_replace", "review_no_impact"):
            if not segments:
                raise ValueError("v1 publication missing segment")
            segments[-1] = row
        elif row["mode"] == "withdrawn":
            if not segments:
                raise ValueError("v1 publication missing segment")
            segments.pop()
    return [row for segments in active.values() for row in segments]


def _may_contribute_settlement(calculation, known_kinds):
    kind = calculation.get("kind")
    if kind in _SETTLEMENT_KINDS or kind == "opening_identity_binding" or kind not in known_kinds:
        return True
    outcome = calculation.get("outcome")
    if not isinstance(outcome, dict) or not isinstance(outcome.get("values"), dict):
        return True
    values = outcome["values"]
    if "obligations" not in values:
        return False
    obligations = values["obligations"]
    return not isinstance(obligations, list) or bool(obligations)


def _projection_calculations(reads, identifiers, verified_calculations):
    """Keep all roots and publication identities in the same v1 read transaction."""

    if verified_calculations is None:
        return reads.raw_calculations(identifiers), True
    if not identifiers <= verified_calculations.keys():
        return reads.prime_raw_calculations(
            identifiers, verified_calculations=verified_calculations
        ), False
    records = {
        row["id"]: row
        for row in reads.connection.execute(
            "SELECT c.id,c.subject_id,c.kind,c.fact_id,c.digest,p.id publication_id "
            "FROM json_each(?) ids JOIN calculation c ON c.id=ids.value "
            "JOIN fact_revision f ON f.id=c.fact_id "
            "LEFT JOIN calculation_publication p ON p.calculation_id=c.id",
            (canonical(sorted(identifiers)),),
        )
    }
    if set(records) != identifiers:
        raise KernelError("unknown_calculation", "历史核算版本不存在")
    result = {}
    for ident, row in records.items():
        source = verified_calculations[ident]
        if row["publication_id"] is None and row["kind"] not in {
            "asset_activation", "asset_consumption"
        }:
            raise KernelError("unknown_calculation", "历史核算未正式发布或采用")
        if (
            bytes(row["digest"]) != bytes(source["digest"])
            or row["kind"] != source["kind"]
            or row["fact_id"] != source["fact_id"]
            or row["subject_id"] != source["subject_id"]
        ):
            raise KernelError("content_integrity_failed", "已核验历史核算身份不一致")
        result[ident] = {
            "id": ident,
            "kind": source["kind"],
            "outcome": source["decoded"],
            "result_digest": bytes(row["digest"]).hex(),
        }
    return result, True


_COLUMNS = (
    "publication_id",
    "item_no",
    "posting_period",
    "obligation_key",
    "source_subject_id",
    "category",
    "account",
    "counterparty_id",
    "component",
    "change_kind",
    "amount",
    "state",
    "source_calculation_id",
    "source_digest",
)


def _relation_rows(calculation, relation, sign):
    """Return stable semantic contributions made by one calculation."""

    obligations = {
        item.get("key"): item
        for item in relation.get("obligations", ())
        if isinstance(item.get("key"), str) and item.get("key")
    }
    result = []
    for item in obligations.values():
        if item.get("source_calculation_id") != calculation["id"]:
            continue
        amount = item.get("amount_fen")
        result.append(
            {
                "obligation_key": item["key"],
                "source_subject_id": (item.get("source_business") or {}).get("subject_id"),
                "category": item.get("category"),
                "account": item.get("account"),
                "counterparty_id": item.get("creditor_id") or item.get("counterparty_id"),
                "component": item.get("name"),
                "change_kind": "source",
                "amount": checked(sign * amount) if type(amount) is int else None,
                "state": item.get("state", "unresolved"),
            }
        )
    for item in relation.get("settlements", ()):
        key = item.get("obligation_key")
        source = obligations.get(key, {})
        amount = item.get("amount_fen")
        result.append(
            {
                "obligation_key": key if isinstance(key, str) and key else None,
                "source_subject_id": (item.get("source_business") or {}).get("subject_id"),
                "category": source.get("category"),
                "account": source.get("account"),
                "counterparty_id": (
                    item.get("creditor_id")
                    or source.get("creditor_id")
                    or source.get("counterparty_id")
                ),
                "component": item.get("obligation_name") or source.get("name"),
                "change_kind": "payment" if item.get("mode") == "payment" else "other",
                "amount": checked(sign * amount) if type(amount) is int else None,
                "state": item.get("state", "unresolved"),
            }
        )
    return result


def expected_settlement_projection(
    engine,
    connection,
    *,
    periods=None,
    subject_ids=None,
    verified_calculations=None,
):
    """Derive exact contribution rows without consulting current projections."""

    selected_periods = None if periods is None else set(periods)
    if selected_periods is not None and subject_ids is None:
        subject_ids = {
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT subject_id FROM calculation_publication "
                "WHERE posting_period IN (SELECT value FROM json_each(?))",
                (canonical(sorted(selected_periods)),),
            )
        }
    tranches = [
        item
        for item in _active_tranches(connection, subject_ids=subject_ids)
        if selected_periods is None or item["posting_period"] in selected_periods
    ]
    calculation_ids = {
        ident
        for item in tranches
        for ident in (item.get("calculation_id"), item.get("baseline_calculation_id"))
        if ident is not None
    }
    reads = QueryReads(engine, connection)
    calculations, filter_relations = (
        _projection_calculations(reads, calculation_ids, verified_calculations)
        if calculation_ids
        else ({}, True)
    )
    relation_ids = {
        ident
        for ident, calculation in calculations.items()
        if not filter_relations
        or type(reads) is not _STANDARD_QUERY_READS
        or _may_contribute_settlement(calculation, engine.store.registry.models)
    }
    if relation_ids:
        calculations.update(
            reads.prime_raw_calculations(
                relation_ids, verified_calculations=verified_calculations
            )
        )
    relations = (
        reads.relations_many(relation_ids, resolver=resolve_calculation_relations, raw=True)
        if relation_ids
        else {}
    )
    binding_source_ids = {
        calculation["outcome"]["values"]["source_calculation_id"]
        for calculation in calculations.values()
        if calculation["kind"] == "opening_identity_binding"
    }
    binding_sources = reads.raw_calculations(binding_source_ids) if binding_source_ids else {}
    rows = []
    for tranche in tranches:
        contributions = []
        for field, sign in (("baseline_calculation_id", -1), ("calculation_id", 1)):
            ident = tranche.get(field)
            if ident is None:
                continue
            calculation = calculations[ident]
            if calculation["kind"] == "opening_identity_binding":
                values = calculation["outcome"]["values"]
                original = binding_sources[values["source_calculation_id"]]["outcome"]["values"]
                selected_values = [(original, -sign)]
                if not values.get("superseded"):
                    selected_values.append((values["basis_values"], sign))
                for source_values, direction in selected_values:
                    for obligation in source_values.get("obligations", ()):
                        amount = obligation.get("amount_fen")
                        contributions.append(
                            (
                                {
                                    "obligation_key": obligation["key"],
                                    "source_subject_id": values["source_subject_id"],
                                    "category": obligation.get("category"),
                                    "account": obligation.get("account"),
                                    "counterparty_id": obligation.get("counterparty_id"),
                                    "component": obligation.get("name"),
                                    "change_kind": "source",
                                    "amount": checked(direction * amount)
                                    if type(amount) is int
                                    else None,
                                    "state": "resolved"
                                    if obligation.get("counterparty_id")
                                    else "unresolved",
                                },
                                ident,
                                bytes.fromhex(calculation["result_digest"]),
                            )
                        )
                continue
            contributions.extend(
                (
                    item,
                    ident,
                    bytes.fromhex(calculation["result_digest"]),
                )
                for item in _relation_rows(
                    calculation, relations.get(ident, {"obligations": (), "settlements": ()}), sign
                )
            )
        contributions.sort(
            key=lambda item: canonical(
                {
                    **item[0],
                    "source_calculation_id": item[1],
                    "source_digest": item[2].hex(),
                }
            )
        )
        for item_no, (item, calculation_id, result_digest) in enumerate(contributions, 1):
            rows.append(
                (
                    tranche["id"],
                    item_no,
                    tranche["posting_period"],
                    item["obligation_key"],
                    item["source_subject_id"],
                    item["category"],
                    item["account"],
                    item["counterparty_id"],
                    item["component"],
                    item["change_kind"],
                    item["amount"],
                    item["state"],
                    calculation_id,
                    result_digest,
                )
            )
    return sorted(rows, key=lambda row: (row[2], row[0], row[1]))


def _publication_periods(connection, periods=None):
    query = "SELECT DISTINCT posting_period FROM calculation_publication"
    parameters = []
    if periods is not None:
        query += " WHERE posting_period IN (SELECT value FROM json_each(?))"
        parameters.append(json.dumps(sorted(set(periods))))
    return {row[0] for row in connection.execute(query, parameters)}


def _sealed(connection, rows, periods):
    grouped = {period: [] for period in periods}
    for row in rows:
        grouped.setdefault(row[2], []).append(row)
    publications = {period: [] for period in periods}
    for row in connection.execute(
        "SELECT posting_period,id FROM calculation_publication WHERE posting_period IN "
        "(SELECT value FROM json_each(?)) ORDER BY posting_period,sequence",
        (canonical(sorted(periods)),),
    ):
        publications.setdefault(row["posting_period"], []).append(row["id"])
    return {
        period: (
            len(values),
            digest(
                {
                    "publications": publications.get(period, []),
                    "rows": [list(value[:-1]) + [value[-1].hex()] for value in values],
                }
            ),
        )
        for period, values in grouped.items()
    }


def compare_settlement_projection(engine, connection, *, verified_calculations=None):
    periods = _publication_periods(connection)
    expected = expected_settlement_projection(
        engine, connection, verified_calculations=verified_calculations
    )
    actual = [
        tuple(row)
        for row in connection.execute(
            f"SELECT {','.join(_COLUMNS)} FROM settlement_change ORDER BY 3,1,2"
        )
    ]
    expected_seals = _sealed(connection, expected, periods)
    actual_seals = {
        row[0]: (row[1], bytes(row[2]))
        for row in connection.execute(
            "SELECT posting_period,row_count,digest FROM settlement_projection_seal"
        )
    }
    return {
        "changed": actual != expected or actual_seals != expected_seals,
        "expected": expected,
        "expected_seals": expected_seals,
        "actual_rows": len(actual),
    }


def require_settlement_projection(
    engine, connection, *, verified_calculations=None, _return_verified=False
):
    compared = compare_settlement_projection(
        engine, connection, verified_calculations=verified_calculations
    )
    if compared["changed"]:
        raise KernelError(
            "content_integrity_failed",
            "清偿读取投影与正式发布来源不一致，需要显式维修",
            component="settlement_projection",
            record_id="*",
            reason="projection_mismatch",
        )
    if _return_verified:
        from .verified_source_lease import current_verified_lease

        return _VerifiedProjection(
            connection, tuple(compared["expected"]), current_verified_lease(connection)
        )
    return {"rows": compared["actual_rows"], "periods": len(compared["expected_seals"])}
