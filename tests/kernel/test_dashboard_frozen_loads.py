"""Exact frozen batches retain proof scope and reduce real transferred values."""

import hashlib
import json
import math
import sqlite3
from types import SimpleNamespace

import pytest
from test_banking import book as book
from test_banking import close_month, inventories
from test_dashboard_open_groups import expenses, snapshot
from test_integrity_content import damage

from ai_accounting.kernel import settlement_freeze as freeze
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.types import canonical


def test_full_owned_open_batches_exact_blocks_and_keeps_all_decoded_members(
    book, monkeypatch, record_property,
):
    engine, _, _, proof = book
    expenses(book, 70)
    assert close_month(inventories(engine, proof, "2026-09", {"transactions"}), proof, "2026-09")[
        "status"
    ] == "closed"
    decoded, statements = [], []
    original = freeze._decode_block

    def decode(period, header, payload):
        result = original(period, header, payload)
        decoded.append((tuple(header), len(result), len(payload.encode())))
        return result

    monkeypatch.setattr(freeze, "_decode_block", decode)
    with snapshot(engine, "2026-09") as snap:
        scope = freeze._scope(snap.connection, snap.period, current=False, reads=snap.reads)
        headers = scope.base_root["directories"]["open"]
        snap.connection.set_trace_callback(statements.append)
        try:
            sources = freeze.frozen_dashboard_group_sources(
                snap.connection, snap.period, reads=snap.reads,
            )["obligations"]
        finally:
            snap.connection.set_trace_callback(None)
        block_reads = [sql for sql in statements if "settlement_freeze_block" in sql]
        assert len(block_reads) == 1
        assert {item[0] for item in decoded} == {tuple(header) for header in headers}
        assert sum(item[1] for item in decoded) == len(sources) == 70
        assert len(decoded) == len(headers) > 1
        assert len(snap.reads._frozen_settlement_open_entries) == 70
        before = len(decoded)
        for header in headers:
            # The batch uses the exact original decoder, with no range/member
            # checks substituted by a row count or cached directory digest.
            assert freeze._read_block(snap.connection, scope.base_period, header) == (
                snap.reads._frozen_settlement_blocks[scope.base_period, tuple(header)]
            )
        record_property("actual_batch_decoded_blocks", before)
        record_property("actual_batch_decoded_members", sum(item[1] for item in decoded[:before]))
        record_property("actual_batch_payload_bytes", sum(item[2] for item in decoded[:before]))


@pytest.mark.parametrize("exact", [False, True])
def test_local_page_and_exact_keys_never_preload_unrelated_directory_blocks(book, exact):
    engine, _, _, proof = book
    expenses(book, 70)
    close_month(inventories(engine, proof, "2026-09", {"transactions"}), proof, "2026-09")
    with snapshot(engine, "2026-09") as snap:
        scope = freeze._scope(snap.connection, snap.period, current=False, reads=snap.reads)
        directory = scope.base_root["directories"]["open"]
        last = directory[-1]
        selected = freeze._page_entries(
            snap.connection, scope, include_settled=False,
            page_keys={last[2]} if exact else None,
            after=None if exact else last[1], limit=1, reads=snap.reads,
        )
        assert selected
        assert set(snap.reads._frozen_settlement_blocks) == {(scope.base_period, tuple(last))}


def test_failed_complete_block_batch_publishes_no_earlier_success(book):
    engine, _, _, proof = book
    expenses(book, 40)
    close_month(inventories(engine, proof, "2026-09", {"transactions"}), proof, "2026-09")
    with snapshot(engine, "2026-09") as snap:
        scope = freeze._scope(snap.connection, snap.period, current=False, reads=snap.reads)
        digest = scope.base_root["directories"]["open"][-1][4]
    damage(engine, "settlement_freeze_block",
           "UPDATE settlement_freeze_block SET payload='[]' WHERE digest=unhex(?)", (digest,))
    with snapshot(engine, "2026-09") as snap:
        with pytest.raises(KernelError, match="冻结清偿依据不匹配"):
            freeze.frozen_dashboard_group_sources(snap.connection, snap.period, reads=snap.reads)
        assert snap.reads._frozen_settlement_blocks == {}
        assert snap.reads._frozen_settlement_open_entries == {}


