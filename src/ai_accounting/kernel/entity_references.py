"""Explicit object/business references, and rebuildable discovery indexes.

The declarations below are business contracts. No suffix-based inference takes
place when saving, searching, correcting or verifying facts.
"""

from __future__ import annotations

import json

from .contracts import KernelError
from .entities import require_entity
from .types import canonical, digest

DECLARATIONS: dict[str, list[dict]] = {}


def _declare(kinds, path, role, entity_kinds=(), *, account_type=None, reference_type="entity"):
    for kind in kinds.split():
        DECLARATIONS.setdefault(kind, []).append(
            dict(
                path=path,
                role=role,
                kinds=entity_kinds,
                account_type=account_type,
                reference_type=reference_type,
            )
        )


_declare(
    "payroll_profile payroll_contribution_actual payroll_opening_state "
    "payroll_first_wage_treatment "
    "payroll_withholding_actual annual_bonus_opening_usage payroll payroll_bounded annual_bonus "
    "reimbursed_deposit opening_payroll_payable opening_payroll_state payroll_plan_v2 "
    "payroll_plan_bounded payroll_change_notice_v2 tax_import_identity_v2 tax_import_details_v2 "
    "payroll_tax_declaration_actual payroll_disbursement_basis",
    "employee_id",
    "employee",
    ("person",),
)
_declare("payroll_plan_v2 payroll_plan_bounded", "payroll.employee_id", "employee", ("person",))
_declare("payroll_no_change_v2", "employees.*.employee_id", "employee", ("person",))
_declare("payroll_no_change_v2", "employees.*.payroll.employee_id", "employee", ("person",))
_declare("labor labor_accrual labor_project_cost", "person_id", "worker", ("person",))
_declare(
    "funding payment bank_income loan_drawdown bank_opening bank_statement bank_reconciliation "
    "cash_bank_transfer bank_platform_transfer managed_reserve_expense managed_reserve_refund "
    "payroll_reserve_payment "
    "opening_bank",
    "bank_account_id",
    "bank_account",
    ("fund_account",),
    account_type="bank",
)
_declare(
    "funds_transfer",
    "source_bank_account_id",
    "source_account",
    ("fund_account",),
    account_type="bank",
)
_declare(
    "funds_transfer",
    "destination_bank_account_id",
    "destination_account",
    ("fund_account",),
    account_type="bank",
)
_declare(
    "cash_payment cash_funding cash_bank_transfer opening_cash "
    "managed_reserve_expense managed_reserve_refund",
    "cash_account_id",
    "cash_account",
    ("fund_account",),
    account_type="cash",
)
_declare(
    "platform_movement platform_expense_confirmation platform_payment platform_funding "
    "bank_platform_transfer managed_reserve_expense managed_reserve_refund",
    "platform_account_id",
    "platform_account",
    ("fund_account",),
    account_type="platform",
)
_declare(
    "expense expense_recovery advance payment overpayment bank_income refundable_deposit "
    "reimbursed_deposit asset_advance cash_payment platform_payment money_fund_subscription "
    "money_fund_redemption opening_obligation managed_reserve_expense managed_reserve_refund",
    "counterparty_id",
    "counterparty",
    ("person", "organization"),
)
_declare("project_cost asset", "supplier_id", "supplier", ("person", "organization"))
_declare("service_sale sale_return", "customer_id", "customer", ("person", "organization"))
_declare("loan_agreement opening_loan", "lender_id", "lender", ("person", "organization"))
_declare("funding cash_funding platform_funding", "owner_id", "owner", ("person", "organization"))
_declare(
    "pass_through employee_advance reimbursement_acceptance",
    "payer_id",
    "payer",
    ("person", "organization"),
)
_declare("pass_through", "beneficiary_id", "beneficiary", ("person", "organization"))
_declare("asset_disposal", "buyer_id", "buyer", ("person", "organization"))
_declare(
    "opening_payroll_payable",
    "recipient_id",
    "recipient",
    ("person", "organization"),
)
_declare(
    "payment cash_payment platform_payment payroll_reserve_payment",
    "allocations.*.recipient_id",
    "recipient",
    ("person", "organization"),
)
_declare(
    "employee_advance reimbursement_acceptance",
    "sources.*.recipient_id",
    "recipient",
    ("person", "organization"),
)
_declare("settlement", "first.recipient_id", "recipient", ("person", "organization"))
_declare("settlement", "second.recipient_id", "recipient", ("person", "organization"))
_declare(
    "money_fund_subscription money_fund_redemption opening_money_fund",
    "fund_id",
    "fund_product",
    ("fund_product",),
)
_declare("project_cost project_release labor_project_cost", "project_id", "project", ("project",))
_declare(
    "asset reimbursed_asset opening_asset asset_activation asset_consumption asset_disposal",
    "asset_id",
    "asset",
    ("asset",),
)
_declare("reimbursed_asset_batch", "assets.*.asset_id", "asset", ("asset",))
_declare(
    "reimbursed_asset reimbursed_asset_batch", "creditors.*.employee_id", "employee", ("person",)
)
_declare("opening_tax", "authority_id", "tax_authority", ("organization",))
_declare(
    "report_classification",
    "counterparties.*.counterparty_id",
    "counterparty",
    ("person", "organization"),
)
_declare("opening_identity_binding", "assignments.*.entity_id", "corrected_object")

