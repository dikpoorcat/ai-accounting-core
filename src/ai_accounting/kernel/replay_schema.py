"""Durable per-company storage for typed replay plans, runs and receipts."""

# DDL is held as exact SQL text; keep its rows intact for its stored fingerprint.
# ruff: noqa: E501

REPLAY_DDL = """
CREATE TABLE replay_plan(
 id TEXT PRIMARY KEY,request_id TEXT NOT NULL UNIQUE,payload TEXT NOT NULL CHECK(json_valid(payload)),
 plan_digest BLOB NOT NULL UNIQUE CHECK(length(plan_digest)=32),
 catalog_id TEXT NOT NULL,company_id TEXT NOT NULL,database_id TEXT NOT NULL,
 first_period INTEGER NOT NULL CHECK(first_period BETWEEN 0 AND 119987),
 last_period INTEGER NOT NULL CHECK(last_period BETWEEN first_period AND 119987),
 source_manifest_digest BLOB NOT NULL CHECK(length(source_manifest_digest)=32),
 owner_confirmation BLOB NOT NULL REFERENCES evidence(digest),
 scope_digest BLOB NOT NULL CHECK(length(scope_digest)=32),
 schema_fingerprint TEXT NOT NULL,build_id TEXT NOT NULL,
 authority TEXT NOT NULL CHECK(json_valid(authority)),status TEXT NOT NULL
 CHECK(status IN('prepared','confirmed','stale','completed','blocked')),
 created_at TEXT NOT NULL) STRICT;
CREATE TABLE replay_run(
 id TEXT PRIMARY KEY,plan_id TEXT NOT NULL UNIQUE REFERENCES replay_plan(id),
 request_id TEXT NOT NULL UNIQUE,payload TEXT NOT NULL CHECK(json_valid(payload)),
 status TEXT NOT NULL CHECK(status IN('pending','running','completed','partial','blocked','failed')),
 checkpoint TEXT NOT NULL CHECK(json_valid(checkpoint)),
 current_step TEXT,started_at TEXT,finished_at TEXT,last_error TEXT,error_code TEXT,
 result TEXT CHECK(result IS NULL OR json_valid(result))) STRICT;
CREATE TABLE replay_step(
 run_id TEXT NOT NULL REFERENCES replay_run(id),position INTEGER NOT NULL CHECK(position>=0),
 step_id TEXT NOT NULL,command TEXT NOT NULL,payload TEXT NOT NULL CHECK(json_valid(payload)),
 request_id TEXT NOT NULL UNIQUE,request_digest BLOB CHECK(request_digest IS NULL OR length(request_digest)=32),
 dependencies TEXT NOT NULL CHECK(json_valid(dependencies)),output_ref TEXT,
 output_fields TEXT NOT NULL CHECK(json_valid(output_fields)),
 status TEXT NOT NULL CHECK(status IN('pending','running','completed','blocked','failed')),
 response TEXT CHECK(response IS NULL OR json_valid(response)),error_code TEXT,last_error TEXT,
 PRIMARY KEY(run_id,step_id),UNIQUE(run_id,position)) STRICT;
CREATE INDEX replay_step_ready ON replay_step(run_id,status,position);
CREATE TABLE replay_binding(
 run_id TEXT NOT NULL REFERENCES replay_run(id),reference TEXT NOT NULL,field TEXT NOT NULL,
 value TEXT NOT NULL CHECK(json_valid(value)),source_step TEXT NOT NULL,
 PRIMARY KEY(run_id,reference,field),
 FOREIGN KEY(run_id,source_step) REFERENCES replay_step(run_id,step_id)) STRICT;
"""


def replay_schema_sql():
    """Return stable DDL to be included in the current company schema bundle."""
    return REPLAY_DDL
