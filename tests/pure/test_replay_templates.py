"""Offline replay checks must reject stale references without opening a database."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def verifier(monkeypatch):
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        "replay_template_verifier", scripts / "verify_replay_templates.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def template(tmp_path):
    content = b"synthetic account source\n"
    (tmp_path / "source.txt").write_bytes(content)
    sha = hashlib.sha256(content).hexdigest()
    document = {
        "sources": [{"key": "source", "path": "source.txt", "sha256": sha, "bytes": len(content)}],
        "entities": [
            {
                "key": "account",
                "kind": "fund_account",
                "account_type": "bank",
                "data": {
                    "display_name": "managed account",
                    "bank_name": "Test bank",
                    "bank_account_number": "00001234",
                    "account_holder_name": "Test company",
                },
                "sources": ["source"],
            }
        ],
        "facts": [
            {
                "key": "opening",
                "kind": "bank_opening",
                "data": {
                    "period": "2026-01",
                    "bank_account_id": "$ref:account:entity_id",
                    "opening_fen": 0,
                    "basis": "new_account",
                },
                "source_keys": ["source"],
                "depends_on": ["account"],
                "references": [
                    {"path": "/bank_account_id", "target_key": "account", "binding": "entity_id"}
                ],
            }
        ],
        "materials": [],
        "steps": [],
        "unresolved": [],
    }
    path = tmp_path / "synthetic.final-facts.json"
    return document, path, tmp_path


def run(verifier, template):
    document, path, root = template
    path.write_text(json.dumps(document), encoding="utf-8")
    return verifier.check_document(path, root)


def test_uses_synthetic_target_bindings_and_verifies_original_bytes(verifier, template):
    before = json.dumps(template[0], sort_keys=True)
    bound = verifier.bind(template[0])
    assert bound["facts"][0]["data"]["bank_account_id"] != "$ref:account:entity_id"
    bound["facts"][0]["data"]["bank_account_id"] = "consumer changed its bound copy"
    bound["facts"].append({})
    assert json.dumps(template[0], sort_keys=True) == before
    result = run(verifier, template)
    assert result["references"] == 1
    assert result["source_verification"]["sources"] == 1
    assert result["business_writes"] == 0
    assert json.dumps(template[0], sort_keys=True) == before
    assert json.loads(template[1].read_text(encoding="utf-8")) == template[0]


@pytest.mark.parametrize(
    "mutation,expected",
    [
        (lambda data: data["facts"][0]["references"].clear(), "binding paths differ"),
        (lambda data: data["facts"][0]["depends_on"].clear(), "missing from dependencies"),
        (
            lambda data: data["entities"][0]["data"].update(display_number="00001234"),
            "extra_forbidden",
        ),
        (lambda data: data["facts"][0]["source_keys"].append("missing"), "unknown source key"),
        (lambda data: data["entities"][0].update(depends_on=["opening"]), "cyclic dependency"),
    ],
)
def test_rejects_invalid_schema_bindings_and_cycles(verifier, template, mutation, expected):
    mutation(template[0])
    with pytest.raises(verifier.TemplateError, match=expected):
        run(verifier, template)


def test_source_hash_mismatch_cannot_pass_with_valid_template_schema(verifier, template):
    (template[2] / "source.txt").write_bytes(b"changed source\n")
    with pytest.raises(ValueError, match="byte count mismatch|SHA-256 mismatch"):
        run(verifier, template)


def test_marker_requires_both_logical_key_and_binding(verifier):
    with pytest.raises(verifier.TemplateError, match="invalid runtime binding"):
        verifier.marker("$ref:unbound")


@pytest.mark.parametrize(
    "kind,account_type,data,expected",
    [
        ("person", None, {"display_name": "Test person"}, "entity kind mismatch"),
        ("fund_account", "cash", {"display_name": "Test cash"}, "entity account type mismatch"),
    ],
)
def test_entity_role_contract_rejects_known_key_of_wrong_category(
    verifier, template, kind, account_type, data, expected
):
    entity = template[0]["entities"][0]
    entity.update(kind=kind, account_type=account_type, data=data)
    with pytest.raises(verifier.TemplateError, match=expected):
        run(verifier, template)


def publication_consumer(document, target="opening"):
    document["steps"] = [
        {"key": "publish", "commands": ["preview", "confirm"], "inputs": ["opening"]}
    ]
    document["publication_bindings"] = [
        {
            "key": "publication:opening",
            "subject_key": "opening",
            "producer_step_key": "publish",
            "depends_on": ["opening"],
        }
    ]
    document["facts"].append(
        {
            "key": "consumer",
            "kind": "report_income_tax_confirmation",
            "data": {
                "period": "2026-03",
                "treatment": "assessed",
                "cumulative_assessed_fen": 0,
                "calculation_id": "$ref:" + target + ":calculation_id",
                "explanation": "Synthetic pointer check",
            },
            "source_keys": ["source"],
            "depends_on": [target],
            "references": [
                {"path": "/calculation_id", "target_key": target, "binding": "calculation_id"}
            ],
            "publication_dependencies": ["publication:opening"],
        }
    )


def test_calculation_reference_waits_for_explicit_publish_output(verifier, template):
    publication_consumer(template[0])
    assert run(verifier, template)["publication_events"] == 1


def test_calculation_reference_cannot_bind_a_known_source_key(verifier, template):
    publication_consumer(template[0], "source")
    with pytest.raises(verifier.TemplateError, match="lacks an accounting fact"):
        run(verifier, template)


def test_saved_accounting_subject_does_not_supply_published_calculation(verifier, template):
    publication_consumer(template[0])
    template[0]["publication_bindings"] = []
    template[0]["facts"][-1]["publication_dependencies"] = []
    with pytest.raises(verifier.TemplateError, match="lacks a publication event"):
        run(verifier, template)


def test_consumer_must_wait_for_publication_even_when_fact_is_saved(verifier, template):
    publication_consumer(template[0])
    template[0]["facts"][-1]["publication_dependencies"] = []
    with pytest.raises(verifier.TemplateError, match="wait dependencies differ"):
        run(verifier, template)


def test_prepare_guide_cannot_serve_as_a_publication_producer(verifier, template):
    publication_consumer(template[0])
    template[0]["steps"][0]["commands"] = ["prepare_payroll", "confirm_payroll_preparation"]
    with pytest.raises(verifier.TemplateError, match="does not confirm this subject"):
        run(verifier, template)


def test_preview_step_cannot_claim_to_return_a_fact_revision(verifier, template):
    template[0]["steps"] = [
        {
            "key": "preview",
            "command": "preview",
            "outputs": [{"key": "fake-fact", "kind": "bank_opening"}],
        }
    ]
    with pytest.raises(verifier.TemplateError, match="does not return the declared fact kind"):
        run(verifier, template)


def test_revision_reference_cannot_use_a_source_or_preview_key(verifier, template):
    template[0]["facts"].append(
        {
            "key": "confirmation",
            "kind": "payroll_plan_v2",
            "data": {
                "period": "2026-01",
                "payroll_revision": {
                    "subject_id": "$ref:opening:subject_id",
                    "revision": "$ref:source:revision",
                },
            },
            "depends_on": ["opening", "source"],
            "source_keys": ["source"],
            "references": [
                {
                    "path": "/payroll_revision/subject_id",
                    "target_key": "opening",
                    "binding": "subject_id",
                },
                {
                    "path": "/payroll_revision/revision",
                    "target_key": "source",
                    "binding": "revision",
                },
            ],
        }
    )
    with pytest.raises(verifier.TemplateError, match="fact binding must target a fact"):
        run(verifier, template)
