-- owner: records
-- 公司结构1至2的唯一增量；由storage绑定精确起止合同在事务中执行。
-- 此文件用于生成运行资源，不应手工对公司库执行。
ALTER TABLE company_note ADD COLUMN note_format TEXT NOT NULL DEFAULT 'plain_text'
  CHECK(note_format IN ('plain_text', 'markdown'))
  CHECK(note_format='plain_text' OR scope_key='company');

-- owner: storage
-- v1系统头固定CHECK=1，采用保留原行的事务内换表，不能只改指纹。
CREATE TABLE company_system_meta_next (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 format_family TEXT NOT NULL CHECK(format_family='ai-accounting-modular/1'),
 database_id TEXT NOT NULL UNIQUE CHECK(length(database_id)=36),
 schema_version INTEGER NOT NULL CHECK(schema_version=2),
 structure_fingerprint TEXT NOT NULL CHECK(length(structure_fingerprint)=64),
 global_seq INTEGER NOT NULL DEFAULT 0 CHECK(global_seq>=0)
) STRICT;
INSERT INTO company_system_meta_next
 SELECT singleton,format_family,database_id,2,structure_fingerprint,global_seq FROM system_meta;
DROP TABLE system_meta;
ALTER TABLE company_system_meta_next RENAME TO system_meta;
