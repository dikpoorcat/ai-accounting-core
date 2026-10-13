"""Resolution response schemas preserve the native target boundaries."""

from types import SimpleNamespace
from typing import get_args

import pytest
from pydantic import TypeAdapter, ValidationError

from ai_accounting.kernel.resolution import ResolutionCommand, validate_resolution
from ai_accounting.kernel.response_contracts import IssueResolution


def test_response_commands_share_the_native_resolution_allowlist():
    adapter = TypeAdapter(IssueResolution)
    allowed = get_args(ResolutionCommand)
    schema = adapter.json_schema()
    command_schema = schema["$defs"]["IssueCommandResolution"]["properties"]["command"]
    assert command_schema["enum"] == list(allowed)
    for command in allowed:
        target = {"command": command}
        assert adapter.validate_python(target, strict=True) == validate_resolution(target)
    with pytest.raises(ValueError, match="unknown resolution command"):
        validate_resolution({"command": "unknown"})
    with pytest.raises(ValidationError):
        adapter.validate_python({"command": "unknown"}, strict=True)


def test_response_candidates_export_uniqueness_without_relaxing_native_validation():
    adapter = TypeAdapter(IssueResolution)
    schema = adapter.json_schema()["$defs"]["IssueCandidateResolution"]["properties"]["candidates"]
    assert schema["uniqueItems"] is True
    assert schema["minItems"] == 1
    assert schema["maxItems"] == 100
    assert schema["items"]["maxLength"] == 200
    target = {"candidates": ["first", "second", "😀" * 200]}
    assert adapter.validate_python(target, strict=True) == validate_resolution(target)
    for candidates in (["same", "same"], ["😀" * 201]):
        with pytest.raises(ValueError):
            validate_resolution({"candidates": candidates})
        with pytest.raises(ValidationError):
            adapter.validate_python({"candidates": candidates}, strict=True)


def test_native_fact_resolution_still_requires_registry_membership():
    registry = SimpleNamespace(models={"known": object()})
    assert validate_resolution({"fact_kind": "known"}, registry=registry) == {"fact_kind": "known"}
    for kind in ("known", "unknown"):
        with pytest.raises(ValueError, match="must be registered"):
            validate_resolution({"fact_kind": kind})
    with pytest.raises(ValueError, match="must be registered"):
        validate_resolution({"fact_kind": "unknown"}, registry=registry)
