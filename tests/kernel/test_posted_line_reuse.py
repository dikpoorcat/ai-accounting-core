"""Whole-line equality never lets Python's bool/float equality change money types."""

import pytest
from test_engine import engine as engine  # noqa: F401
from test_engine import publish, save

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard


@pytest.mark.parametrize("value", [False, 0.0, True, 100.0])
def test_posted_line_reader_rejects_noninteger_values_equal_to_saved_money(
    engine, monkeypatch, value
):
    save(engine, subject="cost", amount=1 if value is True else 100)
    publish(engine, ["cost"])
    with Dashboard(engine)._snapshot("2026-01") as snap:
        original = snap.reads.voucher_lines

        def faulty_decoder(identifiers):
            rows = {ident: [dict(line) for line in lines]
                    for ident, lines in original(identifiers).items()}
            for lines in rows.values():
                for line in lines:
                    if value is False or value == 0.0:
                        line["credit"] = value if line["credit"] == 0 else line["credit"]
                    elif value is True and line["credit"] == 1:
                        line["credit"] = True
                    elif line["debit"] == 100:
                        line["debit"] = value
            return rows

        monkeypatch.setattr(snap.reads, "voucher_lines", faulty_decoder)
        with pytest.raises(KernelError) as failure:
            snap.month_journal.account_amounts()
        assert failure.value.code == "content_integrity_failed"
