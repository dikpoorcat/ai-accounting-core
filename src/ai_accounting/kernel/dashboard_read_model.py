"""Authenticated, derived dashboard content with immutable production receipts.

This module authenticates stored dashboard bytes, not business sources. Production
must use the ordinary domain algorithms, and consumers must authenticate returned
sources. Complete verification independently rebuilds the model. No success proof
or decoded value is retained across requests.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from sqlite3 import Connection
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contracts import KernelError
from .dashboard_model_dates import ModelDateBasis
from .response_contracts import (
    RESPONSE_ADAPTERS,
    DashboardAssetsResponse,
    DashboardBriefGroupResponse,
    DashboardBriefResponse,
    DashboardEmployeesResponse,
    DashboardQuarterlyReportResponse,
    FundsDashboardResponse,
)
from .stored_json import loads_finite_unique, loads_unique
from .types import ActualDate, EvidenceDigest, YearMonth, canonical, digest

CONTENT_VERSION = 3
DASHBOARD_READ_MODEL_DDL = """
CREATE TABLE dashboard_read_model_commit(
 id BLOB PRIMARY KEY CHECK(length(id)=32),
 period TEXT NOT NULL,
 request_id TEXT NOT NULL,
 audit_id INTEGER NOT NULL REFERENCES audit(id),
 descriptor TEXT NOT NULL CHECK(json_valid(descriptor)),
 root_digest BLOB NOT NULL CHECK(length(root_digest)=32),
 parent_id BLOB REFERENCES dashboard_read_model_commit(id),
 UNIQUE(audit_id,period)
) STRICT;
CREATE INDEX dashboard_read_model_commit_period ON dashboard_read_model_commit(period,audit_id);
CREATE TRIGGER immutable_dashboard_read_model_commit_update
 BEFORE UPDATE ON dashboard_read_model_commit
 BEGIN SELECT RAISE(ABORT,'immutable dashboard model receipt'); END;
CREATE TRIGGER immutable_dashboard_read_model_commit_delete
 BEFORE DELETE ON dashboard_read_model_commit
 BEGIN SELECT RAISE(ABORT,'immutable dashboard model receipt'); END;
