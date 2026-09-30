"""Instrument actual read work, separately from uninstrumented browser timing."""

from __future__ import annotations

import hashlib
import json
import sys
import time
import tracemalloc
from collections import Counter, defaultdict
from contextlib import contextmanager

from ai_accounting.kernel.contracts import Fact

DECODE_COUNTERS = {
    "stdlib_json_loads": (
        "Successful json.loads calls only; excludes Pydantic and pydantic-core JSON parsing."
    ),
    "typed_fact_json_decodes": (
        "Successful Fact.model_validate_json calls, including Store.fact/facts and other actual "
        "typed validations; repeated cache hits do not count."
    ),
    "raw_fact_json_decodes": (
        "Successful pydantic-core from_json calls on same-snapshot stored raw fact bytes."
    ),
    "report_contribution_json_decodes": (
        "Successful report_open_contribution._decode calls, independent of the JSON parser; "
        "same-snapshot contribution cache hits do not count."
    ),
    "calculation_result_json_decodes": (
        "Successful json.loads calls matching the byte length and SHA256 of calculation.outcome "
        "returned by instrumented SQL; raw outcomes are not retained, and results from other "
        "sources are outside this count."
    ),
    "adoption_accounting_slice_reads": (
        "Successful close_storage.read_accounting calls; nested adopted rows are counted "
        "separately and a QueryReads cache hit does not count."
    ),
    "adoption_section_reads": (
        "Successful close_storage.read_section calls for adopted_results; may overlap a "
        "full-manifest read."
    ),
    "full_close_manifest_decodes": (
        "Successful close_storage.decode_close calls; overlap with its section reads and must "
        "not be added to them."
    ),
    "material_original_parses": (
        "Successful materials.inspect_bytes calls on original bytes; successful "
        "inspection-cache hits do not count."
    ),
}


def _value_bytes(value):
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    if isinstance(value, (bytes, bytearray, memoryview)):
        return len(value)
    return 8


class _Cursor:
    def __init__(self, cursor, counter, statement_counter, outcome_payloads):
        self.cursor, self.counter, self.statement_counter = cursor, counter, statement_counter
        self.outcome_payloads = outcome_payloads
        self.outcome_columns = tuple(
            index for index, column in enumerate(cursor.description or ()) if column[0] == "outcome"
        )

    def _read(self, operation, *args):
        before = self.counter["sqlite_vm_steps"]
        started = time.perf_counter()
        try:
            return operation(*args)
        finally:
            self.statement_counter["sqlite_ms"] += (time.perf_counter() - started) * 1000
            self.statement_counter["sqlite_vm_steps"] += self.counter["sqlite_vm_steps"] - before

    def _row(self, row):
        if row is not None:
            for index in self.outcome_columns:
                if isinstance(row[index], str):
                    raw = row[index].encode("utf-8")
                    self.outcome_payloads.add((len(raw), hashlib.sha256(raw).digest()))
            size = sum(_value_bytes(v) for v in row)
            self.counter["returned_rows"] += 1
            self.counter["returned_value_bytes"] += size
            self.statement_counter["returned_rows"] += 1
            self.statement_counter["returned_value_bytes"] += size
        return row

    def __iter__(self):
        return self

    def __next__(self):
        return self._row(self._read(next, self.cursor))

    def fetchone(self):
        return self._row(self._read(self.cursor.fetchone))

    def fetchall(self):
        return [self._row(row) for row in self._read(self.cursor.fetchall)]

    def fetchmany(self, *args):
        return [self._row(row) for row in self._read(self.cursor.fetchmany, *args)]

    def __getattr__(self, name):
        return getattr(self.cursor, name)


class _Connection:
    def __init__(self, connection, counts, statements, work, outcome_payloads):
        self.connection, self.counts, self.statements = connection, counts, statements
        self.work = work
        self.outcome_payloads = outcome_payloads

    def execute(self, statement, parameters=()):
        self.counts["sql_calls"] += 1
        key = " ".join(statement.split())
        self.statements[key] += 1
        work = self.work[key]
        before = self.counts["sqlite_vm_steps"]
        started = time.perf_counter()
        try:
            cursor = self.connection.execute(statement, parameters)
        finally:
            work["sqlite_ms"] += (time.perf_counter() - started) * 1000
            work["sqlite_vm_steps"] += self.counts["sqlite_vm_steps"] - before
        return _Cursor(cursor, self.counts, work, self.outcome_payloads)

    def __getattr__(self, name):
        return getattr(self.connection, name)


