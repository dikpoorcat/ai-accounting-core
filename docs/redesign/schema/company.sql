-- 新体系公司库初版设计；仅设计产物，不是对现有库执行的迁移。
PRAGMA foreign_keys = ON;

-- owner: storage
CREATE TABLE system_meta (
singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 format_family TEXT NOT NULL CHECK(format_family='ai-accounting-modular/1'),
 database_id TEXT NOT NULL UNIQUE CHECK(length(database_id)=36),
 schema_version INTEGER NOT NULL CHECK(schema_version=1),
 structure_fingerprint TEXT NOT NULL CHECK(length(structure_fingerprint)=64),
 global_seq INTEGER NOT NULL DEFAULT 0 CHECK(global_seq>=0)
) STRICT;

-- owner: storage
CREATE TABLE company_profile (
singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 company_id TEXT NOT NULL UNIQUE CHECK(length(company_id)=36),
 credit_code TEXT NOT NULL UNIQUE CHECK(length(credit_code)=18),
 legal_name TEXT NOT NULL CHECK(length(legal_name) BETWEEN 1 AND 200),
 accounting_standard TEXT NOT NULL CHECK(accounting_standard='small_enterprise'),
 currency TEXT NOT NULL CHECK(currency='CNY'),
 start_period TEXT NOT NULL CHECK(length(start_period)=7),
 profile_seq INTEGER NOT NULL DEFAULT 1 CHECK(profile_seq>=1)
) STRICT;

-- owner: storage
CREATE TABLE module_schema (
module_key TEXT PRIMARY KEY,
 version INTEGER NOT NULL CHECK(version>=1),
 contract_digest TEXT NOT NULL CHECK(length(contract_digest)=64),
 parent_digest TEXT CHECK(parent_digest IS NULL OR length(parent_digest)=64),
 sql_fingerprint TEXT NOT NULL CHECK(length(sql_fingerprint)=64)
) STRICT;

-- owner: identity
CREATE TABLE identity_object (
object_id TEXT PRIMARY KEY CHECK(length(object_id)=36),
 kind TEXT NOT NULL CHECK(kind IN ('person','organization','bank_account','cash_account','platform_account','project','investment_product','asset')),
 name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 200),
 sort_key TEXT NOT NULL,
 seq INTEGER NOT NULL CHECK(seq>=1),
 data_json TEXT NOT NULL CHECK(json_valid(data_json)),
 content_digest TEXT NOT NULL CHECK(length(content_digest)=64),
 latest_expected_seq INTEGER NOT NULL CHECK(latest_expected_seq>=0),
 latest_command_id TEXT NOT NULL CHECK(length(latest_command_id)=36)
) STRICT;

CREATE INDEX identity_object_kind_sort ON identity_object(kind,sort_key,name,object_id);

-- owner: application
CREATE TABLE business_head (
business_id TEXT PRIMARY KEY CHECK(length(business_id)=36),
 family TEXT NOT NULL CHECK(family IN ('operations','people','assets','financing','investment','money','taxes','opening','reports')),
 kind TEXT NOT NULL,
 natural_key TEXT,
 seq INTEGER NOT NULL CHECK(seq>=1),
 state TEXT NOT NULL CHECK(state IN ('active','withdrawn')),
 period TEXT,
 archive_month TEXT,
 current_digest TEXT CHECK(current_digest IS NULL OR length(current_digest)=64),
 latest_op TEXT NOT NULL CHECK(latest_op IN ('create','replace','withdraw','restore')),
 latest_expected_seq INTEGER NOT NULL CHECK(latest_expected_seq>=0),
 latest_command_id TEXT NOT NULL CHECK(length(latest_command_id)=36),
 latest_content_digest TEXT NOT NULL CHECK(length(latest_content_digest)=64),
 CHECK((state='active' AND period IS NOT NULL AND current_digest IS NOT NULL) OR (state='withdrawn' AND current_digest IS NULL))
) STRICT;

CREATE INDEX business_head_family_period ON business_head(family,state,period,business_id);

CREATE INDEX business_head_kind_period ON business_head(kind,state,period,business_id);

CREATE UNIQUE INDEX business_head_natural ON business_head(kind,natural_key) WHERE state='active' AND natural_key IS NOT NULL;

-- owner: domains.operations
CREATE TABLE op_fact (
business_id TEXT PRIMARY KEY REFERENCES business_head(business_id) ON DELETE RESTRICT,
 schema_key TEXT NOT NULL,
 effective_on TEXT,
 main_object_id TEXT REFERENCES identity_object(object_id),
 data_json TEXT NOT NULL CHECK(json_valid(data_json))
) STRICT;

CREATE INDEX op_fact_object ON op_fact(main_object_id,business_id);

-- owner: domains.people
CREATE TABLE people_fact (
business_id TEXT PRIMARY KEY REFERENCES business_head(business_id) ON DELETE RESTRICT,
 schema_key TEXT NOT NULL,
 effective_on TEXT,
 main_object_id TEXT REFERENCES identity_object(object_id),
 data_json TEXT NOT NULL CHECK(json_valid(data_json))
) STRICT;

CREATE INDEX people_fact_object ON people_fact(main_object_id,business_id);

-- owner: domains.assets
CREATE TABLE asset_fact (
business_id TEXT PRIMARY KEY REFERENCES business_head(business_id) ON DELETE RESTRICT,
 schema_key TEXT NOT NULL,
 effective_on TEXT,
 main_object_id TEXT REFERENCES identity_object(object_id),
 data_json TEXT NOT NULL CHECK(json_valid(data_json))
) STRICT;

CREATE INDEX asset_fact_object ON asset_fact(main_object_id,business_id);