CREATE TABLE dashboard_read_model_block(
 digest BLOB PRIMARY KEY CHECK(length(digest)=32),
 payload TEXT NOT NULL CHECK(json_valid(payload))
) STRICT;
CREATE TABLE dashboard_read_model_current(
 period TEXT PRIMARY KEY,
 commit_id BLOB NOT NULL REFERENCES dashboard_read_model_commit(id)
) STRICT;
"""

Nonempty = Annotated[str, Field(strict=True, min_length=1, max_length=200)]
Revision = Annotated[int, Field(strict=True, ge=0)]
Positive = Annotated[int, Field(strict=True, ge=1)]
BlockKind = Literal[
    "dashboard_brief",
    "dashboard_funds",
    "dashboard_employees",
    "dashboard_assets",
    "dashboard_quarterly_report",
    "dashboard_brief_group",
]
DashboardPayload = (
    DashboardBriefResponse
    | FundsDashboardResponse
    | DashboardEmployeesResponse
    | DashboardAssetsResponse
    | DashboardQuarterlyReportResponse
    | DashboardBriefGroupResponse
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class ModelEpochs(_StrictModel):
    accounting: Revision
    material: Revision
    management: Revision


class ModelScope(_StrictModel):
    company_id: Nonempty
    database_id: Nonempty
    period: YearMonth
    epochs: ModelEpochs
    read_repair_revision: Revision
    rule_build: Nonempty
    as_of: ActualDate


class BlockReference(_StrictModel):
    key: Nonempty
    kind: BlockKind
    digest: EvidenceDigest
    item_count: Revision
    total_count: Revision | None
    filtered_count: Revision | None
    keys: tuple[tuple[str | int, ...], ...]
    collection_version: Nonempty | None = None

    @model_validator(mode="after")
    def valid_directory(self):
        if len({tuple(key) for key in self.keys}) != len(self.keys):
            raise ValueError("duplicate seek key")
        if any(not key for key in self.keys):
            raise ValueError("empty seek key")
        if self.total_count is not None and len(self.keys) != self.item_count:
            raise ValueError("seek directory count mismatch")
        if (self.total_count is None) != (self.filtered_count is None):
            raise ValueError("incomplete collection counts")
        if (
            self.total_count is not None
            and not self.item_count <= self.filtered_count <= self.total_count
        ):
            raise ValueError("collection counts mismatch")
        if self.total_count is None and (self.keys or self.item_count != 1):
            raise ValueError("overview directory mismatch")
        return self


class ModelRoot(_StrictModel):
    format: Literal[
        "ai-accounting-kernel/2/dashboard-read-model/1",
        "ai-accounting-kernel/2/dashboard-read-model/2",
        "ai-accounting-kernel/2/dashboard-read-model/3",
    ]
    content_version: Positive
    scope: ModelScope
    blocks: tuple[BlockReference, ...]
    date_basis: ModelDateBasis | None = None

    @model_validator(mode="after")
    def unique_blocks(self):
        if self.format != f"ai-accounting-kernel/2/dashboard-read-model/{self.content_version}":
            raise ValueError("read model content format/version mismatch")
        if self.content_version == 1 and self.date_basis is not None:
            raise ValueError("historical read model cannot acquire calendar inputs")
        if self.date_basis is not None and self.date_basis.valid_from != self.scope.as_of:
            raise ValueError("calendar inputs belong to another production day")
        if len({block.key for block in self.blocks}) != len(self.blocks):
            raise ValueError("duplicate block key")
        collections = {}
        for block in self.blocks:
            has_context = "collection_version" in block.model_fields_set
            if has_context != (self.content_version >= 3):
                raise ValueError("paging context belongs to its content version")
            if block.total_count is None and block.collection_version is not None:
                raise ValueError("overview cannot carry a collection version")
            pieces = block.key.split("/")
            if len(pieces) != 4 or f"dashboard_{pieces[0]}" != block.kind or not pieces[1]:
                raise ValueError("block page identity mismatch")
            if pieces[0] == "brief_group" and (
                self.content_version < 2
                or re.fullmatch(r"(activity|open_items):[0-9a-f]{64}", pieces[1]) is None
                or pieces[2] != "members"
            ):
                raise ValueError("business member directory identity mismatch")
            try:
                ordinal = int(pieces[3])
            except ValueError as exc:
                raise ValueError("invalid block ordinal") from exc
            if ordinal < 0 or str(ordinal) != pieces[3]:
                raise ValueError("invalid block ordinal")
            if block.total_count is None:
                if pieces[2:] != ["overview", "0"]:
                    raise ValueError("invalid overview locator")
                continue
            if pieces[2] == "overview":
                raise ValueError("invalid collection locator")
            collections.setdefault(tuple(pieces[:3]), []).append((ordinal, block))
        for blocks in collections.values():
            blocks.sort(key=lambda item: item[0])
            if [ordinal for ordinal, _ in blocks] != list(range(len(blocks))):
                raise ValueError("missing collection block")
            references = [block for _, block in blocks]
            first = references[0]
            if any(
                (block.total_count, block.filtered_count, block.collection_version)
                != (first.total_count, first.filtered_count, first.collection_version)
                for block in references
            ):
                raise ValueError("inconsistent collection scope")
            if sum(block.item_count for block in references) != first.filtered_count:
                raise ValueError("incomplete collection directory")
            keys = [key for block in references for key in block.keys]
            if len(set(keys)) != len(keys):
                raise ValueError("duplicate collection member")
        if self.date_basis is not None:
            roster = collections.get(("employees", "all", "employees"))
            if roster is None:
                raise ValueError("calendar inputs need the complete employee directory")
            keys = [key[0] for _, block in roster for key in block.keys]
            if any(len(key) != 4 for _, block in roster for key in block.keys):
                raise ValueError("calendar employee directory requires exact period predicates")
            expected = {item.employee_id for item in self.date_basis.employees}
            if set(keys) != expected or len(keys) != len(expected):
                raise ValueError(
                    "calendar input identities differ from complete employee directory"
                )
            if any(
                block.total_count != len(expected) or block.filtered_count != len(expected)
                for _, block in roster
            ):
                raise ValueError("calendar directory does not cover every employee")
        return self


class ModelDescriptor(_StrictModel):
    commit_id: EvidenceDigest
    request_id: Nonempty
    content_version: Positive
    scope: ModelScope
    root_digest: EvidenceDigest
    parent_commit_id: EvidenceDigest | None


_PAYLOAD_ADAPTERS = {
    name: RESPONSE_ADAPTERS[name]
    for name in (
        "dashboard_brief",
        "dashboard_funds",
        "dashboard_employees",
        "dashboard_assets",
        "dashboard_quarterly_report",
        "dashboard_brief_group",
    )
}


def _failure():
    return KernelError(
        "content_integrity_failed", "看板读取内容或生产依据不完整", component="dashboard_read_model"
    )


def _checked_payload(kind: BlockKind, payload):
    """Use the existing finite native contracts; no generic JSON money escape."""
    try:
        adapter = _PAYLOAD_ADAPTERS[kind]
        return adapter.validate_python(payload, strict=True)
    except (KeyError, TypeError, ValueError) as exc:
        raise _failure() from exc


def _checked_scope(payload, scope: ModelScope):
    if not isinstance(payload, dict):
        return
    context = payload.get("read_context")
    if context is not None and (
        context["company_id"] != scope.company_id
        or context["database_id"] != scope.database_id
        or context["as_of"] != scope.as_of
    ):
        raise _failure()
    selected = payload.get("selected_period")
    if selected is not None and selected["key"] != scope.period:
        raise _failure()
    if "period" in payload:
        # A quarterly aggregate belongs to its registered month's quarter;
        # a company may be working in January before March exists.
        period = payload["period"]
        year, month = map(int, str(scope.period).split("-"))
        if period["year"] != year or period["quarter"] != (month - 1) // 3 + 1:
            raise _failure()


def _checked_metadata(value):
    if not isinstance(value, dict) or set(value) != {
        "keys",
        "sources",
        "voucher_version_ids",
        "item_sources",
        "item_voucher_version_ids",
        "fact_sources",
        "item_fact_sources",
    }:
        raise _failure()
    keys, sources, vouchers = value["keys"], value["sources"], value["voucher_version_ids"]
    if not isinstance(keys, list) or any(
        not isinstance(key, list) or not key or any(type(item) not in (str, int) for item in key)
        for key in keys
    ):
        raise _failure()
    if len({tuple(key) for key in keys}) != len(keys):
        raise _failure()
    if not isinstance(sources, list) or any(
        not isinstance(source, list)
        or len(source) != 4
        or any(type(item) is not str or not item for item in source)
        or any(len(source[index]) > 200 for index in (0, 2))
        or any(not _is_digest(source[index]) for index in (1, 3))
        for source in sources
    ):
        raise _failure()
    source_set = {tuple(source) for source in sources}
    if len(source_set) != len(sources):
        raise _failure()
    facts = value["fact_sources"]
    if not isinstance(facts, list) or any(
        not isinstance(fact, list)
        or len(fact) != 2
        or type(fact[0]) is not str
        or not fact[0]
        or len(fact[0]) > 200
        or not _is_digest(fact[1])
        for fact in facts
    ):
        raise _failure()
    fact_set = {tuple(fact) for fact in facts}
    if len(fact_set) != len(facts):
        raise _failure()
    if not isinstance(vouchers, list) or any(
        type(item) is not str or not item or len(item) > 200 for item in vouchers
    ):
        raise _failure()
    voucher_set = set(vouchers)
    if len(voucher_set) != len(vouchers):
        raise _failure()
    item_sources, item_vouchers = value["item_sources"], value["item_voucher_version_ids"]
    item_facts = value["item_fact_sources"]
    if (
        not isinstance(item_sources, list)
        or not isinstance(item_vouchers, list)
        or len(item_sources) != len(keys)
        or len(item_vouchers) != len(keys)
        or not isinstance(item_facts, list)
        or len(item_facts) != len(keys)
    ):
        raise _failure()
    bound_sources, bound_vouchers, bound_facts = set(), set(), set()
    for bindings, versions, fact_bindings in zip(
        item_sources, item_vouchers, item_facts, strict=True
    ):
        # Top-level bindings already prove every ID's length and digest format.
        # Check item shape/types before hashing, then require those exact tuples.
        if not isinstance(bindings, list) or any(
            not isinstance(source, list)
            or len(source) != 4
            or any(type(item) is not str for item in source)
            for source in bindings
        ):
            raise _failure()
        selected_sources = {tuple(source) for source in bindings}
        if len(selected_sources) != len(bindings) or not selected_sources <= source_set:
            raise _failure()
        if not isinstance(versions, list) or any(type(item) is not str for item in versions):
            raise _failure()
        selected_vouchers = set(versions)
        if len(selected_vouchers) != len(versions) or not selected_vouchers <= voucher_set:
            raise _failure()
        if not isinstance(fact_bindings, list) or any(
            not isinstance(fact, list)
            or len(fact) != 2
            or type(fact[0]) is not str
            or not isinstance(fact[1], str)
            for fact in fact_bindings
        ):
            raise _failure()
        selected_facts = {tuple(fact) for fact in fact_bindings}
        if len(selected_facts) != len(fact_bindings) or not selected_facts <= fact_set:
            raise _failure()
        bound_sources.update(selected_sources)
        bound_vouchers.update(selected_vouchers)
        bound_facts.update(selected_facts)
    if keys and (
        source_set != bound_sources
        or voucher_set != bound_vouchers
        or fact_set != bound_facts
    ):
        raise _failure()
    return value


def _is_digest(value):
    from .types import is_sha256_hex

    return is_sha256_hex(value)


def _check_collection(payload, reference: BlockReference):
    if reference.kind == "dashboard_brief" and payload["scope"] != reference.key.split("/")[-2]:
        raise _failure()
    if reference.total_count is None:
        if reference.keys:
            raise _failure()
        return
    try:
        if reference.kind == "dashboard_brief_group":
            section, group_key = reference.key.split("/")[1].split(":", 1)
            if payload["data"]["section"] != section or payload["data"]["group_key"] != group_key:
                raise _failure()
            members = payload["data"]["collections"]["members"]["items"]
            if reference.keys != tuple(
                (
                    item["key" if section == "activity" else "id"],
                    item["voucher_version_id"] or "",
                )
                for item in members
            ):
                raise _failure()
        section = reference.key.split("/")[-2]
        collection = payload["data"]["collections"][section]
        page_name = reference.key.split("/")[0]
        if page_name in {"funds", "employees", "assets"} and any(
            len(key) > 1 for key in reference.keys
        ):
            from .dashboard_model_filters import decorate_keys

            expected_keys = decorate_keys(
                page_name, section, [key[0] for key in reference.keys], collection["items"]
            )
            if reference.keys != expected_keys:
                raise _failure()
            identity_field = {
                ("funds", "movements"): "id",
                ("funds", "statements"): "id",
                ("employees", "employees"): "employee_id",
                ("assets", "assets"): "asset_id",
            }.get((page_name, section))
            if identity_field is not None and any(
                key[0] != item[identity_field]
                for key, item in zip(reference.keys, collection["items"], strict=True)
            ):
                raise _failure()
        if (
            reference.kind == "dashboard_brief"
            and section == "vouchers"
            and any(len(key) > 1 for key in reference.keys)
            and reference.keys
            != tuple(
                (int(item["number"]), item["voucher_version_id"]) for item in collection["items"]
            )
        ):
            raise _failure()
        page = collection["page"]
        if ("collection_version" in reference.model_fields_set
                and page.get("collection_version") != reference.collection_version):
            raise _failure()
        if (
            len(collection["items"]) != reference.item_count
            or page["returned_count"] != reference.item_count
            or page["total_count"] != reference.total_count
            or page["filtered_count"] != reference.filtered_count
        ):
            raise _failure()
    except (TypeError, KeyError, IndexError) as exc:
        raise _failure() from exc


@dataclass(frozen=True)
class PageBlock:
    """Internal producer input. Every value passes a known dashboard contract."""

    key: str
    kind: BlockKind
    payload: DashboardPayload
    keys: tuple[tuple[str | int, ...], ...] = ()
    sources: tuple[tuple[str, str, str, str], ...] = ()
    voucher_version_ids: tuple[str, ...] = ()
    total_count: int | None = None
    filtered_count: int | None = None
    item_sources: tuple[tuple[tuple[str, str, str, str], ...], ...] = ()
    item_voucher_version_ids: tuple[tuple[str, ...], ...] = ()
    fact_sources: tuple[tuple[str, str], ...] = ()
    item_fact_sources: tuple[tuple[tuple[str, str], ...], ...] = ()


@dataclass(frozen=True)
class PreparedModel:
    """Canonical bytes prepared before the transaction's audit insertion."""

    descriptor_json: str
    root_json: str
    block_rows: tuple[tuple[bytes, str], ...]

    @property
    def descriptor(self) -> dict:
        # A fresh object cannot mutate the canonical production content.
        return loads_unique(self.descriptor_json)