# Business/source identities are explicitly declared too. Their domain validators
# determine exact existence, versions and permitted lifecycle; they are never
# registered as real objects simply because an input field contains an ID.
BUSINESS_PATHS = {
    "opening_identity_binding": (
        "source_subject_id source_fact_id source_calculation_id package_calculation_id "
        "replacement_source_fact_id replacement_source_calculation_id "
        "replacement_package_calculation_id"
    ),
    "opening_basis_correction": (
        "members.*.binding_subject_id members.*.binding_fact_id members.*.package_calculation_id"
    ),
    "payroll": "profile_id contribution_policy_id income_tax_policy_id",
    "payroll_bounded": "profile_id contribution_policy_id income_tax_policy_id",
    "annual_bonus": "bonus_policy_id income_tax_policy_id regular_payroll_id",
    "labor": "policy_id gross_payment_id",
    "tax_assessment": "vat_policy_id surtax_policy_id tax_credit_ids.*",
    "tax_credit_confirmation": (
        "original_assessment_id returns.*.return_id "
        "returns.*.original_advance_id returns.*.original_sale_id"
    ),
    "service_sale": "vat_policy_id",
    "expense_recovery": "source_expense_id",
    "project_release": "project_sources.*.source_id",
    "asset": "project_sources.*.source_id",
    "advance": "vat_policy_id",
    "advance_fulfillment": "advance_id vat_policy_id",
    "advance_refund": "advance_id",
    "payment": "allocations.*.source_id",
    "cash_payment": "allocations.*.source_id",
    "platform_payment": "movement_ids.* allocations.*.source_id",
    "payroll_reserve_payment": "allocations.*.source_id",
    "service_tax_point": "sale_id payment_id",
    "overpayment": "source_id",
    "settlement": "first.source_id second.source_id",
    "sale_return": "sale_id",
    "reimbursed_asset": "acceptance_id",
    "asset_disposal": "vat_policy_id",
    "loan_drawdown": "agreement_id",
    "loan_interest": "drawdown_id agreement_id",
    "asset_activation_batch": "members.*.subject_id",
    "bank_reconciliation": "statement_id matches.*.source_id",
    "employee_advance": "sources.*.source_id",
    "reimbursement_acceptance": "sources.*.source_id",
    "pass_through_return": "source_id",
    "platform_expense_confirmation": "outgoing_movement_ids.* returned_movement_ids.*",
    "platform_funding": "movement_ids.*",
    "bank_platform_transfer": "movement_ids.*",
    "managed_reserve_expense": "movement_ids.*",
    "managed_reserve_refund": "movement_ids.*",
    "money_fund_redemption": "costs.*.source_id",
    "continuation_report_profile": "opening_package_id",
    "report_carry_forward": "opening_package_id",
    "report_classification": "voucher_version_id",
    "report_income_tax_confirmation": "calculation_id",
    "company_workflow_scope_v2": "calendar_policy_id",
    "external_completion": (
        "obligation_id obligation_fact_id accepted_calculations.*.subject_id "
        "accepted_calculations.*.calculation_id source_facts.*.subject_id "
        "source_facts.*.fact_id adopted_evidence_digests.* previous_completion_fact_id"
    ),
    "external_basis_review": (
        "completion_id completion_fact_id obligation_id obligation_fact_id "
        "source_facts.*.subject_id source_facts.*.fact_id "
        "adopted_calculations.*.subject_id adopted_calculations.*.calculation_id "
        "reviewed_calculations.*.subject_id reviewed_calculations.*.calculation_id"
    ),
    "opening_bank": "package_id",
    "opening_cash": "package_id",
    "opening_obligation": "package_id",
    "opening_asset": "package_id",
    "opening_loan": "package_id agreement_id",
    "opening_tax": "package_id",
    "opening_payroll_payable": "package_id",
    "opening_payroll_state": "package_id",
    "opening_equity": "package_id holder_or_basis_id",
    "opening_money_fund": "package_id",
    "opening_package": "package_id members.*.subject_id members.*.agreement_id",
    "material_resolution_v2": (
        "source_id source_fact_id links.*.subject_id links.*.fact_id "
        "links.*.calculation_id duplicate_source_id"
    ),
    "material_period_allocation": "source_id source_fact_id",
    "material_group_resolution": (
        "source_id source_fact_id links.*.subject_id links.*.fact_id links.*.calculation_id"
    ),
    "payroll_plan_v2": (
        "payroll.profile_id payroll.contribution_policy_id payroll.income_tax_policy_id "
        "profile_revision.subject_id contribution_policy_revision.subject_id "
        "income_tax_policy_revision.subject_id change_notice_revisions.*.subject_id"
    ),
    "payroll_plan_bounded": (
        "payroll.profile_id payroll.contribution_policy_id payroll.income_tax_policy_id "
        "profile_revision.subject_id contribution_policy_revision.subject_id "
        "income_tax_policy_revision.subject_id change_notice_revisions.*.subject_id"
    ),
    "payroll_no_change_v2": (
        "employees.*.payroll.profile_id employees.*.payroll.contribution_policy_id "
        "employees.*.payroll.income_tax_policy_id "
        "employees.*.prior_payroll_revision.subject_id "
        "employees.*.profile_revision.subject_id "
        "employees.*.contribution_policy_revision.subject_id "
        "employees.*.income_tax_policy_revision.subject_id"
    ),
    "payroll_disbursement_basis": "payroll_id declaration_id declaration_fact_id",
}
for _kind, _paths in BUSINESS_PATHS.items():
    for _path in _paths.split():
        _declare(_kind, _path, "business_source", reference_type="business")

