"""Package-owned exact draft source snapshots, separate from released contracts."""

from pathlib import Path

from .migration_contracts import HEADERS, _apply_operations, _digest, _object_map
from .stored_json import loads_finite_unique

# The exact last installed draft before confirmed remuneration tax facts.  It
# remains a readable package source; older undeclared drafts are not upgraded.
REMUNERATION_SOURCE_FINGERPRINT = "3ead9f3bcf5ec7507d31f9ef4b36f5aeb24b91251bab184db3a59b77a39ef3a8"
REMUNERATION_TARGET_FINGERPRINT = "e7fc519628e260724cbd9307cdffef1a05904e76860c985a7b06624633114d58"

SOURCE24_FINGERPRINT = "0c8abb83f06fa66266ba5802f5d70cabed3b861015a977757e0538f14a8ee2e7"
SOURCE24_BASELINE_FINGERPRINT = "52556e8ec6cbbb81ab9ad9a69bb87897401471dba9c693e7b7ebddd2a5b08bf4"
HISTORY_PRIOR_FINGERPRINT = "0290c4fcea7c559ed6be6353cf2e68996fcfc2a7bce7f5171c6e91385288b673"
HISTORY_TARGET_FINGERPRINT = "041fb3c8e4de2cf0bde980a54a061cd66563495c4f06d5f6e6b93978d00dadf8"
TAX_POINT_SOURCE_FINGERPRINT = HISTORY_TARGET_FINGERPRINT
TAX_POINT_TARGET_FINGERPRINT = "5f3099cd0fd34330a8f5e917e5c35ad328d7d7685a18df55cf22a07654e18932"
TAX_DECLARATION_SOURCE_FINGERPRINT = TAX_POINT_TARGET_FINGERPRINT
TAX_DECLARATION_TARGET_FINGERPRINT = (
    "5e180a0299af6797b2e83383345085924d8d177729ef593a1cc8e9bbe5607b69"
)
INCOME_TAX_COMPUTATION_SOURCE_FINGERPRINT = TAX_DECLARATION_TARGET_FINGERPRINT
INCOME_TAX_COMPUTATION_TARGET_FINGERPRINT = (
    "4ac19c9f5bff28f19be40b620e8e1c23515b72c471d7a77720ee7fa2bae05099"
)
CATALOG_SOURCE24_FINGERPRINT = "b484d315272bb64de3856b458f04d2d4881da05db7552250864260916c69a4ee"
CATALOG_PRIOR_FINGERPRINT = "0ec502b9c9ca75f97097da3abb496b779d325e6afc4210577755d52b7b75060e"
CATALOG_HISTORY_FINGERPRINT = "42f63ac448d8a7f840a39fac8704680877a3e96e26668c197800b7c224bff35c"
DEVELOPMENT_SOURCES = {
    "company": (REMUNERATION_SOURCE_FINGERPRINT, REMUNERATION_TARGET_FINGERPRINT,
                SOURCE24_FINGERPRINT, SOURCE24_BASELINE_FINGERPRINT, HISTORY_PRIOR_FINGERPRINT,
                TAX_POINT_SOURCE_FINGERPRINT, TAX_DECLARATION_SOURCE_FINGERPRINT,
                INCOME_TAX_COMPUTATION_SOURCE_FINGERPRINT),
    "catalog": (CATALOG_SOURCE24_FINGERPRINT, CATALOG_PRIOR_FINGERPRINT),
}
DEVELOPMENT_TRANSITIONS = {
    "company": ((REMUNERATION_SOURCE_FINGERPRINT, REMUNERATION_TARGET_FINGERPRINT),
                (REMUNERATION_TARGET_FINGERPRINT, HISTORY_TARGET_FINGERPRINT),
                (SOURCE24_BASELINE_FINGERPRINT, SOURCE24_FINGERPRINT),
                (SOURCE24_FINGERPRINT, HISTORY_TARGET_FINGERPRINT),
                (SOURCE24_FINGERPRINT, HISTORY_PRIOR_FINGERPRINT),
                (REMUNERATION_TARGET_FINGERPRINT, HISTORY_PRIOR_FINGERPRINT),
                (HISTORY_PRIOR_FINGERPRINT, HISTORY_TARGET_FINGERPRINT),
                (TAX_POINT_SOURCE_FINGERPRINT, TAX_POINT_TARGET_FINGERPRINT),
                (TAX_DECLARATION_SOURCE_FINGERPRINT, TAX_DECLARATION_TARGET_FINGERPRINT),
                (INCOME_TAX_COMPUTATION_SOURCE_FINGERPRINT,
                 INCOME_TAX_COMPUTATION_TARGET_FINGERPRINT)),
    "catalog": ((CATALOG_SOURCE24_FINGERPRINT, CATALOG_HISTORY_FINGERPRINT),
                (CATALOG_PRIOR_FINGERPRINT, CATALOG_HISTORY_FINGERPRINT)),
}


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
        if kind in {
            "remuneration_tax_basis", "tax_point_basis", "vat_declaration_actual",
            "income_tax_declaration_actual",
        }:
            continue
        if kind in original:
            result.register(original[kind])
        else:
            result.register(
                model, registry.evaluators.get(kind), table=registry.current_table(kind),
                historical=registry.fact_bodies(kind)[1:],
            )
    result.historical_only = registry.historical_only | original.keys()
    # Integrity-only source reads do not install old payroll calculators.
    result.accounting_projectors = dict(registry.accounting_projectors)
    result.accounting_consumers = dict(registry.accounting_consumers)
    result.readiness = dict(registry.readiness)
    result.snapshot_readiness = dict(registry.snapshot_readiness)
    return result