def test_state_digest_driver_keeps_indexed_exact_rows_all_bytes_and_fixed_scalar_parse(
    monkeypatch, record_property,
):
    with sqlite3.connect(":memory:") as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE settlement_state_revision("
                           "digest BLOB PRIMARY KEY,obligation_key TEXT,payload TEXT)")
        entries, payloads = {}, {}
        for index in range(128):
            key = f"expense:formal-subject-{index:05}:primary"
            state = freeze._empty_state(key)
            state.update(first_source_period=1, source_event_count=1, source_amount=index)
            state["events"] = [{"unused": n, "body": "x" * 16} for n in range(64)]
            payload = canonical(state)
            digest = hashlib.sha256(payload.encode()).hexdigest()
            connection.execute("INSERT INTO settlement_state_revision VALUES(?,?,?)",
                               (bytes.fromhex(digest), key, payload))
            entries[key], payloads[key] = [digest, 1], payload
        parsed, statements, hashed = [], [], []
        original_parse, original_sha = freeze.from_json, freeze._sha
        original_native = freeze._STATE_SCALAR_VALUES

        def parse(raw, **kwargs):
            assert raw.startswith("["), "state event histories must never be decoded"
            value = original_parse(raw, **kwargs)
            parsed.append((len(value), len(raw.encode())))
            return value

        def parse_native(raw):
            assert raw.startswith("["), "state event histories must never be decoded"
            value = original_native.validate_json(raw)
            parsed.append((len(value), len(raw.encode())))
            return value

        def sha(raw):
            hashed.append(len(raw.encode()))
            return original_sha(raw)

        with monkeypatch.context() as patch:
            patch.setattr(freeze, "from_json", parse)
            patch.setattr(
                freeze, "_STATE_SCALAR_VALUES", SimpleNamespace(validate_json=parse_native),
            )
            patch.setattr(freeze, "_sha", sha)
            connection.set_trace_callback(statements.append)
            try:
                states = freeze._dashboard_state_scalars(
                    connection, SimpleNamespace(base_period=1, overrides={}), entries,
                )
            finally:
                connection.set_trace_callback(None)
        assert states.keys() == entries.keys()
        assert len(parsed) == len(hashed) == 128 and all(item[0] == 18 for item in parsed)
        assert sum(hashed) == sum(len(payload.encode()) for payload in payloads.values())
        sql = next(sql for sql in statements if "FROM json_each" in sql)
        plan = list(connection.execute("EXPLAIN QUERY PLAN " + sql))
        assert any("SEARCH s USING INDEX" in row[3] and "digest=?" in row[3] for row in plan)
        assert not any("SCAN s" in row[3] for row in plan)
        request = canonical([digest for digest, _ in entries.values()])
        previous_request = canonical([[key, digest] for key, (digest, _) in entries.items()])
        assert len(request.encode()) < len(previous_request.encode())
        rows = list(connection.execute(sql))
        assert len(rows) == 128 and all(len(row) == 4 for row in rows)
        record_property("actual_state_hash_bytes", sum(hashed))
        record_property("actual_scalar_array_bytes", sum(item[1] for item in parsed))
        record_property("actual_state_return_rows", len(rows))
        record_property("actual_request_bytes", len(request.encode()))
        record_property("previous_request_bytes", len(previous_request.encode()))


@pytest.mark.parametrize("raw", [
    '[null,true,false,"中文","\\u0000",9223372036854775807,-9223372036854775808]',
    '["\\ud800"]', '["\\udfff"]',
    '[0.1,-0.0,5e-324,1.7976931348623157e308,9e999,-9e999]',
])
def test_bundled_scalar_parser_keeps_original_types_unicode_and_reals(raw):
    expected = json.loads(raw)
    expected = tuple(int(value) if type(value) is bool else value for value in expected)
    actual = freeze._packed_state_values(None, "unused", raw, tuple(range(len(expected))))
    assert actual == expected
    assert tuple(type(value) for value in actual) == tuple(type(value) for value in expected)
    for left, right in zip(actual, expected, strict=True):
        if type(left) is float:
            assert math.copysign(1, left) == math.copysign(1, right)


@pytest.mark.parametrize("raw", ['["unterminated', '[1,]', 'not-json'])
def test_malformed_scalar_array_retains_original_exception(raw):
    with pytest.raises(json.JSONDecodeError):
        freeze._packed_state_values(None, "unused", raw, ("field",))


def test_native_eighteen_scalar_tuple_avoids_python_field_reader(monkeypatch):
    values = [None, True, False, "中文", "\x00", -(1 << 63), (1 << 63) - 1] + ["same"] * 11
    expected = tuple(int(value) if type(value) is bool else value for value in values)

    def not_used(*_args, **_kwargs):
        pytest.fail("ordinary fixed scalars must use the native tuple parser")

    monkeypatch.setattr(freeze, "from_json", not_used)
    actual = freeze._packed_state_values(
        None, "unused", json.dumps(values), freeze._DashboardStateRecord._fields,
    )
    assert actual == expected
    assert tuple(map(type, actual)) == tuple(map(type, expected))


@pytest.mark.parametrize("literal", [
    "null", "true", "false", "1", "-1", "9223372036854775807", "-9223372036854775808",
    "9223372036854775808", "-9223372036854775809", "0.123456789012345678901234", "-0.0",
    "9e999", '"中文"', '"\\u0000"', '"\\ud800"', '[1,true,null]', '{"x":[2,false]}',
])
def test_native_fixed_scalar_tuple_keeps_all_original_coercions_and_exceptions(literal):
    fields = freeze._DashboardStateRecord._fields
    paths = ",".join(f"'$.{name}'" for name in fields)
    with sqlite3.connect(":memory:") as connection:
        connection.execute("CREATE TABLE settlement_state_revision(digest BLOB,payload TEXT)")
        for name in fields:
            state = freeze._empty_state("expense:subject:primary")
            state[name] = "__special__"
            payload = json.dumps(state, ensure_ascii=True).replace('"__special__"', literal)
            digest = hashlib.sha256(payload.encode()).digest()
            connection.execute("DELETE FROM settlement_state_revision")
            connection.execute("INSERT INTO settlement_state_revision VALUES(?,?)",
                               (digest, payload))
            packed = connection.execute(
                f"SELECT json_extract(payload,{paths}) FROM settlement_state_revision",
            ).fetchone()[0]
            outcomes = []
            for standard in (True, False):
                try:
                    outcomes.append(("row", freeze._packed_state_values(
                        connection, digest.hex(), packed, fields, _standard=standard,
                    )))
                except (ValueError, sqlite3.Error, UnicodeError) as error:
                    outcomes.append(("error", type(error), str(error)))
            assert outcomes[0] == outcomes[1], (name, literal)
            if outcomes[0][0] == "row":
                left, right = outcomes[0][1], outcomes[1][1]
                assert tuple(map(type, left)) == tuple(map(type, right))
                for old, new in zip(left, right, strict=True):
                    if type(old) is float:
                        assert math.copysign(1, old) == math.copysign(1, new)
