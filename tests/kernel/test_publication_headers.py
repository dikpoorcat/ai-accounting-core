"""The bounded SQL representation must verify the existing canonical identity."""

import sqlite3

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.publication import CONTENT_FIELDS, verified_period_headers, verify_record
from ai_accounting.kernel.types import digest


def _database(values):
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE calculation_publication("
        "id TEXT,sequence INTEGER,subject_id TEXT,previous_publication_id TEXT,"
        "calculation_id TEXT,mode TEXT,posting_period INTEGER,"
        "baseline_calculation_id TEXT,voucher_id TEXT) STRICT"
    )
    ident = "p_" + digest(values).hex()
    connection.execute(
        "INSERT INTO calculation_publication VALUES(?,?,?,?,?,?,?,?,?)",
        (ident, *(values[field] for field in CONTENT_FIELDS)),
    )
    return connection, ident


def _values(subject, sequence):
    return {
        "sequence": sequence,
        "subject_id": subject,
        "previous_publication_id": None,
        "calculation_id": '计算😀\n\\"',
        "mode": "initial",
        "posting_period": 0,
        "baseline_calculation_id": None,
        "voucher_id": "凭证\x00\b\t\r\f",
    }


@settings(max_examples=80)
@given(
    st.text(st.characters(blacklist_categories=("Cs",)), max_size=120),
    st.integers(min_value=0, max_value=2**63 - 1),
)
def test_period_headers_match_canonical_unicode_control_and_integer_encoding(subject, sequence):
    values = _values(subject, sequence)
    connection, ident = _database(values)
    try:
        verify_record({"id": ident, **values})
        assert list(verified_period_headers(connection, [0])) == [
            {"id": ident, "posting_period": 0, "sequence": sequence}
        ]
    finally:
        connection.close()


@pytest.mark.parametrize("field", CONTENT_FIELDS)
def test_period_headers_reject_damage_to_every_hashed_field(field):
    values = _values("业务", 1)
    connection, _ = _database(values)
    try:
        value = values[field]
        replacement = value + 1 if type(value) is int else "changed"
        connection.execute(f"UPDATE calculation_publication SET {field}=?", (replacement,))
        selected_period = replacement if field == "posting_period" else 0
        with pytest.raises(KernelError) as error:
            list(verified_period_headers(connection, [selected_period]))
        assert error.value.code == "content_integrity_failed"
    finally:
        connection.close()


@settings(max_examples=50)
@given(
    st.lists(
        st.tuples(
            st.integers(0, 3),
            st.sampled_from(("bank", "cash", "跨月\n😀")),
            st.text(st.characters(blacklist_categories=("Cs",)), max_size=30),
            st.integers(-(2**63), 2**63 - 1),
            st.booleans(),
        ),
        max_size=30,
    )
)
def test_sql_balance_seals_match_independent_full_row_encoder(items):
    from ai_accounting.kernel.period_balances import COLUMNS, _selected_seals, expected_seals

    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(
            "CREATE TABLE period_balance(publication_id TEXT,posting_period INTEGER,"
            "category TEXT,balance_key TEXT,component TEXT,amount INTEGER,"
            "calculation_id TEXT,source_digest BLOB,baseline_calculation_id TEXT,"
            "baseline_digest BLOB) STRICT"
        )
        rows = [
            (
                f"p_{index}",
                month,
                category,
                key,
                "activity",
                amount,
                f"calculation-{index}",
                digest([index]),
                "baseline" if baseline else None,
                digest("baseline") if baseline else None,
            )
            for index, (month, category, key, amount, baseline) in enumerate(items)
        ]
        connection.executemany(
            f"INSERT INTO period_balance({','.join(COLUMNS)}) VALUES(?,?,?,?,?,?,?,?,?,?)",
            reversed(rows),
        )
        assert _selected_seals(connection, "b.posting_period<=?", (2,)) == expected_seals(
            [row for row in rows if row[1] <= 2]
        )
    finally:
        connection.close()


@settings(max_examples=50)
@given(
    st.lists(
        st.tuples(
            st.one_of(st.none(), st.text(st.characters(blacklist_categories=("Cs",)), max_size=30)),
            st.one_of(st.none(), st.integers(-(2**63), 2**63 - 1)),
        ),
        max_size=25,
    )
)
def test_sql_settlement_seals_match_full_encoder_including_empty_periods(items):
    from ai_accounting.kernel.settlement_projection import _COLUMNS, _read_seals, _sealed

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        connection.execute(
            "CREATE TABLE calculation_publication(id TEXT,posting_period INTEGER,sequence INTEGER)"
        )
        connection.execute(
            "CREATE TABLE settlement_change(publication_id TEXT,item_no INTEGER,"
            "posting_period INTEGER,obligation_key TEXT,source_subject_id TEXT,"
            "category TEXT,account TEXT,counterparty_id TEXT,"
            "component TEXT,change_kind TEXT,amount INTEGER,state TEXT,source_calculation_id TEXT,"
            "source_digest BLOB) STRICT"
        )
        rows = [
            (
                f"p_{index // 3}",
                index % 3,
                index // 3 % 3,
                key,
                "subject",
                "payable",
                "2202",
                key,
                "净薪\n😀",
                "payment",
                amount,
                "resolved" if amount is not None else "unresolved",
                f"calculation-{index}",
                digest([index]),
            )
            for index, (key, amount) in enumerate(items)
        ]
        publications = {(row[0], row[2], index // 3) for index, row in enumerate(rows)}
        connection.executemany(
            "INSERT INTO calculation_publication VALUES(?,?,?)", sorted(publications, reverse=True)
        )
        connection.executemany(
            f"INSERT INTO settlement_change({','.join(_COLUMNS)}) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            reversed(rows),
        )
        assert _read_seals(connection, [0, 1, 2, 3]) == _sealed(
            connection, sorted(rows, key=lambda row: (row[2], row[0], row[1])), [0, 1, 2, 3]
        )
    finally:
        connection.close()
