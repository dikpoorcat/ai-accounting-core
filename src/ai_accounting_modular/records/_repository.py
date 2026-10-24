"""Only records-owned tables are visible to this repository."""

from __future__ import annotations

from ai_accounting_modular.contracts.public import (
    ContractError,
    canonical_json,
    content_digest,
    loads_json,
)
from ai_accounting_modular.storage.public import RepositorySession

from ._sources import issue_source_ids


def _source(row):
    value = {key: row[key] for key in ("source_id", "kind", "sha256")}
    if row["archive_month"] is not None:
        value["archive_month"] = row["archive_month"]
    if row["kind"] == "file":
        value.update(
            {key: row[key] for key in ("relative_path", "original_name", "media_type", "byte_size")}
        )
    else:
        value["text"] = row["statement_text"]
    return value


class RecordsRepository:
    def __init__(self, session: RepositorySession):
        self._session = session

    def sources(self, ids):
        if not ids:
            return {}
        rows = self._session.fetchall(
            "SELECT * FROM source WHERE source_id IN (SELECT value FROM json_each(?))",
            (canonical_json(list(ids)),),
        )
        return {row["source_id"]: _source(row) for row in rows}

    def all_sources(self):
        return tuple(
            _source(row)
            for row in self._session.fetchall("SELECT * FROM source ORDER BY source_id")
        )

    @staticmethod
    def _source_scope(first, last):
        clauses, values = [], []
        if first is not None:
            clauses.append("archive_month>=?")
            values.append(first)
        if last is not None:
            clauses.append("archive_month<=?")
            values.append(last)
        return clauses, values

    def source_query_metadata(self, first, last):
        clauses, values = self._source_scope(first, last)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return tuple(
            tuple(row)
            for row in self._session.fetchall(
                "SELECT source_id,kind,archive_month,relative_path,original_name,"
                "media_type,byte_size,sha256 "
                "FROM source" + where + " ORDER BY archive_month IS NULL,archive_month,source_id",
                values,
            )
        )

    def source_query_ids(self, first, last, after, limit):
        clauses, values = self._source_scope(first, last)
        if after is not None:
            month, source_id = after
            if month is None:
                clauses.append("(archive_month IS NULL AND source_id>?)")
                values.append(source_id)
            else:
                clauses.append(
                    "(archive_month IS NULL OR archive_month>? "
                    "OR (archive_month=? AND source_id>?))"
                )
                values.extend((month, month, source_id))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return tuple(
            row["source_id"]
            for row in self._session.fetchall(
                "SELECT source_id FROM source"
                + where
                + " ORDER BY archive_month IS NULL,archive_month,source_id LIMIT ?",
                (*values, limit),
            )
        )

    def save_source(self, source):
        self._session.execute(
            "INSERT INTO source(source_id,kind,archive_month,relative_path,original_name,"
            "media_type,byte_size,sha256,statement_text) "
            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET "
            "archive_month=excluded.archive_month,relative_path=excluded.relative_path,"
            "original_name=excluded.original_name,media_type=excluded.media_type",
            (
                source["source_id"],
                source["kind"],
                source.get("archive_month"),
                source.get("relative_path"),
                source.get("original_name"),
                source.get("media_type"),
                source.get("byte_size"),
                source["sha256"],
                source.get("text"),
            ),
        )

    def find_source_content(self, kind, digest):
        row = self._session.fetchone(
            "SELECT source_id FROM source WHERE kind=? AND sha256=? ORDER BY source_id LIMIT 1",
            (kind, digest),
        )
        return self.sources((row["source_id"],))[row["source_id"]] if row else None

    def uses(self, kind, key):
        return tuple(
            row["source_id"]
            for row in self._session.fetchall(
                "SELECT DISTINCT source_id FROM source_use WHERE owner_kind=? AND owner_key=? "
                "ORDER BY source_id",
                (kind, key),
            )
        )

    def used_source_ids(self):
        return tuple(
            row["source_id"]
            for row in self._session.fetchall(
                "SELECT DISTINCT source_id FROM source_use ORDER BY source_id"
            )
        )

    def referenced_source_ids(self, ids):
        if not ids:
            return ()
        return tuple(
            row["source_id"]
            for row in self._session.fetchall(
                "SELECT DISTINCT source_id FROM source_use "
                "WHERE source_id IN (SELECT value FROM json_each(?)) ORDER BY source_id",
                (canonical_json(list(ids)),),
            )
        )

    def delete_sources(self, ids):
        return self._session.execute(
            "DELETE FROM source WHERE source_id IN (SELECT value FROM json_each(?))",
            (canonical_json(list(ids)),),
        )

    def all_uses(self):
        return tuple(
            tuple(row)
            for row in self._session.fetchall(
                "SELECT owner_kind,owner_key,source_id FROM source_use "
                "ORDER BY owner_kind,owner_key,source_id"
            )
        )

    def issue_scopes(self):
        return tuple(
            row["scope_key"]
            for row in self._session.fetchall(
                "SELECT scope_key FROM current_issue_scope ORDER BY scope_key"
            )
        )

    def uses_many(self, kind, keys):
        if not keys:
            return {}
        rows = self._session.fetchall(
            "SELECT DISTINCT owner_key,source_id FROM source_use WHERE owner_kind=? "
            "AND owner_key IN (SELECT value FROM json_each(?)) ORDER BY owner_key,source_id",
            (kind, canonical_json(list(keys))),
        )
        result = {key: [] for key in keys}
        for row in rows:
            result[row["owner_key"]].append(row["source_id"])
        return result

    def issue_owners(self, ids):
        if not ids:
            return {}
        return {
            row["issue_id"]: row["scope_key"]
            for row in self._session.fetchall(
                "SELECT issue_id,scope_key FROM current_issue "
                "WHERE issue_id IN (SELECT value FROM json_each(?))",
                (canonical_json(list(ids)),),
            )
        }

    def replace_uses(self, kind, key, ids):
        self._session.execute(
            "DELETE FROM source_use WHERE owner_kind=? AND owner_key=?", (kind, key)
        )
        self._session.executemany(
            "INSERT INTO source_use(owner_kind,owner_key,source_id) VALUES(?,?,?)",
            ((kind, key, source_id) for source_id in ids),
        )

    def notes(self, scopes=None):
        where = "" if scopes is None else " WHERE scope_key IN (SELECT value FROM json_each(?))"
        args = () if scopes is None else (canonical_json(list(scopes)),)
        result = {}
        for row in self._session.fetchall(
            "SELECT * FROM company_note" + where + " ORDER BY scope_key", args
        ):
            result[row["scope_key"]] = dict(row)
        return result

    def period_note_metadata(self, first, last):
        return tuple(
            tuple(row)
            for row in self._session.fetchall(
                "SELECT scope_key,seq,content_digest FROM company_note "
                "WHERE scope_key BETWEEN ? AND ? ORDER BY scope_key",
                ("period:" + first, "period:" + last),
            )
        )

    def period_note_rows(self, first, last, after, limit):
        return tuple(
            dict(row)
            for row in self._session.fetchall(
                "SELECT * FROM company_note WHERE scope_key BETWEEN ? AND ? "
                "AND scope_key>? ORDER BY scope_key LIMIT ?",
                ("period:" + first, "period:" + last, after or "", limit),
            )
        )

    def save_note(self, current, expected_seq, command_id, digest):
        self._session.execute(
            "INSERT INTO company_note(scope_key,note_text,basis_head,state,seq,latest_command_id,"
            "latest_expected_seq,content_digest,note_format) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(scope_key) DO UPDATE SET "
            "note_text=excluded.note_text,basis_head=excluded.basis_head,state=excluded.state,seq=excluded.seq,"
            "latest_command_id=excluded.latest_command_id,"
            "latest_expected_seq=excluded.latest_expected_seq,content_digest=excluded.content_digest,"
            "note_format=excluded.note_format",
            (
                current["scope"],
                current["text"],
                current["basis_head"],
                current["state"],
                current["seq"],
                command_id,
                expected_seq,
                digest,
                current.get("text_format", "plain_text"),
            ),
        )

    def issue_scope(self, scope):
        row = self._session.fetchone(
            "SELECT * FROM current_issue_scope WHERE scope_key=?", (scope,)
        )
        return dict(row) if row else None

    def issues(self, scope=None):
        where, args = ("", ()) if scope is None else (" WHERE scope_key=?", (scope,))
        rows = self._session.fetchall(
            "SELECT * FROM current_issue" + where + " ORDER BY scope_key,issue_id", args
        )
        return self._issues(rows)

    def _issues(self, rows):
        uses = self.uses_many("issue", [row["issue_id"] for row in rows])
        result = []
        for row in rows:
            issue = {
                "issue_id": row["issue_id"],
                "scope": row["scope_key"],
                "evidence_ids": loads_json(row["evidence_ids_json"]),
                "missing_fields": loads_json(row["missing_fields_json"]),
                "question": row["current_question"],
                "assignee": row["assignee"],
                "action": row["action"],
            }
            for key in ("period", "business_id", "due_on"):
                if row[key] is not None:
                    issue[key] = row[key]
            if row["known_fact_json"] is not None:
                issue["known_fact"] = loads_json(row["known_fact_json"])
            if (
                content_digest(issue) != row["content_digest"]
                or row["known_schema_key"] != issue.get("known_fact", {}).get("kind")
                or tuple(uses[row["issue_id"]]) != issue_source_ids(issue)
            ):
                raise ContractError("storage_corrupt", "当前待办正文、来源或摘要不一致。")
            result.append(issue)
        return result

    def period_issue_metadata(self, first, last):
        return tuple(
            tuple(row)
            for row in self._session.fetchall(
                "SELECT period,scope_key,issue_id,content_digest FROM current_issue "
                "WHERE period IS NULL OR period BETWEEN ? AND ? "
                "ORDER BY period,scope_key,issue_id",
                (first, last),
            )
        )

    def period_issue_rows(self, first, last, after, limit):
        if after is None:
            where, args = "(i.period IS NULL OR i.period BETWEEN ? AND ?)", (first, last)
        elif after[0] is None:
            where = (
                "((i.period IS NULL AND (i.scope_key,i.issue_id)>(?,?)) "
                "OR i.period BETWEEN ? AND ?)"
            )
            args = (*after[1:], first, last)
        else:
            where = "i.period BETWEEN ? AND ? AND (i.period,i.scope_key,i.issue_id)>(?,?,?)"
            args = (first, last, *after)
        rows = self._session.fetchall(
            "SELECT i.*,s.head AS scope_head FROM current_issue AS i "
            "LEFT JOIN current_issue_scope AS s ON s.scope_key=i.scope_key WHERE "
            + where
            + " ORDER BY i.period,i.scope_key,i.issue_id LIMIT ?",
            (*args, limit),
        )
        if any(row["scope_head"] is None for row in rows):
            raise ContractError("storage_corrupt", "当前待办缺少所属范围的完整版本。")
        return self._issues(rows), {row["scope_key"]: row["scope_head"] for row in rows}

    def replace_issues(self, scope, head, expected_head, command_id, digest, issues):
        old_ids = [item["issue_id"] for item in self.issues(scope)]
        for issue_id in old_ids:
            self.replace_uses("issue", issue_id, ())
        self._session.execute("DELETE FROM current_issue WHERE scope_key=?", (scope,))
        for issue in issues:
            self._session.execute(
                "INSERT INTO current_issue(issue_id,scope_key,period,business_id,known_schema_key,"
                "known_fact_json,missing_fields_json,current_question,assignee,action,due_on,"
                "content_digest,evidence_ids_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    issue["issue_id"],
                    scope,
                    issue.get("period"),
                    issue.get("business_id"),
                    issue.get("known_fact", {}).get("kind"),
                    canonical_json(issue["known_fact"]) if "known_fact" in issue else None,
                    canonical_json(issue["missing_fields"]),
                    issue["question"],
                    issue["assignee"],
                    issue["action"],
                    issue.get("due_on"),
                    content_digest(issue),
                    canonical_json(issue["evidence_ids"]),
                ),
            )
            self.replace_uses("issue", issue["issue_id"], issue_source_ids(issue))
        self._session.execute(
            "INSERT INTO current_issue_scope VALUES(?,?,?,?,?) ON CONFLICT(scope_key) "
            "DO UPDATE SET "
            "head=excluded.head,latest_command_id=excluded.latest_command_id,"
            "latest_expected_head=excluded.latest_expected_head,content_digest=excluded.content_digest",
            (scope, head, command_id, expected_head, digest),
        )

    def targets(self):
        return [
            dict(row)
            for row in self._session.fetchall(
                "SELECT * FROM document_shard_head ORDER BY shard_key"
            )
        ]

    def mark_targets(self, keys, head):
        self._session.executemany(
            "INSERT INTO document_shard_head(shard_key,wanted_head) VALUES(?,?) "
            "ON CONFLICT(shard_key) DO UPDATE SET wanted_head=excluded.wanted_head,"
            "last_error_code=NULL,last_error_message=NULL",
            ((key, head) for key in keys),
        )

    def request_documents(self, keys, head):
        self._session.executemany(
            "INSERT INTO document_shard_head(shard_key,wanted_head) VALUES(?,?) "
            "ON CONFLICT(shard_key) DO UPDATE SET wanted_head=excluded.wanted_head,"
            "published_head=NULL,last_error_code=NULL,last_error_message=NULL",
            ((key, head) for key in keys),
        )

    def document_set(self):
        row = self._session.fetchone("SELECT * FROM document_set WHERE singleton=1")
        return dict(row) if row else None

    def confirm_documents(self, targets, manifest):
        shards = {shard["key"]: shard for shard in manifest["shards"]}
        for target in targets:
            shard = shards.get(target.key, {})
            self._session.execute(
                "UPDATE document_shard_head SET published_head=?,current_path=?,current_digest=?,"
                "last_error_code=NULL,last_error_message=NULL WHERE shard_key=? AND wanted_head=?",
                (
                    target.wanted_head,
                    shard.get("path"),
                    shard.get("sha256"),
                    target.key,
                    target.wanted_head,
                ),
            )
        self._session.execute(
            "INSERT INTO document_set VALUES(1,?,'standard/current.json') ON CONFLICT(singleton) "
            "DO UPDATE SET published_set_id=excluded.published_set_id,"
            "manifest_path=excluded.manifest_path",
            (manifest["set_id"],),
        )

    def document_error(self, keys, code, message, expected_heads):
        self._session.executemany(
            "UPDATE document_shard_head SET last_error_code=?,last_error_message=? "
            "WHERE shard_key=? AND wanted_head=? "
            "AND (published_head IS NULL OR wanted_head>published_head)",
            ((code, message, key, expected_heads[key]) for key in keys),
        )
