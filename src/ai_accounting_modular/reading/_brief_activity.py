"""Prepare scalar month activities and precise voucher descriptors without journal lines."""

from collections import defaultdict

from ai_accounting_modular.contracts.public import (
    ContractError,
    QuerySelection,
)

from . import _brief_activity_index as index
from . import _brief_groups, _brief_vouchers, _brief_vouchers_display
from ._brief_activity_amounts import EVENT_KINDS
from ._brief_activity_parts import ActivityEvent, parts, rows, semantic
from ._brief_activity_sources import Contents, identities, pages
from ._collections import IndexItem, merge_items, prepare_items, prepare_model
from ._dependencies import bind

COMPONENT = index.COMPONENT
COLLECTIONS = index.COLLECTIONS


class _RootContents:
    """Share exact reads while attributing only this root's actual body use."""

    def __init__(self, shared):
        self.shared = shared
        self.sources = []

    def include(self, content):
        self.sources.extend((content.source, *content.basis["source_results"]))
        return content

    def source(self, content, business_id, *, result_slot=None):
        return self.include(self.shared.source(content, business_id, result_slot=result_slot))

    def opening(self, content):
        return self.shared.opening(content)


def _obligations(reader, scope, read, contents):
    """Bind progress to an actual obligation under the exact formal adoption."""
    selected = {item.source["result_id"]: item for item in contents}
    found = {}

    def collect(bodies, cutoff, selection):
        ids = sorted({item.reference["business_id"] for item in bodies.values()})
        for start in range(0, len(ids), 500):
            positions = reader.ledger.get_obligation_positions(
                cutoff, read, business_ids=tuple(ids[start : start + 500]), selection=selection
            )
            for position in positions:
                definition = position.definition
                content = bodies.get(definition.source["result_id"])
                if content is None or content.source != definition.source:
                    continue
                person = (
                    content.target.get("values", {}).get("person_id")
                    if content.target["kind"] == "people.remuneration"
                    else None
                )
                found.setdefault(definition.source["result_id"], []).append(
                    {
                        "source": definition.reference,
                        "producer": definition.source,
                        "person_id": person,
                        "recipient_id": definition.value.get("party_id"),
                        "payroll_period": content.basis["period"] if person else None,
                        "component": definition.reference["slot"] if person else None,
                    }
                )

    collect(selected, scope["period_to"], QuerySelection(scope["view"], scope["as_of"]))
    # A correction leg still describes its old adoption. Its obligations can have
    # left this month's selection, so verify them at their actual posting cutoff.
    historical = {}
    for result_id, body in selected.items():
        cutoff = body.reference["posting_period"]
        if result_id not in found and cutoff < scope["period_to"]:
            historical.setdefault(cutoff, {})[result_id] = body
    for cutoff, bodies in historical.items():
        collect(bodies, cutoff, QuerySelection("as_posted", cutoff))
    return found


def _reversed_entries(reader, read, directory, chosen):
    reversed_entries = [entry for entry in directory if entry.reverses_voucher_id]
    refs = [chosen[entry.voucher_id] for entry in reversed_entries]
    actual = _brief_vouchers.effective_headers(reader, refs, read)
    found = {}
    for entry, original in zip(reversed_entries, actual, strict=True):
        if original is None or original.voucher_id != entry.reverses_voucher_id:
            raise ContractError("storage_corrupt", "冲正凭证与原正式采用的有效凭证不一致。")
        found[original.voucher_id] = original
    return found


def _meaning(body, prepared):
    return (
        None
        if body is None
        else semantic(body, prepared[body.reference["result_id"], body.reference["generation"]])
    )


