"""Package-owned exact draft source snapshots, separate from released contracts."""

import json
from pathlib import Path

from .migration_contracts import HEADERS, _digest, _object_map

# The exact last installed draft before confirmed remuneration tax facts.  It
# remains a readable package source; older undeclared drafts are not upgraded.
REMUNERATION_SOURCE_FINGERPRINT = "3ead9f3bcf5ec7507d31f9ef4b36f5aeb24b91251bab184db3a59b77a39ef3a8"
REMUNERATION_TARGET_FINGERPRINT = "e7fc519628e260724cbd9307cdffef1a05904e76860c985a7b06624633114d58"


def remuneration_source_registry(registry):
    """Decode the source using its original bodies, independent of active input."""
    from .contracts import Registry
    from .payroll_history import (
        HistoricalAnnualBonus,
        HistoricalLaborRemuneration,
        HistoricalPayroll,
        HistoricalPayrollBounded,
        HistoricalPayrollProfile,
        HistoricalTaxImportDetails,
    )

    original = {
        model.kind: model for model in (
            HistoricalPayroll, HistoricalPayrollBounded, HistoricalPayrollProfile,
            HistoricalAnnualBonus, HistoricalLaborRemuneration, HistoricalTaxImportDetails,
        )
    }
    result = Registry()
    for kind, model in registry.models.items():
        if kind == "remuneration_tax_basis":
            continue
        result.register(
            original.get(kind, model),
            registry.evaluators.get(kind) if kind not in original else None,
        )
    result.historical_only = registry.historical_only | original.keys()
    # Integrity-only source reads do not install old payroll calculators.
    result.accounting_projectors = dict(registry.accounting_projectors)
    result.accounting_consumers = dict(registry.accounting_consumers)
    result.readiness = dict(registry.readiness)
    result.snapshot_readiness = dict(registry.snapshot_readiness)
    return result


def validate_draft_contract(value, *, family, application_id, kind="company"):
    if (
        not isinstance(value, dict)
        or set(value) != HEADERS | {"objects", "sha256"}
        or value["family"] != family
        or value["kind"] != kind
        or value["status"] != "draft"
        or type(value["version"]) is not int
        or value["version"] != 0
        or type(value["application_id"]) is not int
        or value["application_id"] != application_id
    ):
        raise ValueError("invalid packaged development contract")
    objects = _object_map(value["objects"], sql=True)
    ordered = [objects[key] for key in sorted(objects)]
    if value["objects"] != ordered or _digest(ordered) != value["sha256"]:
        raise ValueError("packaged development contract fingerprint mismatch")
    return value


def load_development_contracts(directory, fingerprints, *, family, application_id):
    """Read only explicitly declared company snapshots; never discover loose files."""
    result = {}
    for fingerprint in fingerprints:
        if not isinstance(fingerprint, str) or len(fingerprint) != 64:
            raise ValueError("invalid development source fingerprint")
        try:
            if bytes.fromhex(fingerprint).hex() != fingerprint:
                raise ValueError("invalid development source fingerprint")
            value = json.loads((Path(directory) / (fingerprint + ".json")).read_text("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("missing or damaged packaged development source") from exc
        validate_draft_contract(value, family=family, application_id=application_id)
        if value["sha256"] != fingerprint or fingerprint in result:
            raise ValueError("development source identity mismatch")
        result[fingerprint] = value
    return {"company": result}


def get_contract_sha(bundle, kind, fingerprint):
    """Return an exact declared contract, without trusting database-provided SQL."""
    current = bundle.current(kind)
    if current["sha256"] == fingerprint:
        return current
    return bundle.development_contracts.get(kind, {}).get(fingerprint)
