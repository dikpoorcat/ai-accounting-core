"""Fixed historical grammar and synthetic results for the 2016–2025 benchmark.

These declarations belong only to archived synthetic samples under the complete
041 contract. They neither register current tax months nor claim a tax-office
form or an actual historical tax policy. Current production remains unchanged.
"""

import copy
from dataclasses import replace
from datetime import date
from decimal import Decimal

from ai_accounting.kernel.contracts import BalanceEffect, Fact, KernelError, Line, Outcome
from ai_accounting.kernel.development_contracts import HISTORY_TARGET_FINGERPRINT
from ai_accounting.kernel.domains.taxes import VAT_CALCULATION_KINDS, round_fen
from ai_accounting.kernel.domains.transactions import ServiceSale, calculate_sale
from ai_accounting.kernel.offline_development_upgrade import source_bundle
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.types import YearMonth, sum_fen


def _historical_period(period):
    if not YearMonth("2016-01") <= period <= YearMonth("2025-12"):
        raise KernelError("synthetic_history_period", "历史规模样本仅声明2016至2025年")


class ArchivedSyntheticSale(ServiceSale):
    def validate_registration(self):
        Fact.validate_registration(self)
        _historical_period(self.period)
        if self.tax_obligation_period != self.period or self.tax_obligation_date is not None:
            raise KernelError("synthetic_history_source", "历史样本须保留明确的当月旧税期来源")

    def reads_for(self, subject_id):
        return self.reads()

    def reads_after(self, subject_id, context):
        return ()


def archived_synthetic_tax(version, context):
    """Use the fixture's declared accrued tax, without a modern filing table."""
    fact = version.fact
    _historical_period(fact.period)
    if fact.period_start.period != fact.period or fact.period_end.period != fact.period:
        raise KernelError("synthetic_history_period", "历史样本税结果只声明完整本月")
    if fact.tax_credit_ids:
        raise KernelError("synthetic_history_source", "历史规模样本未声明退抵税结果")
    vat = context.one("vat_policy", "@" + fact.vat_policy_id)
    surtax = context.one("surtax_policy", "@" + fact.surtax_policy_id)
    if (vat.fact.policy.version, surtax.fact.policy.version) != (
        "synthetic-vat-v1", "synthetic-surtax-v1"
    ):
        raise KernelError("synthetic_history_policy", "只接受规模样本原有的合成规则声明")
    start, end = date.fromisoformat(fact.period_start), date.fromisoformat(fact.period_end)
    vat.fact.policy.require_effective(start, end)
    surtax.fact.policy.require_effective(start, end)
    if any(other.subject_id != version.subject_id for other in context.facts(
        fact.kind, "tax-assessment:" + fact.period
    )):
        raise KernelError("overlapping_tax_assessments", "同一合成税期不能重复确认")
    sources = []
    for kind in VAT_CALCULATION_KINDS:
        for source in context.calculations(kind, "tax:" + fact.period):
            if kind != "service_sale":
                if source.values.get("tax_sales") or source.values.get("accrued_vat_fen", 0):
                    raise KernelError("synthetic_history_source", "未声明其他历史计税来源")
                continue
            if (source.values.get("tax_obligation_period") != fact.period
                    or source.values.get("exemption_eligible") is not False
                    or source.values.get("tax_point_basis") is not None):
                raise KernelError("synthetic_history_source", "历史销售必须使用原有当月核算来源")
            sources.append(source)
    net = sum_fen(source.values["net_sales_fen"] for source in sources)
    accrued = sum_fen(source.values["accrued_vat_fen"] for source in sources)
    policy = surtax.fact.policy
    charges = tuple(round_fen(Decimal(accrued) * rate / 100 * policy.payable_fraction)
                    for rate in (policy.urban_rate_percent, policy.education_rate_percent,
                                 policy.local_education_rate_percent))
    total = sum_fen(charges)
    obligations = [
        {"name": name, "key": f"tax_assessment:{version.subject_id}:{name}",
         "amount_fen": amount, "account": account, "normal": "credit", "category": "payable",
         "counterparty_id": None, "cashflow": "tax_payments"}
        for name, amount, account in (("vat", accrued, "222101"), ("surtax", total, "222102"))
        if amount
    ]
    values = {
        "net_sales_fen": net, "accrued_vat_fen": accrued, "relief_fen": 0,
        "payable_vat_fen": accrued, "urban_tax_fen": charges[0],
        "education_tax_fen": charges[1], "local_education_tax_fen": charges[2],
        "surtax_fen": total, "vat_policy_version": vat.fact.policy.version,
        "surtax_policy_version": policy.version, "obligations": obligations,
        "period_start": str(fact.period_start), "period_end": str(fact.period_end),
        "vat_source_calculation_ids": sorted(source.id for source in sources),
        "vat_exemption_profit_lines": [], "tax_credit_calculations": [],
        "synthetic_history_contract": HISTORY_TARGET_FINGERPRINT,
    }
    return Outcome(
        (Line("5403", debit=total), Line("222102", credit=total)) if total else (),
        values,
        tuple(BalanceEffect(item["key"], item["amount_fen"], item["category"])
              for item in obligations),
        ({"vat_policy_id": vat.id, "surtax_policy_id": surtax.id,
          "meaning": "declared archived synthetic accounting result; no filing form"},),
    )


def historical_bundle():
    bundle = source_bundle(production_bundle(), HISTORY_TARGET_FINGERPRINT)
    registry = copy.copy(bundle.registry)
    registry.models = dict(bundle.registry.models, service_sale=ArchivedSyntheticSale)
    registry.body_models = dict(bundle.registry.body_models)
    bodies = bundle.registry.fact_bodies("service_sale")
    registry.body_models["service_sale"] = ((ArchivedSyntheticSale, bodies[0][1]), *bodies[1:])
    registry.evaluators = dict(bundle.registry.evaluators, service_sale=calculate_sale,
                               tax_assessment=archived_synthetic_tax)
    return replace(bundle, registry=registry)