ENTITY_ROLES = frozenset(
    item["role"]
    for declarations in DECLARATIONS.values()
    for item in declarations
    if item["reference_type"] == "entity"
)

ENTITY_REFERENCE_DDL = """
CREATE TABLE entity_reference_recorded(fact_id TEXT NOT NULL REFERENCES fact_revision(id),
 path TEXT NOT NULL,entity_id TEXT NOT NULL REFERENCES entity(id),role TEXT NOT NULL,
 kind TEXT NOT NULL,period INTEGER NOT NULL,
 source_digest BLOB NOT NULL CHECK(length(source_digest)=32),
 PRIMARY KEY(fact_id,path)) STRICT;
CREATE TABLE entity_reference_current(fact_id TEXT NOT NULL REFERENCES fact_revision(id),
 path TEXT NOT NULL,entity_id TEXT NOT NULL REFERENCES entity(id),role TEXT NOT NULL,
 kind TEXT NOT NULL,period INTEGER NOT NULL,
 source_digest BLOB NOT NULL CHECK(length(source_digest)=32),
 PRIMARY KEY(fact_id,path)) STRICT;
CREATE INDEX entity_recorded_lookup ON entity_reference_recorded(
 entity_id,period DESC,fact_id DESC);
CREATE INDEX entity_current_lookup ON entity_reference_current(entity_id,period DESC,fact_id DESC);
CREATE INDEX entity_recorded_entity_role ON entity_reference_recorded(
 entity_id,role,period DESC,fact_id DESC);
CREATE INDEX entity_current_entity_role ON entity_reference_current(
 entity_id,role,period DESC,fact_id DESC);
CREATE INDEX entity_recorded_entity_role_kind ON entity_reference_recorded(
 entity_id,role,kind,period DESC,fact_id DESC);
CREATE INDEX entity_current_entity_role_kind ON entity_reference_current(
 entity_id,role,kind,period DESC,fact_id DESC);
CREATE INDEX entity_recorded_role ON entity_reference_recorded(role,period DESC,fact_id DESC);
CREATE INDEX entity_current_role ON entity_reference_current(role,period DESC,fact_id DESC);
"""


