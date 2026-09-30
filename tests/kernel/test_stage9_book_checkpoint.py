"""A benchmark resume must not silently accept a partially changed month."""

import json

import pytest
import stage9_book
from stage9_book import MixedBook


def test_completed_month_resume_extends_through_real_close(tmp_path, monkeypatch):
    monkeypatch.setattr(stage9_book, "__file__", str(tmp_path / "tests/kernel/stage9_book.py"))
    root = tmp_path / ".tmp/stage9-checkpoint"
    book = MixedBook(root, employees=1, businesses=26)
    book.add_month(0, close=False)
    resumed = MixedBook.resume(root)
    assert resumed.company == book.company
    assert resumed.inputs == {
        key: list(value) for key, value in book.inputs.items()
    }
    resumed.close_last_month()
    resumed.add_month(1, close=False)
    assert len(resumed.business_subjects) == 52
    assert resumed.snapshots["2016-01"]["closed"] is True
    assert resumed.snapshots["2016-02"]["closed"] is False
    assert MixedBook.resume(root).sequence == resumed.sequence

    resumed.entities.update_entity_profile(
        resumed.owner, {"display_name": "合成资料已改变"},
        source="明确的测试变更", expected_revision=1,
        request_id=resumed.request("after-checkpoint"),
    )
    with pytest.raises(ValueError, match="changed after"):
        MixedBook.resume(root)


def test_resume_rejects_roots_outside_synthetic_namespace(tmp_path):
    with pytest.raises(ValueError, match="explicit Stage 9 synthetic root"):
        MixedBook.resume(tmp_path)


def test_resume_selects_checkpoint_company_when_catalog_has_another(tmp_path, monkeypatch):
    monkeypatch.setattr(stage9_book, "__file__", str(tmp_path / "tests/kernel/stage9_book.py"))
    root = tmp_path / ".tmp/stage9-multiple-companies"
    book = MixedBook(root, employees=1, businesses=26)
    book.add_month(0, close=False)
    other = book.catalog.create_company("91310000123456789B", "另一家合成企业")

    resumed = MixedBook.resume(root)
    assert resumed.company == book.company
    assert resumed.company["id"] != other["id"]
    assert resumed.engine.store.path == book.engine.store.path

    checkpoint = root / "stage9-builder.json"
    state = json.loads(checkpoint.read_text(encoding="utf-8"))
    state["company"]["name"] = "伪造的合成公司身份"
    checkpoint.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="synthetic checkpoint company changed"):
        MixedBook.resume(root)


def test_scoped_fixture_deferral_and_independent_full_verification(tmp_path, monkeypatch):
    from ai_accounting.kernel import integrity

    monkeypatch.setattr(stage9_book, "__file__", str(tmp_path / "tests/kernel/stage9_book.py"))
    book = MixedBook(tmp_path / ".tmp/stage9-deferred-fixture", employees=1, businesses=26)
    production_check = integrity.verify_close_integrity
    with book.defer_historical_verification_for_construction():
        book.add_month(0, close=True)
        book.add_month(1, close=False)
        assert integrity.verify_close_integrity is not production_check
    assert integrity.verify_close_integrity is production_check
    assert book.deferred_close_checks == {"2016-01": 2, "2016-02": 1}
    resumed = MixedBook.resume(book.root)
    assert resumed.deferred_close_checks == book.deferred_close_checks
    assert resumed.describe()["construction_integrity"] == {
        "mode": "deferred_historical_verification",
        "deferred_checks_by_period": {"2016-01": 2, "2016-02": 1},
        "final_full_verification_required": True,
    }
    with book.engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        verified = integrity.verify_integrity(book.engine, connection)
    assert verified["status"] == "verified"
    assert verified["counts"]["closes"] == 1
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        with book.defer_historical_verification_for_construction():
            raise RuntimeError("synthetic interruption")
    assert integrity.verify_close_integrity is production_check


def test_fixture_deferral_rejects_other_roots_and_companies(tmp_path, monkeypatch):
    from ai_accounting.kernel import integrity

    monkeypatch.setattr(stage9_book, "__file__", str(tmp_path / "tests/kernel/stage9_book.py"))
    book = MixedBook(tmp_path / ".tmp/stage9-boundary", employees=1, businesses=26)
    production_check = integrity.verify_close_integrity
    original_root = book.root
    book.root = tmp_path / "outside"
    with pytest.raises(ValueError, match="isolated Stage 9 fixture"):
        with book.defer_historical_verification_for_construction():
            pytest.fail("unsafe root accepted")
    book.root = original_root
    book.company = {**book.company, "name": "另一个公司"}
    with pytest.raises(ValueError, match="isolated Stage 9 fixture"):
        with book.defer_historical_verification_for_construction():
            pytest.fail("other company accepted")
    assert integrity.verify_close_integrity is production_check