-- owner: domains.financing
CREATE TABLE financing_fact (
business_id TEXT PRIMARY KEY REFERENCES business_head(business_id) ON DELETE RESTRICT,
 schema_key TEXT NOT NULL,
 effective_on TEXT,
 main_object_id TEXT REFERENCES identity_object(object_id),
 data_json TEXT NOT NULL CHECK(json_valid(data_json))
) STRICT;

CREATE INDEX financing_fact_object ON financing_fact(main_object_id,business_id);

-- owner: domains.financing
CREATE TABLE investment_fact (
business_id TEXT PRIMARY KEY REFERENCES business_head(business_id) ON DELETE RESTRICT,
 schema_key TEXT NOT NULL,
 effective_on TEXT,
 main_object_id TEXT REFERENCES identity_object(object_id),
 data_json TEXT NOT NULL CHECK(json_valid(data_json))
) STRICT;

CREATE INDEX investment_fact_object ON investment_fact(main_object_id,business_id);

-- owner: money
CREATE TABLE money_fact (
business_id TEXT PRIMARY KEY REFERENCES business_head(business_id) ON DELETE RESTRICT,
 schema_key TEXT NOT NULL,
 effective_on TEXT,
 main_object_id TEXT REFERENCES identity_object(object_id),
 data_json TEXT NOT NULL CHECK(json_valid(data_json))
) STRICT;

CREATE INDEX money_fact_object ON money_fact(main_object_id,business_id);

-- owner: domains.taxes
CREATE TABLE tax_fact (
business_id TEXT PRIMARY KEY REFERENCES business_head(business_id) ON DELETE RESTRICT,
 schema_key TEXT NOT NULL,
 effective_on TEXT,
 main_object_id TEXT REFERENCES identity_object(object_id),
 data_json TEXT NOT NULL CHECK(json_valid(data_json))
) STRICT;

CREATE INDEX tax_fact_object ON tax_fact(main_object_id,business_id);

-- owner: ledger.opening
CREATE TABLE opening_fact (
business_id TEXT PRIMARY KEY REFERENCES business_head(business_id) ON DELETE RESTRICT,
 schema_key TEXT NOT NULL,
 effective_on TEXT,
 main_object_id TEXT REFERENCES identity_object(object_id),
 data_json TEXT NOT NULL CHECK(json_valid(data_json))
) STRICT;

CREATE INDEX opening_fact_object ON opening_fact(main_object_id,business_id);

-- owner: ledger.reporting
CREATE TABLE report_basis_fact (
business_id TEXT PRIMARY KEY REFERENCES business_head(business_id) ON DELETE RESTRICT,
 schema_key TEXT NOT NULL,
 effective_on TEXT,
 main_object_id TEXT REFERENCES identity_object(object_id),
 data_json TEXT NOT NULL CHECK(json_valid(data_json))
) STRICT;

CREATE INDEX report_basis_fact_object ON report_basis_fact(main_object_id,business_id);

-- owner: records
CREATE TABLE source (
source_id TEXT PRIMARY KEY CHECK(length(source_id)=36),
 kind TEXT NOT NULL CHECK(kind IN ('file','owner_statement')),
 archive_month TEXT,
 relative_path TEXT,
 original_name TEXT,
 media_type TEXT,
 byte_size INTEGER CHECK(byte_size>=0),
 sha256 TEXT NOT NULL CHECK(length(sha256)=64),
 statement_text TEXT,
 CHECK((kind='file' AND relative_path IS NOT NULL AND original_name IS NOT NULL AND byte_size IS NOT NULL AND statement_text IS NULL) OR (kind='owner_statement' AND statement_text IS NOT NULL AND relative_path IS NULL))
) STRICT;

CREATE INDEX source_digest ON source(kind,sha256,source_id);

CREATE UNIQUE INDEX source_path ON source(relative_path) WHERE relative_path IS NOT NULL;

-- owner: records
CREATE TABLE source_use (
owner_kind TEXT NOT NULL CHECK(owner_kind IN ('business','object','issue','note')),
 owner_key TEXT NOT NULL,
 source_id TEXT NOT NULL REFERENCES source(source_id) ON DELETE RESTRICT,
 location TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(owner_kind,owner_key,source_id,location)
) STRICT;

CREATE INDEX source_use_reverse ON source_use(source_id,owner_kind,owner_key);

-- owner: records
CREATE TABLE current_issue (
issue_id TEXT PRIMARY KEY CHECK(length(issue_id)=36),
 scope_key TEXT NOT NULL,
 period TEXT,
 business_id TEXT,
 known_schema_key TEXT,
 known_fact_json TEXT CHECK(known_fact_json IS NULL OR json_valid(known_fact_json)),
 missing_fields_json TEXT NOT NULL CHECK(json_valid(missing_fields_json)),
 current_question TEXT NOT NULL,
 assignee TEXT NOT NULL CHECK(assignee IN ('ai','owner')),
 action TEXT NOT NULL CHECK(action IN ('provide_material','clarify_reality','external_payment','external_filing','professional_check')),
 due_on TEXT,
 content_digest TEXT NOT NULL CHECK(length(content_digest)=64)
) STRICT;

CREATE INDEX current_issue_scope_idx ON current_issue(scope_key,period,issue_id);

-- owner: records
CREATE TABLE current_issue_scope (
scope_key TEXT PRIMARY KEY,
 head INTEGER NOT NULL CHECK(head>=0),
 latest_command_id TEXT,
 latest_expected_head INTEGER NOT NULL CHECK(latest_expected_head>=0),
 content_digest TEXT NOT NULL CHECK(length(content_digest)=64)
) STRICT;

-- owner: records
CREATE TABLE company_note (
scope_key TEXT PRIMARY KEY,
 note_text TEXT,
 basis_head TEXT,
 state TEXT NOT NULL CHECK(state IN ('current','needs_refresh')),
 seq INTEGER NOT NULL CHECK(seq>=1),
 latest_command_id TEXT NOT NULL CHECK(length(latest_command_id)=36),
 latest_expected_seq INTEGER NOT NULL CHECK(latest_expected_seq>=0),
 content_digest TEXT NOT NULL CHECK(length(content_digest)=64),
 CHECK((state='current' AND note_text IS NOT NULL AND length(note_text)>0) OR (state='needs_refresh' AND note_text IS NULL))
) STRICT;

