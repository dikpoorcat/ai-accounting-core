"""Allocate customer cash refunds to the receipts that authorize them."""

from collections import defaultdict

from sqlalchemy import select

from .models import BusinessEvent, BusinessEventComponent, OpenItem, Settlement


def validate_refund_receipts(compiler):
    session, request = compiler.session, compiler.request
    used = defaultdict(int)
    for refund in session.scalars(
        select(BusinessEventComponent)
        .join(BusinessEvent, BusinessEvent.id == BusinessEventComponent.event_id)
        .where(
            BusinessEventComponent.org_id == request.org_id,
            BusinessEventComponent.kind == "customer_refund",
            BusinessEvent.status == "posted",
        )
    ):
        for allocation in refund.derived.get("receipt_allocations", []):
            used[(allocation["source_component_id"], allocation["receipt_component_id"])] += (
                allocation["amount_fen"]
            )

    for component in request.components:
        if component.kind != "customer_refund" or component.refund_kind != "sale_return":
            continue
        facts, _, _ = compiler.source(component, {"service_sale"})
        if facts["recognition_basis"] != "credit":
            continue
        source = component.source
        source_identity = (
            str(source.component_id) if source.component_id else ("local", source.component_key)
        )
        item = (
            session.scalar(
                select(OpenItem).where(
                    OpenItem.source_component_id == source.component_id,
                    OpenItem.component_key == "primary",
                )
            )
            if source.component_id
            else None
        )
        candidates = []
        if item:
            for settlement, receipt in session.execute(
                select(Settlement, BusinessEventComponent)
                .join(
                    BusinessEventComponent,
                    BusinessEventComponent.id == Settlement.payment_component_id,
                )
                .join(BusinessEvent, BusinessEvent.id == Settlement.payment_event_id)
                .where(
                    Settlement.open_item_id == item.id,
                    Settlement.reversed.is_(False),
                    BusinessEvent.status == "posted",
                    BusinessEvent.posting_date <= request.posting_date,
                    BusinessEventComponent.kind == "receivable_settlement",
                )
                .order_by(
                    BusinessEvent.posting_date,
                    BusinessEvent.idempotency_key,
                    BusinessEventComponent.key,
                )
            ):
                candidates.append(
                    {
                        "component_id": str(receipt.id),
                        "event_id": receipt.event_id,
                        "amount_fen": settlement.amount_fen,
                    }
                )
        for plan in sorted(compiler.plans.values(), key=lambda p: p.key):
            if plan.kind != "receivable_settlement":
                continue
            amount = sum(
                settlement.amount_fen
                for settlement in plan.settlements
                if (item is not None and settlement.open_item_id == item.id)
                or (
                    source.component_key is not None
                    and settlement.source_component_key == source.component_key
                    and settlement.source_open_item_key == "primary"
                )
            )
            if amount:
                candidates.append(
                    {"component_key": plan.key, "event_id": None, "amount_fen": amount}
                )
        remaining, allocations = component.amount_fen, []
        for candidate in candidates:
            receipt_identity = candidate.get("component_id") or (
                "local",
                candidate["component_key"],
            )
            capacity_key = (source_identity, receipt_identity)
            amount = min(remaining, candidate["amount_fen"] - used[capacity_key])
            if amount <= 0:
                continue
            used[capacity_key] += amount
            remaining -= amount
            allocations.append({**candidate, "amount_fen": amount})
            if remaining == 0:
                break
        if remaining:
            raise ValueError("CUSTOMER_REFUND_EXCEEDS_RECEIVED_AMOUNT")

        def persist(session, event, materialized, allocations=allocations, source=source):
            def local_id(key):
                return session.scalar(
                    select(BusinessEventComponent.id).where(
                        BusinessEventComponent.event_id == event.id,
                        BusinessEventComponent.key == key,
                    )
                )

            source_id = source.component_id or local_id(source.component_key)
            resolved = []
            for allocation in allocations:
                receipt_id = allocation.get("component_id") or str(
                    local_id(allocation["component_key"])
                )
                resolved.append(
                    {
                        "source_component_id": str(source_id),
                        "receipt_component_id": receipt_id,
                        "amount_fen": allocation["amount_fen"],
                    }
                )
                if allocation["event_id"] is not None and allocation["event_id"] != event.id:
                    import uuid

                    compiler.source_dependency(
                        allocation["event_id"], uuid.UUID(receipt_id), allocation["amount_fen"]
                    )(session, event, materialized)
            materialized.derived = dict(materialized.derived, receipt_allocations=resolved)

        compiler.plans[component.key].effects.append(persist)
