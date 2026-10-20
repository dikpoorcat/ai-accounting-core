"""The fixed small-enterprise continuation rows and their accounting identities."""

from ai_accounting_modular.contracts.public import (
    ContractError,
    YearMonth,
    parse_fen,
    sum_fen,
    validate_contract,
)

from ._equations import balance_totals, cash_totals, profit_totals

KIND = "reports.continuation"


def fail(message, code="invalid_input"):
    raise ContractError(code, message)


def problem(fact, field, message, *, missing=False):
    raise ContractError(
        "needs_information" if missing else "dependency_conflict",
        message,
        fact_issues=[
            {
                "code": "missing_continuation_basis" if missing else "inconsistent_continuation",
                "business_id": fact["business_id"],
                "field": field,
                "meaning": "建账前的报表接续依据",
                "message": message,
                "allowed_precision": ["month", "fen", "source"],
                "reusable_sources": [{"source_id": sid} for sid in fact["evidence_ids"][:500]],
                "blocking_scope": "business",
            }
        ],
    )


def total(values):
    try:
        return sum_fen(values)
    except ValueError as error:
        raise ContractError("invalid_input", "报表接续的合计超出有符号64位整数分范围。") from error


def rows(fact, name, column):
    return {row["line"]: parse_fen(row[column]) for row in fact["data"][name]}


def _formula(fact, name, column, calculator):
    values = rows(fact, name, column)
    cells = {
        row["line"]: {"state": "known", "amount_fen": row[column]} for row in fact["data"][name]
    }
    for line, expected in calculator(cells).items():
        if cells[line] != expected:
            index = next(i for i, row in enumerate(fact["data"][name]) if row["line"] == line)
            problem(
                fact,
                f"/data/{name}/{index}/{column}",
                f"接续{name}第{line}行不符合固定报表合计公式。",
            )
    return values


def validate_fact(fact, start_period=None):
    validate_contract("business-facts.schema.json", "Fact", fact)
    if fact["kind"] != KIND:
        fail("报表接续模块只接收reports.continuation。", "unsupported_business")
    data = fact["data"]
    start = data["bookkeeping_start"]
    if not fact["evidence_ids"]:
        problem(
            fact,
            "/evidence_ids",
            "三表接续需要至少一项可复用的原件、标准资料或有效补充来源。",
            missing=True,
        )
    if fact["period"] != start or start_period is not None and start != start_period:
        problem(
            fact, "/data/bookkeeping_start", "报表接续的业务月份、建账起点和本公司建账月必须一致。"
        )
    for name, count in (("year_beginning_balance", 53), ("prior_profit", 32), ("prior_cash", 22)):
        if {row["line"] for row in data[name]} != set(range(1, count + 1)):
            problem(
                fact, f"/data/{name}", f"接续{name}必须完整包含1至{count}行，各行只能出现一次。"
            )
    balance = _formula(fact, "year_beginning_balance", "beginning_fen", balance_totals)
    if balance[30] != balance[53]:
        problem(fact, "/data/year_beginning_balance", "年初资产总额必须等于负债和所有者权益总额。")
    for column in ("quarter_to_date_fen", "year_to_date_fen"):
        profit = _formula(fact, "prior_profit", column, profit_totals)
        cash = _formula(fact, "prior_cash", column, cash_totals)
        if column == "year_to_date_fen" and cash[21] != balance[1]:
            problem(
                fact,
                "/data/prior_cash",
                "接续现金流本年累计的期初现金必须等于年初资产负债表货币资金。",
            )
        empty_span = (
            int(start[5:]) in (1, 4, 7, 10)
            and column == "quarter_to_date_fen"
            or start[5:] == "01"
            and column == "year_to_date_fen"
        )
        if empty_span:
            for name, values in (
                ("prior_profit", profit.values()),
                ("prior_cash", (cash[line] for line in range(1, 21))),
            ):
                if any(values):
                    problem(
                        fact,
                        f"/data/{name}",
                        "建账月位于所述年度或季度首月，此前该范围的发生额必须为零。",
                    )
    if int(start[5:]) <= 3:
        for name in ("prior_profit", "prior_cash"):
            if any(row["quarter_to_date_fen"] != row["year_to_date_fen"] for row in data[name]):
                problem(
                    fact, f"/data/{name}", "第一季度的建账前当季和本年范围相同，两列必须逐行一致。"
                )
    if "income_tax_carry" in data:
        carry = data["income_tax_carry"]
        previous = (
            str(YearMonth.from_ordinal(YearMonth(start).ordinal - 1)) if start > "0001-01" else None
        )
        if (
            carry["tax_year"] != int(start[:4])
            or carry["period_to"] != previous
            or start[5:] == "01"
        ):
            problem(
                fact,
                "/data/income_tax_carry",
                "累计已计所得税必须属于建账当年，准确截止于建账前一个月；一月不承接上年累计税额。",
            )
