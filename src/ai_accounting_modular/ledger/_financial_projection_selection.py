"""The original calculation consumes selected owner folds, not a second set of formulas."""

from collections import defaultdict

from ai_accounting_modular.contracts.public import parse_fen
from ai_accounting_modular.ledger.reporting.public import cash_totals, profit_totals

from ._balance_cube import previous
from ._financial_calculation import amount
from ._financial_cells import known
from ._financial_order import CHILD_KEYS
from ._financial_positions import cash_before, positions
from ._financial_projection_bodies import COLUMN
from ._financial_projection_fold import Fold, Notice, Unknown
from ._financial_projection_indexes import flow_source_used, pretax_slice
from ._financial_selection import Selection
from ._journal_amounts import journal_amount
from ._values import fail


class ProjectionSelection(Selection):
    def __init__(self, model, scope, read, issues):
        super().__init__(
            read.repository("ledger"), scope["view"], scope["period_to"], read.start_period, issues
        )
        self.model = model
        self._balance_cubes = {}
        self.used_atoms, self.used_roles = set(), defaultdict(set)

    def source(self, head):
        if "atom" not in head:
            return super().source(head)
        atom = head["atom"]
        body = self.model.bodies.get_many((atom,))[0]
        if any(
            body["source"][key] != head[key] for key in ("result_id", "business_id", "result_slot")
        ):
            fail("storage_corrupt", "财务采用来源与同一正文目录不一致。")
        self.used_atoms.add(atom)
        return dict(body["source"])

    def selected_cube(self, cutoff):
        if cutoff not in self._balance_cubes:
            self._balance_cubes[cutoff] = self._selected_cube(cutoff)
        return self._balance_cubes[cutoff]

    def _selected_cube(self, cutoff):
        opening = self.opening_rows()
        if opening is None or cutoff < previous(self.start):
            return None
        if cutoff < self.start:
            return self.model.empty_stock.cube(opening)
        role_key, role = self.model.stock_role(self.view, cutoff)
        self.used_roles[role_key].add("journal")
        return role.cube(opening)

    def stock(self, cutoff):
        # Only positions asks for stock in this adapter, after its full GL cube.
        # Its remaining use is the precisely controlled resource reconciliation.
        if cutoff not in self._stocks:
            _, role = self.model.stock_role(self.view, cutoff)
            self._stocks[cutoff] = role.resource_heads()
        return self._stocks[cutoff]

    def position_resources(self, heads, accounts):
        grouped = {name: {} for name in ("obligation", "settlement", "obligation_reclassification")}
        ordinary = []
        for head in heads:
            if head is None:
                fail("storage_corrupt", "财务受控资源缺少真实期初来源。")
            if "atom" not in head:
                ordinary.append(head)
                continue
            records = self.model.bodies.get_many((head["atom"],))[0]["resources"]
            for table, rows in records.items():
                if rows:
                    grouped[table][head["result_id"]] = rows
        if ordinary:
            records = super().position_resources(ordinary, accounts)
            for table, values in records.items():
                grouped[table].update(values)
        selected = {
            (head["business_id"], row["public_slot"])
            for head in heads
            for row in grouped["obligation"].get(head["result_id"], ())
        }
        result = {}
        for table, values in grouped.items():
            result[table] = {}
            for head in heads:
                rows = values.get(head["result_id"], ())
                if table != "obligation":
                    rows = [
                        row
                        for row in rows
                        if (row["source_business_id"], row["source_public_slot"]) in selected
                    ]
                if rows:
                    result[table][head["result_id"]] = sorted(
                        rows, key=lambda row: row[CHILD_KEYS[table]]
                    )
        return result

    def all_sources(self):
        atoms = set(self.used_atoms)
        for key, phases in self.used_roles.items():
            role = self.model.roles[key]
            if role.spec["kind"] == "stock":
                if "journal" in phases:
                    atoms.update(role.source_atoms())
            else:
                entries = tuple(role.entries())
                bodies = self.model.bodies.get_many(entry.value[0] for entry in entries)
                atoms.update(
                    entry.value[0]
                    for entry, body in zip(entries, bodies, strict=True)
                    if any(flow_source_used(body, phase) for phase in phases)
                )
        sources = [dict(body["source"]) for body in self.model.bodies.get_many(sorted(atoms))]
        parent_source = super().source
        sources.extend(parent_source(self.heads[key]) for key in tuple(self.used))
        return tuple(
            sorted(
                sources, key=lambda ref: (ref["business_id"], ref["result_slot"], ref["result_id"])
            )
        )


