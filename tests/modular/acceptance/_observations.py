"""Translate complete public values into the fixed examples' named observations."""

from collections import Counter

from ._support import SPEC, atomic, expense


def get_path(value, path):
    for part in path.split("."):
        value = value[part]
    return value


def focus(snapshot, business_id=None):
    if business_id is None:
        active = {item["state"]["business_id"] for item in snapshot["businesses"]}
        business_id = (
            SPEC["ids"]["wage"] if SPEC["ids"]["wage"] in active else SPEC["ids"]["expense_a"]
        )
    return next(
        (item for item in snapshot["businesses"] if item["state"]["business_id"] == business_id),
        None,
    )


def obligation(item):
    defined, balance = item["obligation"], item["balance"]
    return {
        **defined,
        **balance,
        "actual_money_settled_fen": balance["money_settled_fen"],
        "settled_fen": str(
            sum(
                int(balance[field])
                for field in (
                    "money_settled_fen",
                    "offset_settled_fen",
                    "agency_settled_fen",
                    "transferred_out_fen",
                )
            )
        ),
    }


def normalized(snapshot, business_id=None):
    selected = focus(snapshot, business_id)
    results, vouchers = snapshot["results"], snapshot["vouchers"]
    all_results = snapshot["all_results"]
    current = selected["state"]["results"] if selected else []
    main = next((ref for ref in current if ref["result_slot"] == "main"), {})
    selected_results = [
        item for item in results if item["reference"]["result_id"] == main.get("result_id")
    ]
    result = selected_results[0] if selected_results else {}
    current_vouchers = result.get("vouchers", [])
    originals = [item for item in all_results if item["reference"]["state"] == "frozen"]
    business = (
        {
            **selected["state"],
            **selected.get("fact", {}).get("data", {}),
            "fact_absent": "fact" not in selected,
        }
        if selected
        else {}
    )
    movements = snapshot["money"]["movements"]
    payment_id = SPEC["ids"]["payment"]
    payment = next(
        (item for item in snapshot["businesses"] if item["state"]["business_id"] == payment_id),
        None,
    )
    role_order = ("original", "reversal", "replacement", "reclassification", "closing")
    observed = dict(
        business=business,
        result_id=main.get("result_id"),
        generation=main.get("generation"),
        correction_result_id=main.get("result_id") if main.get("baseline_result_id") else None,
        baseline_result_id=main.get("baseline_result_id"),
        voucher={"number": main.get("voucher_number")},
        current_target_fen=result.get("target", {}).get("values", {}).get("amount_fen"),
        frozen_original_fen=originals[0]["target"]["values"].get("amount_fen")
        if originals
        else None,
        frozen_target_amounts_fen=[
            item["target"]["values"]["amount_fen"]
            for item in originals
            if item["target"]["kind"] == "operations.expense"
        ],
        voucher_count=len(vouchers),
        active_voucher_count=len(vouchers),
        open_result_count=sum(item["reference"]["state"] == "open" for item in results),
        open_correction_result_count=sum(
            item["reference"]["state"] == "open" and "baseline_result_id" in item["reference"]
            for item in results
        ),
        voucher_roles=[
            role for role in role_order if any(item["role"] == role for item in vouchers)
        ],
        expense_total_fen=expense(vouchers),
        payroll_cost_fen=expense(
            [
                voucher
                for voucher in vouchers
                if voucher["result"]["business_id"] == SPEC["ids"]["wage"]
            ]
        ),
        posting_totals_fen={
            month: expense([item for item in vouchers if item["period"] == month])
            for month in ("2026-01", "2026-02", "2026-03")
        },
        current_obligation_count=len(snapshot["obligations"]),
        current_document_fact_count=sum(
            fact["business_id"] == business.get("business_id")
            for fact in snapshot["documents"]["facts"]
        ),
        active_business_ids=[
            item["business_id"] for item in snapshot["directory"] if item["state"] == "active"
        ],
        business_seq={item["business_id"]: item["seq"] for item in snapshot["directory"]},
        formal_results=all_results,
        document_set_id=snapshot["documents"]["set_id"],
        scope=atomic(snapshot),
        actual_outflow_fen=str(snapshot["money"]["outflow_fen"]),
        money_occurrence_count=snapshot["money"]["movement_count"],
        money_occurrence_id=movements[0]["movement"]["movement_id"] if movements else None,
        money_fact_digest=payment["state"]["fact_digest"] if payment else None,
        payment_allocations=movements[0]["allocations"] if movements else [],
        database_id=snapshot["database_id"],
        result_ids=sorted(item["reference"]["result_id"] for item in all_results),
        voucher_numbers=[
            {"period": item["period"], "number": item["number"], "role": item["role"]}
            for item in vouchers
        ],
        period_close_count=int(snapshot["closed"][5:]) if snapshot["closed"] else 0,
        net_assets_fen=snapshot["financial"]["equity"].get("amount_fen"),
        logical_expense_fact_count=sum(
            item.get("fact", {}).get("kind") == "operations.expense"
            for item in snapshot["businesses"]
        ),
        document_occurrences=snapshot["documents"]["document_occurrences"],
        duplicate_voucher_count=len(vouchers) - len({item["voucher_id"] for item in vouchers}),
        outflow_allocation_to_recovery_count=sum(
            part["source"]["business_id"] == SPEC["ids"]["overpayment"]
            for movement in movements
            for part in movement["allocations"]
        ),
    )
    for role in ("reversal", "replacement"):
        observed[role + "_voucher"] = {
            "number": next(
                (item["number"] for item in current_vouchers if item["role"] == role), None
            )
        }
    balances = [obligation(item) for item in snapshot["obligations"]]
    for name, slot in (("obligation", "net_pay"), ("recovery", "recovery")):
        observed[name] = next((item for item in balances if item["slot"] == slot), {})
    for direction in ("receivable", "payable"):
        observed[direction + "_fen"] = str(
            sum(int(item["remaining_fen"]) for item in balances if item["direction"] == direction)
        )
    observed["tax_payable_fen"] = str(
        sum(int(item["remaining_fen"]) for item in balances if item["category"] == "withholding")
    )
    observed["contribution_payable_fen"] = str(
        sum(
            int(item["remaining_fen"])
            for item in balances
            if item["category"] in ("social", "housing")
        )
    )
    a = focus(snapshot, SPEC["ids"]["expense_a"])
    if a:
        ref = next((ref for ref in a["state"]["results"] if ref["result_slot"] == "main"), {})
        observed["a"] = dict(
            result_id=ref.get("result_id"),
            generation=ref.get("generation"),
            voucher={"number": ref.get("voucher_number")},
        )
    return observed


