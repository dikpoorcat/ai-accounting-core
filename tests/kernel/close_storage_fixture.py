"""Synthetic damage helpers that preserve the physical close encoding.

Only tests may replace immutable close sources. Semantic corruption cases must
reach their original domain checker instead of failing on an obsolete encoding.
"""

import json

from ai_accounting.kernel.close_storage import decode_close, write_close
from ai_accounting.kernel.types import YearMonth


def stored_manifest(connection, period=None):
    if period is None:
        row = connection.execute(
            "SELECT * FROM period_close ORDER BY period DESC LIMIT 1"
        ).fetchone()
    else:
        row = connection.execute(
            "SELECT * FROM period_close WHERE period=?", (YearMonth(period).ordinal,)
        ).fetchone()
    return decode_close(connection, row)


def replace_stored_manifest(engine, manifest):
    period = YearMonth(manifest["period"]).ordinal
    tables = (
        "close_storage_block",
        "close_storage_directory",
        "close_storage_subroot",
        "close_storage_root",
        "period_close",
        "read_index_source",
    )
    with engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("PRAGMA defer_foreign_keys=ON")
        root = json.loads(
            connection.execute(
                "SELECT manifest FROM period_close WHERE period=?", (period,)
            ).fetchone()[0]
        )
        roots = {key: bytes.fromhex(value) for key, value in root["derived_roots"].items()}
        triggers = connection.execute(
            "SELECT name,sql FROM sqlite_schema WHERE type='trigger' "
            "AND tbl_name IN (SELECT value FROM json_each(?))",
            (json.dumps(tables),),
        ).fetchall()
        for name, _ in triggers:
            connection.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
        for table in tables[:-1]:
            connection.execute(f"DELETE FROM {table} WHERE period=?", (period,))
        digest = write_close(connection, period, manifest, projection_roots=roots)
        connection.execute(
            "UPDATE read_index_source SET source_digest=? "
            "WHERE source_kind='close' AND source_id=?",
            (digest, str(period)),
        )
        for _, sql in triggers:
            connection.execute(sql)
        connection.commit()