def _notice(note, column):
    return Notice(
        note.field.replace(COLUMN, column), note.code, note.meaning, note.message, note.mixed_codes
    )


def _register(issues, notices, column):
    for note in notices:
        note = _notice(note, column).final()
        issues.unknown(note.field, note.meaning, note.message, code=note.code)


def _initial(cell, issues):
    if cell["state"] == "known":
        return parse_fen(cell["amount_fen"])
    return Unknown(
        tuple(
            Notice(value["field"], value["code"], value["meaning"], value["message"])
            for index in cell["issue_indexes"]
            for value in (issues.values[index],)
        )
    )


def _cell(value, issues, column):
    if not isinstance(value, Unknown):
        return known(value)
    _register(issues, value.notices, column)
    return {
        "state": "unestablished",
        "issue_indexes": sorted(
            {issues._keys[_notice(note, column).field] for note in value.notices}
        ),
    }


class ProjectionCalculation:
    def __init__(self, selection):
        self.selection = selection

    def positions(self, cutoff, column):
        return positions(
            self.selection, cutoff, column, balance_provider=self.selection.selected_cube
        )

    def amount(self, cutoff, codes, *, object_id=None, sign=1, field):
        return amount(
            self.selection,
            cutoff,
            codes,
            object_id=object_id,
            sign=sign,
            field=field,
            balance_provider=self.selection.selected_cube,
        )

    def _flow(self, first, last, phase):
        selection = self.selection
        key, role = selection.model.flow_role(
            selection.view, first, last, include_cash=selection.include_cash
        )
        selection.used_roles[key].add(phase)
        return role

    def _values(self, first, last, column, statement, count, prefix):
        selection = self.selection
        role = self._flow(first, last, statement)
        prior = selection.prior(statement, first, last)
        summary = role.summary()
        _register(selection.issues, summary.get(prefix + "!", Fold()).notices, column)
        return {
            line: _cell(
                summary.get(prefix + str(line), Fold()).finish(
                    _initial(prior[line], selection.issues)
                ),
                selection.issues,
                column,
            )
            for line in range(1, count + 1)
        }

    def profit(self, first, last, column):
        values = self._values(first, last, column, "profit", 32, "p")
        values.update(profit_totals(values))
        return values

    def cash(self, first, last, column):
        values = self._values(first, last, column, "cash", 22, "c")
        values.update(cash_totals(values))
        return values

    def cash_before(self, first, last, column):
        return cash_before(
            self.selection, first, last, column, cash_total_provider=self._cash_total
        )

    def _cash_total(self, cutoff, opening):
        selection = self.selection
        total = sum(
            journal_amount(row["debit_fen"]) - journal_amount(row["credit_fen"])
            for row in opening
            if row["account_code"] in ("1001", "1002", "1012")
        )
        if cutoff >= selection.start:
            key, role = selection.model.stock_role(selection.view, cutoff)
            selection.used_roles[key].add("journal")
            total += role.trees["cash"].summary(role.roots["cash"])
        return total

    def pretax_slices(self, first, last):
        role = self._flow(first, last, "pretax")
        result = [value for entry in role.entries() if (value := pretax_slice(entry)) is not None]
        return sorted(
            result,
            key=lambda item: (
                item["business_id"],
                item["result_slot"],
                item["period"],
                item["sign"],
            ),
        )
