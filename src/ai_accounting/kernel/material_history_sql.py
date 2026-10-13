"""Exact SQL projections and bounded identity checks for retained material bodies."""

from .contracts import KernelError
from .types import canonical

_PARENTS = frozenset({"fact_material_resolution_v2"})
_LINK_COLUMNS = ("revision_id", "item_no", "calculation_id", "amount_field", "amount_fen")
_OLD_BINDING = frozenset({"subject_id", "fact_kind", "fact_id", "recognition_period"})


def _columns(connection, table):
    return tuple(row[1] for row in connection.execute(f'PRAGMA table_info("{table}")'))


def material_relation(connection, table):
    """Union only declared body families, projecting shared columns explicitly."""
    parent = table.removesuffix("_links")
    if parent not in _PARENTS:
        return table
    historical = parent + "_pre_reference" + ("_links" if table.endswith("_links") else "")
    old_columns = _columns(connection, historical)
    if not old_columns:
        return table
    columns = _LINK_COLUMNS if table.endswith("_links") else _columns(connection, table)
    if not columns or not set(columns) <= set(old_columns):
        raise KernelError("content_integrity_failed", "历史资料正文公共字段不完整")
    fields = ",".join('"' + column + '"' for column in columns)
    return f"(SELECT {fields} FROM {table} UNION ALL SELECT {fields} FROM {historical})"


def verify_pre_reference_links(connection, *, revision_ids=(), calculation_ids=()):
    """Check old redundant identity fields only for the caller's exact selected scope.

    Missing calculations fail through LEFT JOIN. Ordinary candidate discovery
    does not claim a full-table proof; complete integrity still checks every body.
    """
    revisions, calculations = sorted(set(revision_ids)), sorted(set(calculation_ids))
    if not revisions and not calculations:
        return
    for parent in sorted(_PARENTS):
        if revisions and _columns(connection, parent + "_pre_reference"):
            duplicate = connection.execute(
                "SELECT ids.value FROM json_each(?) ids "
                f"CROSS JOIN {parent} current ON current.revision_id=ids.value "
                f"CROSS JOIN {parent}_pre_reference historical "
                "ON historical.revision_id=ids.value LIMIT 1",
                (canonical(revisions),),
            ).fetchone()
            if duplicate is not None:
                raise KernelError(
                    "content_integrity_failed",
                    "资料修订必须恰有一个合法正文",
                    component="material",
                    record_id=duplicate[0],
                )
        for table in (parent + "_links", parent + "_pre_reference_links"):
            columns = set(_columns(connection, table))
            if (
                columns
                and (table.endswith("_pre_reference_links") or _OLD_BINDING & columns)
                and not _OLD_BINDING <= columns
            ):
                raise KernelError("content_integrity_failed", "历史资料关联身份字段不完整")
            if not _OLD_BINDING <= columns:
                continue
            for field, identifiers in (
                ("revision_id", revisions),
                ("calculation_id", calculations),
            ):
                if not identifiers:
                    continue
                row = connection.execute(
                    "SELECT l.revision_id,l.item_no FROM json_each(?) ids "
                    f"CROSS JOIN {table} l ON l.{field}=ids.value "
                    "LEFT JOIN calculation c ON c.id=l.calculation_id "
                    "WHERE c.id IS NULL OR l.subject_id IS NOT c.subject_id "
                    "OR l.fact_kind IS NOT c.kind OR l.fact_id IS NOT c.fact_id "
                    "OR l.recognition_period IS NOT c.period LIMIT 1",
                    (canonical(identifiers),),
                ).fetchone()
                if row is not None:
                    raise KernelError(
                        "content_integrity_failed",
                        "历史资料关联的完整身份与精确计算不一致",
                        component="material_link",
                        record_id=row[0],
                    )
