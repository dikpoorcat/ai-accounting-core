"""Authority anchors and durable work for independently produced owner views."""

# ruff: noqa: E501

PROJECTION_DDL = """
CREATE TABLE dashboard_projection_publication(
 id TEXT PRIMARY KEY CHECK(length(id)=64), period TEXT NOT NULL,
 store_id TEXT NOT NULL, model_id BLOB NOT NULL CHECK(length(model_id)=32),
 root_digest BLOB NOT NULL CHECK(length(root_digest)=32),
 source_sequence INTEGER NOT NULL CHECK(source_sequence>=0),
 basis TEXT NOT NULL CHECK(json_valid(basis)),
 dependencies TEXT NOT NULL CHECK(json_valid(dependencies)),
 input_digest BLOB NOT NULL CHECK(length(input_digest)=32),
 created_at TEXT NOT NULL DEFAULT(strftime('%Y-%m-%dT%H:%M:%fZ','now'))
) STRICT;
CREATE INDEX dashboard_projection_publication_period
 ON dashboard_projection_publication(period,source_sequence);
CREATE TRIGGER immutable_dashboard_projection_publication_update
 BEFORE UPDATE ON dashboard_projection_publication
 BEGIN SELECT RAISE(ABORT,'immutable projection publication'); END;
CREATE TRIGGER immutable_dashboard_projection_publication_delete
 BEFORE DELETE ON dashboard_projection_publication
 BEGIN SELECT RAISE(ABORT,'immutable projection publication'); END;
CREATE TABLE dashboard_projection_current(
 period TEXT PRIMARY KEY,
 publication_id TEXT NOT NULL REFERENCES dashboard_projection_publication(id),
 validated_sequence INTEGER NOT NULL CHECK(validated_sequence>=0)
) STRICT;
CREATE TABLE dashboard_projection_request(
 period TEXT PRIMARY KEY, target_sequence INTEGER NOT NULL CHECK(target_sequence>=0),
 status TEXT NOT NULL CHECK(status IN('pending','running','ready','failed')),
 priority INTEGER NOT NULL DEFAULT 0 CHECK(priority>=0),
 worker_id TEXT, lease_until_ms INTEGER NOT NULL DEFAULT 0 CHECK(lease_until_ms>=0),
 attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0), error_code TEXT,
 updated_at TEXT NOT NULL DEFAULT(strftime('%Y-%m-%dT%H:%M:%fZ','now'))
) STRICT;
CREATE TABLE dashboard_projection_reset(
 sequence INTEGER PRIMARY KEY CHECK(sequence>0),
 publication_through INTEGER NOT NULL CHECK(publication_through>=0),
 reason TEXT NOT NULL CHECK(reason IN('restore','verified_repair')),
 created_at TEXT NOT NULL DEFAULT(strftime('%Y-%m-%dT%H:%M:%fZ','now'))
) STRICT;
CREATE TRIGGER immutable_dashboard_projection_reset_update BEFORE UPDATE ON dashboard_projection_reset
 BEGIN SELECT RAISE(ABORT,'immutable projection reset'); END;
CREATE TRIGGER immutable_dashboard_projection_reset_delete BEFORE DELETE ON dashboard_projection_reset
 BEGIN SELECT RAISE(ABORT,'immutable projection reset'); END;
CREATE TABLE operation_timing(
 request_id TEXT NOT NULL REFERENCES request(id),
 stage TEXT NOT NULL CHECK(stage IN('queue','calculation','write','authorization','commit','projection')),
 elapsed_ms INTEGER NOT NULL CHECK(elapsed_ms>=0),
 created_at TEXT NOT NULL DEFAULT(strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 PRIMARY KEY(request_id,stage)
) STRICT;
CREATE TRIGGER immutable_operation_timing_update BEFORE UPDATE ON operation_timing
 BEGIN SELECT RAISE(ABORT,'immutable operation timing'); END;
CREATE TRIGGER immutable_operation_timing_delete BEFORE DELETE ON operation_timing
 BEGIN SELECT RAISE(ABORT,'immutable operation timing'); END;
"""
