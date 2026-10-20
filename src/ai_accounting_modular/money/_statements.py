"""Original rows prove activity and never create another company payment."""

from ._common import account, amount, base_plan, fail, total


def check_rows(fact):
    data = fact["data"]
    rows = data["rows"]
    ids = [row["row_id"] for row in rows]
    if len(ids) != len(set(ids)):
        fail("同一原始清单的行身份不能重复。", "invalid_input")
    references = [row["reference"] for row in rows if "reference" in row]
    if len(references) != len(set(references)):
        fail("同一原始清单已提供的银行参考号不能重复。", "invalid_input")
    if any(row["actual_on"][:7] != fact["period"] for row in rows):
        fail("原始行实际日期必须属于清单月份。", "invalid_input")
    if fact["kind"] == "money.bank_statement":
        change = total(
            amount(row["amount_fen"]) * (1 if row["direction"] == "inflow" else -1) for row in rows
        )
        if total((amount(data["opening_fen"]), change)) != amount(data["closing_fen"]):
            fail("银行期初加有符号原行金额必须等于银行期末。")


def statement(fact, context):
    check_rows(fact)
    data = fact["data"]
    if fact["kind"] == "money.bank_statement":
        values = {
            "funds_account_id": data["funds_account_id"],
            "opening_fen": data["opening_fen"],
            "closing_fen": data["closing_fen"],
            "row_count": len(data["rows"]),
        }
    else:
        values = {
            "platform_account_id": data["platform_account_id"],
            "row_count": len(data["rows"]),
            "company_money_effect_fen": "0",
        }
    return base_plan(fact, context, values)


def _account_facts(context, account_id, period):
    return context.related_facts(
        account_id, "identity", producer_kind="object", period_from=period, period_to=period
    )


def original_rows(context, account_id, period):
    result = {}
    for candidate in _account_facts(context, account_id, period):
        if (
            candidate["kind"] != "money.platform_activity"
            or candidate["data"]["platform_account_id"] != account_id
        ):
            continue
        for row in candidate["data"]["rows"]:
            if row["row_id"] in result:
                fail("同一平台原行身份存在于多份当前清单。")
            result[row["row_id"]] = (row, candidate)
    return result


def fund_sources(fact, fund_movements, context):
    """Return exact platform-row evidence without editing the owner's plan."""
    data = fact["data"]
    ids = data.get("platform_row_ids", ())
    expected = []
    for movement in fund_movements:
        _, kind = account(fact, context, movement["funds_account_id"], "/data")
        if kind == "platform_account":
            expected.append(movement)
    if not expected:
        if ids:
            fail("没有平台实际资金端时不能消费平台原行。", "invalid_input")
        return ()
    selected, used = {}, set()
    for movement in expected:
        account_id = movement["funds_account_id"]
        rows = original_rows(context, account_id, fact["period"])
        matched = validate_platform_movement(
            movement, {key: pair[0] for key, pair in rows.items()}, ids
        )
        for row_id in matched:
            selected[row_id] = rows[row_id][1]
        used.update(matched)
        for other in _account_facts(context, account_id, fact["period"]):
            if other["business_id"] == fact["business_id"]:
                continue
            if used.intersection(other["data"].get("platform_row_ids", ())):
                fail("同一平台原行已被其他当前资金或代管内部业务消费。")
    if used != set(ids):
        fail("所选平台原行不属于本次真实资金的公司平台账户。")
    locations = []
    for row_id, owner in selected.items():
        location = next(row["source"] for row in owner["data"]["rows"] if row["row_id"] == row_id)
        if location not in locations:
            locations.append(dict(location))
    context.resolve_sources(list(dict.fromkeys(location["source_id"] for location in locations)))
    return tuple(locations)


def validate_platform_movement(movement, rows, row_ids):
    """Use identical original-row matching during adoption and closing."""
    matched = [row_id for row_id in row_ids if row_id in rows]
    if len(matched) != len(set(matched)):
        fail("同一平台原行不能重复计入一笔真实资金。")
    if any(
        rows[row_id]["actual_on"] != movement["actual_on"]
        or rows[row_id]["direction"] != movement["direction"]
        for row_id in matched
    ):
        fail("平台原行的实际日期、方向与真实资金不一致。")
    if not matched or total(amount(rows[key]["amount_fen"]) for key in matched) != amount(
        movement["amount_fen"]
    ):
        fail("平台真实资金必须消费本账户、同日、同方向、等额的完整原行。")
    return matched


def reserve_internal(fact, context):
    data = fact["data"]
    account_id = data["platform_account_id"]
    rows = original_rows(context, account_id, fact["period"])
    if not all(row_id in rows for row_id in data["platform_row_ids"]):
        fail("代管内部说明引用的平台原行不存在或账户、月份不一致。")
    for other in _account_facts(context, account_id, fact["period"]):
        if other["business_id"] != fact["business_id"] and set(
            data["platform_row_ids"]
        ).intersection(other["data"].get("platform_row_ids", ())):
            fail("平台原行已经承接其他真实资金或代管内部业务。")
    plan = base_plan(
        fact,
        context,
        {
            "platform_account_id": account_id,
            "row_count": len(data["platform_row_ids"]),
            "company_money_effect_fen": "0",
        },
    )
    for row_id in data["platform_row_ids"]:
        location = rows[row_id][0]["source"]
        context.resolve_sources([location["source_id"]])
        if location not in plan["basis"]["source_locations"]:
            plan["basis"]["source_locations"].append(dict(location))
    return plan