-- owner: application
CREATE TABLE application_state (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 working_period_to TEXT NOT NULL CHECK(length(working_period_to)=7)
) STRICT;

-- owner: application
CREATE TABLE current_dependency (
consumer_business_id TEXT NOT NULL REFERENCES business_head(business_id),
 consumer_result_slot TEXT NOT NULL,
 producer_kind TEXT NOT NULL CHECK(producer_kind IN ('business','object','scope','calendar')),
 producer_id TEXT NOT NULL CHECK(
  (producer_kind IN ('business','object') AND length(producer_id)=36) OR
  (producer_kind='scope' AND length(producer_id) BETWEEN 1 AND 200) OR
  (producer_kind='calendar' AND producer_id='work')
 ),
 producer_public_slot TEXT NOT NULL,
 read_mode TEXT NOT NULL CHECK(read_mode IN ('fact','result','collection')),
 relation_kind TEXT NOT NULL CHECK(relation_kind IN ('reference','calculation','settlement','cost','identity','policy')),
 period_from TEXT,
 period_to TEXT,
 PRIMARY KEY(consumer_business_id,consumer_result_slot,producer_kind,producer_id,producer_public_slot,read_mode,relation_kind)
) STRICT;

CREATE INDEX current_dependency_reverse ON current_dependency(producer_kind,producer_id,producer_public_slot,read_mode,relation_kind,consumer_business_id);

-- owner: ledger
CREATE TABLE adopted_result (
result_id TEXT PRIMARY KEY CHECK(length(result_id)=36),
 business_id TEXT NOT NULL REFERENCES business_head(business_id),
 result_slot TEXT NOT NULL,
 generation INTEGER NOT NULL CHECK(generation>=1),
 business_period TEXT NOT NULL,
 posting_period TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('open','frozen')),
 target_state TEXT NOT NULL CHECK(target_state IN ('active','withdrawn')),
 baseline_result_id TEXT REFERENCES adopted_result(result_id),
 adopted_fact_seq INTEGER NOT NULL CHECK(adopted_fact_seq>=1),
 calculation_key TEXT NOT NULL,
 calculation_revision INTEGER NOT NULL CHECK(calculation_revision>=1),
 basis_schema TEXT NOT NULL,
 basis_json TEXT NOT NULL CHECK(json_valid(basis_json)),
 target_schema TEXT NOT NULL,
 target_json TEXT NOT NULL CHECK(json_valid(target_json)),
 basis_digest TEXT NOT NULL CHECK(length(basis_digest)=64),
 target_digest TEXT NOT NULL CHECK(length(target_digest)=64)
) STRICT;

CREATE INDEX adopted_result_period ON adopted_result(posting_period,state,business_id,result_slot);

CREATE INDEX adopted_result_calculation ON adopted_result(calculation_key,calculation_revision,state,result_id);

-- owner: ledger
CREATE TABLE result_slot_current (
business_id TEXT NOT NULL REFERENCES business_head(business_id),
 result_slot TEXT NOT NULL,
 head_result_id TEXT NOT NULL UNIQUE REFERENCES adopted_result(result_id) DEFERRABLE INITIALLY DEFERRED,
 generation INTEGER NOT NULL CHECK(generation>=1),
 PRIMARY KEY(business_id,result_slot)
) STRICT;

-- owner: ledger
CREATE TABLE result_source (
result_id TEXT NOT NULL REFERENCES adopted_result(result_id) ON DELETE RESTRICT,
 source_id TEXT NOT NULL REFERENCES source(source_id) ON DELETE RESTRICT,
 location TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(result_id,source_id,location)
) STRICT;

CREATE INDEX result_source_reverse ON result_source(source_id,result_id);

-- owner: ledger
CREATE TABLE voucher (
voucher_id TEXT PRIMARY KEY CHECK(length(voucher_id)=36),
 result_id TEXT NOT NULL REFERENCES adopted_result(result_id) ON DELETE RESTRICT,
 role TEXT NOT NULL CHECK(role IN ('original','reversal','replacement','reclassification','closing')),
 period TEXT NOT NULL,
 number INTEGER NOT NULL CHECK(number>=1),
 summary TEXT NOT NULL,
 reverses_voucher_id TEXT REFERENCES voucher(voucher_id),
 sealed INTEGER NOT NULL DEFAULT 0 CHECK(sealed IN (0,1)),
 UNIQUE(period,number),
 UNIQUE(result_id,role)
) STRICT;

-- owner: ledger
CREATE TABLE voucher_line (
voucher_id TEXT NOT NULL REFERENCES voucher(voucher_id) ON DELETE CASCADE,
 line_no INTEGER NOT NULL CHECK(line_no>=1),
 account_code TEXT NOT NULL,
 debit_fen INTEGER NOT NULL CHECK(debit_fen>=0),
 credit_fen INTEGER NOT NULL CHECK(credit_fen>=0),
 party_id TEXT REFERENCES identity_object(object_id),
 object_id TEXT REFERENCES identity_object(object_id),
 summary TEXT NOT NULL,
 CHECK((debit_fen>0 AND credit_fen=0) OR (credit_fen>0 AND debit_fen=0)),
 PRIMARY KEY(voucher_id,line_no)
) STRICT;

CREATE INDEX voucher_line_account ON voucher_line(account_code,voucher_id,line_no);

