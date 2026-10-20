"""Identity-owned SQL, restricted to one storage-owned transaction."""

from __future__ import annotations

from collections.abc import Sequence

from pypinyin import Style, lazy_pinyin

from ai_accounting_modular.contracts.public import canonical_json
from ai_accounting_modular.storage.public import RepositorySession


def sort_key(name: str) -> str:
    # Pinned pypinyin provides complete spelling; SQL adds original name and ID ties.
    return "".join(lazy_pinyin(name, style=Style.NORMAL)).casefold()


class IdentityRepository:
    def __init__(self, session: RepositorySession):
        self._session = session

    def get(self, ids: Sequence[str]) -> dict[str, dict]:
        if not ids:
            return {}
        rows = self._session.fetchall(
            "SELECT * FROM identity_object WHERE object_id IN (SELECT value FROM json_each(?))",
            (canonical_json(list(ids)),),
        )
        return {row["object_id"]: dict(row) for row in rows}

    def save(self, obj, expected_seq: int, command_id: str) -> None:
        self._session.execute(
            "INSERT INTO identity_object "
            "(object_id,kind,name,sort_key,seq,data_json,content_digest,"
            "latest_expected_seq,latest_command_id) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(object_id) DO UPDATE SET name=excluded.name,"
            "sort_key=excluded.sort_key,seq=excluded.seq,data_json=excluded.data_json,"
            "content_digest=excluded.content_digest,"
            "latest_expected_seq=excluded.latest_expected_seq,"
            "latest_command_id=excluded.latest_command_id",
            (
                obj["object_id"],
                obj["kind"],
                obj["name"],
                sort_key(obj["name"]),
                obj["seq"],
                canonical_json(obj),
                obj["content_digest"],
                expected_seq,
                command_id,
            ),
        )

    def page(self, kind: str, name_contains: str | None, after, limit: int):
        conditions, args = ["kind=?"], [kind]
        total = self._session.fetchone(
            "SELECT count(*) AS n FROM identity_object WHERE kind=?", (kind,)
        )["n"]
        if name_contains is not None:
            conditions.append("instr(name,?)>0")
            args.append(name_contains)
        where = " AND ".join(conditions)
        filtered = (
            total
            if name_contains is None
            else self._session.fetchone(
                "SELECT count(*) AS n FROM identity_object WHERE " + where, args
            )["n"]
        )
        if after is not None:
            where += " AND (sort_key,name,object_id)>(?,?,?)"
            args.extend(after)
        rows = self._session.fetchall(
            "SELECT * FROM identity_object WHERE "
            + where
            + " ORDER BY sort_key,name,object_id LIMIT ?",
            (*args, limit + 1),
        )
        return total, filtered, [dict(row) for row in rows]
