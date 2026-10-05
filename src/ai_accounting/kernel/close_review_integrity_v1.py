"""Versioned owner review frozen into each exact monthly close.

The compact full-month summary is stored in the close manifest.  Growing detail
collections are represented by ordered immutable-source directories and fixed
block digests.  A normal page rebuilds and verifies one block only; the close
integrity verifier may explicitly walk every block.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Annotated, Literal

from pydantic import BeforeValidator, ConfigDict, TypeAdapter
from typing_extensions import TypedDict

from .close_review_v1 import WireFen
from .contracts import KernelError
from .history_reads_v1 import V1Reads as QueryReads
from .stored_json_v1 import load_outcome


def _integer_literal_v1(value):
    if type(value) is not int:
        raise ValueError("integer required")
    return value


Version1 = Annotated[Literal[1], BeforeValidator(_integer_literal_v1)]


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).digest()


_REVENUE_ACCOUNTS = frozenset({"5001", "5111", "6301", "630101"})
_EXPENSE_ACCOUNTS = frozenset(
    {
        "5401",
        "540101",
        "540102",
        "540103",
        "540104",
        "5403",
        "5601",
        "560101",
        "560102",
        "560103",
        "560104",
        "5602",
        "560201",
        "560202",
        "560203",
        "560204",
        "5603",
        "560301",
        "571101",
        "571102",
        "571103",
        "571104",
        "5801",
    }
)


def _business_amount_v1(calculation):
    data = calculation["fact"]["data"]
    values = calculation["outcome"]["values"]
    spec = {
        "service_sale": ("gross_fen", "含税收入确认额"),
        "payroll": ("gross_fen", "税前工资"),
        "payroll_bounded": ("gross_fen", "税前工资"),
        "annual_bonus": ("gross_fen", "税前奖金"),
        "labor": ("gross_fen", "劳务确认毛额"),
        "labor_accrual": ("gross_fen", "劳务确认毛额"),
        "labor_project_cost": ("gross_fen", "资本化劳务确认毛额"),
        "asset": ("cost_fen", "已确认资产成本"),
        "reimbursed_asset": ("cost_fen", "已确认资产成本"),
        "reimbursed_asset_batch": ("cost_fen", "整批确认成本"),
        "asset_activation": ("cost_fen", "启用资产成本"),
        "asset_consumption": ("consumption_fen", "本期折旧摊销"),
        "asset_activation_batch": ("amount_fen", "本批启用资产成本"),
        "asset_consumption_month": ("amount_fen", "本月折旧摊销"),
        "asset_disposal": ("gross_proceeds_fen", "处置确认价款"),
        "loan_interest": ("interest_fen", "本期确认利息"),
        "loan_drawdown": ("principal_fen", "借款本金"),
        "project_release": ("released_fen", "转费用成本"),
        "money_fund_subscription": ("cost_fen", "申购确认成本"),
        "money_fund_redemption": ("net_proceeds_fen", "赎回结算额"),
        "income_tax_assessment": ("change_fen", "本期所得税确认额"),
        "platform_expense_confirmation": ("confirmed_amount_fen", "确认费用"),
        "managed_reserve_expense": ("amount_fen", "备用金实际支出"),
        "managed_reserve_refund": ("amount_fen", "备用金实际退款"),
    }
    kind = calculation["kind"]
    field, label = spec.get(kind, ("amount_fen", "业务确认金额"))
    amount = values.get(field, data.get(field))
    if kind == "employee_advance":
        obligations = values.get("obligations", ())
        amount = obligations[0]["amount_fen"] if len(obligations) == 1 else None
        label = "代付转债确认额"
    elif kind in {"payment", "cash_payment", "platform_payment", "payroll_reserve_payment"}:
        label = "实际收付款"
    elif kind in {"funding", "cash_funding", "platform_funding"}:
        label = "实际投入或借入金额"
    elif kind == "bank_platform_transfer":
        label = "内部划转金额"
    return (amount if type(amount) is int else None), label


PRESENTATION_CONTRACT = "ai-accounting-kernel/2/close-review/1"
DETAIL_BLOCK_SIZE = 50
CloseReviewSection = Literal[
    "vouchers", "adopted_bases", "policies", "payroll_confirmations", "evidence"
]
CloseReviewState = Literal["prepared", "closed", "covered", "unprepared", "stale"]
# These are accounting policy facts with official sources.  The renderer never
# treats an arbitrary kind ending in ``_policy`` as an adopted policy.
POLICY_FACT_KINDS = frozenset(
    {
        "vat_policy",
        "surtax_policy",
        "used_asset_vat_policy",
        "payroll_contribution_policy",
        "payroll_income_tax_policy",
        "annual_bonus_policy",
        "labor_income_tax_policy",
        "filing_calendar_policy_v2",
    }
)
PAYROLL_RESULT_KINDS = frozenset(
    {"payroll", "payroll_bounded", "annual_bonus", "labor", "labor_accrual", "labor_project_cost"}
)


class _StrictObject(TypedDict):
    __pydantic_config__ = ConfigDict(extra="forbid", strict=True)


class CloseReviewSourceReference(_StrictObject):
    source_type: Literal["voucher", "calculation", "fact", "evidence", "inventory"]
    id: str
    revision: int | None = None
    digest: str | None = None
    name: str | None = None
    media_type: str | None = None


class AdoptedPolicy(_StrictObject):
    label: str
    kind: str
    version: str | None
    effective_from: str | None
    effective_to: str | None
    official_urls: list[str]
    reference: CloseReviewSourceReference


class AdoptedPayrollConfirmation(_StrictObject):
    label: str
    mode: str
    calculation_reference: CloseReviewSourceReference
    confirmation_references: list[CloseReviewSourceReference]


class CloseReviewDetailItem(_StrictObject):
    key: str
    section: CloseReviewSection
    title: str
    subtitle: str
    status: str
    amount_fen: WireFen | None
    count: int | None
    references: list[CloseReviewSourceReference]


class CloseReviewBlock(_StrictObject):
    index: int
    first_key: str
    last_key: str
    count: int
    digest: str
    keys: list[str]


class CloseReviewDirectory(_StrictObject):
    section: CloseReviewSection
    label: str
    total_count: int
    block_size: int
    root_digest: str
    blocks: list[CloseReviewBlock]


class CloseReviewAccountingSummary(_StrictObject):
    voucher_count: int
    line_count: int
    total_debit_fen: WireFen
    total_credit_fen: WireFen
    month_revenue_fen: WireFen
    month_expense_fen: WireFen
    month_result_fen: WireFen
    ending_assets_fen: WireFen | None
    ending_liabilities_fen: WireFen | None
    ending_equity_fen: WireFen | None
    funds_total_fen: WireFen | None
    bank_fen: WireFen | None
    cash_fen: WireFen | None
    payment_platform_fen: WireFen | None
    actual_receipts_fen: WireFen
    actual_payments_fen: WireFen
    internal_transfer_fen: WireFen
    voucher_balanced: bool
    financial_position_balanced: bool | None
    financial_position_complete: bool


class CloseReviewBusinessSummary(_StrictObject):
    kind: str
    label: str
    action: Literal["business", "correction", "opening", "state"]
    reversal: bool
    count: int
    amount_label: str
    business_amount_fen: WireFen | None
    journal_total_fen: WireFen


class CloseReviewMaterialSummary(_StrictObject):
    category: str
    inventory_id: int
    expected: int
    received: int
    no_business: bool
    confirmation: CloseReviewSourceReference


class CloseReviewAdoptedBasisSummary(_StrictObject):
    policy_count: int
    payroll_confirmation_count: int
    evidence_count: int
    summary: str


class CloseReviewFollowupSummary(_StrictObject):
    close_issue_count: int
    settlement_issue_count: int
    external_issue_count: int
    file_issue_count: int
    followup_count: int


class OwnerReview(_StrictObject):
    presentation_contract: Literal["ai-accounting-kernel/2/close-review/1"]
    period: str
    accounting_summary: CloseReviewAccountingSummary
    business_summary: list[CloseReviewBusinessSummary]
    material_summary: list[CloseReviewMaterialSummary]
    adopted_basis_summary: CloseReviewAdoptedBasisSummary
    owner_confirmation: CloseReviewSourceReference
    followup_summary: CloseReviewFollowupSummary
    collections: list[CloseReviewDirectory]


class CloseReviewCoveredBy(_StrictObject):
    period: str
    digest: str


class CloseReviewPage(_StrictObject):
    total_count: int
    returned_count: int
    has_more: bool
    next_cursor: str | None


class CloseReviewCollection(_StrictObject):
    section: CloseReviewSection
    items: list[CloseReviewDetailItem]
    page: CloseReviewPage


class DashboardCloseReviewResponse(_StrictObject):
    schema_version: Version1
    company_id: str
    database_id: str
    period: str
    state: CloseReviewState
    preview_digest: str | None
    close_digest: str | None
    reason: str | None
    covered_by: CloseReviewCoveredBy | None
    owner_review: OwnerReview | None
    collection: CloseReviewCollection | None


DASHBOARD_CLOSE_REVIEW_ADAPTER = TypeAdapter(DashboardCloseReviewResponse)
_REFERENCE_ADAPTER = TypeAdapter(CloseReviewSourceReference)
_ADOPTED_POLICY_ADAPTER = TypeAdapter(AdoptedPolicy)
_ADOPTED_PAYROLL_CONFIRMATION_ADAPTER = TypeAdapter(AdoptedPayrollConfirmation)
_DETAIL_ADAPTER = TypeAdapter(CloseReviewDetailItem)
_DIRECTORY_ADAPTER = TypeAdapter(CloseReviewDirectory)
_OWNER_REVIEW_ADAPTER = TypeAdapter(OwnerReview)
_COLLECTION_ADAPTER = TypeAdapter(CloseReviewCollection)

_SECTION_LABELS = {
    "vouchers": "本月凭证",
    "adopted_bases": "直接采用核算结果",
    "policies": "实际采用政策",
    "payroll_confirmations": "工资及劳务确认",
    "evidence": "实际采用依据",
}
_POLICY_LABELS = {
    "vat_policy": "增值税规则",
    "surtax_policy": "附加税费规则",
    "used_asset_vat_policy": "已使用资产增值税规则",
    "payroll_contribution_policy": "社保公积金规则",
    "payroll_income_tax_policy": "工资个人所得税规则",
    "annual_bonus_policy": "全年一次性奖金个人所得税规则",
    "labor_income_tax_policy": "个人劳务所得税规则",
    "filing_calendar_policy_v2": "外部办理日历规则",
}


def _fail(reason: str):
    raise KernelError(
        "content_integrity_failed",
        "关账核对内容与冻结依据不一致",
        component="close_review",
        reason=reason,
    )


def _label(kind: str) -> str:
    labels = {
        "external_completion": "外部办理完成依据",
        "external_basis_review": "外部办理与账务核对",
        "payroll_disbursement_basis": "工资代发金额依据",
        "service_sale": "服务收入",
        "expense": "费用",
        "expense_recovery": "费用退回确认",
        "reimbursement_acceptance": "已付负债报销承接",
        "project_cost": "项目阶段成本",
        "project_release": "项目成本转费用",
        "pass_through": "代收代付",
        "advance": "预收预付款",
        "asset_advance": "资产预付款",
        "funding": "股东投入或借款",
        "payment": "实际收付款",
        "cash_payment": "现金收付款",
        "cash_funding": "现金投入或借款",
        "cash_bank_transfer": "现金存取",
        "platform_payment": "支付平台收付款",
        "platform_funding": "支付平台投入或借款",
        "bank_platform_transfer": "银行与支付平台转款",
        "platform_movement": "支付平台原始资金记录",
        "platform_expense_confirmation": "平台管理资金费用确认",
        "managed_reserve_expense": "备用金支出",
        "managed_reserve_refund": "备用金退款",
        "payroll_reserve_payment": "净薪及备用金支出付款",
        "settlement": "非现金核销",
        "sale_return": "销售退回",
        "funds_transfer": "资金调拨",
        "bank_income": "其他收入",
        "refundable_deposit": "可退保证金",
        "reimbursed_deposit": "垫付押金确认",
        "overpayment": "超付追收",
        "employee_advance": "个人垫付及债务转移",
        "pass_through_return": "代收款退回",
        "asset": "资产购置",
        "reimbursed_asset": "报销形成的资产",
        "reimbursed_asset_batch": "整批资产验收",
        "asset_activation": "资产启用",
        "asset_consumption": "折旧与摊销",
        "asset_activation_batch": "资产批次启用",
        "asset_consumption_month": "月度折旧摊销",
        "asset_disposal": "资产处置",
        "loan_drawdown": "借款到账",
        "loan_interest": "借款利息计提",
        "tax_assessment": "增值税及附加税费确认",
        "income_tax_assessment": "企业所得税确认",
        "tax_credit_confirmation": "税额抵减与退税确认",
        "payroll": "工资计提",
        "payroll_bounded": "工资计提",
        "annual_bonus": "全年一次性奖金",
        "labor": "个人劳务计提",
        "labor_accrual": "未支付个人劳务计提",
        "labor_project_cost": "资产项目劳务成本",
        "bank_statement": "银行流水",
        "bank_opening": "银行账面起点",
        "bank_reconciliation": "银行对账",
        "service_tax_point": "服务收入增值税确认",
        "advance_fulfillment": "预收款履约确认",
        "advance_refund": "预收预付款退回",
        "money_fund_subscription": "货币基金申购确认",
        "money_fund_redemption": "货币基金赎回确认",
        "opening_asset": "期初资产卡片",
        "opening_package": "期初接续总清单",
        "opening_bank": "银行存款期初",
        "opening_cash": "库存现金期初",
        "opening_platform": "支付平台期初",
        "opening_obligation": "往来明细期初",
        "opening_money_fund": "期初货币基金成本",
        "opening_loan": "借款本金及利息期初",
        "opening_tax": "税费明细期初",
        "opening_payroll_payable": "薪酬未付明细期初",
        "opening_payroll_state": "人员薪酬累计接续",
        "opening_equity": "权益明细期初",
    }
    return labels.get(kind, kind.replace("_", " "))


def _source_reference(
    source_type: str,
    ident: str,
    *,
    revision: int | None = None,
    hashed: str | None = None,
    name: str | None = None,
    media_type: str | None = None,
) -> dict:
    return _REFERENCE_ADAPTER.validate_python(
        {
            "source_type": source_type,
            "id": ident,
            "revision": revision,
            "digest": hashed,
            "name": name,
            "media_type": media_type,
        }
    )


def _evidence_rows(connection, identifiers):
    identifiers = sorted(set(identifiers))
    if not identifiers:
        return {}
    return {
        row["digest"].hex(): dict(row)
        for row in connection.execute(
            "SELECT digest,name,media_type FROM evidence WHERE digest IN "
            "(SELECT unhex(value) FROM json_each(?)) ORDER BY digest",
            (canonical(identifiers),),
        )
    }


def _direct_calculation_ids(roots):
    # Owner review names this month's direct adopted results.  Their immutable
    # dependency graph remains available for drill-through, but copying every
    # transitive ancestor here would make a late month repeat all prior history.
    return sorted(set(roots))


def _policy_values(fact):
    data = fact["data"]
    policy = data.get("policy") if isinstance(data.get("policy"), dict) else data
    urls = [
        policy.get("source_url"),
        policy.get("primary_source_url"),
        policy.get("legal_basis_source_url"),
    ]
    urls.extend(
        policy.get("source_urls", ()) if isinstance(policy.get("source_urls"), list) else ()
    )
    return {
        "version": policy.get("version"),
        "effective_from": policy.get("effective_from"),
        "effective_to": policy.get("effective_to"),
        "official_urls": list(dict.fromkeys(value for value in urls if isinstance(value, str))),
    }


def _adopted_policy(fact, *, hashed: str):
    return _ADOPTED_POLICY_ADAPTER.validate_python(
        {
            "label": _POLICY_LABELS[fact["kind"]],
            "kind": fact["kind"],
            **_policy_values(fact),
            "reference": _source_reference(
                "fact", fact["id"], revision=fact["revision"], hashed=hashed
            ),
        }
    )


def _adopted_payroll_confirmation(row, fact_rows, confirmation):
    if row["kind"] not in PAYROLL_RESULT_KINDS or confirmation is None:
        _fail("payroll_confirmation_source_missing")
    confirmation_ids = _confirmation_fact_ids(confirmation)
    if set(fact_rows) != set(confirmation_ids):
        _fail("payroll_confirmation_fact_missing")
    return _ADOPTED_PAYROLL_CONFIRMATION_ADAPTER.validate_python(
        {
            "label": _label(row["kind"]),
            "mode": confirmation.get("mode") or "confirmed",
            "calculation_reference": _source_reference(
                "calculation", row["id"], hashed=row["digest"].hex()
            ),
            "confirmation_references": [
                _source_reference(
                    "fact",
                    ident,
                    revision=fact_rows[ident]["revision"],
                    hashed=fact_rows[ident]["digest"].hex(),
                )
                for ident in confirmation_ids
            ],
        }
    )


def business_adopted_basis(connection, engine, calculation_ids):
    """Return exact immutable policy, payroll-confirmation and evidence references.

    This is deliberately rooted in the named calculation versions.  It never
    consults a current policy/profile head and never infers policy identity from
    a kind suffix.
    """
    calculations = _direct_calculation_ids(calculation_ids)
    if not calculations:
        return {"policies": [], "payroll_confirmations": [], "evidence": []}
    rows = {
        row["id"]: dict(row)
        for row in connection.execute(
            "SELECT id,fact_id,kind,outcome,digest FROM calculation WHERE id IN "
            "(SELECT value FROM json_each(?)) ORDER BY id",
            (canonical(calculations),),
        )
    }
    fact_ids = {row["fact_id"] for row in rows.values()}
    fact_ids.update(
        row[0]
        for row in connection.execute(
            "SELECT fact_id FROM dependency_fact WHERE calculation_id IN "
            "(SELECT value FROM json_each(?))",
            (canonical(calculations),),
        )
    )
    facts = QueryReads(engine, connection).facts(fact_ids)
    fact_rows = {
        row["id"]: dict(row)
        for row in connection.execute(
            "SELECT id,revision,digest FROM fact_revision WHERE id IN "
            "(SELECT value FROM json_each(?))",
            (canonical(sorted(fact_ids)),),
        )
    }
    policies = [
        _adopted_policy(fact, hashed=fact_rows[ident]["digest"].hex())
        for ident, fact in sorted(facts.items())
        if fact["kind"] in POLICY_FACT_KINDS
    ]
    payroll_confirmations = []
    for _ident, row in sorted(rows.items()):
        confirmation = load_outcome(row["outcome"]).get("values", {}).get("payroll_confirmation")
        if row["kind"] in PAYROLL_RESULT_KINDS and confirmation is not None:
            confirmation_ids = _confirmation_fact_ids(confirmation)
            payroll_confirmations.append(
                _adopted_payroll_confirmation(
                    row,
                    {
                        fact_id: fact_rows[fact_id]
                        for fact_id in confirmation_ids
                        if fact_id in fact_rows
                    },
                    confirmation,
                )
            )
    evidence_ids = sorted({proof for fact in facts.values() for proof in fact["evidence"]})
    evidence_rows = _evidence_rows(connection, evidence_ids)
    evidence_refs = [
        _source_reference(
            "evidence",
            ident,
            hashed=ident,
            name=evidence_rows[ident]["name"],
            media_type=evidence_rows[ident]["media_type"],
        )
        for ident in evidence_ids
    ]
    return {
        "policies": policies,
        "payroll_confirmations": payroll_confirmations,
        "evidence": evidence_refs,
    }


def _basis_inventory(connection, manifest, reads):
    roots = [item["calculation_id"] for item in manifest["adopted_results"]]
    roots.extend(item["calculation_id"] for item in manifest["asset_card_adoptions"])
    roots.extend(item["acceptance_calculation_id"] for item in manifest["asset_card_adoptions"])
    roots.extend(item["owner_calculation_id"] for item in manifest["asset_batch_adoptions"])
    calculations = _direct_calculation_ids(roots)
    raw = {
        row["id"]: dict(row)
        for row in connection.execute(
            "SELECT id,fact_id,kind,outcome,digest FROM calculation WHERE id IN "
            "(SELECT value FROM json_each(?)) ORDER BY id",
            (canonical(calculations),),
        )
    }
    fact_ids = {row["fact_id"] for row in raw.values()}
    fact_ids.update(
        row[0]
        for row in connection.execute(
            "SELECT fact_id FROM dependency_fact WHERE calculation_id IN "
            "(SELECT value FROM json_each(?))",
            (canonical(calculations),),
        )
    )
    facts = reads.facts(fact_ids)
    fact_digests = {
        row["id"]: row["digest"].hex()
        for row in connection.execute(
            "SELECT id,digest FROM fact_revision WHERE id IN (SELECT value FROM json_each(?))",
            (canonical(sorted(fact_ids)),),
        )
    }
    for ident, fact in facts.items():
        fact["digest"] = fact_digests[ident]
    policies = sorted(ident for ident, fact in facts.items() if fact["kind"] in POLICY_FACT_KINDS)
    payroll = []
    for ident, row in raw.items():
        if row["kind"] not in PAYROLL_RESULT_KINDS:
            continue
        confirmation = load_outcome(row["outcome"]).get("values", {}).get("payroll_confirmation")
        if confirmation is not None:
            payroll.append(ident)
    evidence = {manifest["owner_confirmation"]}
    evidence.update(proof for fact in facts.values() for proof in fact["evidence"])
    inventory_ids = sorted(manifest["inventories"].values())
    evidence.update(
        row[0].hex()
        for row in connection.execute(
            "SELECT evidence_digest FROM material_revision WHERE id IN "
            "(SELECT value FROM json_each(?))",
            (canonical(inventory_ids),),
        )
    )
    evidence.update(
        row[0].hex()
        for row in connection.execute(
            "SELECT evidence_digest FROM material_item WHERE inventory_id IN "
            "(SELECT value FROM json_each(?))",
            (canonical(inventory_ids),),
        )
    )
    return {
        "calculations": calculations,
        "raw_calculations": raw,
        "facts": facts,
        "policies": policies,
        "payroll": sorted(payroll),
        "evidence": sorted(evidence),
    }


def _policy_display(fact):
    values = _policy_values(fact)
    version = values["version"]
    effective_from = values["effective_from"]
    effective_to = values["effective_to"]
    urls = values["official_urls"]
    interval = (
        f"{effective_from} 至 {effective_to}"
        if effective_to
        else f"{effective_from} 起"
        if effective_from
        else "生效期间见原始规则"
    )
    version_text = f"版本 {version}" if version else f"事实第 {fact['revision']} 版"
    source_text = "；".join(urls) if urls else "官方来源见精确事实版本"
    return _POLICY_LABELS[fact["kind"]], f"{version_text}；适用期间 {interval}；来源 {source_text}"


def _confirmation_fact_ids(confirmation):
    identifiers = []
    for key, value in confirmation.items():
        if key.endswith("_fact_id") and isinstance(value, str):
            identifiers.append(value)
        elif key.endswith("_fact_ids") and isinstance(value, list):
            identifiers.extend(item for item in value if isinstance(item, str))
    return list(dict.fromkeys(identifiers))


def _voucher_cards(connection, vouchers, keys):
    selected = {item["id"]: item for item in vouchers if item["id"] in set(keys)}
    cards = {}
    if selected:
        rows = {
            row["id"]: dict(row)
            for row in connection.execute(
                "SELECT v.id,v.total,v.reverses_id,c.id calculation_id,c.kind,c.fact_id,c.digest "
                "FROM voucher_version v JOIN calculation c ON c.id=v.calculation_id "
                "WHERE v.id IN (SELECT value FROM json_each(?))",
                (canonical(sorted(selected)),),
            )
        }
        totals = {
            row["version_id"]: dict(row)
            for row in connection.execute(
                "SELECT version_id,count(*) line_count,sum(debit) debit,sum(credit) credit "
                "FROM voucher_line WHERE version_id IN (SELECT value FROM json_each(?)) "
                "GROUP BY version_id",
                (canonical(sorted(selected)),),
            )
        }
        facts = {
            row["id"]: dict(row)
            for row in connection.execute(
                "SELECT id,revision,digest FROM fact_revision WHERE id IN "
                "(SELECT c.fact_id FROM calculation c WHERE c.id IN "
                "(SELECT v.calculation_id FROM voucher_version v WHERE v.id IN "
                "(SELECT value FROM json_each(?))))",
                (canonical(sorted(selected)),),
            )
        }
        evidence = defaultdict(list)
        for row in connection.execute(
            "SELECT f.fact_id,e.digest,e.name,e.media_type FROM fact_evidence f "
            "JOIN evidence e ON e.digest=f.evidence_digest WHERE f.fact_id IN "
            "(SELECT c.fact_id FROM calculation c WHERE c.id IN "
            "(SELECT v.calculation_id FROM voucher_version v WHERE v.id IN "
            "(SELECT value FROM json_each(?)))) ORDER BY f.fact_id,e.digest",
            (canonical(sorted(selected)),),
        ):
            evidence[row["fact_id"]].append(dict(row))
        for ident, item in selected.items():
            row, line = rows[ident], totals.get(ident, {"line_count": 0, "debit": 0, "credit": 0})
            fact = facts[row["fact_id"]]
            refs = [
                _source_reference("voucher", ident),
                _source_reference("calculation", row["calculation_id"], hashed=row["digest"].hex()),
                _source_reference(
                    "fact", row["fact_id"], revision=fact["revision"], hashed=fact["digest"].hex()
                ),
            ]
            refs.extend(
                _source_reference(
                    "evidence",
                    proof["digest"].hex(),
                    hashed=proof["digest"].hex(),
                    name=proof["name"],
                    media_type=proof["media_type"],
                )
                for proof in evidence[row["fact_id"]]
            )
            cards[ident] = _DETAIL_ADAPTER.validate_python(
                {
                    "key": ident,
                    "section": "vouchers",
                    "title": f"记-{item['number']:04d} · {_label(row['kind'])}",
                    "subtitle": f"{line['line_count']} 行，借贷金额相等",
                    "status": "reversal" if row["reverses_id"] else "posted",
                    "amount_fen": row["total"],
                    "count": line["line_count"],
                    "references": refs,
                }
            )
    if set(cards) != set(keys):
        _fail("voucher_detail_source_missing")
    return [cards[key] for key in keys]


def _adopted_cards(connection, adopted_results, asset_cards, asset_batches, keys):
    selected = set(keys)
    rows = {
        row["id"]: dict(row)
        for row in connection.execute(
            "SELECT c.id,c.kind,c.fact_id,c.digest,f.revision,f.digest fact_digest "
            "FROM calculation c JOIN fact_revision f ON f.id=c.fact_id "
            "WHERE c.id IN (SELECT value FROM json_each(?))",
            (canonical(sorted(selected)),),
        )
    }
    cards = {}
    declared = {item["calculation_id"]: item for item in adopted_results}
    asset_roles = {}
    for item in asset_cards:
        asset_roles[item["calculation_id"]] = "asset_card_basis"
        asset_roles[item["acceptance_calculation_id"]] = "asset_acceptance_basis"
    for item in asset_batches:
        asset_roles[item["owner_calculation_id"]] = "asset_batch_owner"
    for ident, row in rows.items():
        item = declared.get(ident)
        expected_digest = (
            item["result_digest"]
            if item is not None
            else next(
                (
                    value[field]
                    for value in asset_cards
                    for key, field in (
                        ("calculation_id", "result_digest"),
                        ("acceptance_calculation_id", "acceptance_result_digest"),
                    )
                    if value[key] == ident
                ),
                row["digest"].hex(),
            )
        )
        if row["digest"].hex() != expected_digest:
            _fail("adopted_detail_source_missing")
        cards[ident] = _DETAIL_ADAPTER.validate_python(
            {
                "key": ident,
                "section": "adopted_bases",
                "title": _label(row["kind"]),
                "subtitle": (
                    f"来源月份 {item['source_period']}，入账月份 {item['posting_period']}"
                    if item is not None
                    else "资产卡片采用的精确计算版本"
                ),
                "status": item["role"] if item is not None else asset_roles[ident],
                "count": None,
                "amount_fen": None,
                "references": [
                    _source_reference("calculation", ident, hashed=expected_digest),
                    _source_reference(
                        "fact",
                        row["fact_id"],
                        revision=row["revision"],
                        hashed=row["fact_digest"].hex(),
                    ),
                ],
            }
        )
    if set(cards) != set(keys):
        _fail("adopted_detail_source_missing")
    return [cards[key] for key in keys]


def _policy_cards(connection, engine, keys):
    reads = QueryReads(engine, connection)
    facts = reads.facts(keys)
    digests = {
        row["id"]: row["digest"].hex()
        for row in connection.execute(
            "SELECT id,digest FROM fact_revision WHERE id IN (SELECT value FROM json_each(?))",
            (canonical(keys),),
        )
    }
    cards = {}
    for ident, fact in facts.items():
        if fact["kind"] not in POLICY_FACT_KINDS:
            _fail("policy_kind_not_explicit")
        adopted = _adopted_policy(fact, hashed=digests[ident])
        title, subtitle = _policy_display(fact)
        cards[ident] = _DETAIL_ADAPTER.validate_python(
            {
                "key": ident,
                "section": "policies",
                "title": title,
                "subtitle": subtitle,
                "status": "adopted",
                "amount_fen": None,
                "count": None,
                "references": [adopted["reference"]],
            }
        )
    return [cards[key] for key in keys]


def _payroll_cards(connection, keys):
    cards = {}
    for row in connection.execute(
        "SELECT c.id,c.kind,c.fact_id,c.outcome,c.digest,f.revision,f.digest fact_digest "
        "FROM calculation c JOIN fact_revision f ON f.id=c.fact_id "
        "WHERE c.id IN (SELECT value FROM json_each(?))",
        (canonical(keys),),
    ):
        confirmation = load_outcome(row["outcome"]).get("values", {}).get("payroll_confirmation")
        if row["kind"] not in PAYROLL_RESULT_KINDS or confirmation is None:
            _fail("payroll_confirmation_source_missing")
        confirmation_ids = _confirmation_fact_ids(confirmation)
        fact_rows = {
            item["id"]: item
            for item in connection.execute(
                "SELECT id,revision,digest FROM fact_revision WHERE id IN "
                "(SELECT value FROM json_each(?))",
                (canonical(confirmation_ids),),
            )
        }
        if set(fact_rows) != set(confirmation_ids):
            _fail("payroll_confirmation_fact_missing")
        adopted = _adopted_payroll_confirmation(row, fact_rows, confirmation)
        mode = confirmation.get("mode")
        mode_label = {
            "monthly_plan": "负责人确认的本月工资方案",
            "explicit_no_change": "负责人确认全员工资输入无变化",
        }.get(mode, "已确认工资依据")
        source_count = len(confirmation_ids)
        cards[row["id"]] = _DETAIL_ADAPTER.validate_python(
            {
                "key": row["id"],
                "section": "payroll_confirmations",
                "title": adopted["label"],
                "subtitle": f"{mode_label}；精确采用 {source_count} 个事实版本",
                "status": "confirmed",
                "amount_fen": None,
                "count": None,
                "references": [
                    adopted["calculation_reference"],
                    _source_reference(
                        "fact",
                        row["fact_id"],
                        revision=row["revision"],
                        hashed=row["fact_digest"].hex(),
                    ),
                    *adopted["confirmation_references"],
                ],
            }
        )
    if set(cards) != set(keys):
        _fail("payroll_confirmation_source_missing")
    return [cards[key] for key in keys]


def _evidence_cards(connection, keys):
    rows = _evidence_rows(connection, keys)
    if set(rows) != set(keys):
        _fail("evidence_source_missing")
    return [
        _DETAIL_ADAPTER.validate_python(
            {
                "key": key,
                "section": "evidence",
                "title": rows[key]["name"],
                "subtitle": rows[key]["media_type"],
                "status": "retained",
                "amount_fen": None,
                "count": None,
                "references": [
                    _source_reference(
                        "evidence",
                        key,
                        hashed=key,
                        name=rows[key]["name"],
                        media_type=rows[key]["media_type"],
                    )
                ],
            }
        )
        for key in keys
    ]


def _render_keys(connection, engine, manifest, section, keys):
    if section == "vouchers":
        return _voucher_cards(connection, manifest["vouchers"], keys)
    if section == "adopted_bases":
        return _adopted_cards(
            connection,
            manifest["adopted_results"],
            manifest["asset_card_adoptions"],
            manifest["asset_batch_adoptions"],
            keys,
        )
    if section == "policies":
        return _policy_cards(connection, engine, keys)
    if section == "payroll_confirmations":
        return _payroll_cards(connection, keys)
    if section == "evidence":
        return _evidence_cards(connection, keys)
    raise ValueError("unsupported close review section")


def _render_frozen_keys(connection, engine, header, section, keys):
    """Verify and render only the named frozen detail block's authoritative roots."""
    from .content_history_context import close_reader

    read_accounting = close_reader().read_accounting
    read_section = close_reader().read_section

    if section in {"vouchers", "adopted_bases"}:
        query = (
            "SELECT c.subject_id FROM json_each(?) ids JOIN voucher_version v ON v.id=ids.value "
            "JOIN calculation c ON c.id=v.calculation_id"
            if section == "vouchers"
            else "SELECT c.subject_id FROM json_each(?) ids JOIN calculation c ON c.id=ids.value"
        )
        subjects = {row[0] for row in connection.execute(query, (canonical(keys),))}
        selected = read_accounting(connection, header, subjects)
        if section == "vouchers":
            return _voucher_cards(connection, selected.vouchers, keys)
        return _adopted_cards(
            connection,
            selected.adopted_results,
            read_section(connection, header, "asset_card_adoptions"),
            read_section(connection, header, "asset_batch_adoptions"),
            keys,
        )
    if section == "policies":
        return _policy_cards(connection, engine, keys)
    if section == "payroll_confirmations":
        return _payroll_cards(connection, keys)
    if section == "evidence":
        return _evidence_cards(connection, keys)
    raise ValueError("unsupported close review section")