CREATE TRIGGER voucher_seal_balance BEFORE UPDATE OF sealed ON voucher
WHEN NEW.sealed=1 AND (NOT EXISTS(SELECT 1 FROM voucher_line WHERE voucher_id=NEW.voucher_id)
 OR (SELECT COALESCE(SUM(debit_fen),0)-COALESCE(SUM(credit_fen),0) FROM voucher_line WHERE voucher_id=NEW.voucher_id)<>0)
BEGIN SELECT RAISE(ABORT,'unbalanced_voucher'); END;

-- owner: ledger
CREATE TABLE voucher_sequence (
period TEXT PRIMARY KEY,
 next_number INTEGER NOT NULL CHECK(next_number>=1)
) STRICT;

-- owner: ledger
CREATE TABLE obligation (
result_id TEXT NOT NULL REFERENCES adopted_result(result_id),
 public_slot TEXT NOT NULL,
 party_id TEXT REFERENCES identity_object(object_id),
 direction TEXT NOT NULL CHECK(direction IN ('receivable','payable')),
 category TEXT NOT NULL,
 amount_fen INTEGER NOT NULL CHECK(amount_fen>=0),
 account_code TEXT NOT NULL,
 cashflow_weights_json TEXT CHECK(cashflow_weights_json IS NULL OR json_valid(cashflow_weights_json)),
 allowed_settlements_json TEXT NOT NULL CHECK(json_valid(allowed_settlements_json)),
 business_period TEXT NOT NULL,
 due_on TEXT,
 PRIMARY KEY(result_id,public_slot)
) STRICT;

CREATE INDEX obligation_party ON obligation(party_id,business_period,result_id,public_slot);

-- owner: ledger
CREATE TABLE settlement (
result_id TEXT NOT NULL REFERENCES adopted_result(result_id),
 settlement_id TEXT NOT NULL CHECK(length(settlement_id)=36),
 source_business_id TEXT NOT NULL REFERENCES business_head(business_id),
 source_public_slot TEXT NOT NULL,
 consumer_public_slot TEXT NOT NULL,
 period TEXT NOT NULL,
 amount_fen INTEGER NOT NULL CHECK(amount_fen>0),
 method TEXT NOT NULL CHECK(method IN ('money','offset','agency')),
 movement_id TEXT REFERENCES money_occurrence(occurrence_id),
 agency_business_id TEXT REFERENCES business_head(business_id),
 agency_public_slot TEXT,
 actual_on TEXT,
 CHECK((method='money' AND movement_id IS NOT NULL AND agency_business_id IS NULL AND agency_public_slot IS NULL)
    OR (method='agency' AND movement_id IS NULL AND agency_business_id IS NOT NULL AND agency_public_slot='agency')
    OR (method='offset' AND movement_id IS NULL AND agency_business_id IS NULL AND agency_public_slot IS NULL)),
 PRIMARY KEY(result_id,settlement_id)
) STRICT;

CREATE INDEX settlement_source ON settlement(source_business_id,source_public_slot,period,result_id);

-- owner: ledger
CREATE TABLE obligation_reclassification (
result_id TEXT NOT NULL REFERENCES adopted_result(result_id),
 relation_id TEXT NOT NULL CHECK(length(relation_id)=36),
 mode TEXT NOT NULL CHECK(mode IN ('transfer','reclassify_excess')),
 source_business_id TEXT NOT NULL REFERENCES business_head(business_id),
 source_public_slot TEXT NOT NULL,
 target_business_id TEXT NOT NULL REFERENCES business_head(business_id),
 target_public_slot TEXT NOT NULL,
 amount_fen INTEGER NOT NULL CHECK(amount_fen>0),
 PRIMARY KEY(result_id,relation_id)
) STRICT;

CREATE INDEX obligation_reclassification_source ON obligation_reclassification(source_business_id,source_public_slot,result_id);

CREATE INDEX obligation_reclassification_target ON obligation_reclassification(target_business_id,target_public_slot,result_id);

-- owner: ledger
CREATE TABLE cost_lot (
result_id TEXT NOT NULL REFERENCES adopted_result(result_id),
 public_slot TEXT NOT NULL,
 object_id TEXT NOT NULL REFERENCES identity_object(object_id),
 category TEXT NOT NULL CHECK(category IN ('project','asset','money_fund','investment')),
 amount_fen INTEGER NOT NULL CHECK(amount_fen>=0),
 business_period TEXT NOT NULL,
 PRIMARY KEY(result_id,public_slot)
) STRICT;

-- owner: ledger
CREATE TABLE cost_consumption (
result_id TEXT NOT NULL REFERENCES adopted_result(result_id),
 consumer_public_slot TEXT NOT NULL,
 source_business_id TEXT NOT NULL REFERENCES business_head(business_id),
 source_public_slot TEXT NOT NULL,
 period TEXT NOT NULL,
 amount_fen INTEGER NOT NULL CHECK(amount_fen>0),
 PRIMARY KEY(result_id,consumer_public_slot,source_business_id,source_public_slot)
) STRICT;

CREATE INDEX cost_consumption_source ON cost_consumption(source_business_id,source_public_slot,period,result_id);

-- owner: money
CREATE TABLE money_occurrence (
occurrence_id TEXT PRIMARY KEY CHECK(length(occurrence_id)=36),
 business_id TEXT NOT NULL REFERENCES business_head(business_id),
 public_slot TEXT NOT NULL,
 UNIQUE(business_id,public_slot)
) STRICT;

-- owner: ledger
CREATE TABLE result_fund_movement (
result_id TEXT NOT NULL REFERENCES adopted_result(result_id),
 movement_id TEXT NOT NULL REFERENCES money_occurrence(occurrence_id),
 public_slot TEXT NOT NULL,
 account_id TEXT NOT NULL REFERENCES identity_object(object_id),
 actual_on TEXT NOT NULL,
 period TEXT NOT NULL,
 direction TEXT NOT NULL CHECK(direction IN ('in','out')),
 amount_fen INTEGER NOT NULL CHECK(amount_fen>0),
 transfer_group_id TEXT,
 party_id TEXT REFERENCES identity_object(object_id),
 cashflow_parts_json TEXT CHECK(cashflow_parts_json IS NULL OR json_valid(cashflow_parts_json)),
 memo TEXT,
 PRIMARY KEY(result_id,movement_id),
 UNIQUE(result_id,public_slot)
) STRICT;