def _at(value, segments, path=()):
    if not segments:
        if value is not None:
            yield ".".join(path), value
        return
    segment, *rest = segments
    if segment == "*":
        for index, child in enumerate(value or ()):
            yield from _at(child, rest, (*path, str(index)))
    elif isinstance(value, dict) and segment in value:
        yield from _at(value[segment], rest, (*path, segment))


def declarations_for(kind):
    return tuple(DECLARATIONS.get(kind, ()))


def references_for(fact, subject_id):
    data = fact.model_dump(mode="json")
    return references_from_data(fact.kind, data)


def references_from_data(kind, data):
    return [
        dict(declaration, path=path, entity_id=value)
        for declaration in declarations_for(kind)
        for path, value in _at(data, declaration["path"].split("."))
    ]


def validate_entity_references(connection, fact, subject_id):
    references = references_for(fact, subject_id)
    for item in references:
        if item["reference_type"] == "entity":
            require_entity(
                connection,
                item["entity_id"],
                kinds=item["kinds"],
                account_type=item["account_type"],
            )
    return references


def validate_filter(connection, entity_id, role, identity_match):
    if identity_match not in ("current", "recorded"):
        raise ValueError("identity_match must be current or recorded")
    if entity_id is not None:
        require_entity(connection, entity_id)
    if role is not None and role not in ENTITY_ROLES:
        raise ValueError("unknown entity reference role")


def _expected_rows(connection, fact_ids, *, identity_match="recorded", registry=None):
    from .storage import Store

    metadata = list(
        connection.execute(
            "SELECT r.*,s.kind FROM fact_revision r JOIN subject s "
            "ON s.id=r.subject_id WHERE r.id IN(SELECT value FROM json_each(?))",
            (canonical(list(fact_ids)),),
        )
    )
    # Raw typed storage must be decoded in batches; a historical payload is never
    # revalidated through today's defaults.
    if registry is None:
        from .service import default_registry

        registry = default_registry()
    reader = object.__new__(Store)
    reader.registry = registry
    data = reader.fact_data_many(connection, [row["id"] for row in metadata])
    expected = []
    for row in metadata:
        if digest(data[row["id"]]) != row["digest"]:
            raise KernelError(
                "content_integrity_failed",
                "对象引用的原始事实内容校验失败",
                component="fact",
                record_id=row["id"],
            )
        for reference in references_from_data(row["kind"], data[row["id"]]):
            if reference["reference_type"] == "entity":
                expected.append(
                    (
                        row["id"],
                        reference["path"],
                        reference["entity_id"],
                        reference["role"],
                        row["kind"],
                        row["period"],
                        row["digest"],
                    )
                )
    if identity_match == "current":
        expected = _current_bindings(connection, expected, registry=registry)
    return expected


