"""Exact original-gross coordinates shared by tax recognition and real returns."""

from ._common import amount, reject, sum_amounts


def ranges(rows, capacity):
    values = sorted((amount(row, "from_fen"), amount(row, "to_fen")) for row in rows)
    if any(start < 0 or start >= end or end > capacity for start, end in values):
        reject("税务金额范围必须位于原业务毛额内。", "invalid_input")
    if any(right[0] < left[1] for left, right in zip(values, values[1:], strict=False)):
        reject("同一税务来源的金额范围不能重叠。", "invalid_input")
    combined = []
    for start, end in values:
        if combined and combined[-1][1] == start:
            combined[-1] = combined[-1][0], end
        else:
            combined.append((start, end))
    return combined


def wire(values):
    return [{"from_fen": str(start), "to_fen": str(end)} for start, end in values]


def gross(values):
    return sum_amounts(end - start for start, end in values)


def intersect(left, right):
    """Inputs are sorted disjoint half-open original-gross intervals."""
    result, i, j = [], 0, 0
    while i < len(left) and j < len(right):
        start, end = max(left[i][0], right[j][0]), min(left[i][1], right[j][1])
        if start < end:
            result.append((start, end))
        if left[i][1] < right[j][1]:
            i += 1
        else:
            j += 1
    return result


def subtract(values, removed):
    result, cursor = [], 0
    for start, end in values:
        while cursor < len(removed) and removed[cursor][1] <= start:
            cursor += 1
        index = cursor
        while index < len(removed) and removed[index][0] < end:
            first, last = removed[index]
            if start < first:
                result.append((start, first))
            start = max(start, last)
            if start >= end:
                break
            index += 1
        if start < end:
            result.append((start, end))
    return result


def local_ranges(values, enclosing):
    result, offset = [], 0
    for start, end in enclosing:
        result.extend(
            (offset + first - start, offset + last - start)
            for first, last in intersect(values, [(start, end)])
        )
        offset += end - start
    return result


def original_ranges(values, enclosing):
    result, offset = [], 0
    for start, end in enclosing:
        result.extend(
            (start + first - offset, start + last - offset)
            for first, last in intersect(values, [(offset, offset + end - start)])
        )
        offset += end - start
    return result


def validate_resolution(resolution, business_id, capacity):
    if resolution["target_business_id"] != business_id:
        reject("税点正式结果必须属于本笔原业务。", "invalid_input")
    combined = []
    for segment in resolution["segments"]:
        values = ranges(segment["ranges"], capacity)
        if not values:
            reject("税点分段不能包含空金额范围。", "invalid_input")
        if segment["state"] == "due" and (
            "tax_on" in segment and segment["tax_on"][:7] != segment["tax_period"]
        ):
            reject("税点正式结果中的实际日与所属月不一致。", "invalid_input")
        combined.extend(values)
    ordered = sorted(combined)
    previous = 0
    for start, end in ordered:
        if start != previous:
            reject("税点正式结果必须互不重叠并完整覆盖原业务毛额。", "invalid_input")
        previous = end
    if previous != capacity:
        reject("税点正式结果没有覆盖全部原业务毛额。", "invalid_input")


def return_ranges(members, capacity):
    if sum_amounts(amount(item["data"], "gross_fen") for item in members) > capacity:
        reject("同一原业务累计退回或履约不能超过原毛额。")
    by_id, free = {}, [(0, capacity)]
    for month in sorted({item["period"] for item in members}):
        monthly = sorted(
            (item for item in members if item["period"] == month),
            key=lambda item: item["business_id"],
        )
        explicit = {}
        for item in monthly:
            data = item["data"]
            current = data.get("tax_ranges")
            if "tax_returns" in data:
                parts = [row for part in data["tax_returns"] for row in part["ranges"]]
                if current is not None and ranges(current, capacity) != ranges(parts, capacity):
                    reject("红字总范围与逐税源范围不一致。", "invalid_input")
                current = parts
            if current is not None:
                selected = ranges(current, capacity)
                if gross(selected) != amount(data, "gross_fen"):
                    reject("红字退回范围合计必须等于本次退回毛额。", "invalid_input")
                if intersect(selected, free) != selected:
                    reject("本月明确范围与此前已采用的退回或履约范围重叠。")
                explicit[item["business_id"]] = selected
        reserved = ranges(wire([pair for values in explicit.values() for pair in values]), capacity)
        free = subtract(free, reserved)
        for item in monthly:
            if item["business_id"] in explicit:
                by_id[item["business_id"]] = explicit[item["business_id"]]
                continue
            quantity, selected = amount(item["data"], "gross_fen"), []
            while quantity:
                if not free:
                    reject("本次金额超过尚未消耗的原毛额。")
                start, end = free[0]
                used = min(end - start, quantity)
                selected.append((start, start + used))
                quantity -= used
                if start + used == end:
                    free.pop(0)
                else:
                    free[0] = start + used, end
            by_id[item["business_id"]] = selected
    return {key: wire(values) for key, values in by_id.items()}
