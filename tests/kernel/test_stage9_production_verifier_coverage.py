"""The production factory must not omit a released company content reader."""

from types import SimpleNamespace

import pytest

from ai_accounting.kernel import content_v1, schema_bundle


def _synthetic_bundle(versions, verifiers, *, status="released"):
    return SimpleNamespace(
        status=status,
        contracts={"company": {version: {"status": "released"} for version in versions}},
        company_verifiers=verifiers,
    )


def test_production_factory_rejects_unregistered_intermediate_release(monkeypatch):
    monkeypatch.setattr(schema_bundle, "STATUS", "released")
    monkeypatch.setattr(schema_bundle, "VERSION", 3)
    monkeypatch.setattr(content_v1, "v1_registry", lambda: None)
    monkeypatch.setattr(
        schema_bundle,
        "load_bundle",
        lambda *args, **kwargs: _synthetic_bundle((1, 2, 3), kwargs["company_verifiers"]),
    )
    schema_bundle.production_bundle.cache_clear()
    try:
        with pytest.raises(ValueError, match=r"verifiers are missing: \[2\]"):
            schema_bundle.production_bundle()
    finally:
        schema_bundle.production_bundle.cache_clear()


def test_production_factory_accepts_declared_release_history(monkeypatch):
    monkeypatch.setattr(schema_bundle, "STATUS", "released")
    monkeypatch.setattr(schema_bundle, "VERSION", 2)
    monkeypatch.setattr(content_v1, "v1_registry", lambda: None)
    monkeypatch.setattr(
        schema_bundle,
        "load_bundle",
        lambda *args, **kwargs: _synthetic_bundle((1, 2), kwargs["company_verifiers"]),
    )
    schema_bundle.production_bundle.cache_clear()
    try:
        bundle = schema_bundle.production_bundle()
        assert set(bundle.company_verifiers) == {1, 2}
    finally:
        schema_bundle.production_bundle.cache_clear()


def test_draft_factory_does_not_require_released_verifiers():
    bundle = schema_bundle.production_bundle()
    assert bundle.status == "draft"
    assert (
        schema_bundle._require_released_company_verifiers(
            _synthetic_bundle((1, 2), {}, status="draft")
        ).status
        == "draft"
    )