def _current_bindings(connection, rows, *, registry=None):
    # Exact subject/path transitions, never a company-wide identity alias.
    # Before/after facts are immutable authority; the projection itself is not.
    if not rows:
        return rows
    source_ids = {row[0] for row in rows}
    subjects = {
        row["id"]: row["subject_id"]
        for row in connection.execute(
            "SELECT id,subject_id FROM fact_revision WHERE id IN(SELECT value FROM json_each(?))",
            (canonical(list(source_ids)),),
        )
    }
    # Follow only explicitly retained subjects. A later correction of the
    # retained business also applies to the discarded duplicate's provenance;
    # unrelated facts referencing the same object never join this chain.
    changes, visited, pending = [], set(), set(subjects.values())
    while pending:
        selected = list(
            connection.execute(
                "SELECT i.rowid AS sequence,i.*,c.plan,c.digest FROM identity_correction_item i "
                "JOIN identity_correction c ON c.id=i.correction_id "
                "WHERE i.subject_id IN(SELECT value FROM json_each(?)) ORDER BY i.rowid",
                (canonical(sorted(pending)),),
            )
        )
        visited.update(pending)
        pending = set()
        for record in selected:
            plan = json.loads(record["plan"])
            if digest(plan) != record["digest"]:
                raise KernelError("identity_correction_corrupt", "身份纠错依据校验失败")
            matches = [item for item in plan["items"] if item["subject_id"] == record["subject_id"]]
            if len(matches) != 1 or any(
                matches[0].get(key) != record[key]
                for key in ("action", "before_fact_id", "after_fact_id", "replacement_subject_id")
            ):
                raise KernelError("identity_correction_corrupt", "身份纠错范围与保存依据不一致")
            changes.append((record["sequence"], matches[0]))
            if record["replacement_subject_id"] and record["replacement_subject_id"] not in visited:
                pending.add(record["replacement_subject_id"])
    output = list(rows)
    owners = {(row[0], row[1]): [subjects[row[0]]] for row in rows}
    restored_ids = [change["after_fact_id"] for _, change in changes if change.get("reinstated")]
    restored = {}
    for row in _expected_rows(connection, restored_ids, registry=registry) if restored_ids else ():
        restored.setdefault(row[0], {})[row[1]] = row[2]
    opening_after_ids = {
        change["after_fact_id"] for _, change in changes if change["action"] == "opening_binding"
    }
    opening_after = {}
    if opening_after_ids:
        for saved in connection.execute(
            "SELECT c.fact_id,c.outcome,c.digest FROM identity_correction_item i "
            "JOIN calculation c ON c.id=i.calculation_id JOIN calculation_seal s "
            "ON s.calculation_id=c.id WHERE i.after_fact_id IN(SELECT value FROM json_each(?)) "
            "AND c.fact_id=i.after_fact_id",
            (canonical(sorted(opening_after_ids)),),
        ):
            outcome = json.loads(saved["outcome"])
            if digest(outcome) != saved["digest"] or saved["fact_id"] in opening_after:
                raise KernelError(
                    "identity_correction_corrupt", "期初纠错的精确采用结果不唯一或已损坏"
                )
            values = outcome["values"]
            opening_after[saved["fact_id"]] = {
                ref["path"]: ref["entity_id"]
                for ref in references_from_data(values["source_kind"], values["basis_data"])
                if ref["reference_type"] == "entity"
            }
        if opening_after.keys() != opening_after_ids:
            raise KernelError("identity_correction_corrupt", "期初纠错缺少精确采用结果")
    for _, change in sorted(changes, key=lambda item: item[0]):
        transitions = {item["path"]: item for item in change["entity_changes"]}
        updated = []
        for row in output:
            key = row[0], row[1]
            if change.get("reinstated") and change["subject_id"] in owners[key]:
                owners[key] = owners[key][: owners[key].index(change["subject_id"]) + 1]
                replacement = restored.get(change["after_fact_id"], {}).get(row[1])
                if replacement is not None:
                    row = (row[0], row[1], replacement, *row[3:])
            if owners[key][-1] == change["subject_id"]:
                if change["action"] == "opening_binding":
                    replacement = opening_after[change["after_fact_id"]].get(row[1])
                    if replacement is not None:
                        row = (row[0], row[1], replacement, *row[3:])
                else:
                    transition = transitions.get(row[1])
                    if transition and row[2] == transition["before"]:
                        row = (row[0], row[1], transition["after"], *row[3:])
                if change["replacement_subject_id"]:
                    owners[key].append(change["replacement_subject_id"])
            updated.append(row)
        output = updated
    return output


