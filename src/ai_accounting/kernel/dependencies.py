"""Shared selection semantics for declared calculator dependencies."""

from __future__ import annotations

from collections.abc import Iterable

from .contracts import Calculation, Fact, FactVersion, KernelError, Read, Registry

ALL_LANES = frozenset({"accounting", "material", "management"})
NO_PERIOD_LIMIT = 119988


def select_fact_dependencies(versions, select_many, reads_for):
    """Batch both declared stages in the same caller-owned transaction."""
    versions = tuple(versions)
    first = {read for version in versions for read in reads_for(version)}
    selections = select_many(sorted(first, key=repr))
    additional = {
        read for version in versions for read in reads_for(version, selections)
    } - first
    for read in additional:
        validate_read(read)
        if read.key == "*" or len(additional) > 64 * max(1, len(versions)):
            raise KernelError("unbounded_read", "第二阶段只允许有界的精确身份依赖")
    if additional:
        selections.update(select_many(sorted(additional, key=repr)))
    return selections


def scope_keys(source: str, fact: Fact, subject_id: str) -> frozenset[str]:
    """Return the persisted scope contract for one source kind.

    Claims describe fact allocations and capacities.  They intentionally do not
    become scopes of the calculation produced from that fact.
    """
    keys = set(fact.scopes_for(subject_id)) | {"@" + subject_id, str(fact.period)}
    if source == "fact":
        keys.update(claim.key for claim in fact.claims())
    elif source != "calculation":
        raise ValueError(f"unknown dependency source {source}")
    return frozenset(keys)


def validate_read(read: Read) -> None:
    if read.key == "*" and read.kind == "*":
        raise KernelError("unbounded_read", "whole-company wildcard reads are not supported")


def read_matches(
    read: Read,
    *,
    source: str,
    kind: str,
    ident: str,
    period,
    scopes: Iterable[str],
    current: bool = True,
    sealed: bool = True,
) -> bool:
    """Match one candidate using the same contract as persistent selection."""
    validate_read(read)
    if read.source != source or (read.before_period is not None and period >= read.before_period):
        return False
    if read.kind != "*" and read.kind != kind:
        return False
    if read.key.startswith("#"):
        return sealed and ident == read.key[1:]
    if not current:
        return False
    if read.key == "*":
        return True
    return read.key in scopes


def calculation_matches(read: Read, calculation: Calculation, fact: Fact) -> bool:
    return read_matches(
        read,
        source="calculation",
        kind=calculation.kind,
        ident=calculation.id,
        period=calculation.period,
        scopes=scope_keys("calculation", fact, calculation.subject_id),
    )


def fact_matches(read: Read, version: FactVersion) -> bool:
    return read_matches(
        read,
        source="fact",
        kind=version.fact.kind,
        ident=version.id,
        period=version.fact.period,
        scopes=scope_keys("fact", version.fact, version.subject_id),
    )


def checked_lanes(registry: Registry, traces) -> tuple[str, ...]:
    """Return every lane that can change an actually used selection."""
    lanes = {"accounting"}
    for version, trace in traces:
        lanes.add(version.fact.lane)
        for read, selected in trace.selections:
            if read.kind == "*":
                # A cross-kind empty selection can gain a matching fact in any
                # lane.  Existing hits cannot safely narrow that future range.
                lanes.update(ALL_LANES)
            elif read.kind in registry.models:
                lanes.add(registry.models[read.kind].lane)
            for item in selected:
                kind = item.fact.kind if isinstance(item, FactVersion) else item.kind
                lanes.add(registry.models[kind].lane)
    return tuple(sorted(lanes))
