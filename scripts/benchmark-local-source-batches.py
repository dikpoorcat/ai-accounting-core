"""Compare identical payroll source facts submitted singly and in atomic batches."""

import json
import runpy
import tempfile
from pathlib import Path

from ai_accounting.kernel.engine import PROGRAM_VERSION
from ai_accounting.kernel.service import default_registry


def main():
    shared = runpy.run_path(str(Path(__file__).with_name("benchmark-local-payroll.py")))
    root = Path(tempfile.mkdtemp(prefix="kernel-source-batches-", dir=".tmp"))
    result = {
        "program_version": PROGRAM_VERSION,
        "scales": [],
        "source_sha256": shared["source_hashes"](),
    }
    for size in (100, 500):
        sources = list(shared["policies"]())
        for employee in range(size):
            sources.extend(shared["source_facts"](employee))
        scale, reference = {"employees": size, "facts": len(sources)}, None
        for mode in ("individual", "batch"):
            engine = shared["CountingEngine"](
                shared["CountingStore"].create(
                    root / f"{size}-{mode}.sqlite",
                    default_registry(),
                    "benchmark",
                    "91310000123456789A",
                    "benchmark-db",
                )
            )
            evidence = engine.register_evidence(
                b"Synthetic confirmed payroll sources",
                "text/plain",
                "benchmark",
                request_id="evidence",
            )["digest"]
            records = [
                {
                    "kind": fact.kind,
                    "subject_id": subject,
                    "data": fact.model_dump(mode="json"),
                    "evidence": [evidence],
                    "expected_revision": 0,
                }
                for subject, fact in sources
            ]

            def operation(mode=mode, engine=engine, records=records):
                if mode == "batch":
                    return engine.save_facts(records, request_id="batch")
                return [
                    engine.save_fact(**record, request_id=f"single-{index}")
                    for index, record in enumerate(records)
                ]

            scale[mode], _ = shared["measure"](engine, operation)
            with engine.store.connection(read_only=True) as connection:
                confirmed = [
                    (row[0], row[1].hex())
                    for row in connection.execute(
                        "SELECT subject_id,digest FROM fact_revision ORDER BY subject_id"
                    )
                ]
            if reference is None:
                reference = confirmed
            assert reference == confirmed and len(confirmed) == len(records)
            scale[mode]["same_confirmed_facts"] = True
            print(size, mode, round(scale[mode]["seconds"], 3), flush=True)
        result["scales"].append(scale)
    result["source_sha256_after"] = shared["source_hashes"]()
    Path("docs/local-source-batch-benchmark.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
