"""The current contract owns project lookup indexes without rewriting frozen evidence."""

from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.domains.financing.public import create_investment

from ._cooperation_support import PROJECT, agency_payment, contract, expense, funding, submitted
from ._support import assemble, change, fact, get


def product(project_id):
    return {
        "object_id": project_id,
        "kind": "investment_product",
        "name": "合成合作项目",
        "expected_seq": 0,
        "data": {"category": "cooperation"},
    }


def indexed(owner, app, company, project):
    with app.storage.read_unit(company) as read:
        return {
            item["business_id"]
            for item in owner.query_current(
                {
                    "company_id": company,
                    "family": "investment",
                    "period_from": "2026-01",
                    "period_to": "2026-02",
                    "object_id": project,
                    "limit": 500,
                },
                read,
            )
        }


@pytest.mark.parametrize("fail_after_sync", (False, True))
def test_project_change_updates_only_direct_current_indexes_in_the_owner_transaction(
    tmp_path, monkeypatch, fail_after_sync
):
    owner = create_investment()
    app, company = assemble(tmp_path, investment=owner)
    agreement, unrelated = contract(), contract()
    unrelated["data"]["project_id"] = str(uuid4())
    charged = expense(agreement)
    operator = fact(
        "investment.cooperation_operator_transfer",
        {
            "contract_id": agreement["business_id"],
            "direction": "into_pool",
            "purpose": "working_capital",
            "amount_fen": "20",
        },
        actual="2026-01-04",
    )
    children = [funding(agreement), operator, agency_payment(agreement, charged)]
    untouched = [unrelated, funding(unrelated)]
    wire = submitted(company, agreement, charged, *children, *untouched)
    wire["objects"].append(product(unrelated["data"]["project_id"]))
    saved = app.accounting_apply(wire)
    assert saved.get("completion") == "complete", saved
    with app.storage.write_unit(company) as unit:
        unit.repository("ledger").freeze_heads("2026-01")
    ids = [item["business_id"] for item in children]
    before = get(app, company, *ids)["businesses"]
    others = get(app, company, *[item["business_id"] for item in untouched])["businesses"]
    refs = [result["reference"] for item in (*before, *others) for result in item["results"]]
    with app.storage.read_unit(company) as read:
        frozen = app.ledger.get_adopted_contents(refs, read)
    first_ids = {agreement["business_id"], *ids}
    other_ids = {item["business_id"] for item in untouched}
    assert indexed(owner, app, company, PROJECT) == first_ids
    assert indexed(owner, app, company, unrelated["data"]["project_id"]) == other_ids
    new_project = str(uuid4())
    agreement["data"]["project_id"] = new_project
    revised = change(company, agreement, correction_period="2026-02")
    revised["objects"] = [product(new_project)]
    if fail_after_sync:
        real = app.records.replace_source_uses

        def interrupted(kind, key, source_ids, unit):
            if kind == "business":
                raise ContractError("temporary_failure", "合成失败：当前索引更新后未保存来源。")
            return real(kind, key, source_ids, unit)

        monkeypatch.setattr(app.records, "replace_source_uses", interrupted)
        failed = app.accounting_apply(revised)
        assert failed.get("completion") == "not_saved", failed
        assert failed["error"] == "temporary_failure"
        assert get(app, company, *ids)["businesses"] == before
        assert indexed(owner, app, company, PROJECT) == first_ids
        assert indexed(owner, app, company, new_project) == set()
        monkeypatch.setattr(app.records, "replace_source_uses", real)
        revised["command_id"] = str(uuid4())
    changed = app.accounting_apply(revised)
    assert changed.get("completion") == "complete", changed
    current = get(app, company, *ids)["businesses"]
    current_others = get(app, company, *[item["business_id"] for item in untouched])["businesses"]
    for old, new in zip((*before, *others), (*current, *current_others), strict=True):
        assert new["fact"] == old["fact"] and new["state"]["seq"] == old["state"]["seq"]
    assert indexed(owner, app, company, PROJECT) == set()
    assert indexed(owner, app, company, new_project) == first_ids
    assert indexed(owner, app, company, unrelated["data"]["project_id"]) == other_ids
    with app.storage.read_unit(company) as read:
        after = app.ledger.get_adopted_contents(refs, read)
    assert [(item.source, item.target, item.basis) for item in frozen] == [
        (item.source, item.target, item.basis) for item in after
    ]
