"""Metadata classification cannot precede its exact source identity check."""

import pytest
from test_engine import engine as engine_fixture
from test_engine import publish, save
from test_integrity_content import damage

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.query_reads import QueryReads

engine = engine_fixture


@pytest.mark.parametrize("state", [False, True])
@pytest.mark.parametrize("field", ["kind", "period", "subject_id"])
def test_metadata_rejects_wrong_source_identity_before_publishing_batch(engine, field, state):
    for subject in ("cached", "valid", "damaged"):
        save(engine, subject=subject, request=subject)
    _, result = publish(engine, ["cached", "valid", "damaged"])
    ids = {item["subject_id"]: item["calculation_id"] for item in result["results"]}
    # test_source has no accounting handler: this also exercises a type that
    # consumers would otherwise exclude before checking the source header.
    value = {"kind": "test_source", "period": 2026 * 12, "subject_id": "valid"}[field]
    damage(
        engine,
        "calculation",
        f"UPDATE calculation SET {field}=? WHERE id=?",
        (value, ids["damaged"]),
    )
    with QueryReads.snapshot(engine) as reads:
        cached = reads.metadata({ids["cached"]}, state=state)
        before = dict(reads._metadata)
        outcomes = {"existing": "caller data"}
        for _ in range(2):
            with pytest.raises(KernelError) as failure:
                reads.metadata({ids["valid"], ids["damaged"]}, state=state, outcomes=outcomes)
            assert failure.value.code == "content_integrity_failed"
            assert reads._metadata == before
            assert outcomes == {"existing": "caller data"}
        assert reads.metadata({ids["cached"]}, state=state) == cached


def test_metadata_missing_publication_does_not_publish_an_earlier_valid_row(engine):
    for subject in ("valid", "unpublished"):
        save(engine, subject=subject, request=subject)
    _, result = publish(engine, ["valid", "unpublished"])
    ids = {item["subject_id"]: item["calculation_id"] for item in result["results"]}
    damage(
        engine,
        "calculation_publication",
        "DELETE FROM calculation_publication WHERE calculation_id=?",
        (ids["unpublished"],),
        foreign_keys=False,
    )
    with QueryReads.snapshot(engine) as reads:
        outcomes = {}
        with pytest.raises(KernelError) as failure:
            reads.metadata(set(ids.values()), state=False, outcomes=outcomes)
        assert failure.value.code == "unknown_calculation"
        assert reads._metadata == {}
        assert outcomes == {}