def _descriptor_id(value: dict) -> str:
    return digest({key: item for key, item in value.items() if key != "commit_id"}).hex()


def prepare_model(
    scope: ModelScope,
    blocks: list[PageBlock],
    *,
    request_id: str,
    parent_commit_id: str | None = None,
    date_basis: ModelDateBasis | None = None,
) -> PreparedModel:
    """Validate typed producer output without writing or accepting arbitrary JSON."""
    scope = ModelScope.model_validate(scope)
    references, rows = [], {}
    for block in blocks:
        payload = _checked_payload(block.kind, block.payload)
        _checked_scope(payload, scope)
        metadata = _checked_metadata(
            {
                "keys": [list(key) for key in block.keys],
                "sources": [list(source) for source in block.sources],
                "voucher_version_ids": list(block.voucher_version_ids),
                "item_sources": [
                    [list(source) for source in bindings] for bindings in block.item_sources
                ],
                "item_voucher_version_ids": [
                    list(versions) for versions in block.item_voucher_version_ids
                ],
                "fact_sources": [list(fact) for fact in block.fact_sources],
                "item_fact_sources": [
                    [list(fact) for fact in bindings] for bindings in block.item_fact_sources
                ],
            }
        )
        encoded = canonical({"kind": block.kind, "payload": payload, **metadata})
        block_digest = hashlib.sha256(encoded.encode("utf-8")).digest()
        reference = BlockReference(
            key=block.key,
            kind=block.kind,
            digest=block_digest.hex(),
            item_count=len(block.keys) if block.total_count is not None else 1,
            total_count=block.total_count,
            filtered_count=block.filtered_count,
            keys=tuple(tuple(key) for key in metadata["keys"]),
            **({"collection_version": (
                payload["data"]["collections"][block.key.split("/")[-2]]["page"].get(
                    "collection_version"
                ) if block.total_count is not None else None
            )} if CONTENT_VERSION >= 3 else {}),
        )
        _check_collection(payload, reference)
        references.append(reference)
        rows[block_digest] = encoded
    root = ModelRoot(
        format=f"ai-accounting-kernel/2/dashboard-read-model/{CONTENT_VERSION}",
        content_version=CONTENT_VERSION,
        scope=scope,
        blocks=tuple(references),
        date_basis=date_basis,
    )
    excluded = {}
    if CONTENT_VERSION == 1:
        excluded["date_basis"] = True
    if CONTENT_VERSION < 3:
        excluded["blocks"] = {"__all__": {"collection_version"}}
    root_json = canonical(
        root.model_dump(
            mode="python",
            exclude=excluded,
        )
    )
    root_digest = hashlib.sha256(root_json.encode("utf-8")).digest()
    value = {
        "request_id": request_id,
        "content_version": CONTENT_VERSION,
        "scope": scope.model_dump(mode="python"),
        "root_digest": root_digest.hex(),
        "parent_commit_id": parent_commit_id,
    }
    descriptor = ModelDescriptor(commit_id=_descriptor_id(value), **value)
    rows[root_digest] = root_json
    return PreparedModel(
        canonical(descriptor.model_dump(mode="python")), root_json, tuple(rows.items())
    )


