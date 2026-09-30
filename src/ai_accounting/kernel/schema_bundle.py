"""A paired registry and exact schema contracts, owned by the installed package."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType

from .contracts import Registry
from .migration_contracts import KINDS, load_contracts
from .migration_steps import MigrationStep

FAMILY = "ai-accounting-kernel/2"
APPLICATION_ID = 0x41414332  # AAC2; an auxiliary marker, not a schema trust decision.
STATUS = "draft"
VERSION = 0
DATABASE_FORMAT_KEYS = frozenset({"family", "kind", "status", "version", "fingerprint"})


def valid_database_format(value):
    """Validate the small wire descriptor before comparing it with a trusted contract."""
    if not isinstance(value, dict) or set(value) != DATABASE_FORMAT_KEYS:
        return False
    if any(
        not isinstance(value[key], str) or not value[key]
        for key in DATABASE_FORMAT_KEYS - {"version"}
    ):
        return False
    return (
        value["kind"] in {"company", "catalog"}
        and value["status"] in {"draft", "released"}
        and type(value["version"]) is int
        and value["version"] >= 0
        and (value["status"] == "draft") == (value["version"] == 0)
        and re.fullmatch(r"[a-f0-9]{64}", value["fingerprint"]) is not None
    )


@dataclass(frozen=True)
class SchemaBundle:
    registry: Registry
    family: str
    application_id: int
    status: str
    current_versions: Mapping[str, int]
    contracts: Mapping[str, Mapping[int, dict]]
    steps: tuple[MigrationStep, ...]
    company_verifiers: Mapping[int, Callable]
    development_contracts: Mapping = field(default_factory=lambda: MappingProxyType({}))
    draft_transitions: Mapping = field(default_factory=lambda: MappingProxyType({}))

    def current(self, kind="company"):
        return self.contracts[kind][self.current_versions[kind]]

    def database_format(self, kind="company"):
        item = self.current(kind)
        return {key: item[key] for key in ("family", "kind", "status", "version")} | {
            "fingerprint": item["sha256"]
        }


def load_bundle(
    registry,
    directory,
    *,
    family,
    application_id,
    status,
    current_versions,
    steps=(),
    company_verifiers=None,
    development_contracts=None,
    draft_transitions=None,
):
    """Internal package/test constructor; never a service or external-SQL input."""
    if (
        not isinstance(registry, Registry)
        or not isinstance(family, str)
        or not family
        or type(application_id) is not int
        or not 0 < application_id <= 0x7FFFFFFF
        or set(current_versions) != KINDS
        or status not in {"draft", "released"}
    ):
        raise ValueError("invalid active database contracts")
    loaded = {
        kind: load_contracts(
            Path(directory) / kind, kind, family=family, application_id=application_id
        )
        for kind in sorted(KINDS)
    }
    for kind, version in current_versions.items():
        if (
            type(version) is not int
            or version not in loaded[kind]
            or (loaded[kind][version]["status"] != status)
        ):
            raise ValueError("active database contract is missing")
    for step in steps:
        if step.family != family or step.kind not in loaded:
            raise ValueError("migration belongs to another database family")
        for version, sha256 in (
            (step.source_version, step.source_sha256),
            (step.target_version, step.target_sha256),
        ):
            item = loaded[step.kind].get(version)
            if item is None or item["status"] != "released" or item["sha256"] != sha256:
                raise ValueError("migration endpoint does not match released contract")
    for version, verifier in (company_verifiers or {}).items():
        if type(version) is not int or version not in loaded["company"] or not callable(verifier):
            raise ValueError("invalid company content verifier declaration")
    from .development_contracts import validate_draft_contract

    development_contracts = development_contracts or {}
    draft_transitions = draft_transitions or {}
    if (development_contracts or draft_transitions) and status != "draft":
        raise ValueError("development contracts cannot enter released bundles")
    if (development_contracts.keys() | draft_transitions.keys()) - {"company"}:
        raise ValueError("development adjustment is company-only")
    for sha, item in development_contracts.get("company", {}).items():
        validate_draft_contract(item, family=family, application_id=application_id)
        if item["sha256"] != sha:
            raise ValueError("development contract key mismatch")
    known = set(development_contracts.get("company", {}))
    if status == "draft":
        known.add(loaded["company"][0]["sha256"])
    for source, target in draft_transitions.get("company", ()):
        if source == target or source not in known or target not in known:
            raise ValueError("development transition endpoints not declared")
    return SchemaBundle(
        registry,
        family,
        application_id,
        status,
        MappingProxyType(dict(current_versions)),
        MappingProxyType({kind: MappingProxyType(value) for kind, value in loaded.items()}),
        tuple(steps),
        MappingProxyType(dict(company_verifiers or {})),
        MappingProxyType(
            {kind: MappingProxyType(dict(items)) for kind, items in development_contracts.items()}
        ),
        MappingProxyType({kind: frozenset(items) for kind, items in draft_transitions.items()}),
    )


def verify_current_company(connection, bundle):
    """Content contract for the current company shape, explicitly registered by version."""
    return verify_company_with_registry(connection, bundle, bundle.registry)


def verify_company_with_registry(connection, bundle, registry):
    """Use the declared historical registry for decoding and reference checks."""
    from .engine import Engine
    from .integrity import verify_integrity
    from .storage import Store

    identity = connection.execute("SELECT * FROM identity WHERE id=1").fetchone()
    path = connection.execute("PRAGMA database_list").fetchone()[2]
    store = Store(
        path,
        replace(bundle, registry=registry),
        identity["company_id"],
        identity["database_id"],
    )
    return verify_integrity(Engine(store), connection)


def _require_released_company_verifiers(bundle):
    if bundle.status != "released":
        return bundle
    released = {
        version
        for version, contract in bundle.contracts["company"].items()
        if contract["status"] == "released"
    }
    missing = sorted(released - bundle.company_verifiers.keys())
    if missing:
        raise ValueError(f"released company content verifiers are missing: {missing}")
    return bundle


@lru_cache(maxsize=1)
def production_bundle():
    """The only production factory; callers cannot substitute a registry or directory."""
    from .content_v1 import v1_registry, verify_v1_company
    from .service import default_registry

    if STATUS == "released":
        v1_registry()

    verifiers = (
        {0: verify_current_company}
        if STATUS == "draft"
        else {1: verify_v1_company, **({VERSION: verify_current_company} if VERSION > 1 else {})}
    )

    from .development_contracts import load_development_contracts

    source_sha = "923584f720781cb28a369dd5b269537f64a4551034d306e9447bc2935d263861"
    sources = (
        load_development_contracts(
            Path(__file__).with_name("schema_contracts") / "development" / "company",
            (source_sha,),
            family=FAMILY,
            application_id=APPLICATION_ID,
        )
        if STATUS == "draft"
        else {}
    )
    target_sha = "c9f9f7051bca67f1241ee5c89676fb9476bc819dc8f92c1a0c0bd1e940f459cb"
    return _require_released_company_verifiers(
        load_bundle(
            default_registry(),
            Path(__file__).with_name("schema_contracts"),
            family=FAMILY,
            application_id=APPLICATION_ID,
            status=STATUS,
            current_versions={"company": VERSION, "catalog": VERSION},
            company_verifiers=verifiers,
            development_contracts=sources,
            draft_transitions={"company": ((source_sha, target_sha),)} if STATUS == "draft" else {},
        )
    )
