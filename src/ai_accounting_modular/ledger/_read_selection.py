"""SQL identity selection shared by formal and resource reads."""


def selected(alias, as_of):
    if as_of is None:
        return (
            f"EXISTS(SELECT 1 FROM result_slot_current c WHERE c.head_result_id={alias}.result_id)",
            (),
        )
    return (
        f"EXISTS(SELECT 1 FROM adopted_result selected_result "
        f"WHERE selected_result.result_id={alias}.result_id "
        "AND selected_result.posting_period<=? "
        "AND NOT EXISTS(SELECT 1 FROM adopted_result newer "
        "WHERE newer.business_id=selected_result.business_id "
        "AND newer.result_slot=selected_result.result_slot "
        "AND newer.posting_period<=? "
        "AND newer.posting_period>selected_result.posting_period))",
        (as_of, as_of),
    )


def business_kind(alias="r"):
    return (
        f"CASE WHEN {alias}.target_state='withdrawn' "
        f"THEN json_extract({alias}.target_json,'$.business_kind') "
        f"ELSE json_extract({alias}.target_json,'$.kind') END"
    )