def _parse_descriptor(raw) -> ModelDescriptor:
    try:
        value = loads_unique(raw)
        descriptor = ModelDescriptor.model_validate(value)
        if descriptor.commit_id != _descriptor_id(value):
            raise _failure()
        return descriptor
    except (TypeError, ValueError) as exc:
        if isinstance(exc, KernelError):
            raise
        raise _failure() from exc


def _audit_descriptors(raw) -> list[ModelDescriptor]:
    try:
        payload = loads_unique(raw)
        from .provenance import _result

        payload = _result(payload)
        if not isinstance(payload, dict) or set(payload) != {"status", "dashboard_models"}:
            raise _failure()
        if payload["status"] != "materialized":
            raise _failure()
        values = payload["dashboard_models"]
        if not isinstance(values, list) or not values:
            raise _failure()
        descriptors = [_parse_descriptor(canonical(value)) for value in values]
        if len({value.scope.period for value in descriptors}) != len(descriptors):
            raise _failure()
        return descriptors
    except (TypeError, ValueError) as exc:
        if isinstance(exc, KernelError):
            raise
        raise _failure() from exc


def _verified_audit_descriptors(
    audit_id, request_id, action, payload, request_result, request_digest, verified_batches,
):
    """Reuse exact batch proof only within the caller's current transaction.

    Batch consumers create and discard this dictionary inside one invocation.
    IDs locate a proof; the complete original audit/request bytes must match
    before it can be reused. A conflicting row never replaces an earlier proof.
    """
    key = (audit_id, request_id)
    original = (action, payload, request_result, request_digest)
    if verified_batches is not None and key in verified_batches:
        previous, descriptors = verified_batches[key]
        if previous != original:
            raise _failure()
        return descriptors
    if action != "dashboard_read_model":
        raise _failure()
    members = _audit_descriptors(payload)
    descriptors = {descriptor.commit_id: descriptor for descriptor in members}
    if len(descriptors) != len(members):
        raise _failure()
    _check_request(payload, request_result, request_digest)
    if verified_batches is not None:
        verified_batches[key] = (original, descriptors)
    return descriptors