def _events(directory, unjournaled, chosen, cache, prepared):
    events = []
    for entry in directory:
        body = cache.get(chosen[entry.voucher_id])
        if body.target["kind"] in EVENT_KINDS:
            events.append(
                (
                    ActivityEvent(
                        entry.result,
                        entry.role,
                        entry.number,
                        _brief_vouchers.token(_brief_vouchers.locator(entry)),
                    ),
                    body,
                )
            )
    for value in unjournaled:
        current = cache.get(value.reference) if value.target["kind"] in EVENT_KINDS else None
        old = (
            cache.get(value.baseline_reference)
            if value.baseline_target is not None and value.baseline_target["kind"] in EVENT_KINDS
            else None
        )

        if _meaning(current, prepared) == _meaning(old, prepared):
            continue
        if old is not None:
            events.append((ActivityEvent(value.reference, "reversal", 0, ""), old))
        if current is not None:
            events.append(
                (
                    ActivityEvent(
                        value.reference,
                        "replacement" if value.baseline_reference else "original",
                        0,
                        "",
                    ),
                    current,
                )
            )
    return events


def _selected_pages(method, scope, read, business_ids):
    batches = (
        tuple(business_ids[start : start + 500] for start in range(0, len(business_ids), 500))
        if business_ids
        else ((),)
    )
    for batch in batches:
        yield from pages(method, scope, read, business_ids=batch)


def capture(reader, scope, read, *, business_ids=()):
    """Build only explicitly selected roots through Ledger's public exact reads."""
    directory = list(
        _selected_pages(reader.ledger.query_voucher_directory, scope, read, business_ids)
    )
    values = {
        item.reference["result_id"]: item
        for item in _selected_pages(reader.ledger.query_adopted_values, scope, read, business_ids)
    }
    refs, chosen = [], {}
    for entry in directory:
        selected = values.get(entry.result["result_id"])
        if selected is None:
            raise ContractError("storage_corrupt", "本月凭证缺少同一正式采用的业务结果。")
        ref = selected.baseline_reference if entry.role == "reversal" else entry.result
        if ref is None:
            raise ContractError("storage_corrupt", "冲正凭证缺少原正式采用依据。")
        chosen[entry.voucher_id] = ref
        refs.append(ref)
    journal_heads = {entry.result["result_id"] for entry in directory}
    unjournaled = [
        value
        for value in values.values()
        if value.reference["result_id"] not in journal_heads
        and (
            value.target["kind"] in EVENT_KINDS
            or value.baseline_target is not None
            and value.baseline_target["kind"] in EVENT_KINDS
        )
    ]
    for value in unjournaled:
        refs.append(value.reference)
        if value.baseline_reference is not None:
            refs.append(value.baseline_reference)
    cache = Contents(reader.ledger, read)
    cache.load(refs)
    bodies = {(ref["result_id"], ref["generation"]): cache.get(ref) for ref in refs}
    views = defaultdict(lambda: _RootContents(cache))
    for body in bodies.values():
        views[body.reference["business_id"]].include(body)
    cache.sources(
        body
        for body in bodies.values()
        if body.target["kind"]
        in {
            "money.movement",
            "money.transfer",
            "financing.loan_drawdown",
            "financing.interest_period",
            "operations.advance_fulfillment",
            "operations.advance_refund",
            "investment.cooperation_funding",
            "investment.cooperation_distribution",
            "investment.cooperation_operator_transfer",
            "investment.cooperation_exit",
        }
    )
    prepared = {
        identity: parts(body, views[body.reference["business_id"]])
        for identity, body in bodies.items()
        if body.target["kind"] in EVENT_KINDS
    }
    events = _events(directory, unjournaled, chosen, cache, prepared)
    obligations = _obligations(reader, scope, read, [body for _, body in events])
    objects = identities(
        reader.identity,
        {
            party
            for _, body in events
            for part in prepared[body.reference["result_id"], body.reference["generation"]]
            for party in part.parties
        },
        read,
    )
    original = _reversed_entries(reader, read, directory, chosen)
    members, raw_vouchers, by_voucher = [], [], {}
    for event, body in events:
        actual = rows(
            event,
            body,
            objects,
            prepared=prepared[body.reference["result_id"], body.reference["generation"]],
            obligations=obligations.get(body.source["result_id"], ()),
        )
        members.extend(actual)
        if event.voucher_token:
            by_voucher[event.voucher_token] = actual
    for entry in directory:
        content = cache.get(chosen[entry.voucher_id])
        token = _brief_vouchers.token(_brief_vouchers.locator(entry))
        reverse = original.get(entry.reverses_voucher_id)
        descriptor = _brief_vouchers.descriptor(
            entry,
            content,
            by_voucher.get(token, ()),
            reverses=_brief_vouchers.token(_brief_vouchers.locator(reverse)) if reverse else None,
            party_ids=_brief_vouchers_display.party_ids(
                content,
                prepared.get((content.reference["result_id"], content.reference["generation"]), ()),
                views[content.reference["business_id"]],
            ),
        )
        raw_vouchers.append(descriptor)
    vouchers = []
    for entry, descriptor in zip(
        directory,
        _brief_vouchers_display.bind(reader, raw_vouchers, read, objects=objects),
        strict=True,
    ):
        token = descriptor["template"]["voucher_version_id"]
        vouchers.append(
            IndexItem(
                "brief_vouchers",
                token,
                f"{entry.number:020d}",
                descriptor,
                business_id=entry.result["business_id"],
                group_key=str(entry.number),
            )
        )
    root_ids = {item.business_id for item in vouchers} | {
        value.reference["business_id"] for value in unjournaled
    }
    root_sources, root_objects = defaultdict(list), defaultdict(set)
    for value in values.values():
        root_sources[value.reference["business_id"]].append(value.source)
    for _, body in events:
        for part in prepared[body.reference["result_id"], body.reference["generation"]]:
            root_objects[body.reference["business_id"]].update(part.parties)
    for voucher in vouchers:
        for kind in ("parties", "assets"):
            root_objects[voucher.business_id].update(voucher.value["display"][kind])
    roots = []
    for business_id in sorted(root_ids):
        roots.append(
            index.root_item(
                business_id,
                [*root_sources[business_id], *views[business_id].sources],
                root_objects[business_id],
            )
        )
    return members, vouchers, roots


