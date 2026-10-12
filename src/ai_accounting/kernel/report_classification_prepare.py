"""Prepare report classification only when typed evidence selects one voucher line."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from .types import canonical


def _line_key(row):
    if isinstance(row, Mapping):
        version, line = row.get("version_id"), row.get("line_no")
    else:
        version, line = row
    if not isinstance(version, str) or not version or type(line) is not int or line < 1:
        raise ValueError("line candidates must contain a voucher version and positive line number")
    return version, line


def resolve_unique_voucher_line(*, source, obligation, payment_allocation):
    """Intersect already typed lineage candidates; never infer account or debit/credit role.

    Each argument is the set of voucher lines identified by one independent domain
    relation. Empty or ambiguous intersections are returned as structured issues so
    a caller can preserve the unresolved item without inventing a classification.
    """
    groups = {
        "source": {_line_key(item) for item in source},
        "obligation": {_line_key(item) for item in obligation},
        "payment_allocation": {_line_key(item) for item in payment_allocation},
    }
    if any(not values for values in groups.values()):
        missing = [name for name, values in groups.items() if not values]
        return {
            "status": "needs_information",
            "line": None,
            "issues": [
                {"field": name, "code": "classification_lineage_missing"} for name in missing
            ],
        }
    candidates = set.intersection(*groups.values())
    if len(candidates) != 1:
        return {
            "status": "needs_information",
            "line": None,
            "issues": [
                {
                    "field": "report_classification.line_no",
                    "code": "classification_line_ambiguous"
                    if candidates
                    else "classification_line_conflict",
                    "candidate_count": len(candidates),
                }
            ],
        }
    version_id, line_no = next(iter(candidates))
    return {
        "status": "prepared",
        "line": {"version_id": version_id, "line_no": line_no},
        "issues": [],
    }


def prepare_report_classifications(items: Iterable[Mapping]):
    """Resolve a batch of source/obligation/allocation intersections."""
    result = []
    for item in items:
        if not isinstance(item, Mapping) or set(item) != {
            "source_id",
            "source_lines",
            "obligation_lines",
            "payment_allocation_lines",
        }:
            raise ValueError("classification inputs must carry typed lineage candidate sets")
        result.append(
            {
                "source_id": item["source_id"],
                **resolve_unique_voucher_line(
                    source=item["source_lines"],
                    obligation=item["obligation_lines"],
                    payment_allocation=item["payment_allocation_lines"],
                ),
            }
        )
    return result


def prepare_typed_report_classifications(engine, items: Iterable[Mapping]):
    """Resolve typed source/obligation/payment identities through verified reads.

    Callers supply domain identities only. Voucher versions, line numbers, and
    account roles are derived from the active publication and typed calculation
    results inside one QueryReads snapshot.
    """
    from .publication import active_tranches, verify_publication_chain
    from .query_reads import QueryReads, verify_current_publication_voucher_heads

    result = []
    with QueryReads.snapshot(engine) as reads:
        connection = reads.connection
        for item in items:
            required = {
                "source_id",
                "source_subject_id",
                "source_calculation_id",
                "obligation_key",
                "payment_revision_id",
                "payment_allocation_scope",
            }
            if not isinstance(item, Mapping) or set(item) != required:
                raise ValueError(
                    "classification requests must carry typed source, obligation "
                    "and allocation identities"
                )

            source_calc = reads.raw_calculations({item["source_calculation_id"]}).get(
                item["source_calculation_id"]
            )
            obligations = (
                (source_calc or {}).get("outcome", {}).get("values", {}).get("obligations", ())
            )
            selected_obligations = (
                [value for value in obligations if value.get("key") == item["obligation_key"]]
                if isinstance(obligations, (list, tuple))
                else []
            )
            valid_source = bool(
                source_calc
                and source_calc.get("subject_id") == item["source_subject_id"]
                and len(selected_obligations) == 1
            )

            payment_version = None
            allocation = None
            payment_version = reads.fact_version(item["payment_revision_id"])
            payment_fact = getattr(payment_version, "fact", None)
            if payment_fact is not None and getattr(payment_fact, "actual_payment", False):
                matches = [
                    candidate
                    for candidate in payment_fact.allocations
                    if candidate.scope == item["payment_allocation_scope"]
                ]
                allocation = matches[0] if len(matches) == 1 else None
            valid_allocation = bool(
                allocation
                and allocation.source_id == item["source_subject_id"]
                and allocation.obligation == item["obligation_key"]
            )

            payment_calc = None
            if valid_allocation and payment_version is not None:
                current = connection.execute(
                    "SELECT c.id FROM calculation_current h JOIN calculation c "
                    "ON c.id=h.calculation_id WHERE h.subject_id=? AND c.fact_id=?",
                    (payment_version.subject_id, item["payment_revision_id"]),
                ).fetchone()
                if current is not None:
                    payment_calc = reads.raw_calculations({current[0]}).get(current[0])
            payment_values = (payment_calc or {}).get("outcome", {}).get("values", {})
            payment_relations = payment_values.get("settlements", ())
            valid_payment_relation = any(
                isinstance(relation, Mapping)
                and relation.get("source_calculation") == item["source_calculation_id"]
                and relation.get("obligation") == item["obligation_key"]
                for relation in payment_relations
            )

            source_rows = []
            if valid_source:
                verify_publication_chain(connection, {item["source_subject_id"]})
                source_rows = [
                    row
                    for row in active_tranches(connection, subject_ids={item["source_subject_id"]})
                    if row.get("calculation_id") == item["source_calculation_id"]
                    and row.get("voucher_id")
                ]
                reads.verify_publication_records(source_rows)
                if source_rows:
                    verify_current_publication_voucher_heads(connection, source_rows)

            obligation_row = connection.execute(
                "SELECT 1 FROM settlement_change WHERE source_subject_id=? "
                "AND source_calculation_id=? AND obligation_key=? AND change_kind='source' LIMIT 1",
                (item["source_subject_id"], item["source_calculation_id"], item["obligation_key"]),
            ).fetchone()
            target_versions = set()
            if source_rows and obligation_row and valid_allocation and valid_payment_relation:
                voucher_ids = sorted({row["voucher_id"] for row in source_rows})
                target_versions.update(
                    row[0]
                    for row in connection.execute(
                        "SELECT h.version_id FROM json_each(?) ids "
                        "JOIN voucher_current h ON h.voucher_id=ids.value",
                        (canonical(voucher_ids),),
                    )
                )
            vouchers = reads.vouchers(target_versions) if target_versions else {}
            line_rows = reads.voucher_lines(target_versions) if target_versions else {}
            account = selected_obligations[0].get("account") if valid_source else None
            candidates = [
                {"version_id": version_id, "line_no": line["line_no"]}
                for version_id, lines in line_rows.items()
                for line in lines
                if isinstance(account, str)
                and line.get("account") == account
                and vouchers[version_id].get("calculation_id") == item["source_calculation_id"]
            ]
            resolved = resolve_unique_voucher_line(
                source=candidates,
                obligation=candidates if obligation_row else [],
                payment_allocation=candidates
                if valid_allocation and valid_payment_relation
                else [],
            )
            result.append({"source_id": item["source_id"], **resolved})
    return result
