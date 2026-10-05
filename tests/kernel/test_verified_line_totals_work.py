"""Validated nonnegative totals retain boundaries without checking each item twice."""

from contextlib import nullcontext

import pytest
from test_engine import close, publish, save
from test_engine import engine as engine  # noqa: F401
from test_integrity_content import damage

from ai_accounting.kernel import history_encoding_v1, integrity, types
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import KernelError


def line(debit=0, credit=0, **changes):
    return {"account": "5602", "debit": debit, "credit": credit, "cashflow": None} | changes


class IntChild(int):
    pass


class DictChild(dict):
    pass


class ListChild(list):
    pass


CASES = [
    (None, "invalid_lines"),
    ((), "invalid_lines"),
    ({}, "invalid_lines"),
    ([{}], "invalid_line_shape"),
    ([line(1, extra=1)], "invalid_line_shape"),
    ([line()], "invalid_line"),
    ([line(1, 1)], "invalid_line"),
    ([line(-1)], "invalid_line"),
    ([line(True)], "invalid_line"),
    ([line(1.0)], "invalid_line"),
    ([line(IntChild(1))], "invalid_line"),
    ([line(1, account="")], "invalid_line"),
    ([line(1, cashflow="")], "invalid_line"),
    ([line(1)], "unbalanced_lines"),
    ([line(1), line(credit=2)], "unbalanced_lines"),
    ([line(types.MAX_FEN), line(1), {}], "invalid_line_shape"),
    ([line(types.MAX_FEN), line(1), line(True)], "invalid_line"),
]


@pytest.mark.parametrize("version", [None, 1])
@pytest.mark.parametrize("opening", [False, True])
@pytest.mark.parametrize("rows,reason", CASES)
def test_lines_error_contract(version, opening, rows, reason):
    with historical_content(version) if version else nullcontext():
        with pytest.raises(KernelError) as failure:
            integrity._lines(rows, "calculation", 17, opening=opening)
    assert failure.value.code == "content_integrity_failed"
    assert str(failure.value) == "已保存的核算内容或来源关系不一致"
    assert failure.value.details == {
        "component": "calculation", "record_id": "17", "reason": reason,
    }


@pytest.mark.parametrize("version", [None, 1])
@pytest.mark.parametrize("opening", [False, True])
def test_lines_valid_shapes_and_final_overflow_match_encoding(version, opening):
    encoding = history_encoding_v1 if version else types
    with historical_content(version) if version else nullcontext():
        assert integrity._lines([], "calculation", 17, opening=opening) == []
        rows = ListChild([DictChild(line(types.MAX_FEN)), DictChild(line(credit=types.MAX_FEN))])
        result = integrity._lines(rows, "calculation", 17, opening=opening)
        assert type(result) is list
        assert result == [("5602", types.MAX_FEN, 0, None), ("5602", 0, types.MAX_FEN, None)]
        assert all(type(item) is tuple for item in result)
        for overflow in (
            [line(types.MAX_FEN + 1), line(credit=1)],
            [line(types.MAX_FEN), line(1), line(credit=types.MAX_FEN), line(credit=1)],
            [line(1), line(credit=types.MAX_FEN), line(credit=1)],
        ):
            with pytest.raises(ValueError) as previous:
                encoding.sum_fen(row["debit"] for row in overflow)
                encoding.sum_fen(row["credit"] for row in overflow)
            with pytest.raises(ValueError) as current:
                integrity._lines(overflow, "calculation", 17, opening=opening)
            assert str(current.value) == str(previous.value)
        cashflow = [line(1, cashflow="operating"), line(credit=1)]
        if opening:
            with pytest.raises(KernelError) as failure:
                integrity._lines(cashflow, "calculation", 17, opening=True)
            assert failure.value.details["reason"] == "invalid_line"
        else:
            assert integrity._lines(cashflow, "calculation", 17)[0][3] == "operating"


@pytest.mark.parametrize("version", [None, 1])
@pytest.mark.parametrize("count", [2, 10, 100])
def test_checked_work_is_only_rows_and_final_totals(version, count, monkeypatch):
    encoding = history_encoding_v1 if version else types
    original, calls = encoding.checked, []

    def counted(value):
        calls.append(value)
        return original(value)

    monkeypatch.setattr(encoding, "checked", counted)
    rows = [line(1) for _ in range(count - 1)] + [line(credit=count - 1)]
    with historical_content(version) if version else nullcontext():
        result = integrity._lines(rows, "calculation", 17)
    assert len(result) == count
    assert len(calls) == 2 * count + 2
    assert calls[-2:] == [count - 1, count - 1]


@pytest.mark.parametrize("version", [None, 1])
def test_general_sum_still_rejects_signed_prefix_overflow(version):
    encoding = history_encoding_v1 if version else types
    with pytest.raises(ValueError):
        encoding.sum_fen([types.MAX_FEN, 1, -1])


@pytest.mark.parametrize("version", [None, 1])
def test_public_source_and_publication_verification(engine, version):
    save(engine)
    _, result = publish(engine)
    ident = result["results"][0]["calculation_id"]
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        with historical_content(version) if version else nullcontext():
            assert integrity.verify_sources(engine, connection, calculation_ids=[ident])["status"] == "verified"
            assert integrity.verify_publication(engine, connection, [ident])["status"] == "verified"


def test_frozen_close_and_complete_verification_keep_totals(engine):
    save(engine)
    publish(engine)
    close(engine)
    assert engine.overview("2026-01")["accounts"][0]["credit"] == 100
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        result = integrity.verify_integrity(engine, connection)
        assert result["status"] == "verified"
        assert result["counts"]["closes"] == 1
        integrity.verify_close_integrity(engine, connection, "2026-01")
    damage(engine, "voucher_version", "UPDATE voucher_version SET total=total+1")
    with engine.store.connection(read_only=True) as connection:
        with pytest.raises(KernelError) as failure:
            integrity.verify_integrity(engine, connection)
    assert failure.value.details["reason"] == "voucher_total_mismatch"