def _anchored_descriptor(row, *, _verified_batches=None) -> ModelDescriptor:
    descriptor = _parse_descriptor(row["descriptor"])
    parent = bytes(row["parent_id"]).hex() if row["parent_id"] is not None else None
    if (
        bytes(row["id"]).hex() != descriptor.commit_id
        or row["period"] != descriptor.scope.period
        or row["request_id"] != descriptor.request_id
        or row["anchor_request_id"] != descriptor.request_id
        or bytes(row["root_digest"]).hex() != descriptor.root_digest
        or parent != descriptor.parent_commit_id
        or row["anchor_action"] != "dashboard_read_model"
    ):
        raise _failure()
    anchored = _verified_audit_descriptors(
        row["audit_id"], row["request_id"], row["anchor_action"], row["anchor_payload"],
        row["request_result"], row["request_digest"], _verified_batches,
    )
    if anchored.get(descriptor.commit_id) != descriptor:
        raise _failure()
    return descriptor


def _receipt_row(connection: Connection, commit_id):
    return connection.execute(
        "SELECT c.*,a.request_id AS anchor_request_id,a.action AS anchor_action,"
        "a.payload AS anchor_payload,r.result AS request_result,r.digest AS request_digest "
        "FROM dashboard_read_model_commit c JOIN audit a "
        "ON a.id=c.audit_id AND a.request_id=c.request_id "
        "JOIN request r ON r.id=c.request_id WHERE c.id=?",
        (commit_id,),
    ).fetchone()


def _latest_id(connection: Connection, period):
    row = connection.execute(
        "SELECT id FROM dashboard_read_model_commit WHERE period=? ORDER BY audit_id DESC LIMIT 1",
        (period,),
    ).fetchone()
    return bytes(row[0]) if row else None


def _check_request(audit_payload, request_result, request_digest):
    from .provenance import _result

    try:
        result = loads_unique(request_result)
        if request_digest != digest(["dashboard_read_model", result]) or canonical(
            _result(loads_unique(audit_payload))
        ) != canonical(result):
            raise _failure()
    except (TypeError, ValueError) as exc:
        if isinstance(exc, KernelError):
            raise
        raise _failure() from exc


def _load_raw(connection: Connection, expected_digest: str):
    row = connection.execute(
        "SELECT payload FROM dashboard_read_model_block WHERE digest=?",
        (bytes.fromhex(expected_digest),),
    ).fetchone()
    if row is None:
        raise _failure()
    try:
        raw = row[0]
        if hashlib.sha256(raw.encode("utf-8")).hexdigest() != expected_digest:
            raise _failure()
        return raw
    except (TypeError, ValueError, AttributeError) as exc:
        if isinstance(exc, KernelError):
            raise
        raise _failure() from exc


def _load_bytes(connection: Connection, expected_digest: str):
    try:
        return loads_finite_unique(_load_raw(connection, expected_digest))
    except (TypeError, ValueError) as exc:
        raise _failure() from exc


def _load_root(connection: Connection, descriptor: ModelDescriptor) -> ModelRoot:
    try:
        raw = _load_raw(connection, descriptor.root_digest)
        value = loads_finite_unique(raw)
        expected_fields = {"format", "content_version", "scope", "blocks"}
        if descriptor.content_version >= 2:
            expected_fields.add("date_basis")
        if not isinstance(value, dict) or set(value) != expected_fields:
            raise _failure()
        # JSON arrays represent immutable tuple directories. Unique-key decode
        # always happens before the strict JSON validation.
        root = ModelRoot.model_validate_json(raw)
        if root.scope != descriptor.scope or root.content_version != descriptor.content_version:
            raise _failure()
        return root
    except (TypeError, ValueError) as exc:
        raise _failure() from exc


