"""Offline source-document reading and publication for the investment split."""

from collections import defaultdict
from uuid import uuid4

from ai_accounting_modular.contracts.public import canonical_json, loads_json

from . import _documents, _sources, _standard
from ._types import DocumentSnapshot


def open_bundle(path):
    return _standard._read_standard_bundle(path, investment_conversion=True)[0]


def write_bundle(bundle, target, facts, issues):
    """Write a new complete collection; every original input remains untouched."""
    _standard.copy_standard_bundle(bundle, target, investment_conversion=True)
    original_parts = (loads_json(raw) for raw in bundle._parts_json)
    parts = [
        part
        for part in original_parts
        if not part["key"].startswith("facts:") and part["key"] != "work"
    ]
    grouped = defaultdict(list)
    for fact in facts:
        grouped[f"facts:{fact['kind'].split('.')[0]}:{fact['period']}"].append(fact)
    parts.extend(
        {
            "format": _documents.FORMAT,
            "key": key,
            "facts": sorted(rows, key=lambda row: row["business_id"]),
        }
        for key, rows in sorted(grouped.items())
    )
    parts.append({"format": _documents.FORMAT, "key": "work", "issues": list(issues)})
    identity = bundle.company_identity
    identity["working_period_to"] = max(
        bundle.working_period_to, *(fact["period"] for fact in facts)
    )
    snapshot = DocumentSnapshot(
        str(uuid4()),
        str(uuid4()),
        0,
        target,
        canonical_json(identity),
        (),
        tuple(part["key"] for part in parts),
        None,
        None,
        tuple(canonical_json(part) for part in parts),
    )
    prepared = _documents.prepare_documents(snapshot)
    _sources.atomic_write(target, "standard/current.json", _documents.part_bytes(prepared.manifest))
    _sources.atomic_write(target, "standard/README.md", prepared.markdown.encode("utf-8"))
    _documents.cleanup_parts(target, prepared.manifest)
    return _standard.open_standard_bundle(target / "standard/current.json")