def _member_order(member):
    row, scope = member["row"], member["scope"]
    reference = scope["reference"]
    return (
        0 if row["voucher_version_id"] else 1,
        row["voucher_number"],
        reference["posting_period"],
        reference["business_id"],
        reference["result_slot"],
        reference["result_id"],
        0 if scope["voucher_role"] == "reversal" else 1,
        scope["part"],
    )


def assemble(members, vouchers, roots, *, retained=None):
    retained_items = tuple(retained) if retained is not None else ()
    members = [
        *members,
        *(item.value for item in retained_items if item.collection == "activity_members"),
    ]
    vouchers = [
        *vouchers,
        *(item for item in retained_items if item.collection == "brief_vouchers"),
    ]
    roots = [*roots, *(item for item in retained_items if item.collection == index.ROOTS)]
    # Keep the group's representative independent of which roots were refreshed.
    members = sorted(members, key=_member_order)
    groups, category_counts = _brief_groups.indexes(members)
    entries = sorted(
        [*groups, *vouchers, *roots],
        key=lambda item: (item.collection, item.sort_key, item.item_id),
    )
    if retained is None:
        prepared = prepare_items(entries, collections=COLLECTIONS)
    else:
        reused = {row[:2]: row[:7] for row in retained.prepared.rows}
        if len({(item.collection, item.item_id) for item in entries}) != len(entries):
            index.corrupt()
        for item in entries:
            expected = reused.get((item.collection, item.item_id))
            if expected is not None and expected != (
                item.collection,
                item.item_id,
                item.sort_key,
                item.business_id,
                item.object_id,
                item.group_key,
                item.category,
            ):
                index.corrupt()
        prepared = merge_items(
            retained.prepared,
            prepare_items(
                item for item in entries if (item.collection, item.item_id) not in reused
            ),
            collections=COLLECTIONS,
        )
    model = prepare_model(
        {
            "activity_count": len(members),
            "group_count": sum(item["group_count"] for item in category_counts),
            "voucher_count": len(vouchers),
            "activity_groups": category_counts,
            "review_businesses": _brief_groups.review_businesses(members),
        },
        items=prepared,
    )
    return bind(model, index.dependencies(roots))


def prepare(reader, scope, read):
    return assemble(*capture(reader, scope, read))