def subset(actual, expected, path=""):
    if isinstance(expected, dict):
        assert isinstance(actual, dict), (path, actual, expected)
        for key, value in expected.items():
            assert key in actual, (
                f"{path}.{key}: observation is missing; no oracle field may be skipped"
            )
            subset(actual[key], value, path + "." + key)
    elif isinstance(expected, list):
        assert isinstance(actual, list), (path, actual)
        # Explicit identity lists are sets, but duplicate identities remain an error.
        if all(isinstance(item, str) for item in expected) and (
            "business_ids" in path or path.endswith("result_ids") or path.endswith("issue_ids")
        ):
            assert len(actual) == len(set(actual)) and set(actual) == set(expected), (
                path,
                actual,
                expected,
            )
        else:
            assert len(actual) == len(expected), (path, actual, expected)
            for index, (item, wanted) in enumerate(zip(actual, expected, strict=True)):
                subset(item, wanted, path + f"[{index}]")
    else:
        assert actual == expected and type(actual) is type(expected), (path, actual, expected)


def absent(snapshot, expected):
    for forbidden in expected:
        identity = forbidden["business_id"]
        amount = forbidden.get("amount_fen", forbidden.get("target_fen"))
        for item in snapshot["businesses"]:
            if item["state"]["business_id"] == identity:
                assert item.get("fact", {}).get("data", {}).get("amount_fen") != amount
        for item in snapshot["all_results"]:
            if (
                item["reference"]["business_id"] == identity
                and item["reference"]["state"] == "open"
            ):
                assert item["target"]["values"].get("amount_fen") != amount
                assert item["basis"]["input"].get("amount_fen") != amount
                assert all(
                    line.get("debit_fen") != amount and line.get("credit_fen") != amount
                    for voucher in item["vouchers"]
                    for line in voucher["lines"]
                )
        assert all(
            item["obligation"]["amount_fen"] != amount
            for item in snapshot["obligations"]
            if item["obligation"]["business_id"] == identity
        )
        assert all(
            fact["data"].get("amount_fen") != amount
            for fact in snapshot["documents"]["facts"]
            if fact["business_id"] == identity
        )


def document_observations(published, root):
    from pathlib import Path

    referenced = {entry["relative_path"] for entry in published["files"]}
    parts = Path(root) / "company/standard/parts"
    unreferenced = [
        path
        for path in parts.glob("*.json")
        if path.relative_to(Path(root) / "company").as_posix() not in referenced
    ]
    temps = [
        path
        for path in (Path(root) / "company").rglob("*")
        if path.is_file() and (path.suffix == ".tmp" or path.name.startswith(".records-"))
    ]
    expense_facts = [fact for fact in published["facts"] if fact["kind"] == "operations.expense"]
    return {
        **published,
        "partial_new_set_visible": any(
            count != 1 for count in published["occurrences_per_business"].values()
        ),
        "old_unreferenced_shards_after_lease_release": len(unreferenced),
        "temporary_files_after_cleanup": len(temps),
        "duplicate_fact_count": sum(
            count - 1
            for count in Counter(fact["business_id"] for fact in published["facts"]).values()
        ),
        "standard_facts": [
            dict(
                business_id=fact["business_id"],
                period=fact["period"],
                amount_fen=fact["data"]["amount_fen"],
            )
            for fact in expense_facts
        ],
        "standard_facts_scope": "operations.expense",
        "standard_correction_action_count": sum(
            "correction_period" in fact or "op" in fact for fact in published["facts"]
        ),
        "standard_old_voucher_number_count": sum(
            "voucher_number" in fact or "voucher_id" in fact for fact in published["facts"]
        ),
    }
