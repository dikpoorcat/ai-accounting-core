"""Public observations and byte checks; no accounting formula is reproduced here."""

import json
from collections import Counter, defaultdict
from dataclasses import asdict, is_dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from ai_accounting_modular.contracts.public import (
    ContractError,
    LedgerScope,
    PageQuery,
    QuerySelection,
)
from ai_accounting_modular.ledger.public import LedgerQuery, SettlementEventQuery
from ai_accounting_modular.records.public import open_standard_bundle

EXAMPLE = Path(__file__).resolve().parents[3] / "docs/redesign/examples/core-acceptance.json"
SPEC = json.loads(EXAMPLE.read_text(encoding="utf-8"))
COMPANY = SPEC["company"]["company_id"]
DATABASE = SPEC["company"]["database_id"]
FAMILIES = (
    "operations",
    "people",
    "assets",
    "financing",
    "investment",
    "money",
    "taxes",
    "opening",
    "reports",
)


def wire(value):
    if is_dataclass(value):
        return wire(asdict(value))
    if isinstance(value, dict):
        return {key: wire(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [wire(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        selected = datetime.fromisoformat(SPEC["clock"]["now"])
        return selected.astimezone(tz) if tz else selected.replace(tzinfo=None)


def digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def documents(root):
    bundle = open_standard_bundle(Path(root) / "standard/current.json")
    locations = defaultdict(list)
    occurrences = Counter()
    manifest = bundle.manifest
    for entry in manifest["shards"]:
        part = json.loads((Path(root) / entry["path"]).read_text(encoding="utf-8"))
        for fact in part.get("facts", []):
            family, month = fact["kind"].split(".")[0], fact["period"]
            locations[fact["business_id"]].append(family + "--" + month)
            occurrences[fact["business_id"]] += 1
    return {
        "set_id": bundle.set_id,
        "manifest_path": str(bundle.manifest_path),
        "manifest_sha256": digest(bundle.manifest_path),
        "facts": list(bundle.facts),
        "issues": list(bundle.issues),
        "business_locations": dict(locations),
        "occurrences_per_business": dict(occurrences),
        "document_occurrences": dict(occurrences),
        "files": [wire(item) for item in bundle.files],
    }


def complete_ledger(kernel, company, method, business_ids=(), roles=(), selection=None):
    values, cursor = [], None
    with kernel.storage.read_unit(company) as read:
        while True:
            page = getattr(kernel.ledger, method)(
                LedgerQuery(business_ids=tuple(business_ids), roles=tuple(roles), cursor=cursor),
                read,
                selection=selection,
            )
            values.extend(wire(page.items))
            if not page.next_cursor:
                assert len(values) == page.total
                return values
            cursor = page.next_cursor


def company_businesses(kernel, company):
    items = []
    for family in FAMILIES:
        query = dict(
            company_id=company,
            family=family,
            period_from="2026-01",
            period_to="2026-03",
            status="all",
            limit=500,
        )
        while True:
            page = kernel.application.accounting_find(query)
            items.extend(page["items"])
            if not page["page"]["has_more"]:
                break
            query["cursor"] = page["page"]["next_cursor"]
    assert len(items) == len({item["business_id"] for item in items})
    return sorted(items, key=lambda item: item["business_id"])


def expense(vouchers):
    # The fixed examples explicitly identify administration expense / payroll cost.
    # Summing complete public lines is an observation, not a duplicate accounting rule.
    return str(
        sum(
            int(line["debit_fen"]) - int(line["credit_fen"])
            for voucher in vouchers
            for line in voucher["lines"]
            if line["account_code"].startswith("5602")
        )
    )


def snapshot(kernel, company=COMPANY, business_ids=(), roles=()):
    directory = company_businesses(kernel, company)
    ids = [item["business_id"] for item in directory]
    businesses = (
        kernel.application.accounting_get(
            dict(company_id=company, business_ids=ids, include=["facts", "results", "sources"])
        )["businesses"]
        if ids
        else []
    )
    results = complete_ledger(kernel, company, "query_results", business_ids)
    adopted = {
        (item["reference"]["result_id"], item["reference"]["generation"]): item for item in results
    }
    for month in ("2026-01", "2026-02", "2026-03"):
        for item in complete_ledger(
            kernel,
            company,
            "query_results",
            business_ids,
            selection=QuerySelection("as_posted", month),
        ):
            adopted[(item["reference"]["result_id"], item["reference"]["generation"])] = item
    vouchers = complete_ledger(kernel, company, "query_vouchers", business_ids, roles)
    obligations = complete_ledger(kernel, company, "query_obligations", business_ids)
    with kernel.storage.read_unit(company) as read:
        metadata = kernel.storage.company_metadata(read)
        status = kernel.records.document_status(read)
        targets = kernel.records.document_target_heads(read)
        scope = LedgerScope(
            company_id=company, period_from="2026-01", period_to="2026-03", view="current"
        )
        money = wire(kernel.money.account_activity(scope, read))
        financial = wire(kernel.ledger.financial_summary(scope, read))
        closed = kernel.ledger.closed_through(read)
        events, cursor = [], None
        while True:
            page = kernel.ledger.query_settlement_events(
                SettlementEventQuery("2026-01", "2026-03"), PageQuery(500, cursor), read
            )
            events.extend(wire(page.items))
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
    return dict(
        database_id=metadata.database_id,
        directory=directory,
        businesses=businesses,
        results=results,
        all_results=sorted(
            adopted.values(),
            key=lambda item: (item["reference"]["posting_period"], item["reference"]["result_id"]),
        ),
        vouchers=vouchers,
        obligations=obligations,
        money=money,
        financial=financial,
        closed=closed,
        events=events,
        documents=documents(metadata.company_root),
        document_status=status,
        document_targets=targets,
    )


def atomic(snapshot):
    return {
        key: snapshot[key]
        for key in (
            "directory",
            "businesses",
            "results",
            "all_results",
            "vouchers",
            "obligations",
            "events",
            "money",
            "documents",
            "document_targets",
        )
    }


def http(kernel, page, parameters):
    values = {key: str(value) for key, value in parameters.items()}
    with kernel.dashboard.get("/api/dashboard/" + page, tuple(values.items())) as response:
        return {"http_status": response.status, "body": wire(response.body)}


def ready(kernel, page="brief", parameters=None, work=None):
    parameters = parameters or dict(company_id=COMPANY, period="2026-01")
    for _ in range(30):
        response = http(kernel, page, parameters)
        body = response["body"]
        if response["http_status"] == 200 and "data" in body and "snapshot_version" in body:
            return body
        assert response["http_status"] == 200 or (
            response["http_status"] == 503 and body.get("code") == "temporary_failure"
        ), response
        if body.get("state") == "ready":
            return body.get("response", body.get("payload"))
        assert body.get("state") == "preparing" or response["http_status"] == 503, response
        if work is not None:
            work["preparing_scope_count"] += 1
        generated = kernel.reading.run_pending(parameters["company_id"], limit=20)
        if work is not None:
            work["regenerated_component_count"] += len(generated)
        assert generated, response
    raise AssertionError("Real read worker did not finish the selected scope")


def public_heads(kernel):
    scope_heads, component_heads = {}, {}
    work = dict(preparing_scope_count=0, regenerated_component_count=0)
    for month in ("2026-01", "2026-02"):
        for page in ("brief", "funds", "employees", "assets", "quarterly_report"):
            params = (
                dict(company_id=COMPANY, period=month)
                if page != "quarterly_report"
                else dict(company_id=COMPANY, year=2026, quarter=1)
            )
            route = page.replace("_", "-")
            response = ready(kernel, route, params, work)
            key = month + "/" + page
            scope_heads[key] = response.get(
                "snapshot_version", response["read_context"]["read_version"]
            )
            # These are public observable collection heads, not internal component IDs.
            component_heads[key] = {
                name: item["page"]["collection_version"]
                for name, item in response.get("data", {}).get("collections", {}).items()
            }
            if not component_heads[key]:
                component_heads[key] = {
                    "public_projection": response["read_context"]["read_version"]
                }
    return dict(scope_heads=scope_heads, component_heads=component_heads, **work)


def source_files(manifest):
    return [
        item
        for item in manifest["files"]
        if item["kind"] == "original" or item["path"].startswith("standard/parts/sources--")
    ]


def defer_documents(kernel):
    """Commit real business normally; hold standard publication at its prepare boundary."""
    return patch.object(
        kernel.records,
        "prepare_documents",
        side_effect=ContractError("busy", "合成验收正在受控暂停标准文档准备。"),
    )
