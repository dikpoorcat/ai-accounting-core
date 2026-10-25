"""Complete statement groups, including all actual funds in the account month."""

from ai_accounting_modular.contracts.public import ContractError, YearMonth

from ._common import adopt_source, amount, base_plan, fail, issue, stable_id, total


def previous_period(period):
    value = YearMonth(period)
    return str(YearMonth.from_ordinal(value.ordinal - 1)) if value.ordinal else None


def statement_for(fact, context):
    statement = context.get_fact(fact["data"]["statement_id"])
    if statement is None:
        code = "business_not_found"
        message = "引用的银行清单不存在；先查询已登记业务并使用有效引用。"
    elif statement["kind"] != "money.bank_statement":
        code, message = "incompatible_business_kind", "引用的业务不是银行原始清单。"
    elif statement["period"] != fact["period"]:
        code, message = "incompatible_period", "银行核对必须引用同月的银行原始清单。"
    else:
        return statement
    raise ContractError(
        "invalid_input",
        message,
        fact_issues=[issue(fact, "/data/statement_id", message, code, ("identity",))],
    )


def reconcile_plan(fact, context):
    statement = statement_for(fact, context)
    data = statement["data"]
    result = context.get_result(statement["business_id"], "main")
    if result is None or result.target["kind"] != "money.bank_statement":
        fail("银行原始清单尚未取得有效正式结果。")
    plan = base_plan(
        fact,
        context,
        {
            "funds_account_id": data["funds_account_id"],
            "opening_fen": data["opening_fen"],
            "closing_fen": data["closing_fen"],
            "row_count": len(data["rows"]),
            "matched_row_count": sum(
                len(group["statement_row_ids"]) for group in fact["data"]["matches"]
            ),
            "difference_fen": "0",
        },
    )
    adopt_source(plan, result.source)
    _, opening_source = _opening(fact, context, statement)
    if opening_source is not None:
        adopt_source(plan, opening_source)
    return plan


def _opening(fact, context, statement):
    account_id = statement["data"]["funds_account_id"]
    account = context.get_object(account_id)
    card = account["data"]
    period = fact["period"]
    if card.get("opened_period") == period and card.get("zero_at_opening") is True:
        return 0, None
    previous = previous_period(period)
    if previous is not None:
        candidates = context.related_facts(
            account_id, "identity", producer_kind="object", period_from=previous, period_to=previous
        )
        previous_checks = [other for other in candidates if other["kind"] == "money.reconciliation"]
        if len(previous_checks) > 1:
            fail("同一账户上月出现多份当前银行核对。")
        if previous_checks:
            previous_result = context.get_result(previous_checks[0]["business_id"], "main")
            if previous_result is None or previous_result.target["kind"] != "money.reconciliation":
                fail("上月银行核对尚未形成有效正式结果。")
            return amount(previous_result.target["values"]["closing_fen"]), previous_result.source
    for opening in context.facts_in_scope(("opening.package",), period, period):
        result = context.get_result(opening["business_id"], "main")
        if result is None or result.target["kind"] != "opening.package":
            fail("账面期初尚未取得有效正式结果。")
        matches = [
            member
            for member in result.basis["input"]["members"]
            if member["data"]["type"] == "bank" and member["data"]["funds_account_id"] == account_id
        ]
        if len(matches) > 1:
            fail("账面期初对同一银行账户存在重复起点。")
        if matches:
            return amount(matches[0]["data"]["balance_fen"]), result.source
    raise ContractError(
        "needs_information",
        "银行核对缺少有据账面起点或连续上月完整核对。",
        fact_issues=[
            issue(
                fact,
                "/data/statement_id",
                "需要该账户的正式接账期初、开户零余额依据，或连续上月的完整核对；不能直接采用本月银行期初。",
                "missing_book_opening",
                ("month", "fen", "source"),
            )
        ],
    )


def validate_reconciliation(fact, context):
    statement = statement_for(fact, context)
    expected_opening, _ = _opening(fact, context, statement)
    validate_complete_statement(
        statement,
        context.fund_movements(
            statement["data"]["funds_account_id"], fact["period"], fact["period"]
        ),
        fact["data"]["matches"],
        expected_opening,
    )


def validate_complete_statement(statement, definitions, matches, expected_opening):
    """One complete matching rule for adoption and the later closing check."""
    data = statement["data"]
    if expected_opening != amount(data["opening_fen"]):
        fail("银行期初与有据账面起点或上月已核对期末不一致。")
    funds = {item.value["movement_id"]: item.value for item in definitions}
    if len(funds) != len(definitions):
        fail("本账户完整正式资金集合有重复实际发生身份。")
    rows = {row["row_id"]: row for row in data["rows"]}
    seen_rows, seen_funds = set(), set()
    for match in matches:
        row_ids, movement_ids = match["statement_row_ids"], match["fund_movement_ids"]
        if seen_rows.intersection(row_ids) or seen_funds.intersection(movement_ids):
            fail("银行核对的不同匹配组不能重复使用原行或真实资金。")
        if not set(row_ids).issubset(rows) or not set(movement_ids).issubset(funds):
            fail("匹配组包含本账户本月范围以外的原行或真实资金。")
        selected_rows, selected_funds = (
            [rows[key] for key in row_ids],
            [funds[key] for key in movement_ids],
        )
        directions = {row["direction"] for row in [*selected_rows, *selected_funds]}
        dates = {row["actual_on"] for row in [*selected_rows, *selected_funds]}
        if len(directions) != 1 or len(dates) != 1:
            fail("一个银行匹配组的方向和实际日期必须一致，不能用跨日或反向差额凑平。")
        row_amount = total(amount(row["amount_fen"]) for row in selected_rows)
        movement_amount = total(amount(row["amount_fen"]) for row in selected_funds)
        if row_amount != movement_amount:
            fail("匹配组两边真实金额合计不相等，不能自动补手续费或收入。")
        seen_rows.update(row_ids)
        seen_funds.update(movement_ids)
    if seen_rows != set(rows) or seen_funds != set(funds):
        fail("完整银行核对必须覆盖本月全部原始行及本账户全部应核对真实资金。")
    expected_closing = total(
        (
            expected_opening,
            *(
                amount(row["amount_fen"]) * (1 if row["direction"] == "inflow" else -1)
                for row in funds.values()
            ),
        )
    )
    if expected_closing != amount(data["closing_fen"]):
        fail("完整真实资金形成的账面期末与银行期末不一致。")


def groups_from_rows(fact, statement):
    """Storage projection after the unchanged complete candidate was checked."""
    data = statement["data"]
    rows = {row["row_id"]: row for row in data["rows"]}
    groups = []
    for match in fact["data"]["matches"]:
        row_ids, movement_ids = match["statement_row_ids"], match["fund_movement_ids"]
        direction = rows[row_ids[0]]["direction"]
        groups.append(
            {
                "group_id": stable_id(
                    fact["business_id"],
                    "reconciliation/"
                    + "/".join(sorted(row_ids))
                    + "/"
                    + "/".join(sorted(movement_ids)),
                ),
                "account_id": data["funds_account_id"],
                "direction": "in" if direction == "inflow" else "out",
                "amount_fen": total(amount(rows[key]["amount_fen"]) for key in row_ids),
                "members": {"bank_row": sorted(row_ids), "occurrence": sorted(movement_ids)},
            }
        )
    return groups
