"""Native project cards bind displayed values and remaining effects to original sources."""

import json

import pytest
from test_dashboard_model_transactions import comparable, raw_read
from test_integrity_content import damage
from test_labor_assets import book as _book
from test_labor_assets import cost

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.runtime import _PrivateConnection
from ai_accounting.kernel.types import canonical, digest

book = _book


def _cost(kind, amount=1_600_000):
    if kind == "labor_project_cost":
        return cost(gross_fee_fen=amount)
    return {
        "period": "2026-11", "project_id": "synthetic-project", "supplier_id": "supplier",
        "amount_fen": amount, "project_nature": "purchased_intangible",
        "capitalization_conditions_confirmed": True,
    }


@pytest.mark.parametrize("kind", ["project_cost", "labor_project_cost"])
@pytest.mark.parametrize("target", [
    "displayed_amount", "remaining_amount", "project_identity", "float_amount", "bool_amount",
    "calculation_seal", "fact_seal", "physical_fact_amount", "own_fact_dependency",
])
def test_native_project_values_reject_resealed_or_unbound_actual_sources(book, kind, target):
    engine, save, publish = book
    save(kind, "cost", _cost(kind))
    publish("cost")
    dashboard = Dashboard(engine)
    with raw_read():
        before = dashboard.assets("2026-11", section="projects", limit=1)
    assert (
        comparable(dashboard.assets("2026-11", section="projects", limit=1)) == comparable(before)
    )
    card = before["data"]["collections"]["projects"]["items"][0]
    assert type(card["cost_fen"]) is int and card["cost_fen"] == 1_600_000
    assert type(card["remaining_fen"]) is int and card["remaining_fen"] == 1_600_000
    with engine.store.connection(read_only=True) as connection:
        source = dict(connection.execute(
            "SELECT c.id,c.fact_id,c.outcome FROM calculation_current h "
            "JOIN calculation c ON c.id=h.calculation_id WHERE h.subject_id='cost'"
        ).fetchone())
    if target in {"calculation_seal", "fact_seal"}:
        field = "calculation_id" if target == "calculation_seal" else "fact_id"
        ident = source["id"] if target == "calculation_seal" else source["fact_id"]
        damage(engine, target, f"DELETE FROM {target} WHERE {field}=?", (ident,),
               foreign_keys=False)
    elif target == "physical_fact_amount":
        field = "gross_fee_fen" if kind == "labor_project_cost" else "amount_fen"
        table = f"fact_{kind}"
        damage(engine, table, f"UPDATE {table} SET {field}={field}+1 WHERE revision_id=?",
               (source["fact_id"],))
    elif target == "own_fact_dependency":
        damage(engine, "dependency_fact",
               "DELETE FROM dependency_fact WHERE calculation_id=? AND fact_id=?",
               (source["id"], source["fact_id"]), foreign_keys=False)
    else:
        outcome = json.loads(source["outcome"])
        if target == "remaining_amount":
            effect = next(item for item in outcome["balances"]
                          if item["key"] == "project-cost:cost")
            effect["amount"] += 1
        elif target == "project_identity":
            outcome["values"]["project_id"] = "forged-project"
        else:
            outcome["values"]["capitalized_fen"] = {
                "displayed_amount": 1_600_001, "float_amount": 1_600_000.0, "bool_amount": True,
            }[target]
        damage(engine, "calculation", "UPDATE calculation SET outcome=?,digest=? WHERE id=?",
               (canonical(outcome), digest(outcome), source["id"]))
    # Both public consumers reject. Default model keeps its original actual-hit
    # certificate checks; native source identity now checks the original ID too.
    for options in ({}, {"section": "projects", "project_id": card["project_id"]}):
        with raw_read(), pytest.raises(KernelError) as rejected:
            dashboard.assets("2026-11", **options)
        assert rejected.value.code == "content_integrity_failed"
        with pytest.raises(KernelError) as rejected:
            dashboard.assets("2026-11", **options)
        assert rejected.value.code == "content_integrity_failed"


def test_project_identity_proof_batches_consumed_sources_and_reuses_displayed_subset(
    book, monkeypatch,
):
    engine, save, publish = book
    for index in range(3):
        save("project_cost", f"cost-{index}", {
            **_cost("project_cost", index + 1), "project_id": f"project-{index}",
        })
    publish(*(f"cost-{index}" for index in range(3)))
    scopes = []
    execute = _PrivateConnection.execute

    def observe(connection, sql, *args, **kwargs):
        if "JOIN dependency_scope d ON d.calculation_id=ids.value" in sql:
            scopes.append(set(json.loads(args[0][0])))
        return execute(connection, sql, *args, **kwargs)

    monkeypatch.setattr(_PrivateConnection, "execute", observe)
    dashboard = Dashboard(engine)
    with raw_read():
        first = dashboard.assets("2026-11", section="projects", limit=1)
    cards = first["data"]["collections"]["projects"]["items"]
    assert [item["source_id"] for item in cards] == ["cost-0"]
    assert cards[0]["cost_fen"] == cards[0]["remaining_fen"] == 1
    assert first["data"]["project_cost_fen"] == 6
    assert first["data"]["collections"]["projects"]["page"]["total_count"] == 3
    assert len(scopes) == 1 and len(scopes[0]) == 3
    scopes.clear()
    with raw_read():
        exact = dashboard.assets("2026-11", section="projects", project_id="project-2", limit=1)
    selected = exact["data"]["collections"]["projects"]["items"]
    assert [item["source_id"] for item in selected] == ["cost-2"]
    assert selected[0]["cost_fen"] == selected[0]["remaining_fen"] == 3
    assert exact["data"]["project_cost_fen"] == 6
    assert scopes and len(scopes) == 1 and len(scopes[0]) == 3
