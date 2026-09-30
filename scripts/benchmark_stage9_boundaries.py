"""Stage 9 production-entry size boundaries on a fresh synthetic company.

The default calibration is deliberately small. Pass --full only for the
exclusive, final 20 MiB / 100,000 bank rows / 5,000 facts run.
"""

from __future__ import annotations

import argparse
import ctypes
import gc
import json
import random
import threading
import time
import uuid
from pathlib import Path

from stage9_source import configure_source, require_source_module, synthetic_path, workspace_root

MIB = 1024 * 1024


def _rss_bytes() -> int:
    class Counters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("faults", ctypes.c_ulong)] + [
            (name, ctypes.c_size_t)
            for name in (
                "peak",
                "working",
                "peak_paged",
                "paged",
                "peak_nonpaged",
                "nonpaged",
                "pagefile",
                "peak_pagefile",
            )
        ]

    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    kernel = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    psapi = ctypes.WinDLL("Psapi.dll", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
    if not psapi.GetProcessMemoryInfo(
        kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb
    ):
        raise ctypes.WinError()
    return counters.working


def _measure(operation):
    gc.collect()
    before = _rss_bytes()
    peak = [before]
    stop = threading.Event()

    def sample():
        while not stop.wait(0.01):
            peak[0] = max(peak[0], _rss_bytes())

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    started = time.perf_counter()
    try:
        result = operation()
    finally:
        elapsed = time.perf_counter() - started
        peak[0] = max(peak[0], _rss_bytes())
        stop.set()
        sampler.join()
    return {
        "seconds": elapsed,
        "rss_before_bytes": before,
        "sampled_peak_rss_bytes": peak[0],
    }, result


def _counts(engine):
    with engine.store.connection(read_only=True) as connection:
        return {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "evidence",
                "fact_revision",
                "fact_bank_statement_entries",
                "request",
                "audit",
            )
        }


def _assert_unchanged(engine, before):
    after = _counts(engine)
    if after != before:
        raise AssertionError(f"Rejected operation changed physical rows: {before} -> {after}")


def _evidence_size(engine, evidence_digest):
    with engine.store.connection(read_only=True) as connection:
        row = connection.execute(
            "SELECT length(content) FROM evidence WHERE digest=?",
            (bytes.fromhex(evidence_digest),),
        ).fetchone()
    return None if row is None else row[0]


def _bank_csv(rows: int) -> bytes:
    header = "日期,银行流水号,账户,对方,金额,余额,摘要,币种\n"
    body = "".join(
        f"2026-03-01,txn-{index},bank-main,synthetic-party,1.23,100000.00,receipt,CNY\n"
        for index in range(rows)
    )
    total = (
        f"2026-03-31,total,bank-main,synthetic-party,"
        f"{rows * 123 // 100}.{rows * 123 % 100:02d},"
        "100000.00,control-total,CNY\n"
    )
    return (header + body + total).encode("utf-8")


def _specification(rows):
    from ai_accounting.kernel.materials import Column, Specification

    return Specification(
        format="csv",
        columns=tuple(
            Column(
                column=letter,
                role="amount"
                if letter == "E"
                else "recognition_period"
                if letter == "A"
                else "context",
            )
            for letter in "ABCDEFGH"
        ),
        total_rows={"CSV": (rows + 2,)},
    )