def measure_work(engine, operation):
    """Return counters and diagnostic time; never label this as page latency.

    SQL/rows cover application queries after connection validation. VM steps are
    sampled every 100 instructions. Bytes are values delivered to Python, not
    SQLite page-cache or disk reads. Peak memory includes instrumentation state,
    but no retained result bodies; it is not production RSS.
    """
    counts, statements, calls = Counter(), Counter(), Counter()
    outcome_payloads = set()
    statement_work = defaultdict(Counter)
    original_connection = engine.store.connection
    original_profile = sys.getprofile()
    original_loads = json.loads

    @contextmanager
    def counted_connection(*, read_only=False):
        with original_connection(read_only=read_only) as connection:

            def progress():
                counts["sqlite_vm_steps"] += 100
                return 0

            connection.set_progress_handler(progress, 100)
            try:
                yield _Connection(connection, counts, statements, statement_work, outcome_payloads)
            finally:
                connection.set_progress_handler(None, 0)

    def counted_loads(value, *args, **kwargs):
        decoded = original_loads(value, *args, **kwargs)
        counts["stdlib_json_loads"] += 1
        counts["stdlib_json_input_bytes"] += _value_bytes(value)
        raw = value.encode("utf-8") if isinstance(value, str) else value
        if isinstance(raw, bytes) and (len(raw), hashlib.sha256(raw).digest()) in outcome_payloads:
            counts["calculation_result_json_decodes"] += 1
            counts["calculation_result_input_bytes"] += _value_bytes(value)
        return decoded

    def profile(frame, event, arg):
        module = frame.f_globals.get("__name__", "")
        name = frame.f_code.co_name
        if event == "call" and module.startswith("ai_accounting."):
            name = frame.f_code.co_qualname
            calls[module + ":" + name] += 1
        elif event == "return" and module == "pydantic.main" and name == "model_validate_json":
            if isinstance(arg, Fact):
                counts["typed_fact_json_decodes"] += 1
                counts["typed_fact_json_input_bytes"] += _value_bytes(frame.f_locals["json_data"])
        elif event == "c_return" and module == "ai_accounting.kernel.storage":
            if name == "_snapshot_fact_raws" and getattr(arg, "__name__", "") == "from_json":
                counts["raw_fact_json_decodes"] += 1
        elif event == "return" and module == "ai_accounting.kernel.report_open_contribution":
            if name == "_decode" and isinstance(arg, dict):
                counts["report_contribution_json_decodes"] += 1
                counts["report_contribution_json_input_bytes"] += _value_bytes(
                    frame.f_locals["content"]
                )
        elif event == "return" and module in {
            "ai_accounting.kernel.close_storage",
            "ai_accounting.kernel.close_storage_v1",
        }:
            if name == "read_accounting" and arg is not None:
                counts["adoption_accounting_slice_reads"] += 1
                counts["adoption_accounting_rows"] += len(arg.adopted_results)
            elif (
                name == "read_section"
                and frame.f_locals.get("name") == "adopted_results"
                and isinstance(arg, (list, tuple))
            ):
                counts["adoption_section_reads"] += 1
                counts["adoption_section_rows"] += len(arg)
            elif name == "decode_close" and isinstance(arg, dict):
                counts["full_close_manifest_decodes"] += 1
        elif event == "return" and module == "ai_accounting.kernel.materials":
            if name == "inspect_bytes" and isinstance(arg, dict):
                counts["material_original_parses"] += 1
                counts["material_original_input_bytes"] += len(frame.f_locals["raw"])
                counts["material_parsed_items"] += len(arg.get("items", ()))

    engine.store.connection = counted_connection
    json.loads = counted_loads
    tracemalloc.start()
    started = time.perf_counter()
    try:
        sys.setprofile(profile)
        result = operation()
    finally:
        sys.setprofile(original_profile)
        elapsed = (time.perf_counter() - started) * 1000
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        json.loads = original_loads
        engine.store.connection = original_connection
    for name in DECODE_COUNTERS:
        counts.setdefault(name, 0)
    return {
        "instrumented_ms": elapsed,
        "python_peak_bytes": peak,
        "decode_counter_definitions": DECODE_COUNTERS,
        "counters": dict(counts),
        "sql": [
            {"statement": statement, "calls": count, **statement_work[statement]}
            for statement, count in statements.most_common()
        ],
        "kernel_function_calls": dict(sorted(calls.items())),
    }, result
