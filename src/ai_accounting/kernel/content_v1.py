"""Released v1 source interpretation contract.

The saved descriptor reconstructs old storage decoders independently of the
current fact models. Historical identity correction uses retained v1 read-only
scope and dependency semantics.
"""

from __future__ import annotations

import ast
import inspect
import json
import re
import textwrap
import types
from datetime import date
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Annotated, ClassVar, Union, get_args, get_origin

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, create_model, model_validator
from pydantic_core import core_schema

from .contracts import KernelError, Registry
from .history_encoding_v1 import digest
from .schema import base_type, sequence_model
from .schema_bundle import verify_company_with_registry
from .storage import composite
from .types import ActualDate, YearMonth


class _V1StoredFact(BaseModel):
    """The historical verifier's fixed data carrier, never a current Fact subclass."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: ClassVar[str]
    actual_payment: ClassVar[bool] = False

    def scopes(self):
        from .content_v1_semantics import scopes

        return scopes(self)

    def reads(self):
        from .content_v1_semantics import reads

        return reads(self)

    def scopes_for(self, _subject_id):
        return self.scopes()

    def reads_for(self, _subject_id):
        return self.reads()


class _V1YearMonth(str):
    """The persisted v1 month format and ordinal, independent of current input types."""

    def __new__(cls, value):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", value):
            raise ValueError("v1 month must be YYYY-MM")
        if not 1 <= int(value[:4]) <= 9999:
            raise ValueError("v1 year out of range")
        return str.__new__(cls, value)

    @property
    def ordinal(self):
        return (int(self[:4]) - 1) * 12 + int(self[5:]) - 1

    @classmethod
    def from_ordinal(cls, ordinal):
        if type(ordinal) is not int or not 0 <= ordinal < 9999 * 12:
            raise ValueError("invalid v1 month ordinal")
        year, month = divmod(ordinal, 12)
        return str.__new__(cls, f"{year + 1:04d}-{month + 1:02d}")

    @classmethod
    def __get_pydantic_core_schema__(cls, _source, _handler):
        return core_schema.no_info_after_validator_function(
            cls, core_schema.str_schema(strict=True, pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")
        )


class _V1ActualDate(str):
    """The persisted v1 actual-day format, independent of current input types."""

    def __new__(cls, value):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError("v1 actual date must be YYYY-MM-DD")
        date.fromisoformat(value)
        return str.__new__(cls, value)

    @property
    def period(self):
        return _V1YearMonth(self[:7])

    @classmethod
    def __get_pydantic_core_schema__(cls, _source, _handler):
        return core_schema.no_info_after_validator_function(
            cls, core_schema.str_schema(strict=True, pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
        )


def _source_digest(model):
    source = textwrap.dedent(inspect.getsource(model))
    tree = ast.parse(source)
    return digest(ast.dump(tree, include_attributes=False)).hex()


def _field_descriptor(model):
    result = {}
    for name, field in model.model_fields.items():
        child = sequence_model(field.annotation)
        if child is not None:
            result[name] = {
                "kind": "sequence",
                "required": field.is_required(),
                "nullable": type(None) in get_args(field.annotation),
                "fields": _field_descriptor(child),
            }
        else:
            base = base_type(field.annotation)
            category = (
                "year_month"
                if base is YearMonth
                else "bool"
                if base is bool
                else "json_list"
                if composite(field.annotation) and get_origin(base) is list
                else "json_tuple"
                if composite(field.annotation) and get_origin(base) is tuple
                else "json_dict"
                if composite(field.annotation)
                else "int"
                if base is int
                else "decimal"
                if base is Decimal
                else "actual_date"
                if base is ActualDate
                else "str"
            )
            result[name] = {
                "kind": category,
                "required": field.is_required(),
                "nullable": type(None) in get_args(field.annotation),
            }
    return result


def _decode_model(kind, fields, *, root=True, validators=None):
    definitions = {}
    for name, spec in fields.items():
        category = spec["kind"]
        if category == "sequence":
            child = _decode_model(kind + "_" + name, spec["fields"], root=False)
            annotation = list[child]
        else:
            annotation = {
                "year_month": _V1YearMonth,
                "bool": bool,
                "json_list": list[object],
                "json_tuple": tuple[object, ...],
                "json_dict": dict[str, object],
                "int": int,
                "decimal": Decimal,
                "actual_date": _V1ActualDate,
                "str": str,
            }[category]
        if spec["nullable"]:
            annotation |= None
        definitions[name] = (annotation, ...) if spec["required"] else (annotation, None)
    model = create_model(
        "V1Stored_" + kind,
        __base__=_V1StoredFact if root else BaseModel,
        __validators__=validators or {},
        **definitions,
    )
    if root:
        model.kind = kind
    model._v1_fields = fields
    return model


def decode_v1_fields(model, data):
    """Decode persisted v1 columns using the released field categories."""
    for name, spec in model._v1_fields.items():
        if spec["kind"] == "sequence" or data[name] is None:
            continue
        if spec["kind"] == "year_month":
            data[name] = str(_V1YearMonth.from_ordinal(data[name]))
        elif spec["kind"] == "bool":
            data[name] = bool(data[name])
        elif spec["kind"] in {"json_list", "json_tuple", "json_dict"}:
            from .stored_json_v1 import DuplicateStoredKey, loads_unique

            try:
                data[name] = loads_unique(data[name])
            except DuplicateStoredKey as exc:
                raise KernelError(
                    "content_integrity_failed", "已保存的 v1 事实 JSON 有重复字段"
                ) from exc
    return data


def _v1_sequence_item(annotation):
    while get_origin(annotation) in (Annotated, types.UnionType, Union):
        args = get_args(annotation)
        annotation = (
            args[0]
            if get_origin(annotation) is Annotated
            else next(item for item in args if item is not type(None))
        )
    if get_origin(annotation) not in (list, tuple):
        raise ValueError("released v1 sequence field has no item model")
    item = get_args(annotation)[0]
    while get_origin(item) is Annotated:
        item = get_args(item)[0]
    return item


def load_v1_fact_data_many(connection, registry, fact_ids):
    """Read released v1 columns and child rows without current storage codecs."""
    identifiers = sorted(set(fact_ids))
    if not identifiers:
        return {}
    rows = list(
        connection.execute(
            "SELECT f.id,s.kind FROM json_each(?) ids JOIN fact_revision f "
            "ON f.id=ids.value JOIN subject s ON s.id=f.subject_id",
            (json.dumps(identifiers),),
        )
    )
    if len(rows) != len(identifiers):
        raise KernelError("unknown_fact", "fact revision does not exist")
    by_kind = {}
    for row in rows:
        by_kind.setdefault(row["kind"], []).append(row["id"])
    result = {}
    for kind, revision_ids in by_kind.items():
        if kind not in registry.models or not re.fullmatch(r"[a-z][a-z0-9_]*", kind):
            raise KernelError("content_contract_mismatch", "正式 v1 事实类型不在合同中")
        model = registry.models[kind]
        values = json.dumps(revision_ids)
        for row in connection.execute(
            f"SELECT f.* FROM json_each(?) ids JOIN fact_{kind} f ON f.revision_id=ids.value",
            (values,),
        ):
            columns = dict(row)
            ident = columns.pop("revision_id")
            result[ident] = decode_v1_fields(model, columns)
        for name, spec in model._v1_fields.items():
            if spec["kind"] != "sequence":
                continue
            item = _v1_sequence_item(model.model_fields[name].annotation)
            for ident in revision_ids:
                result[ident][name] = []
            for row in connection.execute(
                f"SELECT f.* FROM json_each(?) ids JOIN fact_{kind}_{name} f "
                "ON f.revision_id=ids.value ORDER BY f.revision_id,f.item_no",
                (values,),
            ):
                result[row["revision_id"]][name].append(
                    decode_v1_fields(item, {field: row[field] for field in item.model_fields})
                )
    if len(result) != len(identifiers):
        raise KernelError("unknown_fact", "typed fact data does not exist")
    return result


def load_v1_fact_versions(connection, registry, fact_ids):
    from .storage import FactVersion

    identifiers = sorted(set(fact_ids))
    if not identifiers:
        return {}
    rows = list(
        connection.execute(
            "SELECT f.*,s.kind FROM json_each(?) ids JOIN fact_revision f "
            "ON f.id=ids.value JOIN subject s ON s.id=f.subject_id",
            (json.dumps(identifiers),),
        )
    )
    if len(rows) != len(identifiers):
        raise KernelError("unknown_fact", "fact revision does not exist")
    evidence = {ident: [] for ident in identifiers}
    for row in connection.execute(
        "SELECT e.fact_id,e.evidence_digest FROM json_each(?) ids "
        "JOIN fact_evidence e ON e.fact_id=ids.value ORDER BY e.fact_id,e.evidence_digest",
        (json.dumps(identifiers),),
    ):
        evidence[row["fact_id"]].append(row["evidence_digest"].hex())
    data = load_v1_fact_data_many(connection, registry, identifiers)
    return {
        row["id"]: FactVersion(
            row["id"],
            row["subject_id"],
            row["revision"],
            registry.models[row["kind"]].model_validate_json(
                json.dumps(data[row["id"]], ensure_ascii=False, allow_nan=False)
            ),
            tuple(evidence[row["id"]]),
        )
        for row in rows
    }


def _v1_model(kind, spec):
    schema_validator = Draft202012Validator(spec["schema"])

    @model_validator(mode="before")
    @classmethod
    def validate_released_schema(cls, values):
        error = next(schema_validator.iter_errors(values), None)
        if error is not None:
            raise ValueError(
                f"v1 stored {kind} does not satisfy its released schema: {error.message}"
            )
        return values

    model = _decode_model(
        kind,
        spec["fields"],
        validators={"validate_released_schema": validate_released_schema},
    )
    for name, value in spec["class_vars"].items():
        setattr(model, name, tuple(value) if name == "identity_fields" else value)
    return model


def _storage_semantics_digest():
    return digest(
        {
            item.__name__: _source_digest(item)
            for item in (
                _V1StoredFact,
                _V1YearMonth,
                _V1ActualDate,
                _decode_model,
                decode_v1_fields,
                _v1_sequence_item,
                load_v1_fact_data_many,
                load_v1_fact_versions,
                _v1_model,
            )
        }
    ).hex()


def registry_descriptor(registry):
    """Record storage shapes, validation models and object reference declarations."""
    from . import (
        asset_card_adoption_v1,
        asset_membership_v1,
        change_journal_v1,
        close_contract_v1,
        close_review_integrity_v1,
        close_review_v1,
        close_storage_v1,
        content_v1_semantics,
        duplicate_checks_v1,
        duplicate_freeze_v1,
        duplicate_rules_v1,
        history_encoding_v1,
        history_reads_v1,
        history_types_v1,
        key_membership_filter_v1,
        material_inspection_v1,
        material_watch_v1,
        opening_adoption_v1,
        period_balance_freeze_v1,
        position_v1,
        publication_v1,
        query_relations_v1,
        report_classification_directory_v1,
        report_classification_v1,
        report_flow_rules_v1,
        report_flow_v1,
        report_open_contribution_v1,
        report_party_v1,
        report_projection_v1,
        report_semantics_v1,
        settlement_freeze_v1,
        settlement_projection_v1,
        stored_json_v1,
    )
    from .entity_references import DECLARATIONS

    return {
        "models": {
            kind: {
                "class": f"{model.__module__}:{model.__qualname__}",
                "schema": model.model_json_schema(),
                "fields": _field_descriptor(model),
                "class_vars": {
                    "identity_fields": list(model.identity_fields),
                    "immutable": model.immutable,
                    "lane": model.lane,
                    "material_category": model.material_category,
                    "actual_payment": getattr(model, "actual_payment", False),
                },
                "implementation_sha256": _source_digest(model),
            }
            for kind, model in sorted(registry.models.items())
        },
        "references": {kind: values for kind, values in sorted(DECLARATIONS.items())},
        "history_semantics": "integrity-v1",
        "history_semantics_sha256": _source_digest(content_v1_semantics),
        "storage_semantics_sha256": _storage_semantics_digest(),
        "close_v1_rules_sha256": {
            "asset_card_adoption": _source_digest(asset_card_adoption_v1),
            "asset_membership": _source_digest(asset_membership_v1),
            "contract": _source_digest(close_contract_v1),
            "owner_review_shape": _source_digest(close_review_v1),
            "owner_review_integrity": _source_digest(close_review_integrity_v1),
            "stored_source_reader": _source_digest(history_reads_v1),
            "source_encoding": _source_digest(history_encoding_v1),
            "stored_json": _source_digest(stored_json_v1),
            "source_identity_types": _source_digest(history_types_v1),
            "private_reader": _source_digest(close_storage_v1),
            "source_change": _source_digest(change_journal_v1),
            "duplicate_checks": _source_digest(duplicate_checks_v1),
            "duplicate_directory": _source_digest(duplicate_freeze_v1),
            "key_membership_filter": _source_digest(key_membership_filter_v1),
            "duplicate_rules": _source_digest(duplicate_rules_v1),
            "material_inspection": _source_digest(material_inspection_v1),
            "material_watch": _source_digest(material_watch_v1),
            "opening_adoption": _source_digest(opening_adoption_v1),
            "period_balance": _source_digest(period_balance_freeze_v1),
            "publication": _source_digest(publication_v1),
            "report_classification": _source_digest(report_classification_v1),
            "report_classification_directory": _source_digest(report_classification_directory_v1),
            "report_projection": _source_digest(report_projection_v1),
            "report_flow": _source_digest(report_flow_v1),
            "report_open_contribution": _source_digest(report_open_contribution_v1),
            "report_flow_rules": _source_digest(report_flow_rules_v1),
            "report_semantics": _source_digest(report_semantics_v1),
            "report_party": _source_digest(report_party_v1),
            "position": _source_digest(position_v1),
            "relations": _source_digest(query_relations_v1),
            "settlement_freeze": _source_digest(settlement_freeze_v1),
            "settlement_projection": _source_digest(settlement_projection_v1),
        },
    }


def content_contract(registry):
    descriptor = registry_descriptor(registry)
    return {
        "status": "released",
        "version": 1,
        "descriptor": descriptor,
        "sha256": digest(descriptor).hex(),
    }


@lru_cache(maxsize=1)
def v1_registry():
    path = Path(__file__).with_name("schema_contracts") / "content-v1.json"
    try:
        contract = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise KernelError("content_contract_missing", "正式 v1 内容核验合同缺失") from exc
    if (
        not isinstance(contract, dict)
        or contract.get("status") != "released"
        or contract.get("version") != 1
        or not isinstance(contract.get("descriptor"), dict)
        or contract.get("sha256") != digest(contract["descriptor"]).hex()
    ):
        raise KernelError("content_contract_mismatch", "正式 v1 内容核验合同已损坏")
    saved = contract["descriptor"]
    from . import (
        asset_card_adoption_v1,
        asset_membership_v1,
        change_journal_v1,
        close_contract_v1,
        close_review_integrity_v1,
        close_review_v1,
        close_storage_v1,
        content_v1_semantics,
        duplicate_checks_v1,
        duplicate_freeze_v1,
        duplicate_rules_v1,
        history_encoding_v1,
        history_reads_v1,
        history_types_v1,
        key_membership_filter_v1,
        material_inspection_v1,
        material_watch_v1,
        opening_adoption_v1,
        period_balance_freeze_v1,
        position_v1,
        publication_v1,
        query_relations_v1,
        report_classification_directory_v1,
        report_classification_v1,
        report_flow_rules_v1,
        report_flow_v1,
        report_open_contribution_v1,
        report_party_v1,
        report_projection_v1,
        report_semantics_v1,
        settlement_freeze_v1,
        settlement_projection_v1,
        stored_json_v1,
    )

    if saved.get("history_semantics_sha256") != _source_digest(content_v1_semantics):
        raise KernelError("content_contract_mismatch", "正式 v1 历史核验规则与合同不一致")
    if saved.get("storage_semantics_sha256") != _storage_semantics_digest():
        raise KernelError("content_contract_mismatch", "正式 v1 存储解码规则与合同不一致")
    if saved.get("close_v1_rules_sha256") != {
        "asset_card_adoption": _source_digest(asset_card_adoption_v1),
        "asset_membership": _source_digest(asset_membership_v1),
        "contract": _source_digest(close_contract_v1),
        "owner_review_shape": _source_digest(close_review_v1),
        "owner_review_integrity": _source_digest(close_review_integrity_v1),
        "stored_source_reader": _source_digest(history_reads_v1),
        "source_encoding": _source_digest(history_encoding_v1),
        "stored_json": _source_digest(stored_json_v1),
        "source_identity_types": _source_digest(history_types_v1),
        "private_reader": _source_digest(close_storage_v1),
        "source_change": _source_digest(change_journal_v1),
        "duplicate_checks": _source_digest(duplicate_checks_v1),
        "duplicate_directory": _source_digest(duplicate_freeze_v1),
        "key_membership_filter": _source_digest(key_membership_filter_v1),
        "duplicate_rules": _source_digest(duplicate_rules_v1),
        "material_inspection": _source_digest(material_inspection_v1),
        "material_watch": _source_digest(material_watch_v1),
        "opening_adoption": _source_digest(opening_adoption_v1),
        "period_balance": _source_digest(period_balance_freeze_v1),
        "publication": _source_digest(publication_v1),
        "report_classification": _source_digest(report_classification_v1),
        "report_classification_directory": _source_digest(report_classification_directory_v1),
        "report_projection": _source_digest(report_projection_v1),
        "report_flow": _source_digest(report_flow_v1),
        "report_open_contribution": _source_digest(report_open_contribution_v1),
        "report_flow_rules": _source_digest(report_flow_rules_v1),
        "report_semantics": _source_digest(report_semantics_v1),
        "report_party": _source_digest(report_party_v1),
        "position": _source_digest(position_v1),
        "relations": _source_digest(query_relations_v1),
        "settlement_freeze": _source_digest(settlement_freeze_v1),
        "settlement_projection": _source_digest(settlement_projection_v1),
    }:
        raise KernelError("content_contract_mismatch", "正式 v1 关账读取规则与合同不一致")
    registry = Registry()
    try:
        registry.models = {kind: _v1_model(kind, spec) for kind, spec in saved["models"].items()}
        registry.reference_declarations = saved["references"]
        registry.content_version = 1
    except (KeyError, TypeError, ValueError) as exc:
        raise KernelError("content_contract_mismatch", "正式 v1 解码合同无效") from exc
    return registry


def verify_v1_company(connection, bundle):
    from .content_history_context import historical_content

    with historical_content(1):
        return verify_company_with_registry(connection, bundle, v1_registry())