CREATE INDEX result_fund_movement_account_date ON result_fund_movement(account_id,period,actual_on,movement_id,result_id);

-- owner: ledger
CREATE TABLE result_tax_sale (
result_id TEXT NOT NULL REFERENCES adopted_result(result_id),
 item_no INTEGER NOT NULL CHECK(item_no>=1),
 source_business_id TEXT NOT NULL REFERENCES business_head(business_id),
 tax_period TEXT NOT NULL,
 net_sales_fen INTEGER NOT NULL,
 vat_fen INTEGER NOT NULL,
 nominal_rate TEXT NOT NULL,
 tax_base_rate TEXT NOT NULL,
 payable_rate TEXT NOT NULL,
 exemption_eligible INTEGER NOT NULL CHECK(exemption_eligible IN (0,1)),
 filing_bucket TEXT NOT NULL CHECK(filing_bucket IN ('standard','used_asset')),
 invoice_kind TEXT NOT NULL CHECK(invoice_kind IN ('ordinary','special','uninvoiced','unestablished')),
 ranges_json TEXT NOT NULL CHECK(json_valid(ranges_json)),
 PRIMARY KEY(result_id,item_no)
) STRICT;

CREATE INDEX result_tax_sale_period ON result_tax_sale(tax_period,source_business_id,result_id,item_no);

-- owner: ledger
CREATE TABLE result_report_contribution (
result_id TEXT NOT NULL REFERENCES adopted_result(result_id),
 statement TEXT NOT NULL CHECK(statement='profit'),
 line INTEGER NOT NULL CHECK(line IN (6,10,12,13,15,16,17,19)),
 detail_code TEXT NOT NULL CHECK(detail_code IN ('management_startup','management_entertainment','management_research','management_other','sales_merchandise_repair','sales_advertising_promotion','sales_other','finance_interest','finance_other','tax_urban','tax_education','tax_local_education')),
 amount_fen INTEGER NOT NULL,
 PRIMARY KEY(result_id,statement,line,detail_code)
) STRICT;

-- owner: money
CREATE TABLE bank_statement_row (
row_id TEXT PRIMARY KEY CHECK(length(row_id)=36),
 business_id TEXT NOT NULL REFERENCES business_head(business_id),
 account_id TEXT NOT NULL REFERENCES identity_object(object_id),
 source_id TEXT NOT NULL REFERENCES source(source_id),
 source_location TEXT NOT NULL,
 reference TEXT,
 actual_on TEXT NOT NULL,
 period TEXT NOT NULL,
 direction TEXT NOT NULL CHECK(direction IN ('in','out')),
 amount_fen INTEGER NOT NULL CHECK(amount_fen>0),
 memo TEXT,
 counterparty_text TEXT,
 UNIQUE(business_id,source_location)
) STRICT;

CREATE INDEX bank_statement_row_scope ON bank_statement_row(account_id,period,actual_on,row_id);

CREATE UNIQUE INDEX bank_statement_row_reference ON bank_statement_row(business_id,reference) WHERE reference IS NOT NULL;

-- owner: money
CREATE TABLE platform_activity_row (
row_id TEXT PRIMARY KEY CHECK(length(row_id)=36),
 business_id TEXT NOT NULL REFERENCES business_head(business_id),
 account_id TEXT NOT NULL REFERENCES identity_object(object_id),
 source_id TEXT NOT NULL REFERENCES source(source_id),
 source_location TEXT NOT NULL,
 reference TEXT,
 actual_on TEXT NOT NULL,
 period TEXT NOT NULL,
 direction TEXT NOT NULL CHECK(direction IN ('in','out')),
 amount_fen INTEGER NOT NULL CHECK(amount_fen>0),
 memo TEXT,
 counterparty_text TEXT,
 UNIQUE(business_id,source_location)
) STRICT;

CREATE INDEX platform_activity_row_scope ON platform_activity_row(account_id,period,actual_on,row_id);

CREATE UNIQUE INDEX platform_activity_row_reference ON platform_activity_row(business_id,reference) WHERE reference IS NOT NULL;

-- owner: money
CREATE TABLE money_reconciliation (
group_id TEXT PRIMARY KEY CHECK(length(group_id)=36),
 business_id TEXT NOT NULL REFERENCES business_head(business_id),
 account_id TEXT NOT NULL REFERENCES identity_object(object_id),
 direction TEXT NOT NULL CHECK(direction IN ('in','out')),
 amount_fen INTEGER NOT NULL CHECK(amount_fen>0)
) STRICT;

-- owner: money
CREATE TABLE money_reconciliation_member (
group_id TEXT NOT NULL REFERENCES money_reconciliation(group_id) ON DELETE CASCADE,
 member_kind TEXT NOT NULL CHECK(member_kind IN ('bank_row','platform_row','occurrence')),
 member_id TEXT NOT NULL CHECK(length(member_id)=36),
 PRIMARY KEY(group_id,member_kind,member_id),
 UNIQUE(member_kind,member_id)
) STRICT;

-- owner: ledger.opening
CREATE TABLE opening_member (
member_id TEXT PRIMARY KEY CHECK(length(member_id)=36),
 business_id TEXT NOT NULL REFERENCES business_head(business_id),
 kind TEXT NOT NULL,
 object_id TEXT REFERENCES identity_object(object_id),
 data_json TEXT NOT NULL CHECK(json_valid(data_json))
) STRICT;

CREATE INDEX opening_member_package ON opening_member(business_id,member_id);