def current_role_matches(connection, fact_ids, role, *, registry=None):
    ids = sorted(set(fact_ids))
    verify_hits(connection, [{"fact_id": ident} for ident in ids], registry=registry)
    result = {}
    for row in connection.execute(
        "SELECT fact_id,entity_id FROM entity_reference_current "
        "WHERE fact_id IN(SELECT value FROM json_each(?)) AND role=?",
        (canonical(ids), role),
    ):
        if row["fact_id"] in result and result[row["fact_id"]] != row["entity_id"]:
            raise KernelError(
                "ambiguous_entity_reference", "这一业务角色存在多个对象，不能合并展示"
            )
        result[row["fact_id"]] = row["entity_id"]
    return result


def sync_entity_references(connection, version, hashed):
    rows = [
        (
            version.id,
            item["path"],
            item["entity_id"],
            item["role"],
            version.fact.kind,
            version.fact.period.ordinal,
            hashed,
        )
        for item in references_for(version.fact, version.subject_id)
        if item["reference_type"] == "entity"
    ]
    for table in ("entity_reference_recorded", "entity_reference_current"):
        connection.executemany(f"INSERT INTO {table} VALUES(?,?,?,?,?,?,?)", rows)


def verify_hits(connection, rows, *, identity_match="current", registry=None):
    # Only the requested facts are verified here. Completeness belongs to full
    # integrity checks/backup and explicit repair, not a claimed global seal.
    ids = {row["fact_id"] if "fact_id" in row.keys() else row["id"] for row in rows}
    expected = set(
        _expected_rows(connection, ids, identity_match=identity_match, registry=registry)
    )
    table = (
        "entity_reference_current" if identity_match == "current" else "entity_reference_recorded"
    )
    actual = {
        tuple(row)
        for row in connection.execute(
            f"SELECT * FROM {table} WHERE fact_id IN (SELECT value FROM json_each(?))",
            (canonical(list(ids)),),
        )
    }
    if actual != expected:
        raise KernelError("entity_reference_corrupt", "对象引用目录与原始事实不一致")


def verify_entity_references(connection, *, registry=None):
    rows = list(connection.execute("SELECT id AS fact_id FROM fact_revision"))
    for match in ("recorded", "current"):
        verify_hits(connection, rows, identity_match=match, registry=registry)


def rebuild_entity_references(connection, *, registry=None):
    ids = [row[0] for row in connection.execute("SELECT id FROM fact_revision")]
    changed = False
    for match, table in (
        ("recorded", "entity_reference_recorded"),
        ("current", "entity_reference_current"),
    ):
        expected = set(_expected_rows(connection, ids, identity_match=match, registry=registry))
        actual = {tuple(row) for row in connection.execute(f"SELECT * FROM {table}")}
        if actual != expected:
            changed = True
            connection.execute(f"DELETE FROM {table}")
            connection.executemany(f"INSERT INTO {table} VALUES(?,?,?,?,?,?,?)", sorted(expected))
    return changed
