# ruff: noqa: E501 -- SQL text is fingerprinted; wrapping released DDL changes the contract.
"""Forward-only asset membership storage, independent of legacy voucher contracts."""

ASSET_BATCH_DDL = """
CREATE TABLE asset_batch_member(
 owner_calculation_id TEXT NOT NULL REFERENCES calculation(id) DEFERRABLE INITIALLY DEFERRED,
 position INTEGER NOT NULL CHECK(position>0), asset_id TEXT NOT NULL,
 member_subject_id TEXT NOT NULL REFERENCES subject(id),
 member_fact_id TEXT NOT NULL REFERENCES fact_revision(id),
 member_calculation_id TEXT NOT NULL REFERENCES calculation(id),
 result_digest BLOB NOT NULL CHECK(length(result_digest)=32),
 summary TEXT NOT NULL CHECK(json_valid(summary)),
 line_start INTEGER, line_count INTEGER NOT NULL CHECK(line_count>=0),
 CHECK((line_count=0 AND line_start IS NULL) OR(line_count>0 AND line_start>0)),
 PRIMARY KEY(owner_calculation_id,position),
 UNIQUE(owner_calculation_id,member_subject_id)) STRICT;
CREATE INDEX asset_batch_member_calculation ON asset_batch_member(member_calculation_id,owner_calculation_id);
CREATE INDEX asset_batch_member_subject ON asset_batch_member(member_subject_id,owner_calculation_id);
CREATE INDEX asset_batch_member_asset ON asset_batch_member(asset_id,owner_calculation_id);
CREATE TRIGGER immutable_asset_batch_member_update BEFORE UPDATE ON asset_batch_member
 BEGIN SELECT RAISE(ABORT,'immutable asset batch member'); END;
CREATE TRIGGER immutable_asset_batch_member_delete BEFORE DELETE ON asset_batch_member
 BEGIN SELECT RAISE(ABORT,'immutable asset batch member'); END;
CREATE TRIGGER sealed_asset_batch_member BEFORE INSERT ON asset_batch_member
 WHEN EXISTS(SELECT 1 FROM calculation_seal WHERE calculation_id=NEW.owner_calculation_id)
 BEGIN SELECT RAISE(ABORT,'sealed asset batch'); END;
CREATE TRIGGER asset_member_identity BEFORE INSERT ON asset_batch_member WHEN NOT EXISTS(
 SELECT 1 FROM calculation c JOIN calculation o ON o.id=NEW.owner_calculation_id
 WHERE c.id=NEW.member_calculation_id AND c.subject_id=NEW.member_subject_id
 AND c.fact_id=NEW.member_fact_id AND c.digest=NEW.result_digest AND c.period=o.period
 AND json_extract(c.outcome,'$.values.asset_id')=NEW.asset_id
 AND ((c.kind='asset_activation' AND o.kind='asset_activation_batch')
 OR(c.kind='asset_consumption' AND o.kind='asset_consumption_month')))
 BEGIN SELECT RAISE(ABORT,'asset member identity mismatch'); END;
CREATE TRIGGER asset_member_owner BEFORE INSERT ON asset_batch_member WHEN EXISTS(
 SELECT 1 FROM asset_batch_member m JOIN calculation a ON a.id=m.owner_calculation_id
 JOIN calculation b ON b.id=NEW.owner_calculation_id
 WHERE m.member_calculation_id=NEW.member_calculation_id AND a.subject_id<>b.subject_id)
 BEGIN SELECT RAISE(ABORT,'asset member already belongs to another batch'); END;
CREATE TRIGGER asset_member_no_publication BEFORE INSERT ON calculation_publication
 WHEN EXISTS(SELECT 1 FROM calculation WHERE id=NEW.calculation_id
 AND kind IN('asset_activation','asset_consumption'))
 BEGIN SELECT RAISE(ABORT,'asset card requires batch publication'); END;
CREATE TRIGGER asset_member_seal BEFORE INSERT ON calculation_seal
 WHEN EXISTS(SELECT 1 FROM calculation WHERE id=NEW.calculation_id
 AND kind IN('asset_activation','asset_consumption')) AND NOT EXISTS(
 SELECT 1 FROM asset_batch_member WHERE member_calculation_id=NEW.calculation_id)
 BEGIN SELECT RAISE(ABORT,'orphan asset member'); END;
CREATE TRIGGER asset_owner_seal BEFORE INSERT ON calculation_seal
 WHEN EXISTS(SELECT 1 FROM calculation o WHERE o.id=NEW.calculation_id
 AND o.kind IN('asset_activation_batch','asset_consumption_month') AND (
 json_type(o.outcome,'$.values.member_count') IS NOT 'integer' OR
 json_extract(o.outcome,'$.values.member_count')<>(SELECT count(*) FROM asset_batch_member
 WHERE owner_calculation_id=o.id) OR EXISTS(SELECT 1 FROM asset_batch_member m
 LEFT JOIN calculation_seal s ON s.calculation_id=m.member_calculation_id
 WHERE m.owner_calculation_id=o.id AND s.calculation_id IS NULL)))
 BEGIN SELECT RAISE(ABORT,'incomplete asset batch'); END;
"""