def _directory(section, cards):
    blocks = []
    for index, start in enumerate(range(0, len(cards), DETAIL_BLOCK_SIZE)):
        rows = cards[start : start + DETAIL_BLOCK_SIZE]
        keys = [item["key"] for item in rows]
        blocks.append(
            {
                "index": index,
                "first_key": keys[0],
                "last_key": keys[-1],
                "count": len(keys),
                "digest": digest(rows).hex(),
                "keys": keys,
            }
        )
    return _DIRECTORY_ADAPTER.validate_python(
        {
            "section": section,
            "label": _SECTION_LABELS[section],
            "total_count": len(cards),
            "block_size": DETAIL_BLOCK_SIZE,
            "root_digest": digest(blocks).hex(),
            "blocks": blocks,
        }
    )


def build_owner_review(
    connection,
    engine,
    manifest,
    *,
    _frozen_followups=None,
    _frozen_position=None,
    _checked_open=None,
    _verified_closes=None,
):
    """Build the complete immutable v1 owner review inside the caller transaction."""
    reads = QueryReads(engine, connection)
    basis = _basis_inventory(connection, manifest, reads)
    voucher_keys = [item["id"] for item in manifest["vouchers"]]
    adopted_keys = [item["calculation_id"] for item in manifest["adopted_results"]]
    adopted_keys.extend(
        ident
        for item in manifest["asset_card_adoptions"]
        for ident in (item["calculation_id"], item["acceptance_calculation_id"])
    )
    adopted_keys.extend(item["owner_calculation_id"] for item in manifest["asset_batch_adoptions"])
    adopted_keys = list(dict.fromkeys(adopted_keys))
    keys_by_section = {
        "vouchers": voucher_keys,
        "adopted_bases": adopted_keys,
        "policies": basis["policies"],
        "payroll_confirmations": basis["payroll"],
        "evidence": basis["evidence"],
    }
    directories = []
    for section, keys in keys_by_section.items():
        cards = _render_keys(connection, engine, manifest, section, keys)
        directories.append(_directory(section, cards))

    voucher_ids = voucher_keys
    line_rows = (
        list(
            connection.execute(
                "SELECT l.account,l.debit,l.credit FROM voucher_line l WHERE l.version_id IN "
                "(SELECT value FROM json_each(?))",
                (canonical(voucher_ids),),
            )
        )
        if voucher_ids
        else []
    )
    debit, credit = sum(row["debit"] for row in line_rows), sum(row["credit"] for row in line_rows)
    # Use the same party-aware classifier as the five-page dashboard.  Netting
    # one account code would lose a receivable from one party against a payable
    # to another party.
    from .position_v1 import PositionInputsV1, position_v1

    trial_rows = [
        {"account": row["account"], "amount": row["debit"] - row["credit"]}
        for row in manifest["trial_balance"]
    ]
    if _frozen_position is None:
        inputs = PositionInputsV1(
            engine, connection, manifest, _verified_closes=_verified_closes
        )
        try:
            position = position_v1(inputs)
        finally:
            inputs.release()
    else:
        position = _frozen_position
    movement = defaultdict(int)
    for row in line_rows:
        movement[row["account"]] += row["debit"] - row["credit"]
    revenue = -sum(value for account, value in movement.items() if account in _REVENUE_ACCOUNTS)
    expense = sum(value for account, value in movement.items() if account in _EXPENSE_ACCOUNTS)
    balances = {row["account"]: row["amount"] for row in trial_rows}
    funds_closing = {
        "bank": balances.get("1002", 0),
        "cash": balances.get("1001", 0),
        "platform": balances.get("1012", 0),
    }
    actual_receipts = actual_payments = internal_transfer = 0

    grouped = {}
    represented = set()
    reversed_ids = sorted(
        {voucher["reverses_id"] for voucher in manifest["vouchers"] if voucher["reverses_id"]}
    )
    reversed_owners = {
        row["id"]: row["calculation_id"]
        for row in connection.execute(
            "SELECT id,calculation_id FROM voucher_version WHERE id IN "
            "(SELECT value FROM json_each(?))",
            (canonical(reversed_ids),),
        )
    }
    if set(reversed_owners) != set(reversed_ids):
        _fail("reversed_voucher_source_missing")
    business_ids = {
        reversed_owners[voucher["reverses_id"]]
        if voucher["reverses_id"] is not None
        else voucher["adopted_calculation_id"]
        for voucher in manifest["vouchers"]
    }
    business_ids.update(item["calculation_id"] for item in manifest["adopted_results"])
    reads.calculations(business_ids)

    def add_business(calculation_id, *, action, reversal, journal_total=None):
        nonlocal actual_receipts, actual_payments, internal_transfer
        calculation = reads.calculation(calculation_id)
        kind = calculation["kind"]
        amount, amount_label = _business_amount_v1(
            calculation | {"fact": {"data": calculation["fact_data"]}}
        )
        signed_amount = (-amount if reversal else amount) if amount is not None else None
        entry = grouped.setdefault(
            (kind, action, reversal),
            {
                "kind": kind,
                "label": _label(kind),
                "action": action,
                "reversal": reversal,
                "count": 0,
                "amount_label": amount_label,
                "business_amount_fen": 0 if signed_amount is not None else None,
                "journal_total_fen": 0,
            },
        )
        entry["count"] += 1
        if entry["business_amount_fen"] is not None and signed_amount is not None:
            entry["business_amount_fen"] += signed_amount
        else:
            entry["business_amount_fen"] = None
        if journal_total is not None:
            entry["journal_total_fen"] += journal_total
        effects = [
            (-item["amount"] if reversal else item["amount"])
            for item in calculation["outcome"].get("balances", ())
            if item["category"] in {"bank", "cash", "platform"}
        ]
        transfer = (
            kind in {"funds_transfer", "cash_bank_transfer", "bank_platform_transfer"}
            and len(effects) > 1
            and sum(effects) == 0
        )
        if action == "business":
            if transfer:
                internal_transfer += sum(max(-value, 0) for value in effects)
            else:
                actual_receipts += sum(max(value, 0) for value in effects)
                actual_payments += sum(max(-value, 0) for value in effects)

    for voucher in manifest["vouchers"]:
        reversal = voucher["reverses_id"] is not None
        # A reversal negates its original voucher owner.  A later reviewed
        # replacement may be a different amount and must not rewrite that event.
        calculation_id = (
            reversed_owners[voucher["reverses_id"]]
            if reversal
            else voucher["adopted_calculation_id"]
        )
        declaration = next(
            (
                item
                for item in manifest["adopted_results"]
                if item["calculation_id"] == voucher["adopted_calculation_id"]
            ),
            None,
        )
        action = (
            "correction"
            if reversal or declaration is None or declaration["source_period"] != manifest["period"]
            else "opening"
            if declaration["role"] == "opening_basis"
            else "state"
            if declaration["role"] == "state_only"
            else "business"
        )
        add_business(
            calculation_id,
            action=action,
            reversal=reversal,
            journal_total=voucher["total"],
        )
        represented.add(calculation_id)
    for item in manifest["adopted_results"]:
        if item["calculation_id"] not in represented:
            action = (
                "opening"
                if item["role"] == "opening_basis"
                else "state"
                if item["role"] == "state_only"
                else "correction"
                if item["source_period"] != manifest["period"]
                else "business"
            )
            add_business(item["calculation_id"], action=action, reversal=False)
            represented.add(item["calculation_id"])

    materials = []
    inventory_rows = {
        row["id"]: row
        for row in connection.execute(
            "SELECT id,category,expected,received,no_business,evidence_digest "
            "FROM material_revision WHERE id IN (SELECT value FROM json_each(?))",
            (canonical(sorted(manifest["inventories"].values())),),
        )
    }
    inventory_evidence = _evidence_rows(
        connection, [row["evidence_digest"].hex() for row in inventory_rows.values()]
    )
    for category, inventory_id in sorted(manifest["inventories"].items()):
        row = inventory_rows.get(inventory_id)
        if row is None or row["category"] != category:
            _fail("material_inventory_source_missing")
        proof = inventory_evidence[row["evidence_digest"].hex()]
        materials.append(
            {
                "category": category,
                "inventory_id": row["id"],
                "expected": row["expected"],
                "received": row["received"],
                "no_business": bool(row["no_business"]),
                "confirmation": _source_reference(
                    "evidence",
                    row["evidence_digest"].hex(),
                    hashed=row["evidence_digest"].hex(),
                    name=proof["name"],
                    media_type=proof["media_type"],
                ),
            }
        )
    owner_row = connection.execute(
        "SELECT name,media_type FROM evidence WHERE digest=?",
        (bytes.fromhex(manifest["owner_confirmation"]),),
    ).fetchone()
    if owner_row is None:
        _fail("owner_confirmation_source_missing")

    if _frozen_followups is None:
        from .business_queries import BusinessQueries

        prepared = BusinessQueries(engine)._period_readiness(
            connection, manifest["period"], summary=True, _checked_open=_checked_open
        )
        followups = prepared["current_followups"]
        close_issues = sum(
            len(followups[key].get("issues", ()))
            for key in ("materials", "accounting", "close_requirements")
        )
        settlement_issues = len(followups["settlements"].get("issues", ()))
        external_issues = len(followups["external"].get("fact_issues", ()))
        file_issues = followups["file_jobs"].get("issue_count", 0)
        settlement_followups = followups["settlements"]["followup_count"]
        external_counts = followups["external"].get("actual_completion_status_counts", {})
        external_followups = max(
            0,
            followups["external"].get("obligation_count", 0)
            - external_counts.get("completed", 0)
            - external_counts.get("not_applicable", 0),
        )
        review_counts = followups["external"].get("basis_review_status_counts", {})
        external_followups += sum(
            count
            for state, count in review_counts.items()
            if state not in {"reviewed", "not_applicable"}
        )
        file_followups = sum(
            count
            for status, count in followups["file_jobs"].get("status_counts", {}).items()
            if status != "succeeded"
        )
        frozen_followups = {
            "close_issue_count": close_issues,
            "settlement_issue_count": settlement_issues,
            "external_issue_count": external_issues,
            "file_issue_count": file_issues,
            "followup_count": settlement_followups + external_followups + file_followups,
        }
    else:
        frozen_followups = _frozen_followups
    return _OWNER_REVIEW_ADAPTER.validate_python(
        {
            "presentation_contract": PRESENTATION_CONTRACT,
            "period": manifest["period"],
            "accounting_summary": {
                "voucher_count": len(voucher_ids),
                "line_count": len(line_rows),
                "total_debit_fen": debit,
                "total_credit_fen": credit,
                "month_revenue_fen": revenue,
                "month_expense_fen": expense,
                "month_result_fen": revenue - expense,
                "ending_assets_fen": position["assets_fen"],
                "ending_liabilities_fen": position["liabilities_fen"],
                "ending_equity_fen": position["equity_fen"],
                "funds_total_fen": sum(funds_closing.values()),
                "bank_fen": funds_closing["bank"],
                "cash_fen": funds_closing["cash"],
                "payment_platform_fen": funds_closing["platform"],
                "actual_receipts_fen": actual_receipts,
                "actual_payments_fen": actual_payments,
                "internal_transfer_fen": internal_transfer,
                "voucher_balanced": debit == credit,
                "financial_position_balanced": position["equation_valid"],
                "financial_position_complete": position["complete"],
            },
            "business_summary": [grouped[key] for key in sorted(grouped)],
            "material_summary": materials,
            "adopted_basis_summary": {
                "policy_count": len(basis["policies"]),
                "payroll_confirmation_count": len(basis["payroll"]),
                "evidence_count": len(basis["evidence"]),
                "summary": (
                    f"实际采用政策 {len(basis['policies'])} 项、工资及劳务确认 "
                    f"{len(basis['payroll'])} 项、保全依据 {len(basis['evidence'])} 项"
                ),
            },
            "owner_confirmation": _source_reference(
                "evidence",
                manifest["owner_confirmation"],
                hashed=manifest["owner_confirmation"],
                name=owner_row["name"],
                media_type=owner_row["media_type"],
            ),
            "followup_summary": frozen_followups,
            "collections": directories,
        }
    )


