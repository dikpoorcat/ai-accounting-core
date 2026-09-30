"""Independent fixture construction never substitutes for production verification."""

import pytest
import stage9_book
import stage9_independent_book
from stage9_independent_book import IndependentBook

from ai_accounting.kernel import integrity
from scripts.benchmark_stage9_browser import validate_book_report


def test_independent_deferral_restores_verifier_and_requires_full_check(tmp_path, monkeypatch):
    monkeypatch.setenv("STAGE9_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(stage9_book, "__file__", str(tmp_path / "tests/kernel/stage9_book.py"))
    monkeypatch.setattr(
        stage9_independent_book,
        "__file__",
        str(tmp_path / "tests/kernel/stage9_independent_book.py"),
    )
    root = tmp_path / ".tmp/stage9-independent-checkpoint"
    book = IndependentBook(root, objects=2, businesses=4)
    production_check = integrity.verify_close_integrity
    with book.defer_historical_verification_for_construction():
        book.add_month(0, close=True)
        book.add_month(1, close=False)
        assert integrity.verify_close_integrity is not production_check
    assert integrity.verify_close_integrity is production_check
    assert book.deferred_close_checks == {"2016-01": 2, "2016-02": 1}

    resumed = IndependentBook.resume(root)
    assert resumed.deferred_close_checks == book.deferred_close_checks
    built = {**resumed.describe(requested_months=2), "status": "built_not_verified"}
    assert built["construction_integrity"] == {
        "mode": "deferred_historical_verification",
        "deferred_checks_by_period": {"2016-01": 2, "2016-02": 1},
        "final_full_verification_required": True,
    }
    with pytest.raises(ValueError):
        validate_book_report(built, company_name="阶段九合成独立业务企业")

    with resumed.engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        verified = integrity.verify_integrity(resumed.engine, connection)
    assert verified["status"] == "verified"
    assert verified["counts"]["closes"] == 1
    complete = {**built, "status": "complete", "integrity": verified}
    with pytest.raises(ValueError, match="Current synthetic open preview"):
        validate_book_report(complete, company_name="阶段九合成独立业务企业")
    assert validate_book_report(
        complete,
        company_name="阶段九合成独立业务企业",
        require_current_preview=False,
    ) == "2016-02"


def test_independent_deferral_rejects_other_root_and_company(tmp_path, monkeypatch):
    monkeypatch.setenv("STAGE9_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(stage9_book, "__file__", str(tmp_path / "tests/kernel/stage9_book.py"))
    monkeypatch.setattr(
        stage9_independent_book,
        "__file__",
        str(tmp_path / "tests/kernel/stage9_independent_book.py"),
    )
    with pytest.raises(ValueError, match="named synthetic root"):
        IndependentBook(tmp_path / "outside", objects=2, businesses=4)
    book = IndependentBook(tmp_path / ".tmp/stage9-independent-boundary", objects=2, businesses=4)
    production_check = integrity.verify_close_integrity
    root = book.root
    book.root = tmp_path / "outside"
    with pytest.raises(ValueError, match="isolated Stage 9 fixture"):
        with book.defer_historical_verification_for_construction():
            pytest.fail("outside root accepted")
    book.root = root
    book.company = {**book.company, "taxpayer_id": "91310000123456789S"}
    with pytest.raises(ValueError, match="isolated Stage 9 fixture"):
        with book.defer_historical_verification_for_construction():
            pytest.fail("wrong company identity accepted")
    book.company = {
        **book.company,
        "name": "阶段九合成规模企业",
        "taxpayer_id": "91310000123456789S",
    }
    with pytest.raises(ValueError, match="isolated Stage 9 fixture"):
        with book.defer_historical_verification_for_construction():
            pytest.fail("forged whitelisted identity accepted")
    assert integrity.verify_close_integrity is production_check
