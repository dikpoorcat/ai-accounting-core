"""Asset page labels preserve the owner's formal lifecycle and amount precision."""

from ai_accounting_modular.contracts.public import format_fen


def amount(value):
    return None if value is None else format_fen(value)


def card_value(card):
    if card.state == "unestablished":
        return {
            "asset_id": card.asset_id,
            "asset_type": card.asset_type,
            "name": card.object["name"],
            "selection_status": "unestablished",
            "cost_fen": None,
            "accumulated_charge_fen": None,
            "month_charge_fen": None,
            "book_value_fen": None,
        }
    fixed = card.asset_type == "fixed"
    status = {
        "pending": "pending_activation",
        "active": "active",
        "exited": "disposed" if fixed else "retired",
    }[card.state]
    profile, payment = card.object["data"], card.payment
    result = {
        "asset_id": card.asset_id,
        "asset_type": card.asset_type,
        "code": profile.get("code", ""),
        "name": card.object["name"],
        "category": card.asset_type,
        "category_label": profile.get("category", "固定资产" if fixed else "无形资产"),
        "status": status,
        "status_label": {
            "active": "使用中",
            "pending_activation": "待启用",
            "disposed": "已处置",
            "retired": "已终止",
        }[status],
        "acquisition_date": card.acquired_on or card.acquired_period,
        "posting_period": card.origin["posting_period"],
        "recognition_label": card.acquired_on
        or (
            card.acquired_period + "（按月确认）"
            if card.acquired_period is not None
            else "期初接续"
        ),
        "settlement_scope": {
            "batch": "本验收批次结算",
            "project_sources": "成本来源结算（不分摊为本资产付款）",
            "opening": "期初接续",
        }.get(payment.scope, "本资产结算"),
        "payment_summary": {
            "obligation_count": payment.obligation_count,
            "checking": any(
                value is None
                for value in (
                    payment.amount_fen,
                    payment.paid_fen,
                    payment.other_settled_fen,
                    payment.remaining_fen,
                )
            ),
            "amount_fen": amount(payment.amount_fen),
            "paid_fen": amount(payment.paid_fen),
            "other_settled_fen": amount(payment.other_settled_fen),
            "remaining_fen": amount(payment.remaining_fen),
        },
        "cost_fen": amount(card.cost_fen),
        "accumulated_charge_fen": amount(card.accumulated_fen),
        "month_charge_fen": amount(card.month_charge_fen),
        "book_value_fen": amount(card.net_fen),
        "month_acquired": card.month_acquired,
        "month_activated": card.month_activated,
        "month_exited": card.month_exited,
        "in_service_date" if fixed else "available_for_use_date": card.in_use_on
        or card.in_use_period,
        "disposal" if fixed else "retirement": None,
    }
    if card.exit is not None:
        exit = card.exit
        detail = {"date": exit.actual_on, "book_value_fen": amount(exit.carrying_fen)}
        if fixed:
            detail.update(
                kind="sale" if exit.kind == "sale" else "retirement",
                gross_proceeds_fen=amount(exit.gross_proceeds_fen),
                gain_fen=amount(max(exit.gain_loss_fen, 0)),
                loss_fen=amount(max(-exit.gain_loss_fen, 0)),
                party=exit.buyer["name"] if exit.buyer is not None else "",
            )
        result["disposal" if fixed else "retirement"] = detail
    return result


def project_value(item):
    return {
        "source_id": item.reference["business_id"] + ":" + item.reference["slot"],
        "project_id": item.project["object_id"],
        "period": item.period,
        "kind": item.kind,
        "label": item.project["name"],
        "party": item.party["name"] if item.party is not None else "",
        "cost_fen": amount(item.cost_fen),
        "remaining_fen": amount(item.remaining_fen),
    }