def _bank_boundary(engine, rows, *, full):
    from ai_accounting.kernel.contracts import KernelError
    from ai_accounting.kernel.entities import Entities
    from ai_accounting.kernel.materials import Materials, inspect_bytes

    raw = _bank_csv(rows)
    evidence = engine.register_evidence(raw, "text/csv", "合成银行原行", request_id="bank-source")[
        "digest"
    ]
    spec = _specification(rows)
    api = Materials(engine)
    public, page = _measure(lambda: api.inspect(evidence, spec.model_dump(mode="json")))
    parsed, result = _measure(lambda: inspect_bytes(raw, spec))
    if page["status"] != "ready" or page["summary"]["item_count"] != rows:
        raise AssertionError("Public bank material inspection did not accept every row")
    if (
        len(result["items"]) != rows
        or result["issues"]
        or sum(x["amount_fen"] for x in result["items"]) != rows * 123
    ):
        raise AssertionError("Bank original-row parsing did not match the typed registration")
    account = Entities(engine).register_entity(
        "fund_account",
        account_type="bank",
        data={},
        source="合成银行账户",
        request_id="bank-account",
    )["entity_id"]
    entries = [
        {"reference": f"txn-{index}", "actual_date": "2026-03-01", "signed_fen": item["amount_fen"]}
        for index, item in enumerate(result["items"])
    ]
    before_save = _counts(engine)
    saved, response = _measure(
        lambda: engine.save_fact(
            "bank_statement",
            "bank-statement",
            {
                "period": "2026-03",
                "bank_account_id": account,
                "opening_fen": 0,
                "closing_fen": rows * 123,
                "entries": entries,
            },
            evidence=(evidence,),
            expected_revision=0,
            request_id="bank-statement",
        )
    )
    after_save = _counts(engine)
    if response["status"] != "confirmed" or after_save["fact_bank_statement_entries"] != rows:
        raise AssertionError("Bank child table did not persist every original row")
    del result
    over_limit = {"rows": 100_001, "status": "not_run"}
    if full:
        over_raw = _bank_csv(100_001)
        over_evidence = engine.register_evidence(
            over_raw, "text/csv", "超界合成银行原行", request_id="bank-over-source"
        )["digest"]
        before = _counts(engine)
        rejected, _ = _measure(
            lambda: _expect_kernel_error(
                lambda: api.inspect(over_evidence, _specification(100_001).model_dump(mode="json")),
                KernelError,
                "material_too_large",
            )
        )
        _assert_unchanged(engine, before)
        over_limit = {
            "rows": 100_001,
            "status": "rejected",
            "inspect": rejected,
            "registration_attempted": False,
            "physical_counts_after_rejection": before,
        }
    return {
        "rows": rows,
        "csv_bytes": len(raw),
        "public_inspect": public,
        "full_parse": parsed,
        "typed_save": saved,
        "physical_entries": rows,
        "typed_save_physical_delta": {
            name: after_save[name] - before_save[name]
            for name in ("fact_revision", "fact_bank_statement_entries", "request", "audit")
        },
        "over_limit": over_limit,
    }


def _expect_kernel_error(operation, error_type, code):
    try:
        operation()
    except error_type as exc:
        if exc.code != code:
            raise AssertionError(f"Expected {code}, received {exc.code}") from exc
        return
    raise AssertionError(f"Expected {code}")


def _evidence_boundary(engine, size, *, full):
    from ai_accounting.kernel.contracts import KernelError

    content = random.Random(20260911).randbytes(size)
    before = _counts(engine)
    accepted, response = _measure(
        lambda: engine.register_evidence(
            content, "application/octet-stream", "合成容量依据", request_id="size-proof"
        )
    )
    if (
        response["status"] != "registered"
        or _counts(engine)["evidence"] != before["evidence"] + 1
        or _evidence_size(engine, response["digest"]) != size
    ):
        raise AssertionError("Evidence was not physically stored")
    over_limit = {"bytes": 20 * MIB + 1, "status": "not_run"}
    if full:
        before = _counts(engine)
        rejected, _ = _measure(
            lambda: _expect_kernel_error(
                lambda: engine.register_evidence(
                    b"x" * (20 * MIB + 1),
                    "application/octet-stream",
                    "超界",
                    request_id="over-proof",
                ),
                KernelError,
                "evidence_too_large",
            )
        )
        _assert_unchanged(engine, before)
        over_limit = {"bytes": 20 * MIB + 1, "status": "rejected", "measurement": rejected}
    return {
        "accepted_bytes": size,
        "accepted": accepted,
        "over_limit": over_limit,
        "digest": response["digest"],
    }


