"""New ranges reuse immutable roots without changing Counter order or older scopes."""

from copy import deepcopy

import _financial_snapshot_review_support as support

from ai_accounting_modular.ledger._financial_calculation import calculate_snapshot
from ai_accounting_modular.ledger._financial_cells import Issues
from ai_accounting_modular.ledger._financial_projection_model import Model
from ai_accounting_modular.ledger._financial_projection_requests import request
from ai_accounting_modular.ledger._financial_projection_roles import FlowRole, StockRole
from ai_accounting_modular.ledger._financial_selection import Selection


class Blocks:
    header_json = None

    def __init__(self):
        self.values = {}

    def get_blocks(self, keys):
        return tuple(self.values.get(key) for key in keys)

    def publish(self, plan):
        for key in plan.deletes:
            del self.values[key]
        self.values.update(((collection, key), raw) for collection, key, raw in plan.writes)
        self.header_json = plan.header_json


def close(book, month):
    with book.app.storage.write_unit(book.company) as unit:
        checked = book.app.ledger.check_period(month, unit)
        assert checked.ready
        book.app.ledger.close_period(month, checked.head, unit)


def corrected(fact, amount):
    value = deepcopy(fact)
    value["data"]["amount_fen"] = amount
    value["data"]["report_details"][0]["amount_fen"] = amount
    return value


def two_closed_months(tmp_path):
    book = support.assemble(tmp_path)
    expense = support.expense("range-corrected-cost", "100")
    book.submit(support.opening(cash="1000"), expense)
    close(book, "2026-01")
    book.replace(corrected(expense, "120"), correction_period="2026-02")
    close(book, "2026-02")
    return book, expense


def selected_scope(book, first, last):
    return {
        "company_id": book.company,
        "period_from": first,
        "period_to": last,
        "view": "as_posted",
    }


def test_new_scope_after_two_closed_corrections_keeps_old_scope_and_matches_cold(
    tmp_path, monkeypatch
):
    book, expense = two_closed_months(tmp_path)
    book.replace(corrected(expense, "80"), seq=2, correction_period="2026-03")
    book.submit(support.expense("new-quarter-cost", "7", "2026-04"))
    blocks = Blocks()
    old_scope = selected_scope(book, "2026-01", "2026-03")
    with book.app.storage.read_unit(book.company) as read:
        old_request = request("snapshot", old_scope, read)
        model = Model(read)
        roots = model.advance((old_request,), ())
        old_descriptor = model.descriptor(old_request)
        blocks.publish(model.plan(roots))
        old_artifact = book.app.ledger.read_financial_projection(old_request, read, blocks)

    with book.app.storage.read_unit(book.company) as read:
        hot = Model(read, blocks)
        _, old_stock = hot.stock_role("as_posted", "2026-03")
        _, old_flow = hot.flow_role("as_posted", "2026-01", "2026-03", include_cash=True)
        saved_stock, saved_flow = deepcopy(old_stock.wire()), deepcopy(old_flow.wire())
        new_scope = selected_scope(book, "2026-04", "2026-06")
        new_request = request("snapshot", new_scope, read)
        extensions = []

        def observed(original):
            def extend(role, values, previous_spec=None):
                result = original(role, values, previous_spec)
                extensions.append((dict(role.spec), dict(previous_spec) if previous_spec else None))
                return result

            return extend

        monkeypatch.setattr(StockRole, "extend", observed(StockRole.extend))
        monkeypatch.setattr(FlowRole, "extend", observed(FlowRole.extend))
        roots = hot.advance((new_request,), ())
        assert (
            {"kind": "stock", "view": "as_posted", "cutoff": "2026-06"},
            dict(old_stock.spec),
        ) in extensions
        assert (
            {
                "kind": "flow",
                "view": "as_posted",
                "first": "2026-01",
                "last": "2026-06",
                "cash": True,
            },
            dict(old_flow.spec),
        ) in extensions
        cold = Model(read)
        cold.advance((new_request,), ())
        assert hot.descriptor(new_request) == cold.descriptor(new_request)
        assert hot.descriptor(old_request) == old_descriptor
        assert old_stock.wire() == saved_stock
        assert old_flow.wire() == saved_flow
        _, new_stock = hot.stock_role("as_posted", "2026-06")
        _, new_flow = hot.flow_role("as_posted", "2026-01", "2026-06", include_cash=True)
        assert new_stock.roots is not old_stock.roots
        assert new_stock.supplements is not old_stock.supplements
        assert new_flow.supplements is not old_flow.supplements
        assert old_flow.summary()["p14"].finish() == 80
        assert new_flow.summary()["p14"].finish() == 87
        blocks.publish(hot.plan(roots))
        # Loading the actual published graph also checks that retained root
        # ownership survives adding the new range and replacing its old legs.
        reloaded = Model(read, blocks)
        assert reloaded.descriptor(old_request) == old_descriptor
        assert book.app.ledger.read_financial_projection(old_request, read, blocks) == old_artifact
        actual = book.app.ledger.read_financial_projection(new_request, read, blocks).value
        expected = calculate_snapshot(
            new_request.scope,
            read,
            Selection(
                read.repository("ledger"), "as_posted", "2026-06", read.start_period, Issues()
            ),
        )
        assert {key: value for key, value in actual.items() if key != "selection"} == expected
        assert actual == book.app.ledger.financial_snapshot(new_scope, read)


def test_flow_extension_cancels_middle_leg_and_keeps_first_baseline_anchor(tmp_path):
    book, expense = two_closed_months(tmp_path)
    book.replace(corrected(expense, "80"), seq=2, correction_period="2026-03")
    with book.app.storage.read_unit(book.company) as read:
        model = Model(read)
        model.advance((request("snapshot", selected_scope(book, "2026-01", "2026-03"), read),), ())
        values = tuple(model._old_chains().values())
        old_spec = {
            "kind": "flow",
            "view": "as_posted",
            "first": "2026-02",
            "last": "2026-02",
            "cash": True,
        }
        new_spec = old_spec | {"last": "2026-03"}
        old = FlowRole(old_spec, model.store, model.bodies)
        old.extend(values)
        original_wire = deepcopy(old.wire())
        original_entries = tuple(old.entries())
        extended = FlowRole(new_spec, model.store, model.bodies, old.wire())
        extended.extend(values, old_spec)
        cold = FlowRole(new_spec, model.store, model.bodies)
        cold.extend(values)
        assert extended.wire() == cold.wire()
        assert old.wire() == original_wire
        assert tuple(old.entries()) == original_entries
        assert extended.supplements is not old.supplements
        assert old.summary()["p14"].finish() == 20
        assert extended.summary()["p14"].finish() == -20
        entries = tuple(extended.entries())
        bodies = model.bodies.get_many(entry.value[0] for entry in entries)
        assert [body["posting_period"] for body in bodies] == ["2026-03", "2026-01"]
        assert [entry.value[1] for entry in entries] == [1, -1]
        # The January negative leg stays at its first February consumer even
        # after the February positive leg is cancelled by March's new baseline.
        assert entries[1].key == original_entries[1].key
