"""Fixed report cells retain missing meanings without multiplying per-business issues."""

from ai_accounting_modular.contracts.public import format_fen, parse_fen

from ._balances import sum_amounts

_MIXED_ISSUE = {
    "code": "report_basis_missing",
    "meaning": "报表字段采用依据",
    "message": "本字段存在未明确的正式分类或采用依据，请查看相关业务明细。",
}


def known(amount=0):
    return {"state": "known", "amount_fen": format_fen(sum_amounts((amount,)))}


def add(*cells):
    indexes = sorted(
        {i for cell in cells if cell["state"] == "unestablished" for i in cell["issue_indexes"]}
    )
    return (
        {"state": "unestablished", "issue_indexes": indexes}
        if indexes
        else known(sum_amounts(parse_fen(cell["amount_fen"]) for cell in cells))
    )


def scaled(cell, sign):
    return cell if cell["state"] == "unestablished" else known(sign * parse_fen(cell["amount_fen"]))


class Issues:
    def __init__(self):
        self.values, self._keys = [], {}

    def unknown(self, field, meaning, message, *, code="report_basis_missing"):
        # A report has a fixed number of fields, irrespective of business count.
        if field not in self._keys:
            self._keys[field] = len(self.values)
            self.values.append(
                {
                    "code": code,
                    "field": field,
                    "meaning": meaning,
                    "message": message,
                    "allowed_precision": ["fen", "source"],
                    "reusable_sources": [],
                    "blocking_scope": "none",
                }
            )
        index = self._keys[field]
        if self.values[index]["code"] != code:
            self.values[index].update(_MIXED_ISSUE)
        return {"state": "unestablished", "issue_indexes": [index]}


def zeros(count):
    return {line: known() for line in range(1, count + 1)}
