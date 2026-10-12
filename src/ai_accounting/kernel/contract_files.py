"""Declared runtime contract files, without constructing business registries."""

from pathlib import Path


def contract_file_names(bundle):
    names = []
    for kind in ("company", "catalog"):
        for version, item in sorted(bundle.contracts[kind].items()):
            if bundle.status == "released" and item["status"] != "released":
                continue
            if bundle.status == "draft" and version != bundle.current_versions[kind]:
                continue
            filename = "draft.json" if item["status"] == "draft" else f"v{version}.json"
            names.append(f"{kind}/{filename}")
    if bundle.status == "released":
        if 1 not in bundle.contracts["company"]:
            raise ValueError("Released runtime is missing its v1 company contract")
        names.append("content-v1.json")
    else:
        for kind, sources in getattr(bundle, "development_contracts", {}).items():
            names.extend(f"development/{kind}/{sha}.json" for sha in sources)
    return tuple(sorted(names))


def production_contract_file_names():
    """Use the factory's declarations; never import service or construct its registry.

    Released history is declared from its first version through the active version.
    The current draft also ships its explicitly declared source snapshot.
    """
    from types import SimpleNamespace

    from .development_contracts import REMUNERATION_SOURCE_FINGERPRINT
    from .schema_bundle import STATUS, VERSION

    versions = (0,) if STATUS == "draft" else range(1, VERSION + 1)
    return contract_file_names(
        SimpleNamespace(
            status=STATUS,
            current_versions={kind: VERSION for kind in ("company", "catalog")},
            contracts={
                kind: {version: {"status": STATUS} for version in versions}
                for kind in ("company", "catalog")
            },
            development_contracts=(
                {"company": {REMUNERATION_SOURCE_FINGERPRINT: {}}} if STATUS == "draft" else {}
            ),
        )
    )


def contract_files(directory: Path, names):
    paths = tuple(directory / name for name in names)
    for path in paths:
        if not path.is_file():
            raise ValueError(f"Required runtime contract is missing: {path}")
    return paths
