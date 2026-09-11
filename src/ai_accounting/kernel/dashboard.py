"""Read-only presentation of the local journal for the existing five-page dashboard.

Journal amounts are selected by posting period. Closed periods use sealed versions;
an open-period reversal reads the original calculation, never the replacement's values.
The browser receives summaries of the complete snapshot and bounded detail pages.
"""

from __future__ import annotations

import calendar
import json
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import UTC, datetime

from .contracts import KernelError
from .materials import check_completeness
from .reports import PROFIT_ACCOUNTS, Reports, _workbook_name
from .types import YearMonth

KIND_NAMES = {
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
    "managed_reserve_bank_expense": "银行转入备用金费用确认",
    "managed_reserve_obligation_settlement": "备用金结清原应付款",
    "payroll_reserve_payment": "毛额工资支付及返池费用确认",
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
}
GROUPS = {
    "income_customer": "收入与客户",
    "expense_supplier": "费用与供应商",
    "employee_reimbursement": "员工报销",
    "payroll": "工资与社保",
    "labor": "个人劳务",
    "tax": "税费事项",
    "assets": "长期资产",
    "financing_owner": "融资与股东",
    "fund_movement": "资金调拨与保证金",
    "correction": "更正与冲正",
    "other": "其他业务",
}
ACCOUNT_NAMES = {
    "1001": "库存现金",
    "1002": "银行存款",
    "1012": "其他货币资金",
    "1101": "短期投资",
    "1122": "应收账款",
    "1123": "预付账款",
    "1221": "其他应收款",
    "1601": "固定资产",
    "1602": "累计折旧",
    "1604": "在建工程",
    "1701": "无形资产",
    "1702": "累计摊销",
    "189901": "待启用无形资产",
    "2001": "短期借款",
    "2202": "应付账款",
    "2203": "预收账款",
    "221101": "应付工资",
    "221102": "应付单位社保",
    "221103": "应付单位公积金",
    "2241": "其他应付款",
    "224101": "应付报销款",
    "224102": "代扣个人社保",
    "224103": "代扣个人公积金",
    "224104": "应付劳务报酬",
    "222103": "应付个人所得税",
    "3001": "实收资本",
    "4301": "项目成本",
    "5001": "主营业务收入",
    "5401": "主营业务成本",
    "5601": "销售费用",
    "5602": "管理费用",
    "5603": "财务费用",
    "5801": "所得税费用",
}
PAYROLL_KINDS = {"payroll", "payroll_bounded", "annual_bonus"}
LABOR_KINDS = {"labor", "labor_accrual"}
ASSET_KINDS = {"asset", "reimbursed_asset", "opening_asset"}
FUND_TYPES = {"bank": "bank", "cash": "cash", "platform": "payment_platform"}


def _name(kind):
    return KIND_NAMES.get(kind, "其他业务")


def _group(kind, reversal=False):
    if reversal:
        return "correction"
    if kind in PAYROLL_KINDS or kind.startswith("payroll_"):
        return "payroll"
    if kind in LABOR_KINDS or kind == "labor_project_cost":
        return "labor"
    if "asset" in kind:
        return "assets"
    if "tax" in kind:
        return "tax"
    if "funding" in kind or kind.startswith("loan_"):
        return "financing_owner"
    if kind in {"service_sale", "sale_return", "advance_fulfillment"}:
        return "income_customer"
    if kind in {"employee_advance", "reimbursement_acceptance", "reimbursed_deposit"}:
        return "employee_reimbursement"
    if "expense" in kind or kind.startswith("project_"):
        return "expense_supplier"
    if "payment" in kind or "transfer" in kind or "deposit" in kind or kind == "bank_income":
        return "fund_movement"
    return "other"


def _page(rows, after, limit, *, key="id"):
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("明细页大小必须为 1 至 500")
    selected = [row for row in rows if after is None or row[key] > after]
    items = selected[:limit]
    return items, {
        "has_more": len(selected) > limit,
        "next_cursor": items[-1][key] if len(selected) > limit else None,
        "total_count": len(rows),
    }


def _period_view(period, closed=False, closed_at=None):
    year, month = int(period[:4]), int(period[5:])
    return {
        "key": period,
        "year": year,
        "month": month,
        "label": f"{year} 年 {month} 月",
        "short_label": f"{month} 月",
        "status": "closed" if closed else "open",
        "start_date": f"{period}-01",
        "end_date": f"{period}-{calendar.monthrange(year, month)[1]:02}",
        "closed_at": closed_at,
    }


def _recognition(data, period):
    # Posting has month precision. Only a source's actual day may be displayed as a day.
    for field in (
        "actual_date",
        "acquisition_date",
        "in_use_date",
        "disposal_date",
        "income_date",
        "fulfillment_date",
        "recognition_date",
    ):
        value = data.get(field)
        if isinstance(value, str) and len(value) == 10:
            return {"precision": "day", "period": period, "date": value, "label": value}
    return {"precision": "month", "period": period, "date": None, "label": period}


