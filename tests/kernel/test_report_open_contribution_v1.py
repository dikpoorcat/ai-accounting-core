"""The released reader rebuilds a nonempty contribution without current rules."""

from test_reports import book as book  # noqa: F401
from test_reports import scenario

from ai_accounting.kernel import (
    query_semantics,
    report_open_contribution,
    report_semantics,
)
from ai_accounting.kernel.integrity import _check_sources
from ai_accounting.kernel.report_open_contribution_v1 import compare_open_contributions


def test_v1_nonempty_rebuild_matches_original_and_ignores_future_reader(book, monkeypatch):
    engine = book[0]
    scenario(book)
    with engine.store.connection(read_only=True) as connection:
        source = _check_sources(engine, connection)
        expected = report_open_contribution.compare_open_contributions(
            engine, connection, verified_source=source
        )
        assert expected

        def future_rule(*_args, **_kwargs):
            raise AssertionError("released v1 must use fixed report contribution rules")

        monkeypatch.setattr(query_semantics, "resolve_calculation_relations", future_rule)
        monkeypatch.setattr(report_semantics, "compact_line_fields", future_rule)
        monkeypatch.setattr(report_semantics, "immutable_line_fields", future_rule)
        monkeypatch.setattr(report_open_contribution, "prepare_open_contribution", future_rule)
        monkeypatch.setattr(report_open_contribution, "compare_open_contributions", future_rule)
        monkeypatch.setattr(report_open_contribution, "RECLASS", frozenset())
        actual = compare_open_contributions(engine, connection, verified_source=source)
        assert {ident: row.content for ident, row in actual.items()} == {
            ident: row.content for ident, row in expected.items()
        }
