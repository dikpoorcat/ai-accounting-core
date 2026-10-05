"""Open money authority stays bounded after unrelated frozen history grows."""

import json

from stage9_metrics import measure_work
from test_engine import close, publish, save
from test_engine import engine as engine_fixture

from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.types import YearMonth


def test_open_source_guard_does_not_decode_or_scan_unrelated_frozen_history(tmp_path):
    measurements = []
    cutoff = YearMonth("2026-02").ordinal
    for count in (1, 80):
        directory = tmp_path / str(count)
        directory.mkdir()
        engine = engine_fixture.__wrapped__(directory)
        subjects = [f"frozen-{index}" for index in range(count)]
        for subject in subjects:
            save(engine, subject=subject, request=subject)
        publish(engine, subjects)
        close(engine)
        save(engine, subject="open", period="2026-02", amount=250, request="open-fact")
        publish(engine, ["open"], request="open-publish")

        def read(engine=engine):
            with QueryReads.snapshot(engine) as reads:
                reads.verify_open_voucher_scope(cutoff)
                # A narrow consumer in the same snapshot can use this complete
                # authority check; this never proves a body or frozen adoption.
                reads.verify_open_voucher_scope(cutoff, posting_period=cutoff)
                return [tuple(row) for row in reads.connection.execute(
                    "SELECT v.period,l.account,l.debit,l.credit FROM voucher_current h "
                    "JOIN voucher_version v ON v.id=h.version_id "
                    "JOIN voucher_line l ON l.version_id=v.id WHERE v.period=? "
                    "ORDER BY l.line_no", (cutoff,),
                )]

        work, actual = measure_work(engine, read)
        assert actual == [(cutoff, "5602", 250, 0), (cutoff, "2202", 0, 250)]
        counters = work["counters"]
        assert counters["calculation_result_rows_loaded"] == 0
        assert counters["calculation_result_json_decodes"] == 0
        measurements.append(counters)
    small, large = measurements
    print(json.dumps({"frozen_businesses": [1, 80], "work": measurements}))
    assert large["returned_rows"] == small["returned_rows"]
    assert large["returned_value_bytes"] == small["returned_value_bytes"]
    # Indexed seeks may add B-tree steps; they must not scan eighty unrelated
    # current heads, calculations, or their historical bodies.
    assert large.get("sqlite_vm_steps", 0) <= small.get("sqlite_vm_steps", 0) + 400