-- owner: ledger
CREATE TABLE period_close (
period TEXT PRIMARY KEY,
 result_head TEXT NOT NULL,
 frozen_at TEXT NOT NULL,
 basis_schema TEXT NOT NULL,
 basis_json TEXT NOT NULL CHECK(json_valid(basis_json)),
 report_schema TEXT NOT NULL,
 report_json TEXT NOT NULL CHECK(json_valid(report_json)),
 report_digest TEXT NOT NULL CHECK(length(report_digest)=64)
) STRICT;

-- owner: records
CREATE TABLE document_shard_head (
shard_key TEXT PRIMARY KEY,
 wanted_head INTEGER NOT NULL CHECK(wanted_head>=0),
 published_head INTEGER NOT NULL DEFAULT 0 CHECK(published_head>=0 AND published_head<=wanted_head),
 current_path TEXT,
 current_digest TEXT CHECK(current_digest IS NULL OR length(current_digest)=64),
 last_error_code TEXT,
 last_error_message TEXT
) STRICT;

-- owner: records
CREATE TABLE document_set (
singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 published_set_id TEXT CHECK(published_set_id IS NULL OR length(published_set_id)=64),
 manifest_path TEXT NOT NULL
) STRICT;

-- owner: reading
CREATE TABLE read_scope_head (
scope_key TEXT NOT NULL,
 component_key TEXT NOT NULL,
 wanted_head INTEGER NOT NULL CHECK(wanted_head>=0),
 ready_head INTEGER NOT NULL DEFAULT 0 CHECK(ready_head>=0 AND ready_head<=wanted_head),
 format_version INTEGER NOT NULL CHECK(format_version>=1),
 content_digest TEXT CHECK(content_digest IS NULL OR length(content_digest)=64),
 state TEXT NOT NULL CHECK(state IN ('ready','preparing','failed')),
 error_code TEXT,
 PRIMARY KEY(scope_key,component_key)
) STRICT;

-- owner: reading
CREATE TABLE read_component (
scope_key TEXT NOT NULL,
 component_key TEXT NOT NULL,
 head INTEGER NOT NULL CHECK(head>=0),
 payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
 PRIMARY KEY(scope_key,component_key),
 FOREIGN KEY(scope_key,component_key) REFERENCES read_scope_head(scope_key,component_key)
) STRICT;

-- owner: reading
CREATE TABLE read_item (
scope_key TEXT NOT NULL,
 collection_key TEXT NOT NULL,
 item_id TEXT NOT NULL,
 component_key TEXT NOT NULL,
 head INTEGER NOT NULL CHECK(head>=0),
 sort_key TEXT NOT NULL,
 business_id TEXT,
 object_id TEXT,
 group_key TEXT,
 category TEXT,
 payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
 PRIMARY KEY(scope_key,collection_key,item_id)
) STRICT;

CREATE INDEX read_item_page ON read_item(scope_key,collection_key,sort_key,item_id);

CREATE INDEX read_item_object ON read_item(scope_key,collection_key,object_id,sort_key,item_id);

CREATE INDEX read_item_group ON read_item(scope_key,collection_key,group_key,sort_key,item_id);

CREATE INDEX read_item_category ON read_item(scope_key,collection_key,category,sort_key,item_id);

-- owner: files
CREATE TABLE file_task (
task_id TEXT PRIMARY KEY CHECK(length(task_id)=36),
 kind TEXT NOT NULL CHECK(kind IN ('backup','report_export')),
 state TEXT NOT NULL CHECK(state IN ('pending','running','succeeded','failed','expired')),
 scope_key TEXT NOT NULL,
 scope_head TEXT NOT NULL,
 plan_schema TEXT NOT NULL,
 plan_json TEXT NOT NULL CHECK(json_valid(plan_json)),
 output_path TEXT,
 output_sha256 TEXT CHECK(output_sha256 IS NULL OR length(output_sha256)=64),
 output_bytes INTEGER CHECK(output_bytes>=0),
 error_code TEXT,
 error_message TEXT,
 UNIQUE(kind,scope_key),
 CHECK(state<>'succeeded' OR (output_path IS NOT NULL AND output_sha256 IS NOT NULL AND output_bytes IS NOT NULL))
) STRICT;

CREATE INDEX file_task_scope ON file_task(kind,scope_key,state,task_id);

CREATE TRIGGER adopted_result_frozen_update BEFORE UPDATE ON adopted_result
WHEN OLD.state='frozen' BEGIN SELECT RAISE(ABORT,'frozen_result'); END;

CREATE TRIGGER adopted_result_frozen_delete BEFORE DELETE ON adopted_result
WHEN OLD.state='frozen' BEGIN SELECT RAISE(ABORT,'frozen_result'); END;

CREATE TRIGGER adopted_result_closed_insert BEFORE INSERT ON adopted_result
WHEN EXISTS(SELECT 1 FROM period_close WHERE period=NEW.posting_period)
BEGIN SELECT RAISE(ABORT,'closed_period'); END;

CREATE TRIGGER voucher_frozen_insert BEFORE INSERT ON voucher
WHEN EXISTS(SELECT 1 FROM adopted_result WHERE result_id=NEW.result_id AND state='frozen') BEGIN SELECT RAISE(ABORT,'frozen_result'); END;

CREATE TRIGGER voucher_frozen_update BEFORE UPDATE ON voucher
WHEN EXISTS(SELECT 1 FROM adopted_result WHERE result_id=NEW.result_id AND state='frozen') OR EXISTS(SELECT 1 FROM adopted_result WHERE result_id=OLD.result_id AND state='frozen') BEGIN SELECT RAISE(ABORT,'frozen_result'); END;