def require_owner_review(value):
    try:
        return _OWNER_REVIEW_ADAPTER.validate_python(value)
    except Exception:
        _fail("invalid_owner_review_contract")


def verify_owner_review_integrity(
    connection, engine, manifest, *, verify_financial_position=True, _verified_closes=None
):
    if _verified_closes is not None:
        from .content_v1 import _V1YearMonth
        from .position_v1 import _require_close_prefix

        closes = _require_close_prefix(
            connection, _verified_closes, _V1YearMonth(manifest["period"]).ordinal
        )
        if closes[-1][1] is not manifest:
            raise ValueError("verified closes do not contain this owner review")
    review = require_owner_review(manifest.get("owner_review"))
    rebuilt = build_owner_review(
        connection,
        engine,
        {key: value for key, value in manifest.items() if key != "owner_review"},
        _frozen_followups=review["followup_summary"],
        _verified_closes=_verified_closes,
        _frozen_position=(
            None
            if verify_financial_position
            else {
                "assets_fen": review["accounting_summary"]["ending_assets_fen"],
                "liabilities_fen": review["accounting_summary"]["ending_liabilities_fen"],
                "equity_fen": review["accounting_summary"]["ending_equity_fen"],
                "equation_valid": review["accounting_summary"]["financial_position_balanced"],
                "complete": review["accounting_summary"]["financial_position_complete"],
            }
        ),
    )
    if rebuilt != review:
        _fail("owner_review_semantic_mismatch")
    return {"status": "verified"}
