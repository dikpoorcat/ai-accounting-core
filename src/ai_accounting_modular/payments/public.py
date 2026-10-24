"""Public bank-payment planning and workbook production; never adopts a payment."""

from ai_accounting_modular.contracts.public import (
    ContractError,
    format_fen,
    parse_fen,
    sum_fen,
    validate_contract,
)

from . import _candidates, _plans, _workbook
from ._producer import create_payment_export_producer

__all__ = ["Payments", "create_payments", "create_payment_export_producer"]


class Payments:
    def __init__(self, *, storage, ledger, records, identity, people, businesses):
        self.storage, self.ledger, self.records = storage, ledger, records
        self.identity, self.people, self.businesses = identity, people, businesses

    def inspect(self, request, read, *, resolve_template=True):
        validate_contract("payment-export.schema.json", "PaymentExportInspectRequest", request)
        if request["company_id"] != read.company_id:
            raise ContractError("invalid_input", "代发检查的公司与当前读取快照不一致。")
        scope = {
            "period": request["period"],
            "selection": "selected" if "business_ids" in request else "period",
        }
        if scope["selection"] == "selected":
            scope["business_ids"] = sorted(request["business_ids"])
        balance_period = max(scope["period"], self.ledger.latest_period(read) or scope["period"])
        candidates, issues = _candidates.collect(self, scope, read, balance_period)
        rows, recipient_issues = _candidates.recipient_rows(self, candidates, read)
        issues.extend(recipient_issues)
        result = {
            "company_id": read.company_id,
            "database_id": read.database_id,
            "scope": scope,
            "balance_period": balance_period,
            "candidates": candidates,
            "rows": rows,
            "issues": issues,
            "available": False,
            "total_fen": format_fen(sum_fen(parse_fen(item["payable_fen"]) for item in candidates)),
        }
        if "template_source_id" not in request:
            issues.append(
                _candidates.issue(
                    "payment_template_missing",
                    "/template_source_id",
                    "需要代发时，请先提供这家公司使用的银行模板；不需要代发可跳过。",
                )
            )
        else:
            try:
                template = self._template(request, read, resolve_template)
                result["template"] = template
            except ContractError as error:
                issues.append(_candidates.issue(error.code, "/template_source_id", error.message))
        result["available"] = bool(rows) and not issues
        result["scope_head"] = _plans.head(result)
        return validate_contract("payment-export.schema.json", "PaymentExportInspection", result)

    def _template(self, request, read, resolve):
        source_id = request["template_source_id"]
        sources = self.records.resolve_sources((source_id,), read)
        if (
            not sources
            or sources[0]["kind"] != "file"
            or not sources[0]["original_name"].lower().endswith(".xlsx")
        ):
            raise ContractError(
                "payment_template_invalid", "代发表模板须是已登记在本公司的xlsx原件。"
            )
        source, layout = sources[0], request.get("template_layout")
        if resolve:
            with self.records.lease_source(source_id, read) as lease:
                layout = _workbook.resolve_layout(lease.read(), layout)
        elif layout is None:
            raise ContractError(
                "payment_template_layout_required", "当前计划缺少已经解析的银行模板布局。"
            )
        return {"source_id": source_id, "sha256": source["sha256"], "layout": layout}

    def prepare(self, request, read):
        """Inspect once and freeze its complete bank plan when all required inputs are ready."""
        inspection = self.inspect(request, read)
        if not inspection["available"]:
            return inspection, None
        plan = {
            key: inspection[key]
            for key in (
                "company_id",
                "database_id",
                "scope",
                "scope_head",
                "balance_period",
                "candidates",
                "rows",
                "total_fen",
                "template",
            )
        }
        plan.update(kind="payment_export", plan_version=1)
        return inspection, _plans.validate(plan)

    def plan(self, request, read):
        inspection, plan = self.prepare(request, read)
        if plan is None:
            if not inspection["issues"]:
                raise ContractError(
                    "payment_export_empty", "当前范围没有正数待付金额，无需生成空代发表。"
                )
            raise ContractError("needs_information", "代发表尚有具体缺项，请先查阅检查结果。")
        return plan

    def is_current(self, plan, read):
        _plans.validate(plan)
        if (read.company_id, read.database_id) != (plan["company_id"], plan["database_id"]):
            return False
        request = {
            "company_id": plan["company_id"],
            "period": plan["scope"]["period"],
            "template_source_id": plan["template"]["source_id"],
            "template_layout": plan["template"]["layout"],
        }
        if plan["scope"]["selection"] == "selected":
            request["business_ids"] = plan["scope"]["business_ids"]
        try:
            inspection = self.inspect(request, read, resolve_template=False)
            return inspection["available"] and inspection["scope_head"] == plan["scope_head"]
        except ContractError as error:
            if error.code in {"invalid_input", "not_found", "unsupported_business"}:
                return False
            raise

    @staticmethod
    def scope_key(scope):
        if "selection" not in scope:
            scope = {
                "period": scope["period"],
                "selection": "selected" if "business_ids" in scope else "period",
                **(
                    {"business_ids": sorted(scope["business_ids"])}
                    if "business_ids" in scope
                    else {}
                ),
            }
        return _plans.scope_key(scope)


def create_payments(*, storage, ledger, records, identity, people, businesses):
    return Payments(
        storage=storage,
        ledger=ledger,
        records=records,
        identity=identity,
        people=people,
        businesses=businesses,
    )
