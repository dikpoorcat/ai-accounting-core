"""Tax qualification discovery never loads unrelated historical income bodies."""

from collections import Counter
from copy import deepcopy
from uuid import uuid4

import pytest

from ai_accounting_modular.application._context import SubmissionContext
from ai_accounting_modular.domains.taxes import (
    _investment_basis,
    _investment_basis_dependencies,
)
from tests.modular.domains.financing._support import COUNTERPARTY, assemble, fact

from .test_investment_basis_application import start
from .test_investment_tax_application import inputs, qualification


@pytest.mark.parametrize("history_count", [1, 25])
@pytest.mark.parametrize("reading", ["candidates", "dependencies"])
def test_qualification_reads_do_not_grow_with_same_terms_income(
    tmp_path, monkeypatch, history_count, reading
):
    app, company = assemble(tmp_path, taxes=True)
    terms, acquired, _, _, _, method = inputs()
    terms["period"] = acquired["period"] = "2026-01"
    acquired["effective_on"] = "2026-01-01"
    current = deepcopy(acquired)
    current.update(business_id=str(uuid4()), period="2026-07", effective_on="2026-07-01")
    proof = qualification(terms)
    proof["period"] = "2026-09"  # Discovery uses applicability, not registration month.
    incomes = [
        fact(
            "investment.income",
            {
                "terms_id": terms["business_id"],
                "counterparty_id": COUNTERPARTY,
                "income_character": "fund_distribution",
                "amount_fen": "100",
            },
            actual=f"2026-01-{day:02d}",
        )
        for day in range(2, history_count + 2)
    ]
    start(app, company, terms, acquired, *incomes, current, proof)

    # Count the real SQLite-to-domain body boundary, with a fresh context cache.
    # This fails if candidates first load every reference and filter afterwards.
    loaded = Counter()
    export = app.registry.export_heads

    def record_bodies(heads, read):
        loaded.update(head["kind"] for head in heads)
        return export(heads, read)

    monkeypatch.setattr(app.registry, "export_heads", record_bodies)
    with app.storage.read_unit(company) as read:
        context = SubmissionContext(
            read,
            app.registry,
            app.identity,
            app.ledger,
            app.records,
            money=app.money,
            changed_business_ids=(current["business_id"],),
            evaluation_period_to="2026-07",
        )
        if reading == "candidates":
            rows = _investment_basis.candidates(context, (terms["business_id"],))
            assert rows == {terms["business_id"]: (proof,)}
            assert loaded == {"taxes.investment_basis": 1}
        else:
            queried_dependencies = []
            dependencies = context.current_dependencies

            def record_dependencies(identities):
                queried_dependencies.extend(identities)
                return dependencies(identities)

            monkeypatch.setattr(context, "current_dependencies", record_dependencies)
            edges = _investment_basis_dependencies.dependencies(
                method, context, ("investment.acquire",), "2026-07", "2026-07"
            )
            assert queried_dependencies == [current["business_id"]]
            assert {edge["producer_id"] for edge in edges} == {
                terms["business_id"],
                proof["business_id"],
            }
            observer = next(edge for edge in edges if edge["read_mode"] == "collection")
            assert observer["period_from"] == "0001-01"
            assert observer["period_to"] == "9999-12"
            assert observer["notify_effect"] == "description"
            assert loaded == {
                "investment.acquire": 1,
                "investment.terms": 1,
                "taxes.investment_basis": 1,
            }
    assert loaded["investment.income"] == 0
