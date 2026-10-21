"""Explicit balances select the same formal journals through account-indexed identities."""

from collections import defaultdict

from ai_accounting_modular.contracts.public import format_fen

from ._financial_selection import Selection
from ._formal_output import target_vouchers


class BalanceSelection(Selection):
    def __init__(self, repo, view, end, start, issues, keys):
        super().__init__(repo, view, end, start, issues)
        self.keys = keys

    def stock(self, cutoff):
        if cutoff not in self._stocks:
            heads = self.repo.read_balance_heads(self.keys, cutoff, view=self.view)
            selected = {head["result_id"]: head for head in heads}
            self.heads.update(selected)
            self._stocks[cutoff] = selected
        return self._stocks[cutoff]

    def lines(self, heads):
        missing = [head for head in heads if head["result_id"] not in self._lines]
        if missing:
            journals = target_vouchers(self.repo, missing, headers_only=True)
            grouped = defaultdict(list)
            ids = {row["voucher_id"] for row in journals.values() if row is not None}
            for row in self.repo.read_balance_lines(self.keys, ids) if ids else ():
                grouped[row["voucher_id"]].append(
                    {
                        **row,
                        "debit_fen": format_fen(row["debit_fen"]),
                        "credit_fen": format_fen(row["credit_fen"]),
                    }
                )
            for head_id, voucher in journals.items():
                self._lines[head_id] = grouped[voucher["voucher_id"]] if voucher else []
        for head in heads:
            if self._lines[head["result_id"]]:
                self.source(head)
        return {head["result_id"]: self._lines[head["result_id"]] for head in heads}
