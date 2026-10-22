"""One full/light financial identity and an opaque, atomic owner preparation plan."""

from collections.abc import Sequence
from copy import deepcopy
from hashlib import sha256

from ai_accounting_modular.contracts.public import ContractError, canonical_json, validate_contract

from ._adoption_changes import FormalChange
from ._financial_calculation import calculate_snapshot, summary_values
from ._financial_cells import Issues
from ._financial_projection_model import Model, verified_request
from ._financial_projection_requests import request
from ._financial_projection_selection import ProjectionCalculation, ProjectionSelection
from ._financial_projection_types import FinancialProjectionArtifact, FinancialSummaryValues
from ._types import FinancialSummary
from ._values import fail


def prepare(requests, changes, closure_ranges, read, *, base=None, rebuild=False):
    if (
        type(rebuild) is not bool
        or isinstance(requests, str | bytes)
        or not isinstance(requests, Sequence)
        or isinstance(changes, str | bytes)
        or not isinstance(changes, Sequence)
        or any(not isinstance(value, FormalChange) for value in changes)
    ):
        fail("invalid_input", "财务准备须使用明确请求、正式变更和重建选项。")
    if rebuild and base is not None:
        fail("invalid_input", "完整财务重建不接受旧派生基底。")
    if isinstance(closure_ranges, str | bytes) or not isinstance(closure_ranges, Sequence):
        fail("invalid_input", "财务关账通知须提供明确月份范围。")
    for value in closure_ranges:
        if type(value) is not tuple or len(value) != 2:
            fail("invalid_input", "财务关账通知须提供明确起止月。")
        for period in value:
            validate_contract("core.schema.json", "Month", period)
        if value[0] > value[1]:
            fail("invalid_input", "财务关账通知的起止月份颠倒。")
    model = Model(read, base)
    old_roots = model.advance(requests, changes)
    return model.plan(old_roots)


def _calculate(model, selected, descriptor):
    scope, read = selected.scope, model.read
    selection = ProjectionSelection(model, scope, read, Issues())
    selection.include_cash = selected.kind == "snapshot"
    calculation = ProjectionCalculation(selection)
    version = descriptor["version"]
    saved = model.saved(selected)
    if selected.kind == "summary_values":
        values = summary_values(scope, selection, saved=saved, calculation=calculation)
        return FinancialSummaryValues(
            version, deepcopy(scope), values, tuple(selection.issues.values)
        ), selection
    if saved is not None:
        return deepcopy(saved), selection
    result = calculate_snapshot(scope, read, selection, calculation=calculation)
    last = scope["period_to"]
    year = last[:4] + "-01"
    result["selection"] = {
        "selection_digest": version,
        "closed_periods": read.repository("ledger").closed_periods(
            year, last, start_period=read.start_period
        ),
        "supplement_periods": model.supplements(selection, year, last),
    }
    for name in ("opening", "continuation"):
        head = getattr(selection, name)
        if head:
            result["selection"][name + "_business_id"] = head["business_id"]
    validate_contract("reporting.schema.json", "FinancialSnapshot", result)
    return result, selection


def read_projection(value, read, base):
    selected = verified_request(value, read)
    if base is None or base.header_json is None:
        return None
    try:
        model = Model(read, base)
        if selected.key not in model.requests:
            return None
        raw = base.get_blocks((selected.descriptor_key,))[0]
        descriptor = model.descriptor(selected)
        if raw is None or raw != canonical_json(descriptor):
            fail("storage_corrupt", "财务请求的实际根描述符缺失或已不一致。")
        value, _ = _calculate(model, selected, descriptor)
        return FinancialProjectionArtifact(
            selected.key,
            selected.descriptor_key,
            sha256(raw.encode("utf-8")).hexdigest(),
            descriptor["version"],
            value,
        )
    except ContractError:
        raise
    except (KeyError, IndexError, ValueError, TypeError) as error:
        raise ContractError("storage_corrupt", "财务派生模型没有完整的正式范围或字段。") from error


def _public(kind, scope, read):
    selected = request(kind, scope, read)
    model = Model.for_request(read, selected)
    descriptor = model.descriptor(selected)
    value, selection = _calculate(model, selected, descriptor)
    version = descriptor["version"]
    if scope.get("expected_head", version) != version:
        fail("snapshot_changed", "所选财务范围采用的正式结果已变化，请重新读取。")
    if kind == "summary_values":
        return FinancialSummary(
            version,
            selected.scope,
            **value.values,
            issues=value.issues,
            sources=selection.all_sources(),
        )
    return value


def public_summary(scope, read):
    return _public("summary_values", scope, read)


def public_snapshot(scope, read):
    return _public("snapshot", scope, read)
