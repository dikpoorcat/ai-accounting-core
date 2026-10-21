"""Financial traversal is independent of SQL row order and physical projection keys."""

CHILD_KEYS = {
    "obligation": "public_slot",
    "settlement": "settlement_id",
    "obligation_reclassification": "relation_id",
    "result_fund_movement": "movement_id",
}


def head_key(head):
    return tuple(
        head[field]
        for field in ("posting_period", "business_id", "result_slot", "generation", "result_id")
    )


def target_key(head):
    # A complete selected target is unique per business/slot. Candidate targets
    # carry the same economic identity without an invented adoption timestamp.
    return head["business_period"], head["business_id"], head["result_slot"]


def dimension_key(parts):
    # An absent party/object is a real distinct dimension and sorts before any ID.
    return tuple((part is not None, part or "") for part in parts)
