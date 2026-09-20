"""Direct adoption stays exact across reviews and cumulative dependencies."""

import hashlib
import json
from copy import deepcopy
from typing import ClassVar

import pytest
from schema_fixture import test_bundle
from test_engine import close, evidence, publish, save
from test_engine import engine as engine  # noqa: F401
from test_integrity_content import damage, verify

from ai_accounting.kernel.close_contract import require_close_contract
from ai_accounting.kernel.contracts import Fact, KernelError, Line, Outcome, Read, Registry
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.integrity import verify_integrity
from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.types import canonical, digest


def replace_manifest(engine, manifest):
    damage(
        engine,
        "period_close",
        "UPDATE period_close SET manifest=?,digest=?",
        (canonical(manifest), digest(manifest)),
    )


def test_review_before_close_records_owner_and_exact_reviewed_basis(engine):
    save(engine)
    _, first = publish(engine)
    save(engine, revision=1, request="review")
    _, second = publish(engine, request="review-publish")
    manifest = close(engine)
    owner = first["results"][0]["calculation_id"]
    basis = second["results"][0]["calculation_id"]
    assert owner != basis
    assert manifest["vouchers"][0]["calculation_id"] == owner
    assert manifest["vouchers"][0]["adopted_calculation_id"] == basis
    assert [item["calculation_id"] for item in manifest["adopted_results"]] == [basis]
    assert "calculations" not in manifest and "facts" not in manifest
    assert verify(engine)["status"] == "verified"


def test_review_after_close_does_not_replace_frozen_adoption(engine):
    save(engine)
    publish(engine)
    before = close(engine)
    save(engine, revision=1, request="later-review")
    publish(engine, request="later-publish")
    assert Periods(engine).closed_report("2026-01") == before
    assert verify(engine)["status"] == "verified"
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute("SELECT max(sequence) FROM calculation_publication").fetchone()[0]
            > before["publication_sequence"]
        )


def test_zero_line_state_is_direct_and_omission_is_damage(engine):
    engine.save_fact(
        "test_charge",
        "charge",
        {"period": "2026-01", "amount": 100, "suppress_posting": True},
        evidence=(evidence(engine),),
        expected_revision=0,
        request_id="state",
    )
    publish(engine)
    manifest = close(engine)
    assert manifest["vouchers"] == []
    assert manifest["adopted_results"][0]["role"] == "state_only"
    manifest["adopted_results"] = []
    replace_manifest(engine, manifest)
    with pytest.raises(KernelError) as failure:
        verify(engine, include_indexes=False)
    assert failure.value.details["reason"] == "direct_adoption_set_mismatch"


@pytest.mark.parametrize(
    "field",
    [
        "adopted_results",
        "asset_card_adoptions",
        "opening_calculation_id",
        "publication_sequence",
        "read_version",
    ],
)
def test_missing_required_adoption_evidence_never_falls_back(engine, field):
    save(engine)
    publish(engine)
    manifest = close(engine)
    del manifest[field]
    with pytest.raises(KernelError) as failure:
        require_close_contract(manifest)
    assert failure.value.code == "content_integrity_failed"


def test_exact_frozen_month_is_required(engine):
    with pytest.raises(KernelError) as failure:
        Periods(engine).closed_report("2026-01")
    assert failure.value.code == "frozen_snapshot_unavailable"


def test_close_contract_requires_integer_versions_and_exact_fields(engine):
    save(engine)
    publish(engine)
    manifest = close(engine)
    for version in (True, 1.0):
        changed = deepcopy(manifest)
        changed["format_version"] = version
        with pytest.raises(KernelError):
            require_close_contract(changed)
        changed = deepcopy(manifest)
        changed["asset_card_adoptions"] = [
            {
                "contract_version": version,
                "asset_id": "synthetic-asset",
                "calculation_id": "synthetic-result",
                "result_digest": "a" * 64,
                "acceptance_calculation_id": "synthetic-acceptance",
                "acceptance_result_digest": "b" * 64,
            }
        ]
        with pytest.raises(KernelError):
            require_close_contract(changed)
    changed = deepcopy(manifest)
    changed["unrecognized_contract_field"] = {}
    with pytest.raises(KernelError):
        require_close_contract(changed)