class _Snapshot:
    def __init__(self, engine, connection, period):
        self.store, self.connection, self.period = engine.store, connection, period
        self.month = YearMonth(period).ordinal
        self.epochs = self.store.epochs(connection)
        self.closes = {
            row["period"]: json.loads(row["manifest"])
            for row in connection.execute(
                "SELECT period,manifest FROM period_close WHERE period<=?", (self.month,)
            )
        }
        self.close = self.closes.get(self.month)
        self.fact_cache, self.calculation_cache = {}, {}
        rows = connection.execute(
            "SELECT v.*,n.number FROM period_close p,json_each(p.manifest,'$.vouchers') j "
            "JOIN voucher_version v ON v.id=json_extract(j.value,'$.id') "
            "JOIN voucher n ON n.id=v.voucher_id WHERE p.period<=? UNION ALL "
            "SELECT v.*,n.number FROM voucher_current a "
            "JOIN voucher_version v ON v.id=a.version_id "
            "JOIN voucher n ON n.id=v.voucher_id WHERE v.period<=? AND NOT EXISTS "
            "(SELECT 1 FROM period_close p WHERE p.period=v.period) ORDER BY period,number",
            (self.month, self.month),
        ).fetchall()
        self.journal = []
        for record in rows:
            row = dict(record)
            basis = row["calculation_id"]
            if row["reverses_id"]:
                basis = connection.execute(
                    "SELECT calculation_id FROM voucher_version WHERE id=?", (row["reverses_id"],)
                ).fetchone()[0]
            row["basis"] = self.calculation(basis)
            if not row["reverses_id"] and row["period"] not in self.closes:
                reviewed = connection.execute(
                    "SELECT c.id FROM calculation_current a "
                    "JOIN calculation c ON c.id=a.calculation_id "
                    "JOIN calculation_publication p ON p.calculation_id=c.id "
                    "WHERE p.voucher_id=? AND p.posting_period=? AND c.subject_id=?",
                    (row["voucher_id"], row["period"], row["basis"]["subject_id"]),
                ).fetchone()
                if reviewed:
                    row["basis"] = self.calculation(reviewed[0])
                    row["calculation_id"] = reviewed[0]
            row["sign"] = -1 if row["reverses_id"] else 1
            row["lines"] = [
                dict(item)
                for item in connection.execute(
                    "SELECT line_no,account,debit,credit,cashflow FROM voucher_line "
                    "WHERE version_id=? ORDER BY line_no",
                    (row["id"],),
                )
            ]
            self.journal.append(row)
        self.month_journal = [row for row in self.journal if row["period"] == self.month]
        fact_ids = {ident for close in self.closes.values() for ident in close.get("facts", ())}
        fact_ids.update(
            fact["id"]
            for close in self.closes.values()
            for fact in close.get("management_snapshot", {}).get("typed_facts", ())
        )
        calculation_ids = {
            ident for close in self.closes.values() for ident in close.get("calculations", ())
        }
        fact_ids.update(
            row[0]
            for row in connection.execute(
                "SELECT a.fact_id FROM fact_current a JOIN fact_revision f ON f.id=a.fact_id "
                "WHERE f.period<=? "
                "AND NOT EXISTS(SELECT 1 FROM period_close p WHERE p.period=f.period)",
                (self.month,),
            )
        )
        calculation_ids.update(
            row[0]
            for row in connection.execute(
                "SELECT c.id FROM calculation_current a "
                "JOIN calculation c ON c.id=a.calculation_id "
                "JOIN calculation_publication p ON p.calculation_id=c.id WHERE p.posting_period<=? "
                "AND NOT EXISTS(SELECT 1 FROM period_close z WHERE z.period=p.posting_period)",
                (self.month,),
            )
        )
        for row in self.journal:
            calculation_ids.update((row["calculation_id"], row["basis"]["id"]))
        for ident in calculation_ids:
            calc = self.calculation(ident)
            fact_ids.add(calc["fact_id"])
            fact_ids.update(
                row[0]
                for row in connection.execute(
                    "SELECT fact_id FROM dependency_fact WHERE calculation_id=?", (ident,)
                )
            )
        self.facts = {}
        for ident in fact_ids:
            fact = self.fact(ident)
            old = self.facts.get(fact["subject_id"])
            if old is None or fact["revision"] > old["revision"]:
                self.facts[fact["subject_id"]] = fact
        self.calculations = {}
        for ident in calculation_ids:
            calc = self.calculation(ident)
            old = self.calculations.get(calc["subject_id"])
            if old is None or (calc["posting_period"], calc["fact"]["revision"]) > (
                old["posting_period"],
                old["fact"]["revision"],
            ):
                self.calculations[calc["subject_id"]] = calc
        self.openings = [
            calc for calc in self.calculations.values() if calc["outcome"].get("opening")
        ]
        self.accounts = defaultdict(int)
        self.month_accounts = defaultdict(int)
        for calc in self.openings:
            for line in calc["outcome"].get("opening_lines", ()):
                self.accounts[line["account"]] += line["debit"] - line["credit"]
        for row in self.journal:
            for line in row["lines"]:
                self.accounts[line["account"]] += line["debit"] - line["credit"]
                if row["period"] == self.month:
                    self.month_accounts[line["account"]] += line["debit"] - line["credit"]
        from .display import Display

        self.profiles = Display.profiles(connection, period)
        if self.close:
            snapshot = self.close.get("management_snapshot", {})
            payees = connection.execute(
                "SELECT p.* FROM payee_revision p JOIN json_each(?) j ON p.id=j.value",
                (json.dumps([row["id"] for row in snapshot.get("payees", ())]),),
            )
            management = connection.execute(
                "SELECT p.* FROM management_revision p JOIN json_each(?) j ON p.id=j.value",
                (json.dumps([row["id"] for row in snapshot.get("management", ())]),),
            )
        else:
            payees = connection.execute(
                "SELECT p.* FROM payee_revision p WHERE p.revision=(SELECT max(q.revision) "
                "FROM payee_revision q WHERE q.party_id=p.party_id)"
            )
            management = connection.execute(
                "SELECT p.* FROM management_revision p WHERE p.revision=(SELECT max(q.revision) "
                "FROM management_revision q WHERE q.subject_id=p.subject_id)"
            )
        self.payees = {row["party_id"]: row["name"] for row in payees}
        self.management = {row["subject_id"]: dict(row) for row in management}
        self.commentary = Display.commentary(connection, period, registry=self.store.registry)

    def fact(self, ident):
        if ident not in self.fact_cache:
            version = self.store.fact(self.connection, ident)
            self.fact_cache[ident] = {
                "id": version.id,
                "subject_id": version.subject_id,
                "revision": version.revision,
                "kind": version.fact.kind,
                "data": version.fact.model_dump(mode="json"),
                "evidence": list(version.evidence),
            }
        return self.fact_cache[ident]

    def calculation(self, ident):
        if ident not in self.calculation_cache:
            row = self.connection.execute(
                "SELECT c.*,p.posting_period FROM calculation c "
                "JOIN calculation_publication p ON p.calculation_id=c.id WHERE c.id=?",
                (ident,),
            ).fetchone()
            if row is None:
                raise KernelError("dashboard_source_missing", "看板引用的已发布核算不存在")
            calc = dict(row)
            calc["outcome"] = json.loads(calc["outcome"])
            calc["fact"] = self.fact(calc["fact_id"])
            self.calculation_cache[ident] = calc
        return self.calculation_cache[ident]

    def profile(self, kind, ident):
        return self.profiles.get(kind, {}).get(ident, {})

    def party(self, ident):
        if not ident:
            return "未提供"
        for kind in ("employee", "counterparty"):
            profile = self.profile(kind, ident)
            if profile.get("display_name"):
                return profile["display_name"]
        return self.payees.get(ident, "未提供姓名或名称")

    def by_kind(self, *kinds):
        return [fact for fact in self.facts.values() if fact["kind"] in kinds]

    def effects(self):
        for calc in self.openings:
            for effect in calc["outcome"].get("balances", ()):
                yield effect, calc["period"], None, 1
        for row in self.journal:
            for effect in row["basis"]["outcome"].get("balances", ()):
                yield effect, row["period"], row, row["sign"]
        for calc in self.calculations.values():
            if not calc["outcome"].get("opening") and not calc["outcome"].get("lines"):
                for effect in calc["outcome"].get("balances", ()):
                    yield (
                        effect,
                        calc["posting_period"],
                        {"number": 0, "calculation_id": calc["id"], "basis": calc, "sign": 1},
                        1,
                    )

    def voucher(self, row):
        calc = row["basis"]
        fact = calc["fact"]
        kind, data = calc["kind"], fact["data"]
        period = str(YearMonth.from_ordinal(row["period"]))
        recognition = _recognition(data, period)
        profile = self.profile("business", fact["subject_id"])
        management = self.management.get(fact["subject_id"])
        note = profile.get("note") or (management["note"] if management else None) or ""
        title = profile.get("display_name") or _name(kind)
        summary = ("冲正：" if row["sign"] < 0 else "") + title
        parties = list(
            dict.fromkeys(
                self.party(data[field])
                for field in (
                    "employee_id",
                    "person_id",
                    "counterparty_id",
                    "customer_id",
                    "supplier_id",
                    "owner_id",
                    "buyer_id",
                    "lender_id",
                )
                if data.get(field)
            )
        )
        component = {
            "id": fact["subject_id"],
            "key": fact["subject_id"],
            "kind": kind,
            "group": _group(kind),
            "label": _name(kind),
            "description": note,
            "amount_fen": row["sign"] * row["total"],
            "parties": parties,
            "management": {
                "version": profile.get("revision", 0),
                "metadata": {"purpose": profile.get("purpose") or "", "description": note},
                "history": [],
            },
            "facts": data,
            "recognition": recognition,
            "derived": calc["outcome"]["values"],
            "source_references": [
                {"type": "evidence", "value": proof} for proof in fact["evidence"]
            ],
        }
        return {
            "number": str(row["number"]),
            "calculation_id": row["calculation_id"],
            "date": recognition["date"],
            "recognition": recognition,
            "type": _name(kind),
            "kind": kind,
            "state": "冲正" if row["sign"] < 0 else "已入账",
            "summary": summary,
            "display_summary": summary,
            "list_summary": summary,
            "amount_fen": row["total"],
            "evidence": fact["evidence"],
            "components": [component],
            "funds": [],
            "lines": [
                {
                    "line_number": line["line_no"],
                    "code": line["account"],
                    "account": ACCOUNT_NAMES.get(line["account"], "会计科目"),
                    "debit_fen": line["debit"],
                    "credit_fen": line["credit"],
                    "party": "、".join(parties),
                    "component_id": fact["subject_id"],
                }
                for line in row["lines"]
            ],
        }