def persist_model(
    connection: Connection,
    prepared: PreparedModel,
    *,
    audit_id: int,
    collect_obsolete=True,
) -> str:
    """Insert a receipt only after its original audit anchors it, without COMMIT.

    A caller-owned write transaction is mandatory. Blocks may be repaired to
    their exact authenticated bytes; immutable receipts are never rewritten.
    """
    if not connection.in_transaction or type(audit_id) is not int or audit_id < 1:
        raise _failure()
    descriptor = _parse_descriptor(prepared.descriptor_json)
    audit = connection.execute(
        "SELECT a.request_id,a.action,a.payload,r.result,r.digest FROM audit a "
        "JOIN request r ON r.id=a.request_id WHERE a.id=?",
        (audit_id,),
    ).fetchone()
    if audit is None or audit[0] != descriptor.request_id or audit[1] != "dashboard_read_model":
        raise _failure()
    _check_request(audit[2], audit[3], audit[4])
    if sum(value == descriptor for value in _audit_descriptors(audit[2])) != 1:
        raise _failure()
    if hashlib.sha256(prepared.root_json.encode("utf-8")).hexdigest() != descriptor.root_digest:
        raise _failure()
    try:
        root_value = loads_unique(prepared.root_json)
        expected_fields = {"format", "content_version", "scope", "blocks"}
        if descriptor.content_version >= 2:
            expected_fields.add("date_basis")
        if not isinstance(root_value, dict) or set(root_value) != expected_fields:
            raise _failure()
        root = ModelRoot.model_validate_json(prepared.root_json)
    except (TypeError, ValueError) as exc:
        raise _failure() from exc
    if root.scope != descriptor.scope or root.content_version != descriptor.content_version:
        raise _failure()
    expected_rows = {bytes.fromhex(reference.digest): reference for reference in root.blocks}
    expected_digests = set(expected_rows) | {bytes.fromhex(descriptor.root_digest)}
    rows = dict(prepared.block_rows)
    if len(rows) != len(prepared.block_rows) or set(rows) != expected_digests:
        raise _failure()
    # Authenticate the complete batch before any content can be written.
    for block_digest, raw in rows.items():
        if hashlib.sha256(raw.encode("utf-8")).digest() != block_digest:
            raise _failure()
        reference = expected_rows.get(block_digest)
        if reference is not None:
            _decode_block(raw, reference, root.scope)
    parent = bytes.fromhex(descriptor.parent_commit_id) if descriptor.parent_commit_id else None
    current = connection.execute(
        "SELECT commit_id FROM dashboard_read_model_current WHERE period=?",
        (descriptor.scope.period,),
    ).fetchone()
    if (bytes(current[0]) if current else None) != parent:
        raise _failure()
    if parent != _latest_id(connection, descriptor.scope.period):
        raise _failure()
    if parent is not None:
        parent_row = _receipt_row(connection, parent)
        if parent_row is None:
            raise _failure()
        parent_scope = _anchored_descriptor(parent_row).scope
        if (parent_scope.company_id, parent_scope.database_id, parent_scope.period) != (
            descriptor.scope.company_id,
            descriptor.scope.database_id,
            descriptor.scope.period,
        ):
            raise _failure()
    # A new publication must never erase damaged current content by replacing
    # its locator. Explicit repair must restore that content first.
    if collect_obsolete:
        authenticate_current_storage(connection)
    connection.execute(
        "INSERT INTO dashboard_read_model_commit"
        "(id,period,request_id,audit_id,descriptor,root_digest,parent_id) VALUES(?,?,?,?,?,?,?)",
        (
            bytes.fromhex(descriptor.commit_id),
            descriptor.scope.period,
            descriptor.request_id,
            audit_id,
            prepared.descriptor_json,
            bytes.fromhex(descriptor.root_digest),
            parent,
        ),
    )
    connection.executemany(
        "INSERT INTO dashboard_read_model_block(digest,payload) VALUES(?,?) "
        "ON CONFLICT(digest) DO UPDATE SET payload=excluded.payload",
        prepared.block_rows,
    )
    connection.execute(
        "INSERT INTO dashboard_read_model_current(period,commit_id) VALUES(?,?) "
        "ON CONFLICT(period) DO UPDATE SET commit_id=excluded.commit_id",
        (descriptor.scope.period, bytes.fromhex(descriptor.commit_id)),
    )
    if collect_obsolete:
        collect_obsolete_blocks(connection)
    return descriptor.commit_id


def _decode_block(raw, reference: BlockReference, scope: ModelScope, *, native_contract=True):
    try:
        value = loads_finite_unique(raw)
    except (TypeError, ValueError) as exc:
        raise _failure() from exc
    return _decode_finite_block_value(value, reference, scope, native_contract=native_contract)