def _validate_header(value, *, family, application_id, kind):
    if (
        not isinstance(value, dict)
        or not HEADERS <= value.keys()
        or value["family"] != family
        or value["kind"] != kind
        or value["status"] != "draft"
        or type(value["version"]) is not int
        or value["version"] != 0
        or type(value["application_id"]) is not int
        or value["application_id"] != application_id
    ):
        raise ValueError("invalid packaged development contract")


def validate_draft_contract(value, *, family, application_id, kind="company"):
    _validate_header(value, family=family, application_id=application_id, kind=kind)
    if set(value) != HEADERS | {"objects", "sha256"}:
        raise ValueError("invalid packaged development contract")
    objects = _object_map(value["objects"], sql=True)
    ordered = [objects[key] for key in sorted(objects)]
    if value["objects"] != ordered or _digest(ordered) != value["sha256"]:
        raise ValueError("packaged development contract fingerprint mismatch")
    return value


def load_development_contracts(directory, fingerprints, *, family, application_id, kind="company"):
    """Expand only declared fingerprint parents; preserve exact SQL and identity."""
    raw, result, visiting = {}, {}, set()
    for fingerprint in fingerprints:
        if not isinstance(fingerprint, str) or len(fingerprint) != 64:
            raise ValueError("invalid development source fingerprint")
        try:
            if bytes.fromhex(fingerprint).hex() != fingerprint:
                raise ValueError("invalid development source fingerprint")
            value = loads_finite_unique(
                (Path(directory) / (fingerprint + ".json")).read_text("utf-8")
            )
        except (OSError, UnicodeError, ValueError) as exc:
            raise ValueError("missing or damaged packaged development source") from exc
        _validate_header(value, family=family, application_id=application_id, kind=kind)
        if value.get("sha256") != fingerprint or fingerprint in raw:
            raise ValueError("development source identity mismatch")
        raw[fingerprint] = value

    def expand(fingerprint):
        if not isinstance(fingerprint, str) or fingerprint not in raw:
            raise ValueError("missing declared development parent")
        if fingerprint in result:
            return result[fingerprint]
        if fingerprint in visiting:
            raise ValueError("cyclic development contract parents")
        visiting.add(fingerprint)
        value = raw[fingerprint]
        if "objects" in value:
            expanded = validate_draft_contract(
                value, family=family, application_id=application_id, kind=kind,
            )
        else:
            if set(value) != HEADERS | {"base_sha256", "add", "remove", "replace", "sha256"}:
                raise ValueError("invalid incremental development contract")
            parent = expand(value["base_sha256"])
            try:
                items = _apply_operations(parent["objects"], add=value["add"],
                                          remove=value["remove"], replace=value["replace"])
            except RuntimeError as exc:
                raise ValueError("invalid incremental development operations") from exc
            expanded = validate_draft_contract(
                {**{key: value[key] for key in HEADERS}, "sha256": fingerprint,
                 "objects": [items[key] for key in sorted(items)]},
                family=family, application_id=application_id, kind=kind,
            )
        visiting.remove(fingerprint)
        result[fingerprint] = expanded
        return expanded

    for fingerprint in raw:
        expand(fingerprint)
    return {kind: result}


def get_contract_sha(bundle, kind, fingerprint):
    """Return an exact declared contract, without trusting database-provided SQL."""
    current = bundle.current(kind)
    if current["sha256"] == fingerprint:
        return current
    return bundle.development_contracts.get(kind, {}).get(fingerprint)
