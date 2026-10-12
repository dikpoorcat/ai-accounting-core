"""Prepare documentary report details from this target's verified publications.

Public input names a business calculation and exact classification amounts.
Voucher versions, line positions, account roles and direction come exclusively
from the current typed publication in one read snapshot.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from .account_definitions import CASH_ACCOUNTS
from .contracts import KernelError
from .replay_contracts import Ref, ReplayId
from .reports import CASH_CATEGORIES, INFLOW_ROWS, DetailCode, ReportClassification
from .types import PositiveFen, YearMonth, canonical


class ReplayProfitDetail(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    detail_code: DetailCode
    amount_fen: PositiveFen


class ReplayCashDetail(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    category: Literal[1, 2, 3, 4, 5, 6, 8, 9, 10, 11, 12, 14, 15, 16, 17, 18]
    amount_fen: PositiveFen


class ReplayCounterpartyDetail(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    counterparty_id: ReplayId


class ReplayReportClassificationItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    source_id: Ref
    source_subject_id: ReplayId
    source_calculation_id: ReplayId
    period: YearMonth
    profit_details: tuple[ReplayProfitDetail, ...] = ()
    counterparty_details: tuple[ReplayCounterpartyDetail, ...] = ()
    cash_details: tuple[ReplayCashDetail, ...] = ()

    @model_validator(mode="after")
    def details_present(self):
        if not (self.profit_details or self.counterparty_details or self.cash_details):
            raise ValueError("classification requires explicit report details")
        return self


def _issue(field, code, message, **details):
    return {
        "field": field,
        "code": code,
        "message": message,
        "semantics": "accounting",
        "allowed_precision": ["exact"],
        "reusable_sources": ["current_published_business_result"],
        **details,
    }


def _unresolved(item, issues):
    return {
        "source_id": item.source_id,
        "status": "needs_information",
        "data": None,
        "fact_issues": issues,
    }


def _family(detail_code):
    return {"management": "5602", "sales": "5601", "finance": "5603", "tax": "5403"}[
        detail_code.split("_", 1)[0]
    ]


def _cash_category(line, semantic):
    category = CASH_CATEGORIES.get(line["cashflow"])
    fact = semantic["cash_source"]
    if line["cashflow"] == "financing_receipts":
        return 15 if getattr(fact, "funding_kind", None) == "capital" else 14
    if line["cashflow"] == "operating_payments":
        expense_class = getattr(fact, "expense_class", None)
        return (
            3
            if expense_class == "service"
            else (6 if expense_class in {"administration", "sales", "bank_fee"} else None)
        )
    if line["cashflow"] == "payroll":
        return 5 if semantic.get("cash_obligation", {}).get("account") == "222103" else 4
    return category


def _select_unique(candidates, field, issues):
    if len(candidates) != 1:
        issues.append(
            _issue(
                field,
                "classification_line_not_unique",
                "分类须按业务来源、账户类别和精确金额唯一定位当前凭证行",
                candidate_count=len(candidates),
            )
        )
        return None
    return candidates[0]


def _prepare_details(reads, item, calculation, version_id, lines):
    from .fund_accounts import require_fund_account
    from .report_semantics import immutable_line_fields, report_source_fact

    issues, profit_details, cash_details = [], [], []
    raw_facts = calculation["fact_data"]
    values = calculation["outcome"]["values"]
    kind = calculation["kind"]

    def source_calculation(ident):
        reads.verify_selected_content({ident})
        return reads.raw_calculations({ident})[ident]

    for index, detail in enumerate(item.profit_details):
        family = _family(detail.detail_code)
        candidates = [
            line
            for line in lines
            if line["account"] == family
            and abs(line["debit"] - line["credit"]) == detail.amount_fen
        ]
        if kind == "managed_reserve_refund":
            candidates = [line for line in candidates if line["credit"] > 0 and not line["debit"]]
        elif kind in {"expense", "managed_reserve_expense", "payroll_reserve_payment"}:
            candidates = [line for line in candidates if line["debit"] > 0 and not line["credit"]]
        selected = _select_unique(candidates, f"profit_details.{index}", issues)
        if selected:
            profit_details.append(
                {"line_no": selected["line_no"], **detail.model_dump(mode="json")}
            )

    # Use verified typed account definitions, never a prefix guess for a funds code.
    parents = {account: account for account in CASH_ACCOUNTS}
    if item.cash_details:
        for field in ("bank_account_id", "cash_account_id", "platform_account_id"):
            entity_id = raw_facts.get(field) or values.get(field)
            if entity_id:
                definition = require_fund_account(reads.connection, entity_id)
                parents[definition["detail_code"]] = definition["parent_account"]
    semantics = {}
    for index, detail in enumerate(item.cash_details):
        inflow = detail.category in INFLOW_ROWS
        candidates = []
        for line in lines:
            if parents.get(line["account"]) not in CASH_ACCOUNTS:
                continue
            signed = line["debit"] - line["credit"]
            if abs(signed) != detail.amount_fen or (signed > 0) != inflow:
                continue
            if line["line_no"] not in semantics:
                semantics[line["line_no"]] = immutable_line_fields(
                    line,
                    item.source_calculation_id,
                    calculation=source_calculation,
                    source_fact=lambda ident: report_source_fact(source_calculation(ident)),
                    relations=reads.relations,
                )
            semantic = semantics[line["line_no"]]
            if semantic["relation_issues"]:
                continue
            automatic = _cash_category(line, semantic)
            if automatic is not None and automatic != detail.category:
                continue
            # Actual payment direction is a typed business fact, not an input
            # supplied by classification and not reconstructed from old lines.
            direction = raw_facts.get("direction", values.get("direction"))
            if direction in {"inflow", "outflow"} and (direction == "inflow") != inflow:
                continue
            candidates.append(line)
        selected = _select_unique(candidates, f"cash_details.{index}", issues)
        if selected:
            cash_details.append({"line_no": selected["line_no"], **detail.model_dump(mode="json")})
    for label, details in (("profit_details", profit_details), ("cash_details", cash_details)):
        for line_no in {detail["line_no"] for detail in details}:
            line = next(row for row in lines if row["line_no"] == line_no)
            if sum(
                detail["amount_fen"] for detail in details if detail["line_no"] == line_no
            ) != abs(line["debit"] - line["credit"]):
                issues.append(
                    _issue(
                        label,
                        "classification_amount_not_complete",
                        "分类必须完整覆盖当前原行金额，不能重复分配或保留部分金额",
                    )
                )
    if issues:
        return _unresolved(item, issues)
    data = {
        "period": str(item.period),
        "voucher_version_id": version_id,
        "profit_details": profit_details,
        "counterparties": [],
        "cash_details": cash_details,
    }
    ReportClassification.model_validate_json(canonical(data))
    return {"source_id": item.source_id, "status": "ready", "data": data, "fact_issues": []}


def prepare_source_report_classifications(engine, items):
    """Read only: resolve every supplied detail against one consistent target snapshot."""
    from .publication import active_tranches, verify_publication_chain
    from .query_reads import QueryReads, verify_current_publication_voucher_heads

    typed = [
        item
        if isinstance(item, ReplayReportClassificationItem)
        else ReplayReportClassificationItem.model_validate_json(canonical(item))
        for item in items
    ]
    result = []
    with QueryReads.snapshot(engine) as reads:
        connection = reads.connection
        for item in typed:
            if item.counterparty_details:
                result.append(
                    _unresolved(
                        item,
                        [
                            _issue(
                                "counterparty_details",
                                "classification_counterparty_unsupported",
                                "交易方分类尚无可唯一定位的类型化来源合同",
                            )
                        ],
                    )
                )
                continue
            head = connection.execute(
                "SELECT h.calculation_id,f.fact_id,EXISTS(SELECT 1 FROM pending p "
                "WHERE p.subject_id=h.subject_id) AS pending FROM calculation_current h "
                "JOIN fact_current f ON f.subject_id=h.subject_id WHERE h.subject_id=?",
                (item.source_subject_id,),
            ).fetchone()
            if (
                head is None
                or head["calculation_id"] != item.source_calculation_id
                or head["pending"]
            ):
                result.append(
                    _unresolved(
                        item,
                        [
                            _issue(
                                "source_calculation_id",
                                "classification_source_not_current",
                                "分类来源必须为本轮当前正式采用且无待更正的计算",
                            )
                        ],
                    )
                )
                continue
            calculation = reads.raw_calculations({item.source_calculation_id})[
                item.source_calculation_id
            ]
            reads.verify_selected_content({item.source_calculation_id})
            if (
                calculation["subject_id"] != item.source_subject_id
                or calculation["fact_id"] != head["fact_id"]
            ):
                result.append(
                    _unresolved(
                        item,
                        [
                            _issue(
                                "source_calculation_id",
                                "classification_source_fact_changed",
                                "来源计算须精确采用当前业务事实版本",
                            )
                        ],
                    )
                )
                continue
            verify_publication_chain(connection, {item.source_subject_id})
            publications = [
                row
                for row in active_tranches(connection, subject_ids={item.source_subject_id})
                if row["calculation_id"] == item.source_calculation_id and row["voucher_id"]
            ]
            reads.verify_publication_records(publications)
            verify_current_publication_voucher_heads(connection, publications)
            versions = {
                row[0]
                for row in connection.execute(
                    "SELECT h.version_id FROM json_each(?) ids JOIN voucher_current h "
                    "ON h.voucher_id=ids.value",
                    (canonical(sorted({row["voucher_id"] for row in publications})),),
                )
            }
            if len(versions) != 1:
                result.append(
                    _unresolved(
                        item,
                        [
                            _issue(
                                "source_calculation_id",
                                "classification_voucher_not_unique",
                                "业务来源须唯一对应本轮正式凭证版本",
                                candidate_count=len(versions),
                            )
                        ],
                    )
                )
                continue
            version_id = next(iter(versions))
            voucher = reads.vouchers(versions)[version_id]
            if (
                voucher["calculation_id"] != item.source_calculation_id
                or voucher["period"] != item.period.ordinal
            ):
                result.append(
                    _unresolved(
                        item,
                        [
                            _issue(
                                "period",
                                "classification_period_conflict",
                                "分类月份及计算必须对应实际当前凭证",
                            )
                        ],
                    )
                )
                continue
            lines = reads.voucher_lines(versions)[version_id]
            fields = ("account", "debit", "credit", "cashflow")
            if [tuple(line[field] for field in fields) for line in lines] != [
                tuple(line[field] for field in fields) for line in calculation["outcome"]["lines"]
            ] or [line["line_no"] for line in lines] != list(range(1, len(lines) + 1)):
                raise KernelError(
                    "content_integrity_failed", "分类使用的凭证行与封存核算结果不一致"
                )
            result.append(_prepare_details(reads, item, calculation, version_id, lines))
    return result


def compile_report_classifications(builder, document):
    """Discard historical positions; compile preparation followed by exact live data."""
    for item in document.get("facts", []):
        if item.get("kind") != "report_classification":
            continue
        key, original = item["key"], item["data"]
        reference = original.get("voucher_version_id")
        if not isinstance(reference, str) or not reference.startswith("$ref:"):
            builder.issue(
                key, "classification_source_reference_missing", "分类必须引用最终业务来源键"
            )
            continue
        source, separator, binding = reference[5:].rpartition(":")
        if not source or not separator or binding != "voucher_version_id":
            builder.issue(key, "classification_source_reference_missing", "分类必须有明确业务来源")
            continue
        allowed = {
            "period",
            "voucher_version_id",
            "profit_details",
            "counterparties",
            "cash_details",
        }
        if set(original) - allowed:
            builder.issue(key, "classification_template_invalid", "分类模板有未知字段")
            continue
        typed = {
            "source_id": builder.reference_id(key),
            "source_subject_id": f"$ref:{source}:subject_id",
            "source_calculation_id": f"$ref:{source}:calculation_id",
            "period": original["period"],
            "profit_details": [
                {k: v for k, v in row.items() if k != "line_no"}
                for row in original.get("profit_details", [])
            ],
            "counterparty_details": [
                {k: v for k, v in row.items() if k != "line_no"}
                for row in original.get("counterparties", [])
            ],
            "cash_details": [
                {k: v for k, v in row.items() if k != "line_no"}
                for row in original.get("cash_details", [])
            ],
        }
        try:
            ReplayReportClassificationItem.model_validate_json(canonical(typed))
        except ValidationError as error:
            for problem in error.errors(include_input=False, include_url=False):
                builder.issue(
                    key + "/" + "/".join(map(str, problem["loc"])),
                    "classification_template_invalid",
                    problem["msg"],
                )
            continue
        if typed["counterparty_details"]:
            builder.issue(
                key, "classification_counterparty_unsupported", "当前分类准备不支持交易方分类"
            )
        preparation_key = "classification-prepare:" + key
        dependencies = []
        dependency = builder.dependency(source, published=True)
        if dependency:
            dependencies.append(dependency)
        preparation = builder.add_step(
            preparation_key,
            "replay_classification_prepare",
            {"items": [typed]},
            depends_on=dependencies,
            outputs={"data": "items.0.data"},
        )
        subject_id = builder.allocate_subject(key)
        evidence = builder.evidence(item.get("source_keys", []), key)
        saved = builder.add_step(
            "classification-save:" + key,
            "save_facts",
            {
                "facts": [
                    {
                        "kind": "report_classification",
                        "subject_id": subject_id,
                        "data": f"$ref:{preparation_key}:data",
                        "evidence": evidence,
                        "expected_revision": 0,
                    }
                ]
            },
            depends_on=(preparation,),
            outputs={"fact_id": "results.0.fact_id", "revision": "results.0.revision"},
        )
        builder.save_steps[key] = saved
        builder.bind(key, "fact_id", saved, "results.0.fact_id")
        builder.bind(key, "revision", saved, "results.0.revision")