def _decode_block_value(
    value, reference: BlockReference, scope: ModelScope, *, native_contract=True,
):
    """Validate direct Python input, including every nested finite-number check."""
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend(item)
        elif isinstance(item, float) and not math.isfinite(item):
            raise _failure()
    return _decode_finite_block_value(value, reference, scope, native_contract=native_contract)


def _decode_finite_block_value(
    value, reference: BlockReference, scope: ModelScope, *, native_contract=True,
):
    """Consume only the finite decoder's output or the direct-value validator.

    Raw-byte callers already rejected duplicates and all non-finite literals
    during their single decode. No caller-supplied flag grants that boundary.
    """
    try:
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "kind",
                "payload",
                "keys",
                "sources",
                "voucher_version_ids",
                "item_sources",
                "item_voucher_version_ids",
                "fact_sources",
                "item_fact_sources",
            }
            or value["kind"] != reference.kind
        ):
            raise _failure()
        payload = value["payload"]
        if not isinstance(payload, dict):
            raise _failure()
        if native_contract:
            payload = _checked_payload(reference.kind, payload)
            _checked_scope(payload, scope)
        metadata = _checked_metadata(
            {
                key: value[key]
                for key in (
                    "keys",
                    "sources",
                    "voucher_version_ids",
                    "item_sources",
                    "item_voucher_version_ids",
                    "fact_sources",
                    "item_fact_sources",
                )
            }
        )
        if tuple(tuple(key) for key in metadata["keys"]) != reference.keys:
            raise _failure()
        if native_contract:
            _check_collection(payload, reference)
        return PageBlock(
            reference.key,
            reference.kind,
            payload,
            keys=tuple(tuple(key) for key in metadata["keys"]),
            sources=tuple(tuple(source) for source in metadata["sources"]),
            voucher_version_ids=tuple(metadata["voucher_version_ids"]),
            total_count=reference.total_count,
            filtered_count=reference.filtered_count,
            item_sources=tuple(
                tuple(tuple(source) for source in bindings) for bindings in metadata["item_sources"]
            ),
            item_voucher_version_ids=tuple(
                tuple(versions) for versions in metadata["item_voucher_version_ids"]
            ),
            fact_sources=tuple(tuple(fact) for fact in metadata["fact_sources"]),
            item_fact_sources=tuple(
                tuple(tuple(fact) for fact in bindings)
                for bindings in metadata["item_fact_sources"]
            ),
        )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, KernelError):
            raise
        raise _failure() from exc


@dataclass(frozen=True)
class LoadedModel:
    connection: Connection
    descriptor: ModelDescriptor
    root: ModelRoot
    _blocks_by_key: Mapping[str, BlockReference] = field(
        init=False,
        repr=False,
        compare=False,
    )
    _blocks_by_page: Mapping[str, tuple[BlockReference, ...]] = field(
        init=False,
        repr=False,
        compare=False,
    )
    _blocks_by_collection: Mapping[tuple[str, str, str], tuple[BlockReference, ...]] = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self):
        # The complete root was authenticated before this object was created.
        # These immutable locators share its references; they contain no decoded
        # business bodies or source proofs, and live only with this loaded root.
        keys, pages, collections = {}, {}, {}
        for reference in self.root.blocks:
            if reference.key in keys:
                raise _failure()
            keys[reference.key] = reference
            page, variant, section, _ordinal = reference.key.split("/")
            pages.setdefault(page, []).append(reference)
            collections.setdefault((page, variant, section), []).append(reference)
        object.__setattr__(self, "_blocks_by_key", MappingProxyType(keys))
        object.__setattr__(
            self,
            "_blocks_by_page",
            MappingProxyType(
                {key: tuple(values) for key, values in pages.items()},
            ),
        )
        object.__setattr__(
            self,
            "_blocks_by_collection",
            MappingProxyType(
                {key: tuple(values) for key, values in collections.items()},
            ),
        )

    def has_block(self, key: str) -> bool:
        return key in self._blocks_by_key

    def page_references(self, page: str) -> tuple[BlockReference, ...]:
        return self._blocks_by_page.get(page, ())

    def collection_references(
        self,
        page: str,
        variant: str,
        section: str,
    ) -> tuple[BlockReference, ...]:
        return self._blocks_by_collection.get((page, variant, section), ())

    def read_block(self, key: str):
        return self.read_page_block(key).payload

    def read_page_block(self, key: str, *, native_contract=True) -> PageBlock:
        if not self.connection.in_transaction:
            raise _failure()
        reference = self._blocks_by_key.get(key)
        if reference is None:
            raise _failure()
        # Each returned block is hashed on this read; never cache decoded proof.
        value = _load_bytes(self.connection, reference.digest)
        return _decode_finite_block_value(
            value, reference, self.root.scope, native_contract=native_contract
        )


def supports_native_content(descriptor: ModelDescriptor) -> bool:
    """Only this producer build may be interpreted by today's native adapter.

    Obsolete UI interfaces are not accounting history. Their current bytes
    still require hashes and the unchanged model envelope, not reinterpretation
    with a newer native response contract.
    """
    from .engine import PROGRAM_VERSION

    if descriptor.scope.rule_build != PROGRAM_VERSION:
        return False
    if descriptor.content_version == 1:
        return False  # Retained v1 receipts authenticate their original envelope.
    if descriptor.content_version != CONTENT_VERSION:
        # The identical producer build cannot legitimately emit another
        # content contract. Do not disguise its invalid output as staleness.
        raise _failure()
    return True


