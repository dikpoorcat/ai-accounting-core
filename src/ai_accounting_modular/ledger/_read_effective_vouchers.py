"""Resolve actual journals from owner-selected headers within one read capture."""

from ._formal_output import target_vouchers
from ._read_origin_headers import source_headers
from ._types import VoucherDirectoryEntry
from ._values import fail


def capture(repo, selected_ids, headers, references):
    """Reuse only references produced by Ledger's own checked header capture.

    Public locator requests always locate actual headers before entering here.
    Historical owners missing from this capture are independently read and checked;
    they never become extra obligation sources or origins.
    """
    selected = target_vouchers(
        repo, [headers[identity] for identity in selected_ids], headers_only=True
    )
    owners = {voucher["result_id"] for voucher in selected.values() if voucher is not None}
    owner_refs = dict(references)
    needed = owners - owner_refs.keys()
    if needed:
        actual = {identity: headers[identity] for identity in needed if identity in headers}
        missing = needed - actual.keys()
        if missing:
            rows = {row["result_id"]: row for row in repo.read_heads(tuple(sorted(missing)))}
            if rows.keys() != missing:
                fail("storage_corrupt", "有效凭证缺少实际正式结果所有者。")
            actual.update(rows)
        checked, _ = source_headers(actual.values())
        owner_refs.update((entry.reference["result_id"], entry.reference) for entry in checked)
    return {
        identity: None
        if voucher is None
        else VoucherDirectoryEntry(
            voucher["voucher_id"],
            voucher["period"],
            voucher["number"],
            voucher["role"],
            voucher["summary"],
            dict(owner_refs[voucher["result_id"]]),
            voucher["reverses_voucher_id"],
        )
        for identity, voucher in selected.items()
    }
