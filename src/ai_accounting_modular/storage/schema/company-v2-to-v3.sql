-- 公司结构2至3：扩展受控文件任务种类，原任务及所有业务资料原样保留。
-- 仅由storage核对精确父合同时在同一事务内执行。
CREATE TABLE file_task_next (
task_id TEXT PRIMARY KEY CHECK(length(task_id)=36),
 kind TEXT NOT NULL CHECK(kind IN ('backup','report_export','payment_export')),
 state TEXT NOT NULL CHECK(state IN ('pending','running','succeeded','failed','expired')),
 scope_key TEXT NOT NULL,
 scope_head TEXT NOT NULL CHECK(length(scope_head)=64),
 creator_request_id TEXT NOT NULL CHECK(length(creator_request_id) BETWEEN 1 AND 500),
 attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 3),
 capture_id TEXT CHECK(capture_id IS NULL OR length(capture_id)=36),
 plan_schema TEXT,
 plan_json TEXT CHECK(plan_json IS NULL OR json_valid(plan_json)),
 output_path TEXT,
 output_sha256 TEXT CHECK(output_sha256 IS NULL OR length(output_sha256)=64),
 output_bytes INTEGER CHECK(output_bytes>=0),
 error_code TEXT CHECK(error_code IS NULL OR length(error_code) BETWEEN 1 AND 100),
 error_message TEXT CHECK(error_message IS NULL OR length(error_message) BETWEEN 1 AND 2000),
 UNIQUE(kind,scope_key),
 CHECK((kind='backup' AND capture_id IS NOT NULL) OR (kind IN ('report_export','payment_export') AND capture_id IS NULL)),
 CHECK((state='expired' AND plan_schema IS NULL AND plan_json IS NULL)
    OR (state<>'expired' AND plan_schema IS NOT NULL AND plan_json IS NOT NULL)),
 CHECK((state='succeeded' AND output_path IS NOT NULL AND output_sha256 IS NOT NULL AND output_bytes IS NOT NULL)
    OR (state<>'succeeded' AND output_path IS NULL AND output_sha256 IS NULL AND output_bytes IS NULL)),
 CHECK((error_code IS NULL)=(error_message IS NULL)),
 CHECK(state NOT IN ('failed','expired') OR error_code IS NOT NULL),
 CHECK(state<>'succeeded' OR error_code IS NULL)
) STRICT;
INSERT INTO file_task_next SELECT * FROM file_task;
DROP TABLE file_task;
ALTER TABLE file_task_next RENAME TO file_task;
CREATE INDEX file_task_scope ON file_task(kind,scope_key,state,task_id);

CREATE TABLE company_system_meta_next (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 format_family TEXT NOT NULL CHECK(format_family='ai-accounting-modular/1'),
 database_id TEXT NOT NULL UNIQUE CHECK(length(database_id)=36),
 schema_version INTEGER NOT NULL CHECK(schema_version=3),
 structure_fingerprint TEXT NOT NULL CHECK(length(structure_fingerprint)=64),
 global_seq INTEGER NOT NULL DEFAULT 0 CHECK(global_seq>=0)
) STRICT;
INSERT INTO company_system_meta_next
 SELECT singleton,format_family,database_id,3,structure_fingerprint,global_seq FROM system_meta;
DROP TABLE system_meta;
ALTER TABLE company_system_meta_next RENAME TO system_meta;
