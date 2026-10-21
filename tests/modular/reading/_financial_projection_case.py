"""A real company with a controlled Ledger boundary, never an accounting oracle.

The boundary adapter delegates every amount/Snapshot to the existing real Ledger
public methods. It only supplies small opaque plans to isolate Reading races;
these tests do not qualify the production financial tree or its loader.
"""

from dataclasses import asdict, fields
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import canonical_json, content_digest, loads_json
from ai_accounting_modular.host.public import create_kernel
from ai_accounting_modular.ledger.public import (
    FinancialProjectionArtifact,
    FinancialProjectionPlan,
    FinancialSummaryValues,
)
from ai_accounting_modular.reading import _financial_projection as projection
from ai_accounting_modular.reading import _monthly
from ai_accounting_modular.storage.public import CompanySpec, OwnerProfile, create_catalog
from tests.modular.acceptance.performance.ledger_variant import MODES, install_ledger_implementation
from tests.modular.application._working_case import Case


class BoundaryOwner:
    def __init__(self, ledger):
        self.ledger, self.prepared, self.captured = ledger, [], []

    def prepare(self, requests, changes, closures, read, *, base, rebuild=False):
        self.prepared.append((requests, changes, closures, rebuild))
        header = loads_json(base.header_json) if base is not None else {"requests": []}
        writes = []
        for request in requests:
            if request.kind == "summary_values":
                summary = self.ledger.financial_summary(request.scope, read)
                values = {
                    field.name: getattr(summary, field.name)
                    for field in fields(summary)
                    if field.name not in {"version", "scope", "issues", "sources"}
                }
                value = asdict(
                    FinancialSummaryValues(summary.version, summary.scope, values, summary.issues)
                )
                value["issues"] = list(value["issues"])
                version = summary.version
            else:
                value = self.ledger.financial_snapshot(request.scope, read)
                version = value["selection"]["selection_digest"]
                value = {name: item for name, item in value.items() if name != "head"}
            body = {
                "request_key": request.key,
                "kind": request.kind,
                "value": value,
                "version": version,
            }
            writes.append((*request.descriptor_key, canonical_json(body)))
        header["requests"] = sorted({*header["requests"], *(value.key for value in requests)})
        raw = canonical_json(header)
        return FinancialProjectionPlan(raw, content_digest(header), tuple(writes), ())

    def capture(self, request, read, base):
        self.captured.append(request)
        if base.header_json is None or request.key not in loads_json(base.header_json)["requests"]:
            return None
        raw = base.get_blocks((request.descriptor_key,))[0]
        if raw is None:
            projection.corrupt()
        body = loads_json(raw)
        value = body["value"]
        if request.kind == "summary_values":
            value = FinancialSummaryValues(
                value["version"], value["scope"], value["values"], tuple(value["issues"])
            )
        else:
            value = value | {"head": read.head}
        return FinancialProjectionArtifact(
            request.key, request.descriptor_key, content_digest(body), body["version"], value
        )


@pytest.fixture(params=MODES)
def financial_case(request, tmp_path, monkeypatch):
    with install_ledger_implementation(request.param):
        catalog = tmp_path / "catalog.sqlite3"
        create_catalog(catalog, OwnerProfile(str(uuid4()), "合成财务读取负责人", "synthetic-hash"))
        kernel = create_kernel(catalog)
        company = str(uuid4())
        kernel.storage.create_company(
            CompanySpec(
                company,
                str(uuid4()),
                "913100000000000001",
                "合成财务公司",
                "2026-01",
                tmp_path / "company",
            )
        )
        case = Case(kernel, company, str(uuid4()), str(uuid4()))
        owner = BoundaryOwner(kernel.ledger)
        monkeypatch.setattr(
            kernel.ledger, "prepare_financial_projection", owner.prepare, raising=False
        )
        monkeypatch.setattr(
            kernel.ledger, "read_financial_projection", owner.capture, raising=False
        )
        try:
            yield case, owner
        finally:
            kernel.close()


def scope(case, month="2026-01"):
    return _monthly.normalize(
        {"company_id": case.company, "period_from": month, "period_to": month, "view": "as_posted"}
    )


def page(case, month="2026-01"):
    return _monthly.deliver(
        case.kernel.reading,
        scope(case, month),
        (_monthly.FINANCIAL,),
        lambda snapshot, read: snapshot.components["financial"],
    )


def quarter(case):
    return case.kernel.reading.page(
        "quarterly_report",
        {
            "company_id": case.company,
            "period_from": "2026-01",
            "period_to": "2026-03",
            "view": "as_posted",
        },
        {},
    )


def drain(case):
    completed = []
    for _ in range(10):
        produced = case.kernel.reading.run_pending(case.company)
        if not produced:
            return tuple(completed)
        completed.extend(produced)
    pytest.fail("financial boundary work did not settle")


def captured(case, month="2026-01"):
    reader = case.kernel.reading
    with reader.storage.read_unit(case.company) as read:
        return projection.capture(reader, scope(case, month), "summary_values", read)


def publish(case, prepared, month="2026-01"):
    selected = scope(case, month)
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        return unit.repository("reading").publish(
            _monthly.key(selected), "financial", unit.head, _monthly.version("financial"), prepared
        )
