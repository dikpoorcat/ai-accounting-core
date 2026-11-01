"""Human labels for the Money module's explicit actual-counterparty meaning."""


def label(meaning, objects, *, direction=None):
    if meaning["state"] == "known":
        return "、".join(
            objects[identity]["name"] if identity in objects else "对象名称未提供"
            for identity in meaning["party_ids"]
        )
    if meaning["state"] == "not_applicable":
        if meaning["reason"] == "own_funds_channel":
            return meaning["own_funds_channel"]["name"] + "（公司自有资金）"
        return "公司内部账户划转"
    return {"outflow": "收款方未提供", "inflow": "付款方未提供"}.get(
        direction, "对象未提供"
    )