def _batch_boundary(engine, evidence, count, *, full):
    from ai_accounting.kernel.entities import Entities

    entities = Entities(engine)
    cash = entities.register_entity(
        "fund_account",
        account_type="cash",
        data={},
        source="合成现金账户",
        request_id="cash-account",
    )["entity_id"]
    # Each typed receipt has a distinct real amount. One registered owner is
    # sufficient: the complete actual-money coordinates differ per business.
    owner = entities.register_entity(
        "person", data={}, source="合成出资人", request_id="funding-owner"
    )["entity_id"]

    def records(n):
        return [
            {
                "kind": "cash_funding",
                "subject_id": f"funding-{index}",
                "expected_revision": 0,
                "evidence": [evidence],
                "data": {
                    "period": "2026-03",
                    "owner_id": owner,
                    "amount_fen": 100 + index,
                    "funding_kind": "capital",
                    "actual_date": "2026-03-01",
                    "cash_account_id": cash,
                },
            }
            for index in range(n)
        ]

    before_save = _counts(engine)
    accepted, response = _measure(
        lambda: engine.save_facts(records(count), request_id="valid-batch")
    )
    after = _counts(engine)
    if (
        len(response["results"]) != count
        or after["fact_revision"] != before_save["fact_revision"] + count
    ):
        raise AssertionError("Typed batch did not persist every legal fact")
    over_limit = {"facts": 5001, "status": "not_run"}
    if full:
        rejected, _ = _measure(
            lambda: _expect_value_error(
                lambda: engine.save_facts(records(5001), request_id="over-batch")
            )
        )
        _assert_unchanged(engine, after)
        over_limit = {
            "facts": 5001,
            "status": "rejected",
            "measurement": rejected,
            "physical_counts_after_rejection": after,
        }
    return {
        "accepted_facts": count,
        "accepted": accepted,
        "physical_fact_delta": count,
        "physical_request_delta": after["request"] - before_save["request"],
        "physical_audit_delta": after["audit"] - before_save["audit"],
        "over_limit": over_limit,
    }


def _expect_value_error(operation):
    try:
        operation()
    except ValueError:
        return
    raise AssertionError("Expected ValueError for over-limit batch")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--calibration-rows", type=int, default=3)
    parser.add_argument("--calibration-facts", type=int, default=3)
    args = parser.parse_args()
    workspace = workspace_root(Path(__file__).resolve().parents[1], args.workspace)
    source = configure_source(args.source, workspace)
    if Path(__file__).resolve() != (source / "scripts/benchmark_stage9_boundaries.py").resolve():
        raise ValueError("Boundary harness must run from the selected fixed source")
    root = synthetic_path(
        args.root or workspace / ".tmp" / f"stage9-boundaries-{uuid.uuid4().hex[:12]}", workspace
    )
    root.mkdir(exist_ok=False)

    from ai_accounting.kernel import engine as engine_module
    from ai_accounting.kernel.catalog import Catalog
    from ai_accounting.kernel.engine import Engine

    require_source_module(engine_module, source, "src/ai_accounting/kernel/engine.py")
    catalog = Catalog(root)
    company = catalog.create_company("91310000123456789A", "合成边界验证")
    engine = Engine(catalog.bind(company["id"]))
    if args.calibration_rows < 1 or args.calibration_facts < 1:
        raise ValueError("Calibration sizes must be positive")
    report = {
        "source": str(source),
        "workspace": str(workspace),
        "root": str(root),
        "mode": "full" if args.full else "calibration",
        "note": (
            "Sampled RSS includes this test harness; calibration is not a "
            "boundary acceptance result."
        ),
    }
    report["evidence"] = _evidence_boundary(engine, 20 * MIB if args.full else 1024, full=args.full)
    report["bank_rows"] = _bank_boundary(
        engine, 100_000 if args.full else args.calibration_rows, full=args.full
    )
    report["batch"] = _batch_boundary(
        engine,
        report["evidence"]["digest"],
        5000 if args.full else args.calibration_facts,
        full=args.full,
    )
    report["final_physical_counts"] = _counts(engine)
    output = root / "report.json"
    with output.open("x", encoding="utf-8") as file:
        json.dump(report, file, ensure_ascii=False, indent=2)
    print(output)


if __name__ == "__main__":
    main()