CREATE TRIGGER voucher_frozen_delete BEFORE DELETE ON voucher
WHEN EXISTS(SELECT 1 FROM adopted_result WHERE result_id=OLD.result_id AND state='frozen') BEGIN SELECT RAISE(ABORT,'frozen_result'); END;

CREATE TRIGGER obligation_frozen_insert BEFORE INSERT ON obligation
WHEN EXISTS(SELECT 1 FROM adopted_result WHERE result_id=NEW.result_id AND state='frozen') BEGIN SELECT RAISE(ABORT,'frozen_result'); END;

CREATE TRIGGER obligation_frozen_update BEFORE UPDATE ON obligation
WHEN EXISTS(SELECT 1 FROM adopted_result WHERE result_id=NEW.result_id AND state='frozen') OR EXISTS(SELECT 1 FROM adopted_result WHERE result_id=OLD.result_id AND state='frozen') BEGIN SELECT RAISE(ABORT,'frozen_result'); END;

CREATE TRIGGER obligation_frozen_delete BEFORE DELETE ON obligation
WHEN EXISTS(SELECT 1 FROM adopted_result WHERE result_id=OLD.result_id AND state='frozen') BEGIN SELECT RAISE(ABORT,'frozen_result'); END;

CREATE TRIGGER settlement_frozen_insert BEFORE INSERT ON settlement
WHEN EXISTS(SELECT 1 FROM adopted_result WHERE result_id=NEW.result_id AND state='frozen') BEGIN SELECT RAISE(ABORT,'frozen_result'); END;

CREATE TRIGGER settlement_frozen_update BEFORE UPDATE ON settlement
WHEN EXISTS(SELECT 1 FROM adopted_result WHERE result_id=NEW.result_id AND state='frozen') OR EXISTS(SELECT 1 FROM adopted_result WHERE result_id=OLD.result_id AND state='frozen') BEGIN SELECT RAISE(ABORT,'frozen_result'); END;

CREATE TRIGGER settlement_frozen_delete BEFORE DELETE ON settlement
WHEN EXISTS(SELECT 1 FROM adopted_result WHERE result_id=OLD.result_id AND state='frozen') BEGIN SELECT RAISE(ABORT,'frozen_result'); END;

CREATE TRIGGER obligation_reclassification_frozen_insert BEFORE INSERT ON obligation_reclassification
WHEN EXISTS(SELECT 1 FROM adopted_result WHERE result_id=NEW.result_id AND state='frozen') BEGIN SELECT RAISE(ABORT,'frozen_result'); END;

CREATE TRIGGER obligation_reclassification_frozen_update BEFORE UPDATE ON obligation_reclassification
WHEN EXISTS(SELECT 1 FROM adopted_result WHERE result_id=NEW.result_id AND state='frozen') OR EXISTS(SELECT 1 FROM adopted_result WHERE result_id=OLD.result_id AND state='frozen') BEGIN SELECT RAISE(ABORT,'frozen_result'); END;

CREATE TRIGGER obligation_reclassification_frozen_delete BEFORE DELETE ON obligation_reclassification
WHEN EXISTS(SELECT 1 FROM adopted_result WHERE result_id=OLD.result_id AND state='frozen') BEGIN SELECT RAISE(ABORT,'frozen_result'); END;

CREATE TRIGGER cost_lot_frozen_insert BEFORE INSERT ON cost_lot
WHEN EXISTS(SELECT 1 FROM adopted_result WHERE result_id=NEW.result_id AND state='frozen') BEGIN SELECT RAISE(ABORT,'frozen_result'); END;

CREATE TRIGGER cost_lot_frozen_update BEFORE UPDATE ON cost_lot
WHEN EXISTS(SELECT 1 FROM adopted_result WHERE result_id=NEW.result_id AND state='frozen') OR EXISTS(SELECT 1 FROM adopted_result WHERE result_id=OLD.result_id AND state='frozen') BEGIN SELECT RAISE(ABORT,'frozen_result'); END;

CREATE TRIGGER cost_lot_frozen_delete BEFORE DELETE ON cost_lot
WHEN EXISTS(SELECT 1 FROM adopted_result WHERE result_id=OLD.result_id AND state='frozen') BEGIN SELECT RAISE(ABORT,'frozen_result'); END;

CREATE TRIGGER cost_consumption_frozen_insert BEFORE INSERT ON cost_consumption
WHEN EXISTS(SELECT 1 FROM adopted_result WHERE result_id=NEW.result_id AND state='frozen') BEGIN SELECT RAISE(ABORT,'frozen_result'); END;

CREATE TRIGGER cost_consumption_frozen_update BEFORE UPDATE ON cost_consumption
WHEN EXISTS(SELECT 1 FROM adopted_result WHERE result_id=NEW.result_id AND state='frozen') OR EXISTS(SELECT 1 FROM adopted_result WHERE result_id=OLD.result_id AND state='frozen') BEGIN SELECT RAISE(ABORT,'frozen_result'); END;

CREATE TRIGGER cost_consumption_frozen_delete BEFORE DELETE ON cost_consumption
WHEN EXISTS(SELECT 1 FROM adopted_result WHERE result_id=OLD.result_id AND state='frozen') BEGIN SELECT RAISE(ABORT,'frozen_result'); END;

CREATE TRIGGER result_source_frozen_insert BEFORE INSERT ON result_source
WHEN EXISTS(SELECT 1 FROM adopted_result WHERE result_id=NEW.result_id AND state='frozen') BEGIN SELECT RAISE(ABORT,'frozen_result'); END;

CREATE TRIGGER result_source_frozen_update BEFORE UPDATE ON result_source
WHEN EXISTS(SELECT 1 FROM adopted_result WHERE result_id=NEW.result_id AND state='frozen') OR EXISTS(SELECT 1 FROM adopted_result WHERE result_id=OLD.result_id AND state='frozen') BEGIN SELECT RAISE(ABORT,'frozen_result'); END;

