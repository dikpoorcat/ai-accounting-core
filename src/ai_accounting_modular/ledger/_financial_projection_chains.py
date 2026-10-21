"""Thin exact adoption chains determine stock and Counter flow, without business bodies."""

from collections import Counter, defaultdict

from ai_accounting_modular.contracts.public import validate_contract

from ._financial_order import head_key, target_key
from ._financial_projection_indexes import order_key
from ._values import fail

_FIELDS = (
    "atom",
    "result_id",
    "generation",
    "business_period",
    "posting_period",
    "baseline_result_id",
    "is_current",
    "business_kind",
    "signature",
)
BOUNDARIES = ("opening.package", "reports.continuation")


def key(business, slot):
    return order_key((business, slot))


def records(heads, atoms, signatures):
    grouped = defaultdict(list)
    for head in heads:
        value = head | {
            "atom": atoms[head["result_id"]],
            "signature": signatures[head["result_id"]],
        }
        grouped[head["business_id"], head["result_slot"]].append(
            [value[field] for field in _FIELDS]
        )
    return {
        identity: sorted(values, key=lambda row: (row[4], row[2], row[1]))
        for identity, values in grouped.items()
    }


def heads(identity, values):
    if type(values) is not list:
        fail("storage_corrupt", "财务正式采用链目录格式损坏。")
    result, previous = [], None
    for row in values:
        if type(row) is not list or len(row) != len(_FIELDS):
            fail("storage_corrupt", "财务正式采用链目录缺少精确身份。")
        value = dict(zip(_FIELDS, row, strict=True))
        if (
            type(value["atom"]) is not str
            or len(value["atom"]) != 64
            or any(c not in "0123456789abcdef" for c in value["atom"])
            or type(value["signature"]) is not str
            or len(value["signature"]) != 64
            or any(c not in "0123456789abcdef" for c in value["signature"])
            or type(value["generation"]) is not int
            or value["generation"] < 1
            or type(value["is_current"]) is not int
            or value["is_current"] not in (0, 1)
            or type(value["business_kind"]) is not str
        ):
            fail("storage_corrupt", "财务正式采用链目录含无效的代际或来源。")
        for field, definition in (
            ("result_id", "Id"),
            ("business_period", "Month"),
            ("posting_period", "Month"),
        ):
            validate_contract("core.schema.json", definition, value[field])
        if value["baseline_result_id"] is not None:
            validate_contract("core.schema.json", "Id", value["baseline_result_id"])
        value.update(business_id=identity[0], result_slot=identity[1])
        order = value["posting_period"], value["generation"], value["result_id"]
        if previous is not None and previous >= order:
            fail("storage_corrupt", "财务正式采用链的顺序或身份重复。")
        previous = order
        result.append(value)
    if len({head["result_id"] for head in result}) != len(result):
        fail("storage_corrupt", "财务正式采用链重复引用一个结果。")
    return tuple(result)


def stock(values, view, cutoff):
    eligible = [
        head
        for head in values
        if head["business_kind"] not in BOUNDARIES
        and (
            head["posting_period"] <= cutoff
            if view == "as_posted"
            else head["is_current"] and head["business_period"] <= cutoff
        )
    ]
    if not eligible:
        return None
    if view == "as_posted":
        period = max(head["posting_period"] for head in eligible)
        eligible = [head for head in eligible if head["posting_period"] == period]
    if len(eligible) != 1:
        fail("storage_corrupt", "财务比较时点没有唯一的正式目标。")
    return eligible[0]


def flow(values, view, first, last):
    if view == "current":
        selected = stock(values, view, last)
        return ((selected, 1, None),) if selected and first <= selected["business_period"] else ()
    positive = sorted(
        (
            head
            for head in values
            if head["business_kind"] not in BOUNDARIES and first <= head["posting_period"] <= last
        ),
        key=head_key,
    )
    by_id = {head["result_id"]: head for head in values}
    signs = Counter(head["result_id"] for head in positive)
    anchors = {}
    for head in positive:
        baseline = head["baseline_result_id"]
        if baseline:
            if baseline not in by_id:
                fail("storage_corrupt", "财务正式采用链缺少仍被引用的冻结旧腿。")
            anchors.setdefault(baseline, head)
            signs[baseline] -= 1
    return tuple(
        (by_id[identity], sign, anchors.get(identity)) for identity, sign in signs.items() if sign
    )


def selected_key(head):
    return order_key(target_key(head))
