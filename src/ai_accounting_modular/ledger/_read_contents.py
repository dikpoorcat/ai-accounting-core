"""Selected formal directories and exact contents, without unrelated children."""

from collections import defaultdict

from ai_accounting_modular.contracts.public import (
    MAX_FEN,
    ContractError,
    ReadPage,
    content_digest,
    format_fen,
    loads_json,
    validate_contract,
)

from ._planning import stored_source_reference
from ._read_support import after_key, bounded, cursor, scope_selection, version
from ._types import (
    AdoptedContent,
    AdoptedDirectoryEntry,
    AdoptedValues,
    VoucherDirectoryEntry,
    VoucherLocator,
)
from ._values import fail


def reference(row):
    ref = {
        key: row[key]
        for key in (
            "business_id",
            "result_slot",
            "result_id",
            "generation",
            "business_period",
            "posting_period",
            "state",
        )
    }
    if row["baseline_result_id"] is not None:
        ref["baseline_result_id"] = row["baseline_result_id"]
    return validate_contract("core.schema.json", "ResultRef", ref)


def get_adopted_contents(locators, read):
    locators = bounded(locators, "ResultRef")
    ids = [ref["result_id"] for ref in locators]
    if len(set(ids)) != len(ids):
        fail("invalid_input", "一次不能重复读取同一正式结果。")
    repo = read.repository("ledger")
    rows = {row["result_id"]: row for row in repo.results(ids)}
    result = []
    for locator in locators:
        row = rows.get(locator["result_id"])
        if row is None:
            fail("not_found", "指定正式结果不存在。")
        if any(
            row[key] != locator[key]
            for key in (
                "business_id",
                "result_slot",
                "generation",
                "business_period",
                "posting_period",
            )
        ):
            fail("snapshot_changed", "指定开放结果已经变化，请重新读取。")
        target = _content(row, "target", "ResultTarget")
        basis = _content(row, "basis", "AdoptedBasis")
        result.append(AdoptedContent(reference(row), stored_source_reference(row), target, basis))
    return tuple(result)


def query_adopted(
    scope, kinds, page, read, *, flow=False, business_ids=(), result_slots=(), economic_from=None
):
    selected = scope_selection(scope, read)
    kinds = bounded(kinds, "text")
    business_ids, result_slots = bounded(business_ids), bounded(result_slots, "Slot")
    repo = read.repository("ledger")
    table, where, args = repo.read_result_scope(
        scope, kinds, selected, flow=flow, business_ids=business_ids, result_slots=result_slots
    )
    if economic_from is not None:
        where += " AND r.business_period>=?"
        args = (*args, economic_from)
    identities = repo.read_identity_rows(table, where, args)
    collection = "adopted_values" if flow else "adopted_directory"
    identity = version(
        read,
        collection,
        {
            "from": scope["period_from"],
            "to": scope["period_to"],
            "kinds": kinds,
            "business_ids": business_ids,
            "result_slots": result_slots,
            "economic_from": economic_from,
        },
        selected,
        identities,
    )
    if scope.get("expected_head", identity) != identity:
        fail("snapshot_changed", "正式目录的采用范围已经变化，请重新读取。")
    after = after_key(page, identity, (str,) * (4 if flow else 3))
    rows = repo.read_result_page(table, where, args, after=after, limit=page.limit + 1, flow=flow)
    more, rows = len(rows) > page.limit, rows[: page.limit]
    items = []
    if flow:
        ids = {row["result_id"] for row in rows}
        if selected.view == "as_posted":
            ids.update(row["baseline_result_id"] for row in rows if row["baseline_result_id"])
        bodies = {row["result_id"]: row for row in repo.financial_results(ids)}
        for row in rows:
            target = _content(bodies[row["result_id"]], "target", "ResultTarget")
            baseline = None
            baseline_ref = None
            if selected.view == "as_posted" and row["baseline_result_id"]:
                old = bodies.get(row["baseline_result_id"])
                if old is None:
                    fail("storage_corrupt", "正式采用的原基线不存在。")
                baseline = _content(old, "target", "ResultTarget")
                baseline_ref = reference(old)
            items.append(
                AdoptedValues(
                    reference(row),
                    stored_source_reference(row),
                    row["business_kind"],
                    target,
                    baseline,
                    baseline_ref,
                )
            )
    else:
        items = [
            AdoptedDirectoryEntry(
                reference(row), stored_source_reference(row), row["business_kind"]
            )
            for row in rows
        ]
    keys = (
        ("posting_period", "business_id", "result_slot", "result_id")
        if flow
        else ("business_id", "result_slot", "result_id")
    )
    next_cursor = cursor(identity, [rows[-1][key] for key in keys]) if more else None
    return ReadPage(identity, len(identities), len(identities), tuple(items), next_cursor)


