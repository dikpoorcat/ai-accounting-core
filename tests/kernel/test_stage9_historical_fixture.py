"""Historical samples cannot declare current taxes or lose their source contract."""

from pathlib import Path

import pytest
from stage9_book import MixedBook
from stage9_historical_fixture import ArchivedSyntheticSale, historical_bundle

from ai_accounting.kernel.backup import (
    _retained_history_digest,
    create_portable,
    restore_portable,
    verify_file,
)
from ai_accounting.kernel.contracts import KernelError, NeedsInformation
from ai_accounting.kernel.development_contracts import HISTORY_TARGET_FINGERPRINT
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.schema import schema_sql
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.versions import contract_fingerprint


def sale_data(period="2016-01"):
    return {"period": period, "customer_id": "archived-customer", "gross_fen": 50500,
            "vat_policy_id": "archived-vat", "exemption_eligible": False,
            "tax_obligation_period": period}


def test_archived_grammar_is_the_exact_original_contract_and_not_public_registration():
    source = historical_bundle()
    assert source.current("company")["sha256"] == HISTORY_TARGET_FINGERPRINT
    assert contract_fingerprint(schema_sql(source.registry)).hex() == HISTORY_TARGET_FINGERPRINT
    assert "tax_point_basis" not in source.registry.models
    ArchivedSyntheticSale(**sale_data()).validate_registration()
    with pytest.raises(KernelError, match="历史规模样本"):
        ArchivedSyntheticSale(**sale_data("2026-01")).validate_registration()
    with pytest.raises(KernelError, match="当月旧税期来源"):
        changed = ArchivedSyntheticSale(**{**sale_data(), "tax_obligation_period": "2016-02"})
        changed.validate_registration()
    public = production_bundle().registry.models["service_sale"](**sale_data())
    with pytest.raises(NeedsInformation):
        public.validate_registration()


def _calculation(engine, calculation_id):
    with engine.store.connection(read_only=True) as connection:
        row = connection.execute(
            "SELECT * FROM calculation WHERE id=?", (calculation_id,),
        ).fetchone()
        return engine.store.calculation(row)


def test_completed_historical_book_resume_and_portable_restore_use_the_same_contract(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("STAGE9_WORKSPACE_ROOT", str(tmp_path))
    root = tmp_path / ".tmp" / "stage9-fixed-source"
    book = MixedBook(root, employees=1, businesses=26)
    book.add_month(0, close=True)
    book.add_month(1, close=False)
    original = _calculation(book.engine, book.results["tax-2016-01"])
    assert original.values["synthetic_history_contract"] == HISTORY_TARGET_FINGERPRINT
    assert not any(key.startswith("filing_") for key in original.values)
    resumed = MixedBook.resume(root)
    assert resumed.engine.store.bundle.current("company")["sha256"] == HISTORY_TARGET_FINGERPRINT
    assert _calculation(resumed.engine, book.results["tax-2016-01"]).values == original.values
    source = resumed.engine.store.bundle
    archive = create_portable(resumed.engine.store.path, tmp_path / "portable", _bundle=source)
    restored_path = tmp_path / "restored.sqlite"
    restored = restore_portable(archive["path"], restored_path, _bundle=source)
    identity = restored["identity"]
    engine = Engine(Store(restored_path, historical_bundle(),
                          identity["company_id"], identity["database_id"]))
    verified = verify_file(restored_path, _bundle=engine.store.bundle)
    assert verified["verification"]["status"] == "verified"
    assert _calculation(engine, book.results["tax-2016-01"]).values == original.values
    from scripts.fork_stage9_book import fork

    target = tmp_path / ".tmp" / "stage9-fixed-fork"
    result = fork(root, target, tmp_path / ".tmp" / "stage9-fork-archive",
                  repository=tmp_path, code_source=Path(__file__).resolve().parents[2])
    assert result["status"] == "verified_prefix_fork"
    forked = MixedBook.resume(target)
    assert forked.engine.store.bundle.current("company")["sha256"] == HISTORY_TARGET_FINGERPRINT
    assert forked.snapshots["2016-01"]["closed"] is True
    assert forked.snapshots["2016-02"]["closed"] is False
    with resumed.engine.store.connection(read_only=True) as first:
        with forked.engine.store.connection(read_only=True) as second:
            assert _retained_history_digest(first) == _retained_history_digest(second)
    assert _calculation(forked.engine, book.results["tax-2016-01"]).values == original.values
