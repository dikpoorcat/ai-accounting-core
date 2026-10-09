"""Published identity supplements for formerly unnamed historical obligations.

Only the displayed object is supplemented. The historical obligation, amounts,
status and source locators retain their original frozen meaning.
"""

from .contracts import KernelError
from .types import YearMonth


def pass_through_party_supplements(snap, sources):
    """Bind a missing remittance identity to its exact current adopted source.

    A latest fact can still be an unpublished draft. Likewise, the settlement
    relation may retain the first source's identity after a closed correction.
    Select the current formal calculation independently, and require agreement
    between its typed beneficiary and its own remittance obligation.
    """
    candidates = [source for source in sources
                  if (source.get("source_business") or {}).get("kind") == "pass_through"
                  and source.get("name") == "remittance"
                  and not (source.get("creditor_id") or source.get("counterparty_id")
                           or source.get("recipient_id"))]
    if not candidates:
        return {}
    subjects = {(source.get("source_business") or {}).get("subject_id")
                for source in candidates}
    if None in subjects:
        raise KernelError("content_integrity_failed", "受托转付款缺少精确业务身份")
    heads = {row["subject_id"]: row for row in
             snap.queries._current_accounting_heads(snap.connection, subjects)}
    selected = [(source, heads[subject]) for source in candidates
                if (subject := source["source_business"]["subject_id"]) in heads
                and heads[subject]["id"] != source.get("source_calculation_id")]
    if not selected:
        return {}
    current_ids = {head["id"] for _, head in selected}
    cutoff = str(YearMonth.from_ordinal(max(snap.month, *(head["posting_period"]
                                                       for _, head in selected))))
    adopted = snap.queries._selected_accounting(
        snap.connection, {head["subject_id"] for _, head in selected}, cutoff,
        current_heads=True, kinds={"pass_through"}, include_lines=False,
    )["through_period"]
    adopted_ids = {row["calculation_id"] for row in
                   (*adopted["voucher_events"], *adopted["state_results"])
                   if row.get("sign", 1) == 1}
    if not current_ids <= adopted_ids:
        raise KernelError("content_integrity_failed", "受托转付款补正缺少正式采用")
    identifiers = current_ids | {source["source_calculation_id"] for source, _ in selected}
    snap.reads.verify_selected_content(identifiers)
    calculations = snap.reads.calculations(identifiers)
    result = {}
    for source, head in selected:
        current = calculations[head["id"]]
        historical = calculations[source["source_calculation_id"]]
        subject = source["source_business"]["subject_id"]
        if (historical["subject_id"], historical["kind"], historical["fact_id"],
                historical["period"]) != (subject, "pass_through", source["source_fact_id"],
                                         current["period"]) or (
                current["subject_id"], current["kind"]) != (subject, "pass_through"):
            raise KernelError("content_integrity_failed", "受托转付款补正与历史业务身份不一致")
        if historical["fact_data"].get("beneficiary_id"):
            raise KernelError("content_integrity_failed", "受托转付款历史对象与冻结义务不一致")
        original_obligations = [row for row in
                                historical["outcome"].get("values", {}).get("obligations", ())
                                if row.get("key") == source["key"]]
        if len(original_obligations) != 1 or original_obligations[0].get("counterparty_id"):
            raise KernelError("content_integrity_failed", "受托转付款历史义务身份不一致")
        original = original_obligations[0]
        beneficiary = current["fact_data"].get("beneficiary_id")
        if not beneficiary:
            continue
        obligations = [row for row in current["outcome"].get("values", {}).get("obligations", ())
                       if row.get("key") == source["key"]]
        if len(obligations) != 1 or any(obligations[0].get(field) != value for field, value in (
                ("name", "remittance"), ("counterparty_id", beneficiary),
                ("account", original["account"]), ("category", original["category"]))):
            raise KernelError("content_integrity_failed", "受托转付款补正对象与正式义务不一致")
        fact = snap.reads.fact(current["fact_id"])
        result[source["key"]] = {
            "party_id": beneficiary,
            "identity_source": snap.fact_source(fact, field="beneficiary_id"),
            "adoption": {"calculation_id": current["id"], "fact_id": current["fact_id"],
                         "publication_id": current["publication_id"],
                         "posting_period": current["posting_period"]},
        }
    return result


def supplemented_party_field(snap, party_id, *, missing=None, supplement=None):
    """Keep both the formal identity link and the displayed name's provenance."""
    field = snap.party_field(party_id, missing=missing)
    if supplement is not None:
        name_source = field["field_sources"].get("party")
        name_sources = name_source if isinstance(name_source, list) else (
            [name_source] if name_source else []
        )
        field["field_sources"]["party"] = [supplement["identity_source"], *name_sources]
        field["party_supplement"] = supplement["adoption"]
    return field