class Dashboard:
    def __init__(self, engine, *, company_name="", companies=None):
        self.engine, self.store = engine, engine.store
        self.company_name, self.companies = company_name, companies

    def _periods(self, connection):
        values = {
            row[0]
            for row in connection.execute(
                "SELECT period FROM voucher_version v JOIN voucher_current c ON c.version_id=v.id "
                "UNION SELECT period FROM period_close UNION SELECT f.period FROM fact_revision f "
                "JOIN fact_current a ON a.fact_id=f.id UNION SELECT period FROM material_revision"
            )
            if row[0] > 0
        }
        closed = {row[0] for row in connection.execute("SELECT period FROM period_close")}
        return [
            _period_view(str(YearMonth.from_ordinal(value)), value in closed)
            for value in sorted(values, reverse=True)
        ]

    @contextmanager
    def _snapshot(self, period):
        with self.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            periods = self._periods(connection)
            if period is None:
                period = periods[0]["key"] if periods else None
            if period is not None:
                YearMonth(period)
                if period not in {item["key"] for item in periods}:
                    raise KernelError("dashboard_period_not_found", "没有找到所选会计月份")
            yield _Snapshot(self.engine, connection, period) if period else None

    def context(self):
        with self.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            periods = self._periods(connection)
            identity = dict(connection.execute("SELECT * FROM identity WHERE id=1").fetchone())
        companies = [
            {
                "company_id": row.get("company_id", row.get("id")),
                "name": row["name"],
                "taxpayer_id": row.get("taxpayer_id"),
                "status": "active",
            }
            for row in (
                self.companies
                or [
                    {
                        "id": self.store.company_id,
                        "name": self.company_name,
                        "taxpayer_id": identity["taxpayer_id"],
                    }
                ]
            )
        ]
        current = next(row for row in companies if row["company_id"] == self.store.company_id)
        quarter_keys = sorted(
            {(p["year"], (p["month"] - 1) // 3 + 1) for p in periods}, reverse=True
        )
        month_keys = {p["key"] for p in periods}
        quarters = [
            {
                "key": f"{year}-Q{quarter}",
                "year": year,
                "quarter": quarter,
                "label": f"{year} 年第 {quarter} 季度",
                "complete": all(
                    f"{year}-{month:02}" in month_keys
                    for month in range(quarter * 3 - 2, quarter * 3 + 1)
                ),
            }
            for year, quarter in quarter_keys
        ]
        return {
            "schema_version": 2,
            "company": current["name"],
            "companies": companies,
            "current_company": current,
            "generated_at": datetime.now(UTC).isoformat(),
            "default_period": periods[0]["key"] if periods else None,
            "periods": periods,
            "default_quarter": quarters[0]["key"] if quarters else None,
            "quarters": quarters,
            "disclaimer": "用于负责人内部管理，不作为法定账簿、纳税申报或报税系统。",
        }

    @staticmethod
    def _response(snapshot, data):
        return {
            "schema_version": 1,
            "selected_period": _period_view(snapshot.period, bool(snapshot.close))
            if snapshot
            else None,
            "data": data,
        }

    def brief(self, period: str | None = None, *, after_number: int = 0, limit: int = 100):
        if type(after_number) is not int or after_number < 0:
            raise ValueError("凭证游标必须为非负整数")
        with self._snapshot(period) as snap:
            if snap is None:
                return self._response(None, None)
            rows, page = _page(snap.month_journal, after_number, limit, key="number")
            vouchers = [snap.voucher(row) for row in rows]
            groups = []
            for key, label in GROUPS.items():
                all_rows = [
                    row
                    for row in snap.month_journal
                    if _group(row["basis"]["kind"], row["sign"] < 0) == key
                ]
                if not all_rows:
                    continue
                selected = [
                    v
                    for row, v in zip(rows, vouchers, strict=True)
                    if _group(row["basis"]["kind"], row["sign"] < 0) == key
                ]
                groups.append(
                    {
                        "key": key,
                        "label": label,
                        "event_count": len(all_rows),
                        "loaded_count": len(selected),
                        "type_counts": [
                            {"label": label, "count": count}
                            for label, count in Counter(
                                _name(row["basis"]["kind"]) for row in all_rows
                            ).items()
                        ],
                        "rows": [
                            {
                                "date": v["date"],
                                "recognition": v["recognition"],
                                "reference": v["number"],
                                "calculation_id": v["calculation_id"],
                                "title": v["summary"],
                                "subject": v["type"],
                                "description": v["display_summary"],
                                "display_description": v["display_summary"],
                                "amount_fen": v["amount_fen"],
                                "state": v["state"],
                                "party": "、".join(v["components"][0]["parties"]),
                                "evidence": v["evidence"],
                                "components": v["components"],
                                "funds": v["funds"],
                            }
                            for v in selected
                        ],
                    }
                )
            funds, employees, assets = _funds(snap), _employees(snap), _assets(snap)
            bank = funds["bank_statement"]
            unmatched = [row for row in bank["rows"] if row["state"] != "matched"]
            if snap.close:
                material = {
                    "closed": True,
                    "satisfied": True,
                    "issues": [],
                    "coverage_digest": snap.close.get("material_coverage", {}).get(
                        "coverage_digest"
                    ),
                }
            else:
                from .periods import Periods

                coverage = check_completeness(snap.connection, snap.month, snap.store.registry)
                _, issues, unpublished = Periods.completeness(
                    snap.connection, snap.month, snap.store.registry, material_coverage=coverage
                )
                issues.extend(
                    {"field": row["id"], "message": "业务事实尚未正式处理"}
                    for row in unpublished
                    if row["kind"] in snap.store.registry.evaluators
                    and snap.store.registry.models[row["kind"]].lane != "management"
                )
                issues.extend(
                    {"field": row["subject_id"], "message": "已发布业务存在待复核变更"}
                    for row in snap.connection.execute(
                        "SELECT DISTINCT p.subject_id FROM pending p "
                        "JOIN fact_current c ON c.subject_id=p.subject_id "
                        "JOIN fact_revision f ON f.id=c.fact_id WHERE f.period<=?",
                        (snap.month,),
                    )
                )
                material = {
                    "closed": False,
                    "satisfied": not issues,
                    "issues": [
                        {"code": issue.get("code", "material_information_required"), **issue}
                        for issue in issues
                    ],
                    "coverage_digest": coverage["coverage_digest"],
                }
            debit = sum(line["debit"] for row in snap.month_journal for line in row["lines"])
            credit = sum(line["credit"] for row in snap.month_journal for line in row["lines"])
            position = _position(snap)
            valid = debit == credit and position["equation_valid"]
            attention = len(material["issues"]) + len(unmatched)
            commentary = snap.commentary.get("current")
            data = {
                "generated_at": datetime.now(UTC).isoformat(),
                "management_commentary": (commentary or {}).get("text", ""),
                "management_commentary_details": snap.commentary,
                "material_completeness": material,
                "voucher_count": len(snap.month_journal),
                "line_count": sum(len(row["lines"]) for row in snap.month_journal),
                "total_debit_fen": debit,
                "total_credit_fen": credit,
                "vouchers": vouchers,
                "voucher_page": {
                    "has_more": page["has_more"],
                    "next_after_number": page["next_cursor"],
                    "total_count": page["total_count"],
                },
                "activity_groups": groups,
                "position": position,
                "cash": {
                    k: bank[k]
                    for k in (
                        "transaction_count",
                        "ordinary_count",
                        "late_count",
                        "matched_count",
                        "unmatched_count",
                        "pending_late_count",
                        "inflow_fen",
                        "outflow_fen",
                    )
                }
                | {"net_fen": bank["inflow_fen"] - bank["outflow_fen"]},
                "unmatched_bank_activity": {
                    "count": len(unmatched),
                    "ordinary_count": len(unmatched),
                    "pending_late_count": 0,
                    "inflow_fen": sum(
                        row["amount_fen"] for row in unmatched if row["direction"] == "inflow"
                    ),
                    "outflow_fen": sum(
                        row["amount_fen"] for row in unmatched if row["direction"] == "outflow"
                    ),
                    "rows": unmatched[:limit],
                    "rows_truncated": len(unmatched) > limit,
                },
                "open_items": _open_items(snap),
                "workforce_cost": employees["workforce_cost"],
                "long_term_assets": {
                    "net_fen": assets["ledger_net_fen"],
                    "fixed_net_fen": assets["fixed_asset_net_fen"],
                    "intangible_net_fen": assets["intangible_asset_net_fen"],
                    "fixed_active_count": assets["fixed"]["active_count"],
                    "intangible_active_count": assets["intangible"]["active_count"],
                },
                "validation": {
                    "state": "error" if not valid else "attention" if attention else "complete",
                    "title": "账务核对",
                    "summary": "账务汇总平衡" if valid else "账务汇总需核对",
                    "integrity_valid": valid,
                    "attention_count": attention,
                    "items": [
                        {
                            "key": "balance",
                            "label": "凭证与余额",
                            "state": "pass" if valid else "error",
                            "text": "借贷及资产负债关系核对",
                        },
                        {
                            "key": "materials",
                            "label": "资料完整性",
                            "state": "pass" if material["satisfied"] else "pending",
                            "text": "按逐项资料检查器核对",
                        },
                    ],
                },
            }
            return self._response(snap, data)

    def funds(
        self,
        period: str | None = None,
        *,
        after_movement: str | None = None,
        after_statement: str | None = None,
        limit: int = 100,
    ):
        with self._snapshot(period) as snap:
            if snap is None:
                return self._response(None, None)
            data = _funds(snap)
            data["movements"], data["movement_page"] = _page(
                data["movements"], after_movement, limit
            )
            bank = data["bank_statement"]
            bank["rows"], bank["page"] = _page(bank["rows"], after_statement, limit)
            return self._response(snap, data)

    def employees(self, period: str | None = None):
        with self._snapshot(period) as snap:
            return self._response(snap, _employees(snap) if snap else None)

    def assets(self, period: str | None = None):
        with self._snapshot(period) as snap:
            return self._response(snap, _assets(snap) if snap else None)

    def quarterly_report(self, year: int, quarter: int):
        reports = Reports(self.engine)
        opened = reports.report(year, quarter, source="open")
        closed = reports.report(year, quarter, source="closed")
        plan = closed if closed["status"] == "ready" else opened
        return _quarterly_view(plan, closed)


def _position(snap):
    balances = snap.accounts
    assets = sum(
        value for account, value in balances.items() if account.startswith("1") or account == "4301"
    )
    liabilities = -sum(value for account, value in balances.items() if account.startswith("2"))
    capital = -sum(value for account, value in balances.items() if account.startswith("3"))

    def result(values):
        revenue = -sum(
            value
            for account, value in values.items()
            if PROFIT_ACCOUNTS.get(account, (0, 0))[1] == -1
        )
        expense = sum(
            value
            for account, value in values.items()
            if PROFIT_ACCOUNTS.get(account, (0, 0))[1] == 1
        )
        return revenue, expense, revenue - expense

    revenue, expense, monthly = result(snap.month_accounts)
    cumulative = result(balances)[2]
    fixed, intangible = balances["1601"] + balances["1602"], balances["1701"] + balances["1702"]
    return {
        "assets_fen": assets,
        "liabilities_fen": liabilities,
        "capital_fen": capital,
        "bank_fen": balances["1002"],
        "fixed_asset_cost_fen": balances["1601"],
        "accumulated_depreciation_fen": -balances["1602"],
        "fixed_asset_net_fen": fixed,
        "intangible_asset_cost_fen": balances["1701"],
        "accumulated_amortization_fen": -balances["1702"],
        "intangible_asset_net_fen": intangible,
        "other_assets_fen": assets - balances["1002"] - fixed - intangible,
        "month_revenue_fen": revenue,
        "month_expense_fen": expense,
        "month_result_fen": monthly,
        "cumulative_result_fen": cumulative,
        "equation_valid": assets == liabilities + capital + cumulative,
    }


def _funds(snap):
    accounts = {}

    def account(category, ident):
        key = (category, ident)
        if key not in accounts:
            profile = snap.profile("fund_account", ident)
            accounts[key] = {
                "account_id": ident,
                "code": profile.get("display_number") or "",
                "name": profile.get("display_name") or "未提供账户名称",
                "type": FUND_TYPES[category],
                "active": profile.get("active"),
                "opening_fen": 0,
                "inflow_fen": 0,
                "outflow_fen": 0,
                "net_change_fen": 0,
                "closing_fen": 0,
                "movement_count": 0,
                "last_activity_date": None,
                "negative_balance": False,
            }
        return accounts[key]

    movements = []
    for index, (effect, month, row, sign) in enumerate(snap.effects()):
        if effect["category"] not in FUND_TYPES:
            continue
        item = account(effect["category"], effect["key"])
        amount = sign * effect["amount"]
        if row is None or month < snap.month:
            item["opening_fen"] += amount
        elif amount:
            kind = row["basis"]["kind"]
            fact = row["basis"]["fact"]
            data = fact["data"]
            actual_date = data.get("actual_date")
            profile = snap.profile("business", fact["subject_id"])
            label = ("冲正：" if sign < 0 else "") + (profile.get("display_name") or _name(kind))
            movements.append(
                {
                    "id": f"{row['number']:012}:{index:012}",
                    "date": actual_date,
                    "account_id": effect["key"],
                    "account_code": item["code"],
                    "account_name": item["name"],
                    "account_type": item["type"],
                    "direction": "inflow" if amount > 0 else "outflow",
                    "amount_fen": abs(amount),
                    "signed_amount_fen": amount,
                    "reference": str(row["number"]),
                    "calculation_id": row["calculation_id"],
                    "type": _name(kind),
                    "summary": label,
                    "display_summary": label,
                    "party": snap.party(
                        data.get("counterparty_id") or data.get("owner_id") or data.get("lender_id")
                    ),
                    "internal_transfer": kind
                    in {"funds_transfer", "cash_bank_transfer", "bank_platform_transfer"},
                    "component_kinds": [kind],
                }
            )
            item["inflow_fen"] += max(amount, 0)
            item["outflow_fen"] += max(-amount, 0)
            item["movement_count"] += 1
            if actual_date:
                item["last_activity_date"] = max(item["last_activity_date"] or "", actual_date)
    # Zero-balance accounts still exist when an explicit opening/statement identifies them.
    for fact in snap.facts.values():
        for category, field in (
            ("bank", "bank_account_id"),
            ("cash", "cash_account_id"),
            ("platform", "platform_account_id"),
        ):
            if fact["data"].get(field):
                account(category, fact["data"][field])
    for index, (key, item) in enumerate(sorted(accounts.items()), 1):
        item["code"] = item["code"] or f"账户 {index}"
        item["net_change_fen"] = item["inflow_fen"] - item["outflow_fen"]
        item["closing_fen"] = item["opening_fen"] + item["net_change_fen"]
        item["negative_balance"] = item["closing_fen"] < 0
        item["statement"] = {
            "account_code": item["code"],
            "account_name": item["name"],
            "inflow_fen": 0,
            "outflow_fen": 0,
            "transaction_count": 0,
            "ordinary_count": 0,
            "matched_count": 0,
            "unmatched_count": 0,
            "late_count": 0,
            "pending_late_count": 0,
            "last_activity_date": None,
        }
        item["reconciliation"] = {
            "state": "pending" if key[0] == "bank" else "not_applicable",
            "label": "本月尚未完成银行对账" if key[0] == "bank" else "不适用银行对账",
        }
    for row in movements:
        category = next(key for key, value in FUND_TYPES.items() if value == row["account_type"])
        row["account_code"] = accounts[(category, row["account_id"])]["code"]
    bank_rows = []
    pending = (
        {row[0] for row in snap.connection.execute("SELECT DISTINCT subject_id FROM pending")}
        if not snap.close
        else set()
    )
    statements = [
        fact for fact in snap.by_kind("bank_statement") if fact["data"]["period"] == snap.period
    ]
    for statement in sorted(statements, key=lambda row: row["subject_id"]):
        data = statement["data"]
        item = accounts[("bank", data["bank_account_id"])]
        reconciliations = [
            fact
            for fact in snap.by_kind("bank_reconciliation")
            if fact["data"]["statement_id"] == statement["subject_id"]
            and fact["data"]["period"] == snap.period
        ]
        reconciliation = reconciliations[0] if len(reconciliations) == 1 else None
        result = snap.calculations.get(reconciliation["subject_id"]) if reconciliation else None
        statement_result = snap.calculations.get(statement["subject_id"])
        valid = bool(
            result
            and result["fact_id"] == reconciliation["id"]
            and statement_result
            and statement_result["fact_id"] == statement["id"]
            and statement["subject_id"] not in pending
            and reconciliation["subject_id"] not in pending
            and result["outcome"]["values"].get("balanced")
        )
        matched = (
            {match["reference"] for match in reconciliation["data"]["matches"]} if valid else set()
        )
        for index, entry in enumerate(data["entries"]):
            amount = entry["signed_fen"]
            bank_rows.append(
                {
                    "id": f"{statement['subject_id']}:{index:012}",
                    "date": entry["actual_date"],
                    "account_id": data["bank_account_id"],
                    "account_code": item["code"],
                    "account_name": item["name"],
                    "direction": "inflow" if amount > 0 else "outflow",
                    "amount_fen": abs(amount),
                    "signed_amount_fen": amount,
                    "party": "未提供",
                    "memo": entry.get("description") or "",
                    "state": "matched" if entry["reference"] in matched else "unmatched",
                    "is_late": False,
                }
            )
        rows = [row for row in bank_rows if row["account_id"] == data["bank_account_id"]]
        item["statement"].update(_bank_totals(rows))
        item["statement"]["last_activity_date"] = max((row["date"] for row in rows), default=None)
        item["reconciliation"] = {
            "state": "complete" if valid else "attention",
            "label": "已完成银行对账" if valid else "流水尚未完整核对",
            "version": reconciliation["revision"] if reconciliation else None,
            "statement_closing_fen": data["closing_fen"],
            "book_closing_fen": item["closing_fen"],
            "difference_fen": data["closing_fen"] - item["closing_fen"],
            "unmatched_count": len(rows) - sum(row["state"] == "matched" for row in rows),
            "pending_late_count": 0,
        }
    all_accounts = [item for _, item in sorted(accounts.items())]
    totals = {
        key: sum(item[key] for item in all_accounts)
        for key in ("opening_fen", "inflow_fen", "outflow_fen", "net_change_fen")
    }
    return {
        **totals,
        "total_fen": sum(item["closing_fen"] for item in all_accounts),
        "bank_fen": sum(item["closing_fen"] for item in all_accounts if item["type"] == "bank"),
        "cash_fen": sum(item["closing_fen"] for item in all_accounts if item["type"] == "cash"),
        "payment_platform_fen": sum(
            item["closing_fen"] for item in all_accounts if item["type"] == "payment_platform"
        ),
        "internal_transfer_fen": sum(
            row["amount_fen"]
            for row in movements
            if row["internal_transfer"] and row["direction"] == "outflow"
        ),
        "account_count": len(all_accounts),
        "bank_account_count": sum(item["type"] == "bank" for item in all_accounts),
        "cash_account_count": sum(item["type"] == "cash" for item in all_accounts),
        "payment_platform_account_count": sum(
            item["type"] == "payment_platform" for item in all_accounts
        ),
        "attention_account_count": sum(
            item["negative_balance"] or item["reconciliation"]["state"] in {"attention", "pending"}
            for item in all_accounts
        ),
        "accounts": all_accounts,
        "movements": sorted(movements, key=lambda row: row["id"]),
        "movement_count": len(movements),
        "bank_statement": _bank_totals(bank_rows)
        | {"rows": sorted(bank_rows, key=lambda row: row["id"])},
    }


def _bank_totals(rows):
    return {
        "transaction_count": len(rows),
        "ordinary_count": len(rows),
        "late_count": 0,
        "pending_late_count": 0,
        "inflow_fen": sum(row["amount_fen"] for row in rows if row["direction"] == "inflow"),
        "outflow_fen": sum(row["amount_fen"] for row in rows if row["direction"] == "outflow"),
        "matched_count": sum(row["state"] == "matched" for row in rows),
        "unmatched_count": sum(row["state"] != "matched" for row in rows),
    }


def _open_items(snap):
    obligations = {}
    for calc in snap.calculation_cache.values():
        values = calc["outcome"]["values"]
        sources = [(calc["kind"], calc["subject_id"], values)]
        sources.extend(
            (member["kind"], member["subject_id"], member["values"])
            for member in values.get("members", ())
        )
        for kind, subject, source in sources:
            for item in source.get("obligations", ()):
                obligations[item["key"]] = item | {
                    "kind": kind,
                    "subject_id": subject,
                }
    remaining = defaultdict(int)
    references = {}
    for effect, _, row, sign in snap.effects():
        if effect["category"] in {"receivable", "payable"}:
            remaining[effect["key"]] += sign * effect["amount"]
            if row and effect["amount"] > 0:
                references[effect["key"]] = str(row["number"])
    configurations = {
        "customer_receivables": ("待收客户款", "receivable"),
        "supplier_advances": ("待冲抵供应商预付款", "receivable"),
        "refundable_deposit_receivables": ("待收回保证金", "receivable"),
        "other_receivables": ("其他应收事项", "receivable"),
        "supplier_payables": ("待付供应商款", "payable"),
        "employee_payables": ("待付员工报销款", "payable"),
        "payroll_payables": ("待付工资、社保与个税", "payable"),
        "labor_payables": ("待付个人劳务及个税", "payable"),
        "other_payables": ("其他应付事项", "payable"),
    }
    buckets = defaultdict(list)
    for key, amount in sorted(remaining.items()):
        if amount <= 0:
            continue
        source = obligations.get(key)
        if source is None:
            raise KernelError("dashboard_obligation_missing", "未结余额缺少其正式业务来源")
        account, kind, direction = source["account"], source["kind"], source["category"]
        if direction == "receivable":
            category = (
                "customer_receivables"
                if account == "1122"
                else "supplier_advances"
                if account == "1123"
                else "refundable_deposit_receivables"
                if "deposit" in kind
                else "other_receivables"
            )
        else:
            category = (
                "payroll_payables"
                if kind in PAYROLL_KINDS or kind == "opening_payroll_payable"
                else "labor_payables"
                if kind in LABOR_KINDS
                else "supplier_payables"
                if account == "2202"
                else "employee_payables"
                if kind
                in {
                    "employee_advance",
                    "reimbursement_acceptance",
                    "reimbursed_asset",
                    "reimbursed_asset_batch",
                }
                else "other_payables"
            )
        buckets[category].append(
            {
                "id": key,
                "voucher": references.get(key, "期初"),
                "party_key": source.get("counterparty_id") or key,
                "party": snap.party(source.get("counterparty_id")),
                "description": _name(kind),
                "status": "partial" if amount < source["amount_fen"] else "open",
                "outstanding_fen": amount,
            }
        )
    categories = []
    for key, (label, direction) in configurations.items():
        items = buckets[key]
        if not items:
            continue
        grouped = defaultdict(list)
        for item in items:
            grouped[item["party_key"]].append(item)
        categories.append(
            {
                "key": key,
                "label": label,
                "direction": direction,
                "unit": "笔",
                "count": len(items),
                "outstanding_fen": sum(item["outstanding_fen"] for item in items),
                "items": items,
                "groups": [
                    {
                        "key": ident,
                        "party": rows[0]["party"],
                        "count": len(rows),
                        "outstanding_fen": sum(row["outstanding_fen"] for row in rows),
                        "open_count": sum(row["status"] == "open" for row in rows),
                        "partial_count": sum(row["status"] == "partial" for row in rows),
                    }
                    for ident, rows in grouped.items()
                ],
            }
        )
    totals = {
        f"{direction}_{field}": sum(
            item["count" if field == "count" else "outstanding_fen"]
            for item in categories
            if item["direction"] == direction
        )
        for direction in ("receivable", "payable")
        for field in ("count", "fen")
    }
    # The legacy brief distinguishes month-end obligations from what remains today.
    # Restrict the live view to origins already present at the selected month-end.
    current = dict.fromkeys(
        ("receivable_count", "receivable_fen", "payable_count", "payable_fen"), 0
    )
    for row in snap.connection.execute(
        "SELECT category,balance_key,amount FROM balance "
        "WHERE category IN ('receivable','payable') AND amount>0"
    ):
        if row["balance_key"] in remaining:
            current[row["category"] + "_count"] += 1
            current[row["category"] + "_fen"] += row["amount"]
    current["total_count"] = current["receivable_count"] + current["payable_count"]
    return totals | {
        "total_count": totals["receivable_count"] + totals["payable_count"],
        "categories": categories,
        "current_outstanding": current,
    }


def _employees(snap):
    money_keys = (
        "gross_salary_fen",
        "annual_bonus_fen",
        "employer_social_insurance_fen",
        "employer_housing_fund_fen",
        "employee_social_insurance_fen",
        "employee_housing_fund_fen",
        "individual_income_tax_fen",
        "net_salary_fen",
        "tax_reported_salary_fen",
    )
    aggregates = {}
    period_rows = defaultdict(list)
    rows = [row for row in snap.month_journal if row["basis"]["kind"] in PAYROLL_KINDS]
    posted = {row["basis"]["subject_id"] for row in rows}
    rows.extend(
        {
            "basis": calc,
            "sign": 1,
            "reverses_id": None,
            "calculation_id": calc["id"],
            "lines": [],
            "period": snap.month,
        }
        for calc in snap.calculations.values()
        if calc["kind"] in PAYROLL_KINDS
        and calc["posting_period"] == snap.month
        and calc["subject_id"] not in posted
    )
    for row in rows:
        calc, sign = row["basis"], row["sign"]
        data, values = calc["fact"]["data"], calc["outcome"]["values"]
        ident = data["employee_id"]
        item = aggregates.setdefault(
            ident,
            {
                **dict.fromkeys(money_keys, 0),
                "batches": set(),
                "periods": set(),
                "areas": set(),
                "scopes": set(),
            },
        )
        item["batches"].add(calc["subject_id"])
        item["periods"].add(data["period"])
        item["areas"].add(data.get("expense_class", "management"))
        item["scopes"].add(
            "contributions_only" if values.get("tax_status") == "not_started" else "wage_income"
        )
        item["annual_bonus_fen" if calc["kind"] == "annual_bonus" else "gross_salary_fen"] += (
            sign * values.get("gross_fen", 0)
        )
        item["individual_income_tax_fen"] += sign * values.get("tax_fen", 0)
        item["net_salary_fen"] += sign * values.get("net_fen", 0)
        item["tax_reported_salary_fen"] += sign * data.get("tax_reported_salary_fen", 0)
        contributions = {ob["name"]: ob["amount_fen"] for ob in values.get("obligations", ())}
        for side in ("employee", "employer"):
            for kind, label in (("social", "social_insurance"), ("housing", "housing_fund")):
                item[f"{side}_{label}_fen"] += sign * contributions.get(f"{side}_{kind}", 0)
        period_rows[data["period"]].append(row)
    profiles = defaultdict(list)
    for fact in snap.by_kind("payroll_profile"):
        data = fact["data"]
        if data["effective_from"] <= snap.period and (
            data["effective_to"] is None or data["effective_to"] >= snap.period
        ):
            profiles[data["employee_id"]].append(fact)
    known = set(aggregates) | set(snap.profiles.get("employee", {}))
    known.update(
        fact["data"]["employee_id"]
        for fact in snap.by_kind(
            "payroll_profile",
            "payroll",
            "payroll_bounded",
            "annual_bonus",
            "opening_payroll_payable",
        )
    )
    net_payments = defaultdict(int)
    for row in snap.month_journal:
        calc = row["basis"]
        if calc["kind"] not in {
            "payment",
            "cash_payment",
            "platform_payment",
            "payroll_reserve_payment",
        }:
            continue
        for allocation in calc["fact"]["data"].get("allocations", ()):
            if allocation["source_kind"] not in PAYROLL_KINDS | {"opening_payroll_payable"}:
                continue
            # Resolve from this publication's exact dependency facts, including a
            # prior month's wage or an opening payable with no current accrual.
            dependency = snap.connection.execute(
                "SELECT f.id FROM dependency_fact d JOIN fact_revision f ON f.id=d.fact_id "
                "WHERE d.calculation_id=? AND f.subject_id=?",
                (calc["id"], allocation["source_id"]),
            ).fetchone()
            source = (
                snap.fact(dependency[0]) if dependency else snap.facts.get(allocation["source_id"])
            )
            if source is None or source["kind"] != allocation["source_kind"]:
                raise KernelError(
                    "dashboard_payroll_source_missing", "工资付款缺少已发布的工资来源"
                )
            source_data = source["data"]
            is_net = (
                source_data["component"] == "net"
                if source["kind"] == "opening_payroll_payable"
                else allocation["obligation"] == "net"
            )
            if is_net:
                net_payments[source_data["employee_id"]] += row["sign"] * allocation["amount_fen"]
                known.add(source_data["employee_id"])
    items = []
    for index, ident in enumerate(sorted(known), 1):
        info = snap.profile("employee", ident)
        choices = profiles[ident]
        if choices:
            latest = max(fact["data"]["effective_from"] for fact in choices)
            choices = [fact for fact in choices if fact["data"]["effective_from"] == latest]
        profile = choices[0]["data"] if len(choices) == 1 else None
        amounts = aggregates.get(
            ident,
            {
                **dict.fromkeys(money_keys, 0),
                "batches": set(),
                "periods": set(),
                "areas": set(),
                "scopes": set(),
            },
        )
        start, end = info.get("employment_start"), info.get("employment_end")
        if info.get("employment_status", "unknown") == "unknown":
            state, in_period = "unknown", None
        elif start and start[:7] > snap.period:
            state, in_period = "not_started", False
        elif end and end[:7] < snap.period:
            state, in_period = "ended", False
        elif info.get("employment_status") == "inactive":
            state, in_period = "ended", False
        else:
            state, in_period = "in_period", True
        scopes = amounts["scopes"]
        scope = next(iter(scopes)) if len(scopes) == 1 else "mixed" if scopes else "none"
        tax_details = []
        for row in rows:
            calc = row["basis"]
            if calc["fact"]["data"]["employee_id"] != ident:
                continue
            value = calc["outcome"]["values"]
            actual = value.get("actual_withholding")
            calculated = value.get("calculated_tax_fen") if actual else value.get("tax_fen")
            tax_details.append(
                {
                    "calculation_id": calc["id"],
                    "period": calc["fact"]["data"]["period"],
                    "kind": calc["kind"],
                    "reversal": row["sign"] < 0,
                    "booked_tax_fen": row["sign"] * value.get("tax_fen", 0),
                    "calculated_tax_fen": row["sign"] * calculated
                    if calculated is not None
                    else None,
                    "actual_withholding_tax_fen": row["sign"] * actual["withheld_tax_fen"]
                    if actual
                    else None,
                    "actual_withholding_fact_id": actual["fact_id"] if actual else None,
                }
            )
        declarations = [
            fact
            for fact in snap.by_kind("payroll_tax_declaration_actual")
            if fact["data"]["employee_id"] == ident and fact["data"]["tax_period"] == snap.period
        ]
        items.append(
            {
                "employee_id": ident,
                "code": info.get("display_number") or f"人员 {index}",
                "name": snap.party(ident),
                "record_status": info.get("employment_status", "unknown"),
                "period_state": state,
                "period_state_label": {
                    "unknown": "未确认人员在册状态",
                    "not_started": "本月早于已提供的开始日期",
                    "ended": "已确认结束或不在册",
                    "in_period": "已确认在册",
                }[state],
                "in_period": in_period,
                "employment_start_date": start,
                "employment_end_date": end,
                "tax_withholding_start_date": profile["withholding_start_date"]
                if profile
                else None,
                "profile_available": profile is not None,
                "expense_areas": sorted(
                    {
                        {
                            "management": "管理",
                            "administration": "管理",
                            "sales": "销售",
                            "service": "服务",
                        }.get(area, "其他费用归属")
                        for area in amounts["areas"]
                    }
                ),
                **{
                    field: profile[field] if profile else None
                    for field in (
                        "social_insurance_participating",
                        "housing_fund_participating",
                        "social_insurance_base_fen",
                        "housing_fund_base_fen",
                    )
                },
                "resident_employee": None,
                "has_payroll_activity": bool(amounts["batches"]),
                "batch_count": len(amounts["batches"]),
                "tax_details": tax_details,
                "declared_tax_fen": declarations[0]["data"]["declared_tax_fen"]
                if len(declarations) == 1
                else None,
                "recorded_net_payments_fen": net_payments[ident],
                "payroll_periods": sorted(amounts["periods"]),
                "has_annual_bonus": amounts["annual_bonus_fen"] != 0,
                **{key: amounts[key] for key in money_keys},
                "personal_deduction_fen": amounts["employee_social_insurance_fen"]
                + amounts["employee_housing_fund_fen"]
                + amounts["individual_income_tax_fen"],
                "company_cost_fen": amounts["gross_salary_fen"]
                + amounts["annual_bonus_fen"]
                + amounts["employer_social_insurance_fen"]
                + amounts["employer_housing_fund_fen"],
                "wage_tax_scope": scope,
                "wage_tax_scope_label": {
                    "none": "本月无工资核算",
                    "wage_income": "工资薪金",
                    "contributions_only": "仅确认社保公积金",
                    "mixed": "包含不同核算情形",
                }[scope],
            }
        )
    sums = {
        key: sum(item[key] for item in items)
        for key in (*money_keys, "personal_deduction_fen", "company_cost_fen")
    }
    controlled = sums["company_cost_fen"]
    salary_accounts = {"560201", "560101", "540101"}
    ledger = sum(
        value for account, value in snap.month_accounts.items() if account in salary_accounts
    )
    adjustment = ledger - controlled
    periods = []
    for period, records in sorted(period_rows.items()):
        total = sum(
            sum(
                line["debit"] - line["credit"]
                for line in row["lines"]
                if line["account"] in salary_accounts
            )
            for row in records
        )
        correction_ids = [row["calculation_id"] for row in records if row["sign"] < 0]
        values = {
            "gross_salary_fen": 0,
            "employer_social_insurance_fen": 0,
            "employer_housing_fund_fen": 0,
            "employee_social_insurance_fen": 0,
            "employee_housing_fund_fen": 0,
        }
        for row in records:
            out = row["basis"]["outcome"]["values"]
            values["gross_salary_fen"] += row["sign"] * out.get("gross_fen", 0)
            obligations = {item["name"]: item["amount_fen"] for item in out.get("obligations", ())}
            for side in ("employer", "employee"):
                for field, suffix in (("social", "social_insurance"), ("housing", "housing_fund")):
                    values[f"{side}_{suffix}_fen"] += row["sign"] * obligations.get(
                        f"{side}_{field}", 0
                    )
        periods.append(
            {
                "payroll_period": period,
                "total_fen": total,
                "has_reversal": bool(correction_ids),
                "has_amendment": any(row["basis"]["fact"]["revision"] > 1 for row in records),
                "correction_ids": correction_ids,
                **values,
            }
        )
    unknown = sum(item["in_period"] is None for item in items)
    employee_cost = {
        "has_activity": bool(rows) or ledger != 0,
        "breakdown_available": adjustment == 0,
        "reason": None if adjustment == 0 else "账面人工成本包含未能按人员分配的调整，请查看凭证。",
        "total_fen": ledger,
        "controlled_total_fen": controlled,
        "settlement_adjustment_fen": adjustment,
        "prior_period_settlement_adjustment_fen": 0,
        "batch_count": len({row["basis"]["subject_id"] for row in rows}),
        "periods": periods,
        **{
            key: sums[key]
            for key in (
                "gross_salary_fen",
                "employer_social_insurance_fen",
                "employer_housing_fund_fen",
                "employee_social_insurance_fen",
                "employee_housing_fund_fen",
            )
        },
        "personal_withholding_fen": sums["personal_deduction_fen"],
    }
    labor_rows = [row for row in snap.month_journal if row["basis"]["kind"] in LABOR_KINDS]
    labor_periods = defaultdict(list)
    for row in labor_rows:
        labor_periods[row["basis"]["fact"]["data"]["period"]].append(row)

    def labor_sum(key):
        numbers = [row["basis"]["outcome"]["values"].get(key) for row in labor_rows]
        return (
            None
            if any(value is None for value in numbers)
            else sum(row["sign"] * value for row, value in zip(labor_rows, numbers, strict=True))
        )

    modes = sorted({row["basis"]["outcome"]["values"]["withholding_method"] for row in labor_rows})
    gross = labor_sum("gross_fen") or 0
    labor = {
        "has_activity": bool(labor_rows),
        "breakdown_available": True,
        "reason": None,
        "total_fen": gross,
        "gross_remuneration_fen": gross,
        "theoretical_withholding_tax_fen": labor_sum("theoretical_tax_fen"),
        "actual_withholding_tax_fen": labor_sum("tax_fen"),
        "unwithheld_tax_fen": labor_sum("unwithheld_tax_fen"),
        "pending_theoretical_tax_fen": None,
        "settled_gross_fen": None,
        "unsettled_gross_fen": None,
        "withholding_status": "无劳务事项"
        if not labor_rows
        else "有未扣税或未申报事项"
        if any(mode != "net_after_withholding" for mode in modes)
        else "已按净额扣税口径确认",
        "settlement_modes": modes,
        "batch_count": len({row["basis"]["subject_id"] for row in labor_rows}),
        "periods": [
            {
                "remuneration_period": period,
                "total_fen": sum(
                    row["sign"] * row["basis"]["outcome"]["values"]["gross_fen"] for row in records
                ),
                "gross_remuneration_fen": sum(
                    row["sign"] * row["basis"]["outcome"]["values"]["gross_fen"] for row in records
                ),
                "theoretical_withholding_tax_fen": None
                if any(
                    row["basis"]["outcome"]["values"].get("theoretical_tax_fen") is None
                    for row in records
                )
                else sum(
                    row["sign"] * row["basis"]["outcome"]["values"]["theoretical_tax_fen"]
                    for row in records
                ),
                "has_reversal": any(row["sign"] < 0 for row in records),
                "has_amendment": any(row["basis"]["fact"]["revision"] > 1 for row in records),
                "correction_ids": [row["calculation_id"] for row in records if row["sign"] < 0],
            }
            for period, records in sorted(labor_periods.items())
        ],
    }
    return {
        "employees": {
            **{key: sums[key] for key in (*money_keys, "personal_deduction_fen")},
            "registered_count": len(items),
            "in_period_count": None
            if unknown
            else sum(item["in_period"] is True for item in items),
            "unknown_period_count": unknown,
            "payroll_count": sum(item["has_payroll_activity"] for item in items),
            "without_payroll_count": None
            if unknown
            else sum(item["in_period"] and not item["has_payroll_activity"] for item in items),
            "profile_missing_count": None
            if unknown
            else sum(item["in_period"] and not item["profile_available"] for item in items),
            "contributions_only_count": sum(
                item["wage_tax_scope"] == "contributions_only" for item in items
            ),
            "controlled_cost_fen": controlled,
            "settlement_adjustment_fen": adjustment,
            "ledger_cost_fen": ledger,
            "detail_reconciled": adjustment == 0,
            "breakdown_available": adjustment == 0,
            "breakdown_reason": employee_cost["reason"],
            "items": items,
            "identity_note": (
                "在册状态和入离职日期仅展示已确认的管理资料，不决定工资核算资格或个税起点。"
            ),
        },
        "workforce_cost": {
            "has_activity": employee_cost["has_activity"] or labor["has_activity"],
            "total_fen": ledger + gross,
            "employee": employee_cost,
            "personal_labor": labor,
        },
    }


def _assets(snap):
    monthly_acquired, monthly_adjusted = defaultdict(int), defaultdict(int)
    for row in snap.month_journal:
        calc = row["basis"]
        if calc["kind"] not in {"asset", "reimbursed_asset", "reimbursed_asset_batch"}:
            continue
        data = calc["fact"]["data"]
        amounts = monthly_acquired if data["period"] == snap.period else monthly_adjusted
        for detail in data["assets"] if calc["kind"] == "reimbursed_asset_batch" else (data,):
            amounts[detail["asset_type"]] += row["sign"] * detail["cost_fen"]
    acquisitions = {
        calc["subject_id"]: calc
        for calc in snap.calculations.values()
        if calc["kind"] in ASSET_KINDS
    }
    # A confirmed batch can own the amount before its individual metadata fact is published.
    for calc in snap.calculations.values():
        if calc["kind"] == "reimbursed_asset_batch":
            for detail in calc["fact"]["data"]["assets"]:
                acquisitions.setdefault(detail["asset_id"], calc | {"asset_detail": detail})
    activations, disposals = {}, {}
    for calc in snap.calculations.values():
        if calc["kind"] == "asset_activation":
            activations[calc["fact"]["data"]["asset_id"]] = calc
        elif calc["kind"] == "asset_disposal":
            disposals[calc["fact"]["data"]["asset_id"]] = calc
    charges, monthly_charges, latest_charge = defaultdict(int), defaultdict(int), {}
    for row in snap.journal:
        calc = row["basis"]
        if calc["kind"] != "asset_consumption":
            continue
        ident = calc["fact"]["data"]["asset_id"]
        charge = row["sign"] * calc["outcome"]["values"]["consumption_fen"]
        charges[ident] += charge
        if row["period"] == snap.month:
            monthly_charges[ident] += charge
        latest_charge[ident] = max(
            latest_charge.get(ident, ""), str(YearMonth.from_ordinal(row["period"]))
        )
    items = []
    for index, (ident, calc) in enumerate(sorted(acquisitions.items()), 1):
        data = calc["fact"]["data"] | calc.get("asset_detail", {})
        profile = snap.profile("asset", ident)
        fixed = data["asset_type"] == "fixed"
        opening = calc["kind"] == "opening_asset"
        activation = activations.get(ident)
        basis = data if opening else (activation or {}).get("fact", {}).get("data", {})
        disposal = disposals.get(ident)
        cost = data["cost_fen"]
        accumulated = data.get("accumulated_fen", 0) + charges[ident]
        active = (opening or activation is not None) and disposal is None
        status = (
            "active"
            if active
            else "disposed"
            if disposal and fixed
            else "retired"
            if disposal
            else "pending_activation"
        )
        acquisition_day = data.get("acquisition_date")
        acceptance = snap.calculations.get(data.get("acceptance_id"))
        if acquisition_day is None and acceptance:
            acquisition_day = acceptance["fact"]["data"].get("acquisition_date")
        creditors = data.get("creditors") or (
            acceptance["fact"]["data"].get("creditors", ()) if acceptance else ()
        )
        source_subject = acceptance["subject_id"] if acceptance else calc["subject_id"]
        source_rows = [
            row
            for row in snap.journal
            if row["basis"]["subject_id"] == source_subject and row["sign"] > 0
        ]
        item = {
            "asset_id": ident,
            "asset_type": data["asset_type"],
            "code": profile.get("display_number") or f"资产 {index}",
            "name": profile.get("display_name") or "未提供资产名称",
            "category": data["asset_type"],
            "category_label": profile.get("category_label")
            or ("固定资产" if fixed else "无形资产"),
            "status": status,
            "status_label": {
                "active": "使用中",
                "disposed": "已处置",
                "retired": "已终止",
                "pending_activation": "待启用",
            }[status],
            "acquisition_date": acquisition_day,
            "posting_date": None,
            "posting_period": str(YearMonth.from_ordinal(calc["posting_period"])),
            "supplier": snap.party(data.get("supplier_id")),
            "settlement_method": "reimbursement" if "reimbursed" in calc["kind"] else "unknown",
            "settlement_label": "报销承接"
            if "reimbursed" in calc["kind"]
            else "收付款按独立业务记录",
            "payment_date": None,
            "due_date": None,
            "purchase_price_fen": None,
            "noncreditable_tax_fen": None,
            "other_direct_cost_fen": None,
            "cost_fen": cost,
            "accumulated_charge_fen": accumulated,
            "month_charge_fen": monthly_charges[ident],
            "book_value_fen": 0 if disposal else cost - accumulated,
            "latest_charge_period": latest_charge.get(ident),
            "benefit_area_label": {
                "administration": "管理",
                "sales": "销售",
                "service": "服务",
            }.get(basis.get("benefit_area")),
            "useful_life_months": basis.get("useful_life_months"),
            "acquisition_reference": str(source_rows[-1]["number"])
            if source_rows
            else "期初"
            if opening
            else "暂无凭证",
            "month_acquired": not opening and data["period"] == snap.period,
            "month_activated": bool(
                activation and activation["fact"]["data"]["period"] == snap.period
            ),
            "month_exited": bool(disposal and disposal["fact"]["data"]["period"] == snap.period),
        }
        if fixed:
            item.update(
                {
                    "in_service_date": basis.get("in_use_date"),
                    "reimbursing_employee": "、".join(
                        snap.party(c["employee_id"]) for c in creditors
                    )
                    or "未提供",
                    "residual_value_fen": basis.get("residual_fen"),
                    "depreciation_method_label": "直线法" if basis else None,
                    "depreciation_group_code": None,
                    "disposal": None,
                }
            )
        else:
            item.update(
                {
                    "available_for_use_date": basis.get("in_use_date"),
                    "life_basis_label": profile.get("useful_life_basis") or "未提供",
                    "life_basis_explanation": profile.get("note") or "",
                    "rights_description": profile.get("rights_description") or "未提供",
                    "retirement": None,
                }
            )
        if disposal:
            disposed, values = disposal["fact"]["data"], disposal["outcome"]["values"]
            references = [
                row
                for row in snap.journal
                if row["basis"]["id"] == disposal["id"] and row["sign"] > 0
            ]
            detail = {
                "date": disposed["disposal_date"],
                "book_value_fen": cost - accumulated,
                "reference": str(references[-1]["number"]) if references else "暂无凭证",
            }
            if fixed:
                detail.update(
                    {
                        "kind": "sale" if disposed["disposal_kind"] == "sale" else "retirement",
                        "gross_proceeds_fen": disposed["gross_proceeds_fen"],
                        "gain_fen": max(values["gain_loss_fen"], 0),
                        "loss_fen": max(-values["gain_loss_fen"], 0),
                        "party": snap.party(disposed.get("buyer_id")),
                    }
                )
            item["disposal" if fixed else "retirement"] = detail
        items.append(item)
    fixed_items = [item for item in items if item["asset_type"] == "fixed"]
    intangible_items = [item for item in items if item["asset_type"] == "intangible"]

    def summary(rows, fixed):
        active = [row for row in rows if row["status"] == "active"]
        result = {
            "registered_count": len(rows),
            "active_count": len(active),
            "items": rows,
            "active_cost_fen": sum(row["cost_fen"] for row in active),
            "active_accumulated_fen": sum(row["accumulated_charge_fen"] for row in active),
            "active_net_fen": sum(row["book_value_fen"] for row in active),
            "month_acquired_count": sum(row["month_acquired"] for row in rows),
            "month_acquired_fen": monthly_acquired["fixed" if fixed else "intangible"],
            "month_cost_adjustment_fen": monthly_adjusted["fixed" if fixed else "intangible"],
            "month_depreciation_fen" if fixed else "month_amortization_fen": sum(
                row["month_charge_fen"] for row in rows
            ),
        }
        if fixed:
            result.update(
                {
                    "pending_count": sum(row["status"] == "pending_activation" for row in rows),
                    "pending_cost_fen": sum(
                        row["cost_fen"] for row in rows if row["status"] == "pending_activation"
                    ),
                    "disposed_count": sum(row["status"] == "disposed" for row in rows),
                    "month_activated_count": sum(row["month_activated"] for row in rows),
                    "month_disposed_count": sum(row["month_exited"] for row in rows),
                }
            )
        else:
            result["retired_count"] = sum(row["status"] == "retired" for row in rows)
            result["month_retired_count"] = sum(row["month_exited"] for row in rows)
        return result

    fixed, intangible = summary(fixed_items, True), summary(intangible_items, False)
    balances = snap.accounts
    ledger_cost = balances["1601"] + balances["1701"]
    ledger_accumulated = -balances["1602"] - balances["1702"]
    card_cost = fixed["active_cost_fen"] + intangible["active_cost_fen"]
    card_accumulated = fixed["active_accumulated_fen"] + intangible["active_accumulated_fen"]
    differences = {
        "cost_fen": ledger_cost - card_cost,
        "accumulated_fen": ledger_accumulated - card_accumulated,
        "net_fen": ledger_cost - ledger_accumulated - card_cost + card_accumulated,
    }
    reconciled = not any(differences.values())
    return {
        "fixed_asset_cost_fen": balances["1601"],
        "accumulated_depreciation_fen": -balances["1602"],
        "fixed_asset_net_fen": balances["1601"] + balances["1602"],
        "intangible_asset_cost_fen": balances["1701"],
        "accumulated_amortization_fen": -balances["1702"],
        "intangible_asset_net_fen": balances["1701"] + balances["1702"],
        "active_count": fixed["active_count"] + intangible["active_count"],
        "registered_count": len(items),
        "ledger_cost_fen": ledger_cost,
        "ledger_accumulated_fen": ledger_accumulated,
        "ledger_net_fen": ledger_cost - ledger_accumulated,
        "card_cost_fen": card_cost,
        "card_accumulated_fen": card_accumulated,
        "card_net_fen": card_cost - card_accumulated,
        "pending_fixed_count": fixed["pending_count"],
        "pending_fixed_cost_fen": fixed["pending_cost_fen"],
        "month_charge_fen": sum(row["month_charge_fen"] for row in items),
        "month_acquired_count": sum(row["month_acquired"] for row in items),
        "month_acquired_fen": sum(monthly_acquired.values()),
        "month_cost_adjustment_fen": sum(monthly_adjusted.values()),
        "month_activated_count": sum(row["month_activated"] for row in items),
        "month_exited_count": sum(row["month_exited"] for row in items),
        "reconciled": reconciled,
        "reconciliation_label": "资产卡片与账面一致" if reconciled else "资产卡片与账面差异需核对",
        "differences": differences,
        "fixed": fixed,
        "intangible": intangible,
    }


def _quarterly_view(plan, closed):
    ready = plan["status"] == "ready"
    exportable = closed["status"] == "ready"
    statements = plan["statements"]
    specifications = (
        (
            "balance_sheet",
            "资产负债表",
            (("ending_fen", "期末余额"), ("beginning_fen", "年初余额")),
            {15, 29, 30, 41, 47, 52, 53},
        ),
        (
            "profit_statement",
            "利润表",
            (("current_fen", "本季金额"), ("year_to_date_fen", "本年累计")),
            {19, 21, 30, 32},
        ),
        (
            "cash_flow_statement",
            "现金流量表",
            (("current_fen", "本季金额"), ("year_to_date_fen", "本年累计")),
            {7, 13, 19, 20, 22},
        ),
    )
    views = [
        {
            "key": key,
            "label": label,
            "columns": [{"key": field, "label": title} for field, title in columns],
            "rows": [
                {
                    "line": int(line),
                    "name": row["name"],
                    "values": {field: row[field] for field, _ in columns},
                    "is_total": int(line) in totals,
                    "has_amount": any(row[field] for field, _ in columns),
                }
                for line, row in sorted(statements[key].items(), key=lambda item: int(item[0]))
            ],
        }
        for key, label, columns, totals in specifications
    ]
    issues = plan["fact_issues"]
    readiness = [
        {
            "key": str(index),
            "label": "报表资料核对",
            "state": "pending",
            "summary": issue["message"],
            "details": [{"primary": issue["message"], "secondary": issue.get("period", "")}],
        }
        for index, issue in enumerate(issues)
    ]
    if not readiness:
        readiness = [
            {
                "key": "ready",
                "label": "报表资料核对",
                "state": "pass",
                "summary": "现有来源具备报表展示条件",
                "details": [],
            }
        ]
    labels = {
        "balance_ending_fen": "期末资产负债平衡",
        "balance_beginning_fen": "年初资产负债平衡",
        "profit_current_fen": "本季利润勾稽",
        "profit_year_to_date_fen": "累计利润勾稽",
        "cash_current_fen": "本季现金流量勾稽",
        "cash_year_to_date_fen": "累计现金流量勾稽",
        "cash_ending_current_fen": "本季现金余额勾稽",
        "cash_ending_year_to_date_fen": "累计现金余额勾稽",
    }
    return {
        "schema_version": 1,
        "status": "ready" if exportable else "in_progress" if ready else "blocked",
        "status_label": "可导出" if exportable else "期间尚未关账" if ready else "资料待补充",
        "headline": "季度财务报表",
        "message": "报表来源已核对" if ready else "请根据核对事项补全来源。",
        "checked_at": datetime.now(UTC).isoformat(),
        "organization": plan["organization"],
        "period": plan["period"]
        | {"label": f"{plan['period']['year']} 年第 {plan['period']['quarter']} 季度"},
        "readiness": readiness,
        "summary": {
            "assets_total_fen": statements["balance_sheet"]["30"]["ending_fen"],
            "liabilities_equity_total_fen": statements["balance_sheet"]["53"]["ending_fen"],
            "current_net_profit_fen": statements["profit_statement"]["32"]["current_fen"],
            "year_to_date_net_profit_fen": statements["profit_statement"]["32"]["year_to_date_fen"],
            "current_cash_change_fen": statements["cash_flow_statement"]["20"]["current_fen"],
            "ending_cash_fen": statements["cash_flow_statement"]["22"]["current_fen"],
        },
        "statements": views,
        "checks": {
            "passed": sum(check["passed"] for check in plan["checks"]),
            "total": len(plan["checks"]),
            "items": [
                check | {"label": labels.get(check["code"], "报表勾稽核对")}
                for check in plan["checks"]
            ],
        },
        "draft": not exportable,
        "export": {
            "available": exportable,
            "file_name": _workbook_name(plan),
            "calculation_hash": closed["digest"] if exportable else None,
            "preview_digest": closed["digest"] if exportable else None,
            "epochs": closed["epochs"] if exportable else None,
        },
        "technical": {
            "calculation_hash": plan["digest"],
            "template": plan["template"] | {"file_name": plan["template"]["name"]},
            "rule": plan["rule"],
            "source_close_hashes": [row["digest"] for row in plan["source_closes"]],
            "classification_count": None,
            "income_tax_confirmation_count": None,
            "requirement_codes": list(dict.fromkeys(issue["field"] for issue in issues)),
            "errors": [],
        },
    }
