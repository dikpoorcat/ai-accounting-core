"""Journal calculations share SQL integer amounts and canonical typed-plan text."""

from ai_accounting_modular.contracts.public import checked_fen, parse_fen


def journal_amount(value):
    """Wire inputs still use Fen text; SQL projections retain strict int64 integers."""
    return checked_fen(value) if type(value) is int else parse_fen(value)
