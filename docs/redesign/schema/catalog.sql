-- 新体系目录库初版设计。目录库仅定位公司，不保存公司账务。
PRAGMA foreign_keys = ON;
CREATE TABLE catalog_meta (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 format_family TEXT NOT NULL CHECK(format_family='ai-accounting-modular-catalog/1'),
 catalog_id TEXT NOT NULL UNIQUE CHECK(length(catalog_id)=36),
 schema_version INTEGER NOT NULL CHECK(schema_version=1),
 structure_fingerprint TEXT NOT NULL CHECK(length(structure_fingerprint)=64)
) STRICT;
CREATE TABLE owner_profile (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 owner_id TEXT NOT NULL UNIQUE CHECK(length(owner_id)=36),
 display_name TEXT NOT NULL,
 password_hash TEXT NOT NULL,
 recovery_hash TEXT
) STRICT;
CREATE TABLE company_location (
 company_id TEXT PRIMARY KEY CHECK(length(company_id)=36),
 credit_code TEXT NOT NULL UNIQUE CHECK(length(credit_code)=18),
 legal_name TEXT NOT NULL,
 company_root TEXT NOT NULL UNIQUE,
 database_path TEXT NOT NULL UNIQUE,
 database_id TEXT NOT NULL UNIQUE CHECK(length(database_id)=36),
 backup_directory TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('active','detached'))
) STRICT;