def query_voucher_directory(scope, kinds, page, read, *, business_ids=(), roles=()):
    selected = scope_selection(scope, read)
    kinds, business_ids, roles = (
        bounded(kinds, "text"),
        bounded(business_ids),
        bounded(roles, "text"),
    )
    if set(roles) - {"original", "reversal", "replacement", "reclassification", "closing"}:
        fail("invalid_input", "未知的正式凭证角色。")
    repo = read.repository("ledger")
    table, where, args = repo.read_voucher_scope(scope, kinds, selected, business_ids, roles)
    identities = repo.read_voucher_identities(table, where, args)
    identity = version(
        read,
        "voucher_directory",
        {
            "from": scope["period_from"],
            "to": scope["period_to"],
            "kinds": kinds,
            "business_ids": business_ids,
            "roles": roles,
        },
        selected,
        identities,
    )
    if scope.get("expected_head", identity) != identity:
        fail("snapshot_changed", "凭证目录的采用范围已经变化，请重新读取。")
    after = after_key(page, identity, (str, int, str))
    rows = repo.read_voucher_page(table, where, args, after=after, limit=page.limit + 1)
    more, rows = len(rows) > page.limit, rows[: page.limit]
    items = tuple(
        VoucherDirectoryEntry(
            row["voucher_id"],
            row["period"],
            row["number"],
            row["role"],
            row["summary"],
            reference(row),
            row["reverses_voucher_id"],
        )
        for row in rows
    )
    next_cursor = (
        cursor(identity, (rows[-1]["period"], rows[-1]["number"], rows[-1]["voucher_id"]))
        if more
        else None
    )
    return ReadPage(identity, len(identities), len(identities), items, next_cursor)


def get_vouchers(locators, read):
    if (
        not isinstance(locators, tuple | list)
        or len(locators) > 500
        or any(not isinstance(ref, VoucherLocator) for ref in locators)
    ):
        fail("invalid_input", "一次最多读取500个精确凭证定位。")
    for locator in locators:
        validate_contract("core.schema.json", "Id", locator.voucher_id)
        validate_contract("core.schema.json", "Id", locator.result_id)
        if type(locator.generation) is not int or not 1 <= locator.generation <= MAX_FEN:
            fail("invalid_input", "凭证定位须包含正式结果的有效代次。")
    ids = [ref.voucher_id for ref in locators]
    if len(set(ids)) != len(ids):
        fail("invalid_input", "一次不能重复读取同一凭证。")
    repo = read.repository("ledger")
    vouchers = {row["voucher_id"]: row for row in repo.read_vouchers(ids)}
    heads = {row["result_id"]: row for row in repo.read_heads({ref.result_id for ref in locators})}
    lines = defaultdict(list)
    for row in repo.read_voucher_lines(ids):
        line = {key: row[key] for key in ("line_no", "account_code", "summary")}
        line.update(
            debit_fen=format_fen(row["debit_fen"]), credit_fen=format_fen(row["credit_fen"])
        )
        line.update({key: row[key] for key in ("party_id", "object_id") if row[key] is not None})
        lines[row["voucher_id"]].append(line)
    result = []
    for locator in locators:
        row, head = vouchers.get(locator.voucher_id), heads.get(locator.result_id)
        if row is None or head is None:
            fail("not_found", "指定正式凭证不存在。")
        if row["result_id"] != locator.result_id or head["generation"] != locator.generation:
            fail("snapshot_changed", "指定开放凭证已经变化，请重新读取。")
        value = {key: row[key] for key in ("voucher_id", "period", "number", "role", "summary")}
        value.update(result=reference(head), lines=lines[locator.voucher_id])
        if row["reverses_voucher_id"] is not None:
            value["reverses_voucher_id"] = row["reverses_voucher_id"]
        validate_contract("core.schema.json", "Voucher", value)
        result.append(value)
    return tuple(result)


def _content(row, name, definition):
    try:
        value = loads_json(row[name + "_json"])
        validate_contract("business-facts.schema.json", definition, value)
        if content_digest(value) != row[name + "_digest"]:
            raise ValueError
        return value
    except (ContractError, ValueError) as error:
        raise ContractError("storage_corrupt", "正式结果正文与其合同或保存摘要不一致。") from error