def authenticate_current_storage(connection: Connection) -> set[str]:
    """Authenticate every latest locator and all required current content.

    Retired receipts keep their audit and parent identities. Only current roots
    and their pages own required bytes, including authentic stale scopes.
    """
    if not connection.in_transaction:
        raise _failure()
    current = {
        row[0]: bytes(row[1])
        for row in connection.execute("SELECT period,commit_id FROM dashboard_read_model_current")
    }
    latest = {
        row[0]: bytes(row[1])
        for row in connection.execute(
            "SELECT c.period,c.id FROM dashboard_read_model_commit c WHERE c.audit_id=("
            "SELECT max(q.audit_id) FROM dashboard_read_model_commit q WHERE q.period=c.period)"
        )
    }
    if current != latest:
        raise _failure()
    live = set()
    verified_batches = {}
    for period, ident in current.items():
        row = _receipt_row(connection, ident)
        if row is None:
            raise _failure()
        descriptor = _anchored_descriptor(row, _verified_batches=verified_batches)
        if descriptor.scope.period != period:
            raise _failure()
        root = _load_root(connection, descriptor)
        model = LoadedModel(connection, descriptor, root)
        live.add(descriptor.root_digest)
        native = supports_native_content(descriptor)
        for reference in root.blocks:
            model.read_page_block(reference.key, native_contract=native)
            live.add(reference.digest)
    return live


def collect_obsolete_blocks(connection: Connection) -> int:
    """Reclaim unreferenced UI bytes after authenticating every current root.

    Call only inside explicit production/maintenance. Shared page digests stay
    live; receipt, audit, request and business history are never deleted.
    """
    live = authenticate_current_storage(connection)
    changed = connection.execute(
        "DELETE FROM dashboard_read_model_block WHERE lower(hex(digest)) NOT IN "
        "(SELECT value FROM json_each(?))",
        (canonical(sorted(live)),),
    ).rowcount
    return changed


def load_current_model(connection: Connection, expected_scope: ModelScope) -> LoadedModel | None:
    """Authenticate a company's current root, including an authentic stale scope.

    This does not certify that stale bytes are suitable for a current response.
    Production may inspect the original scope; ordinary consumers use load_model.
    """
    if not connection.in_transaction:
        raise _failure()
    locator = connection.execute(
        "SELECT commit_id FROM dashboard_read_model_current WHERE period=?",
        (expected_scope.period,),
    ).fetchone()
    if locator is None:
        # A deleted derived locator is corruption when a retained receipt exists.
        if connection.execute(
            "SELECT 1 FROM dashboard_read_model_commit WHERE period=? LIMIT 1",
            (expected_scope.period,),
        ).fetchone():
            raise _failure()
        return None
    row = _receipt_row(connection, locator[0])
    if row is None:
        raise _failure()
    if _latest_id(connection, expected_scope.period) != bytes(locator[0]):
        raise _failure()
    descriptor = _anchored_descriptor(row)
    root = _load_root(connection, descriptor)
    supports_native_content(descriptor)
    scope = descriptor.scope
    if (scope.company_id, scope.database_id, scope.period) != (
        expected_scope.company_id,
        expected_scope.database_id,
        expected_scope.period,
    ):
        raise _failure()
    return LoadedModel(connection, descriptor, root)


def load_model(connection: Connection, expected_scope: ModelScope) -> LoadedModel | None:
    """Authenticate receipt/root before deciding an authentic version is stale."""
    model = load_current_model(connection, expected_scope)
    if model is None or model.descriptor.content_version != CONTENT_VERSION:
        return None
    scope = model.descriptor.scope
    if scope != expected_scope:
        # Original payload dates stay bound to their receipt. Only a current
        # root with the complete typed personnel rule may adapt a different day;
        # every other input must match exactly in this owned read transaction.
        if model.root.date_basis is None or scope != expected_scope.model_copy(
            update={"as_of": scope.as_of},
        ):
            return None
    return model


def prepare_evidence_registration_model(
    model: LoadedModel,
    scope: ModelScope,
    *,
    request_id: str,
) -> PreparedModel | None:
    """Re-anchor complete UI bytes only for the caller's evidence INSERT.

    Registering immutable evidence cannot alter facts that already reference
    registered evidence. The material lane must advance exactly once while all
    other business, repair, producer, identity and date inputs stay identical.
    A producer calls this only for that typed action, never arbitrary writes.
    Every reused block is authenticated and decoded within this transaction;
    the original receipt and scope are never rewritten or served as current.
    """
    if not supports_native_content(model.descriptor) or scope.epochs.material < 1:
        return None
    previous = scope.model_copy(
        update={
            "epochs": scope.epochs.model_copy(update={"material": scope.epochs.material - 1}),
        }
    )
    if model.descriptor.scope != previous:
        return None
    blocks = [model.read_page_block(reference.key) for reference in model.root.blocks]
    return prepare_model(
        scope,
        blocks,
        request_id=request_id,
        parent_commit_id=model.descriptor.commit_id,
        date_basis=model.root.date_basis,
    )
