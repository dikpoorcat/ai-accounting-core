"""Financial VAT components come from strict formal quantities, never a net GL guess."""

from ai_accounting_modular.contracts.public import parse_fen, validate_contract

from ._balances import sum_amounts
from ._values import fail

KINDS = frozenset(
    {"taxes.financial_transfer_basis", "taxes.assessment", "taxes.actual_declaration"}
)


def validate_row(row):
    if (
        type(row) is not list
        or len(row) != 6
        or row[0] not in {"position", "assessment"}
        or any(type(amount) is not int or amount < 0 for amount in row[4:])
    ):
        fail("storage_corrupt", "金融税资产及纳税期组成的正式投影损坏。")
    for period in row[1:4]:
        validate_contract("core.schema.json", "Month", period)
    for amount in row[4:]:
        validate_contract("core.schema.json", "NonNegativeFen", str(amount))
    if (
        row[2] > row[3]
        or (row[0] == "position" and not row[2] <= row[1] <= row[3])
        or (row[0] == "assessment" and row[1] < row[3])
    ):
        fail("storage_corrupt", "金融税月度位置不属于其正式纳税期。")
    if row[0] == "assessment" and row[4]:
        fail("storage_corrupt", "纳税期采用不能另造金融负差资产。")
    return row


def capture(selection, heads, resources):
    selected = [
        head
        for head in heads
        if selection.boundary_kind(head) in KINDS
        and (
            head["result_slot"].startswith("financial_transfer:")
            or any(
                row["account_code"] == "222107"
                for row in resources[head["result_id"]]["obligation"]
            )
        )
    ]
    selection.load_bases(selected)
    result = {head["result_id"]: None for head in heads}
    for head in selected:
        quantities = [
            item
            for item in selection.basis(head)["adopted_quantities"]
            if item["type"]
            in {"financial_transfer_tax_position", "financial_transfer_tax_assessment"}
        ]
        if not quantities:
            if head["result_slot"].startswith("financial_transfer:"):
                fail("storage_corrupt", "金融税月度结果缺少正式资产及本期税负组成。")
            continue
        if len(quantities) != 1:
            fail("storage_corrupt", "同一金融税正式结果不能发布重复的位置或纳税期采用。")
        item = quantities[0]
        validate_contract("business-facts.schema.json", "AdoptedQuantity", item)
        position = item["type"] == "financial_transfer_tax_position"
        if position != (selection.boundary_kind(head) == "taxes.financial_transfer_basis"):
            fail("storage_corrupt", "金融税月度及纳税期采用的正式量属于错误结果类型。")
        row = [
            "position" if position else "assessment",
            item["period"] if position else head["business_period"],
            item["tax_period_start"],
            item["tax_period_end"],
            parse_fen(item["carry_asset_fen"]) if position else 0,
            parse_fen(item["current_period_payable_fen"] if position else item["amount_fen"]),
        ]
        validate_row(row)
        if position:
            if (
                head["result_slot"] != "financial_transfer:" + row[1]
                or head["business_period"] != row[1]
            ):
                fail("storage_corrupt", "金融税月度正式量与其结果槽及所属月不一致。")
        else:
            obligations = [
                value
                for value in resources[head["result_id"]]["obligation"]
                if value["public_slot"] == item["obligation_slot"]
            ]
            if (
                len(obligations) != 1
                or obligations[0]["account_code"] != "222107"
                or obligations[0]["direction"] != "payable"
                or obligations[0]["amount_fen"] != row[5]
            ):
                fail("storage_corrupt", "金融VAT采用的完整税期与同源正式义务金额不一致。")
        result[head["result_id"]] = row
    return result


def components(rows):
    """The latest month proves the asset; each tax period retains its latest adoption."""
    latest, periods = None, {}
    for atom, row in rows:
        validate_row(row)
        key = row[2], row[3]
        order = row[1], row[0] == "assessment"
        previous = periods.get(key)
        if previous is None or order > previous[0]:
            periods[key] = order, atom, row
        elif order == previous[0]:
            fail("storage_corrupt", "同一金融税月份或完整纳税期存在多个正式采用。")
        if row[0] == "position":
            if latest is None or row[1] > latest[1][1]:
                latest = atom, row
            elif row[1] == latest[1][1]:
                fail("storage_corrupt", "同一月份存在多个金融负差正式位置。")
    unpaid = sum_amounts(row[5] for _, _, row in periods.values() if row[0] == "position")
    return (latest[1][4] if latest else 0), unpaid