CREATE TRIGGER result_source_frozen_delete BEFORE DELETE ON result_source
WHEN EXISTS(SELECT 1 FROM adopted_result WHERE result_id=OLD.result_id AND state='frozen') BEGIN SELECT RAISE(ABORT,'frozen_result'); END;

CREATE TRIGGER result_fund_movement_frozen_insert BEFORE INSERT ON result_fund_movement
WHEN EXISTS(SELECT 1 FROM adopted_result WHERE result_id=NEW.result_id AND state='frozen') BEGIN SELECT RAISE(ABORT,'frozen_result'); END;

CREATE TRIGGER result_fund_movement_frozen_update BEFORE UPDATE ON result_fund_movement
WHEN EXISTS(SELECT 1 FROM adopted_result WHERE result_id=NEW.result_id AND state='frozen') OR EXISTS(SELECT 1 FROM adopted_result WHERE result_id=OLD.result_id AND state='frozen') BEGIN SELECT RAISE(ABORT,'frozen_result'); END;

CREATE TRIGGER result_fund_movement_frozen_delete BEFORE DELETE ON result_fund_movement
WHEN EXISTS(SELECT 1 FROM adopted_result WHERE result_id=OLD.result_id AND state='frozen') BEGIN SELECT RAISE(ABORT,'frozen_result'); END;

CREATE TRIGGER result_tax_sale_frozen_insert BEFORE INSERT ON result_tax_sale
WHEN EXISTS(SELECT 1 FROM adopted_result WHERE result_id=NEW.result_id AND state='frozen') BEGIN SELECT RAISE(ABORT,'frozen_result'); END;

CREATE TRIGGER result_tax_sale_frozen_update BEFORE UPDATE ON result_tax_sale
WHEN EXISTS(SELECT 1 FROM adopted_result WHERE result_id=NEW.result_id AND state='frozen') OR EXISTS(SELECT 1 FROM adopted_result WHERE result_id=OLD.result_id AND state='frozen') BEGIN SELECT RAISE(ABORT,'frozen_result'); END;

CREATE TRIGGER result_tax_sale_frozen_delete BEFORE DELETE ON result_tax_sale
WHEN EXISTS(SELECT 1 FROM adopted_result WHERE result_id=OLD.result_id AND state='frozen') BEGIN SELECT RAISE(ABORT,'frozen_result'); END;

CREATE TRIGGER result_report_contribution_frozen_insert BEFORE INSERT ON result_report_contribution
WHEN EXISTS(SELECT 1 FROM adopted_result WHERE result_id=NEW.result_id AND state='frozen') BEGIN SELECT RAISE(ABORT,'frozen_result'); END;

CREATE TRIGGER result_report_contribution_frozen_update BEFORE UPDATE ON result_report_contribution
WHEN EXISTS(SELECT 1 FROM adopted_result WHERE result_id=NEW.result_id AND state='frozen') OR EXISTS(SELECT 1 FROM adopted_result WHERE result_id=OLD.result_id AND state='frozen') BEGIN SELECT RAISE(ABORT,'frozen_result'); END;

CREATE TRIGGER result_report_contribution_frozen_delete BEFORE DELETE ON result_report_contribution
WHEN EXISTS(SELECT 1 FROM adopted_result WHERE result_id=OLD.result_id AND state='frozen') BEGIN SELECT RAISE(ABORT,'frozen_result'); END;

CREATE TRIGGER voucher_line_frozen_insert BEFORE INSERT ON voucher_line
WHEN EXISTS(SELECT 1 FROM voucher v JOIN adopted_result r ON r.result_id=v.result_id WHERE v.voucher_id=NEW.voucher_id AND (r.state='frozen' OR v.sealed=1)) BEGIN SELECT RAISE(ABORT,'sealed_voucher'); END;

CREATE TRIGGER voucher_line_frozen_update BEFORE UPDATE ON voucher_line
WHEN EXISTS(SELECT 1 FROM voucher v JOIN adopted_result r ON r.result_id=v.result_id WHERE v.voucher_id=NEW.voucher_id AND (r.state='frozen' OR v.sealed=1)) OR EXISTS(SELECT 1 FROM voucher v JOIN adopted_result r ON r.result_id=v.result_id WHERE v.voucher_id=OLD.voucher_id AND (r.state='frozen' OR v.sealed=1)) BEGIN SELECT RAISE(ABORT,'sealed_voucher'); END;

CREATE TRIGGER voucher_line_frozen_delete BEFORE DELETE ON voucher_line
WHEN EXISTS(SELECT 1 FROM voucher v JOIN adopted_result r ON r.result_id=v.result_id WHERE v.voucher_id=OLD.voucher_id AND (r.state='frozen' OR v.sealed=1)) BEGIN SELECT RAISE(ABORT,'sealed_voucher'); END;

CREATE TRIGGER source_content_immutable BEFORE UPDATE ON source
WHEN NEW.source_id<>OLD.source_id OR NEW.kind<>OLD.kind OR NEW.sha256<>OLD.sha256
 OR NEW.statement_text IS NOT OLD.statement_text OR NEW.byte_size IS NOT OLD.byte_size
BEGIN SELECT RAISE(ABORT,'immutable_source_content'); END;

CREATE TRIGGER period_close_immutable_update BEFORE UPDATE ON period_close BEGIN SELECT RAISE(ABORT,'closed_period'); END;

CREATE TRIGGER period_close_immutable_delete BEFORE DELETE ON period_close BEGIN SELECT RAISE(ABORT,'closed_period'); END;

CREATE TRIGGER money_occurrence_identity_immutable BEFORE UPDATE ON money_occurrence
WHEN NEW.occurrence_id<>OLD.occurrence_id OR NEW.business_id<>OLD.business_id OR NEW.public_slot<>OLD.public_slot
BEGIN SELECT RAISE(ABORT,'immutable_money_identity'); END;
