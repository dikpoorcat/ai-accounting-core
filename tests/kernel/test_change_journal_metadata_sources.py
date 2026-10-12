"""The journal keeps metadata, evidence, and immutable close sources distinct."""

import hashlib

from ai_accounting.kernel.change_journal import changes_since, head, verify_journal
from ai_accounting.kernel.types import digest
from test_materials import Company


def test_metadata_evidence_and_close_writes_append_typed_sources_atomically(tmp_path):
    company = Company(tmp_path)
    with company.engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        start = head(connection)
        evidence_digest = hashlib.sha256(b"synthetic extra evidence").digest()
        connection.execute(
            "INSERT INTO period_commentary_basis VALUES(?,?,?,?)",
            ("synthetic-commentary", "commentary-content-v1", "{}", evidence_digest),
        )
        connection.execute(
            "INSERT INTO evidence(digest,content,media_type,name) VALUES(?,?,?,?)",
            (evidence_digest, b"synthetic extra evidence", "text/plain", "extra.txt"),
        )
        connection.execute(
            "INSERT INTO company_note_revision(id,revision,text,digest,evidence_digest) "
            "VALUES('synthetic-note',1,'synthetic note',?,?)",
            (digest("synthetic note"), evidence_digest),
        )
        connection.execute(
            "INSERT INTO display_profile_revision(id,kind,entity_id,revision,display_name,"
            "purpose,note,source,evidence_digest,digest) "
            "VALUES('synthetic-profile','business','synthetic-subject',1,'Synthetic',NULL,NULL,"
            "'synthetic source',?,?)",
            (evidence_digest, digest("synthetic profile")),
        )
        connection.execute(
            "INSERT INTO entity(id,kind,account_type) VALUES('synthetic-entity','organization',NULL)"
        )
        connection.execute(
            "INSERT INTO entity_profile_revision VALUES(?,?,?,?,?,?,?)",
            (
                "synthetic-entity-profile", "synthetic-entity", 1, "{}", "synthetic source",
                evidence_digest, digest("synthetic entity profile"),
            ),
        )
        connection.execute(
            "INSERT INTO period_commentary_revision VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "synthetic-commentary", 2026 * 12, 1, "synthetic commentary",
                digest("commentary context"), None, "synthetic source", evidence_digest,
                digest("synthetic commentary"),
            ),
        )
        connection.execute(
            "INSERT INTO period_close(period,manifest,digest) VALUES(?,?,?)",
            (2026 * 12, "{}", digest("synthetic close")),
        )
        connection.commit()

    with company.engine.store.connection(read_only=True) as connection:
        events = changes_since(connection, start)
        assert [(event.source, event.target_id) for event in events] == [
            ("evidence", evidence_digest.hex()),
            ("company_note", "company"),
            ("display_profile", "business:synthetic-subject"),
            ("entity_profile", "synthetic-entity"),
            ("commentary", str(2026 * 12)),
            ("close", str(2026 * 12)),
        ]
        assert events[1].before_ref is None and events[1].after_ref == "synthetic-note"
        assert events[2].before_ref is None and events[2].after_ref == "synthetic-profile"
        assert events[3].before_ref is None and events[3].after_ref == "synthetic-entity-profile"
        assert events[4].before_ref is None and events[4].after_ref == "synthetic-commentary"
        assert events[5].before_ref is None
        assert events[5].after_ref == digest("synthetic close").hex()
        assert verify_journal(connection) == head(connection)
