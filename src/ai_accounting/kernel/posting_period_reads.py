"""Discover exact posting months through existing leading-period indexes."""

_TABLES = frozenset({
    "calculation_publication", "period_balance", "period_balance_seal",
    "settlement_change", "settlement_projection_seal",
})


def posting_periods(connection, tables, *, after=None, through=None):
    """Union real months, including orphan projections/seals, without reading rows.

    This is a locator, never a completeness proof. Every consumer must still
    authenticate the resulting periods through its ordinary integrity checks.
    """
    tables = tuple(tables)
    if not tables or any(table not in _TABLES for table in tables):
        raise ValueError("Unsupported posting-period table")
    clauses, parameters = [], []
    if after is not None:
        clauses.append("posting_period>?")
        parameters.append(after)
    if through is not None:
        clauses.append("posting_period<=?")
        parameters.append(through)
    scope = " AND ".join(clauses) or "1"
    result = set()
    for table in tables:
        result.update(row[0] for row in connection.execute(
            "WITH RECURSIVE months(period) AS ("
            f"SELECT min(posting_period) FROM {table} WHERE {scope} UNION ALL "
            f"SELECT (SELECT min(posting_period) FROM {table} "
            f"WHERE posting_period>m.period AND {scope}) "
            "FROM months m WHERE m.period IS NOT NULL) "
            "SELECT period FROM months WHERE period IS NOT NULL",
            parameters * 2,
        ))
    return result
