"""A real ZIP preserves old VAT meaning; a later correction adopts the new rule."""

from uuid import uuid4

from ai_accounting_modular.contracts.public import content_digest
from ai_accounting_modular.domains.taxes.public import Taxes
from ai_accounting_modular.host.public import create_kernel, create_offline_application
from ai_accounting_modular.storage.public import OwnerProfile, create_catalog

from ._working_case import case as case
from .test_isolated_targets import request as isolated_request
from .test_restore_proof import package
from .test_vat_profit_details import _actual, _mixed_facts, _snapshot, _submit


def _results(kernel, company, identities):
    with kernel.storage.read_unit(company) as read:
        return {
            (item["reference"]["business_id"], item["reference"]["result_slot"]): item
            for item in kernel.ledger.get_results(identities, read)
        }


def _sources(kernel, company, keys):
    with kernel.storage.read_unit(company) as read:
        return {
            key: kernel.ledger.get_result_source(key[0], key[1], read)
            for key in keys
        }


def _pl23(report):
    return report["profit_statement"]["23"]["current"]


def _vat_digest(result, revision):
    return content_digest(
        {
            "calculation_key": "taxes.assessment",
            "calculation_revision": revision,
            "basis_digest": content_digest(result["basis"]),
            "target_digest": content_digest(result["target"]),
        }
    )


def test_restore_old_frozen_vat_then_correct_without_rewriting_its_source(
    case, tmp_path, monkeypatch
):
    opening = case.seed(expense=False)
    current, facts = _mixed_facts()
    evaluate = Taxes.evaluate_slot

    def legacy(self, business_id, result_slot, context):
        """Simulate the old publisher before adoption, never patch saved results."""
        plan = evaluate(self, business_id, result_slot, context)
        if plan["target"]["kind"] == "taxes.assessment" and "payable_vat_fen" in plan[
            "target"
        ]["values"]:
            plan["calculation_revision"] = plan["rule_refs"][0]["revision"] = 1
            plan["basis"]["adopted_quantities"] = [
                item
                for item in plan["basis"]["adopted_quantities"]
                if item["type"] != "ordinary_vat_accounting"
            ]
        return plan

    with monkeypatch.context() as old:
        old.setattr(Taxes, "evaluate_slot", legacy)
        _submit(case.app, case.company, *facts)
    for month in range(1, 8):
        checked = case.check(f"2026-{month:02d}")
        assert checked["ready"], checked
        assert case.close(checked)["state"] == "closed"

    identities = [opening["business_id"], *(item["business_id"] for item in facts)]
    original = _results(case.kernel, case.company, identities)
    old_vat = original[current["business_id"], "main"]
    old_sources = _sources(case.kernel, case.company, original)
    vat_key = current["business_id"], "main"
    old_source = old_sources[vat_key]
    assert old_source["content_digest"] == _vat_digest(old_vat, 1)
    assert old_vat["reference"]["state"] == "frozen"
    assert not any(
        item["type"] == "ordinary_vat_accounting"
        for item in old_vat["basis"]["adopted_quantities"]
    )
    # Financial rounding=-1, ordinary 6301 income=0. This is the old adopted
    # subline meaning (-1), rather than a request to recompute it with today's rule.
    old_reports = {
        view: _snapshot(case.app, case.company, view) for view in ("current", "as_posted")
    }
    for report in old_reports.values():
        assert _pl23(report) == {"state": "known", "amount_fen": "-1"}

    archive = package(case, seed=False)
    archive_bytes = archive.read_bytes()
    offline = create_offline_application()
    target = tmp_path / "restored-company"
    restored = offline.accounting_isolated(isolated_request("restore", "run", archive, target))
    assert restored["state"] == "created", restored
    assert restored["company_id"] == case.company

    catalog = tmp_path / "restored-catalog.sqlite3"
    create_catalog(catalog, OwnerProfile(str(uuid4()), "合成恢复负责人", "synthetic-hash"))
    restored_kernel = create_kernel(catalog)
    try:
        registered = restored_kernel.storage.register_company(target)
        assert registered.company_id == case.company
        assert registered.database_id == restored["database_id"]
        assert _results(restored_kernel, case.company, identities) == original
        assert _sources(restored_kernel, case.company, original) == old_sources
        for view, before in old_reports.items():
            after = _snapshot(restored_kernel.application, case.company, view)
            assert after["profit_statement"] == before["profit_statement"]

        declared = _actual(current)
        denied = restored_kernel.application.accounting_apply(case.request(declared))
        assert denied.get("error") == "closed_period_required", denied
        assert _results(restored_kernel, case.company, identities) == original

        corrected = restored_kernel.application.accounting_apply(
            case.request(declared, correction_period="2026-08")
        )
        assert corrected.get("completion") == "complete", corrected
        revised = _results(restored_kernel, case.company, [current["business_id"]])[
            current["business_id"], "main"
        ]
        new_source = _sources(restored_kernel, case.company, [vat_key])[vat_key]
        assert new_source["content_digest"] == _vat_digest(revised, 2)
        assert revised["reference"]["result_id"] != old_vat["reference"]["result_id"]
        assert revised["reference"]["posting_period"] == "2026-08"
        assert new_source != old_source
        assert [
            item
            for item in revised["basis"]["adopted_quantities"]
            if item["type"] == "ordinary_vat_accounting"
        ] == [
            {
                "type": "ordinary_vat_accounting",
                "exemption_income_fen": "0",
                "rounding_adjustment_fen": "0",
            }
        ]
        with restored_kernel.storage.read_unit(case.company) as read:
            (frozen,) = restored_kernel.ledger.get_results(
                [], read, result_refs=[old_vat["reference"]]
            )
            assert frozen == old_vat
        assert _pl23(_snapshot(restored_kernel.application, case.company, "current")) == {
            "state": "known",
            "amount_fen": "0",
        }
        historic = _snapshot(restored_kernel.application, case.company, "as_posted")
        assert historic["profit_statement"] == old_reports["as_posted"]["profit_statement"]
        assert archive.read_bytes() == archive_bytes
        assert _results(case.kernel, case.company, identities) == original
    finally:
        restored_kernel.close()