def test_close_range_is_an_explicit_optional_contract_branch(engine):
    save(engine)
    publish(engine)
    manifest = close(engine)
    scope = {"from_period": "2026-01", "through_period": "2026-03", "preview_digest": "a" * 64}
    assert require_close_contract(manifest | {"close_range": scope})
    invalid = [
        None,
        scope | {"extra": "not allowed"},
        {key: value for key, value in scope.items() if key != "from_period"},
        scope | {"from_period": "2026-02"},
        scope | {"through_period": "2025-12"},
        scope | {"from_period": True},
        scope | {"preview_digest": "a" * 63},
        scope | {"preview_digest": "g" * 64},
        scope | {"preview_digest": "A" * 64},
    ]
    for changed in invalid:
        with pytest.raises(KernelError) as failure:
            require_close_contract(manifest | {"close_range": changed})
        assert failure.value.details["reason"] == "invalid_close_range"


def test_close_verification_batches_inventory_and_shared_evidence(engine, monkeypatch):
    save(engine)
    publish(engine)
    close(engine)
    periods = Periods(engine)
    proof = evidence(engine)
    for category in MATERIAL_CATEGORIES:
        periods.inventory(
            "2026-02",
            category,
            evidence=[],
            expected=0,
            no_business=True,
            confirmation_evidence=proof,
            request_id="february-" + category,
        )
    preview = periods.preview_close("2026-02", owner_confirmation=proof)
    periods.close(
        "2026-02",
        owner_confirmation=proof,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="february-close",
    )
    evidence_hashes = []
    original = hashlib.sha256

    def counted(data=b"", *args, **kwargs):
        if data == b"business evidence":
            evidence_hashes.append(data)
        return original(data, *args, **kwargs)

    monkeypatch.setattr(hashlib, "sha256", counted)
    statements = []
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        connection.set_trace_callback(statements.append)
        assert verify_integrity(engine, connection, include_indexes=False)["status"] == "verified"
    inventory_reads = [sql for sql in statements if "FROM material_revision" in sql]
    assert len(inventory_reads) == 1
    assert len(evidence_hashes) == 1


def test_opening_month_is_not_an_empty_month_that_can_be_skipped(tmp_path):
    from test_integrity_content import opening_engine

    engine = opening_engine(tmp_path)
    proof = evidence(engine)
    engine.save_fact(
        "test_opening",
        "opening",
        {"period": "2026-01", "amount": 100},
        evidence=(proof,),
        expected_revision=0,
        request_id="opening",
    )
    publish(engine, ["opening"])
    with pytest.raises(KernelError) as failure:
        Periods(engine).preview_close("2026-02", owner_confirmation=proof)
    assert failure.value.code == "earlier_period_open"
    manifest = close(engine)
    assert manifest["opening_calculation_id"] is not None
    assert verify(engine)["status"] == "verified"


class Cumulative(Fact):
    kind: ClassVar[str] = "test_cumulative_close"

    def reads(self):
        return (Read("calculation", self.kind, "*", before_period=self.period),)


def test_cumulative_dependencies_are_not_repeated_in_each_close(tmp_path):
    registry = Registry()

    def calculate(version, context):
        parents = context.select(
            Read("calculation", Cumulative.kind, "*", before_period=version.fact.period)
        )
        return Outcome(
            (Line("5602", debit=100), Line("2202", credit=100)), {"parent_count": len(parents)}
        )

    registry.register(Cumulative, calculate)
    engine = Engine(
        Store.create(
            tmp_path / "cumulative.sqlite",
            test_bundle(registry),
            "direct",
            "91310000123456789A",
            "direct-db",
        )
    )
    periods = Periods(engine)
    owner = evidence(engine)
    manifests = []
    for month in ("2026-01", "2026-02", "2026-03"):
        subject = "source-" + month
        engine.save_fact(
            Cumulative.kind,
            subject,
            {"period": month},
            evidence=(owner,),
            expected_revision=0,
            request_id="save-" + month,
        )
        publish(engine, [subject], request="publish-" + month)
        for category in MATERIAL_CATEGORIES:
            periods.inventory(
                month,
                category,
                evidence=[],
                expected=0,
                no_business=True,
                confirmation_evidence=owner,
                request_id=f"{month}-{category}",
            )
        preview = periods.preview_close(month, owner_confirmation=owner)
        periods.close(
            month,
            owner_confirmation=owner,
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id="close-" + month,
        )
        manifests.append(periods.closed_report(month))
    assert [len(item["adopted_results"]) for item in manifests] == [1, 1, 1]
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM dependency_calculation").fetchone()[0] == 3
        roots = [
            json.loads(row[0])["adopted_results"]
            for row in connection.execute("SELECT manifest FROM period_close")
        ]
        assert sum(map(len, roots)) == 3
    assert verify(engine)["status"] == "verified"
