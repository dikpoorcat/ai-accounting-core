"""Compare identical payroll source facts submitted singly and in atomic batches."""

import json
import runpy
import tempfile
from pathlib import Path

from ai_accounting.kernel.engine import PROGRAM_VERSION
from ai_accounting.kernel.entities import Entities
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.types import canonical


def main():
    shared = runpy.run_path(str(Path(__file__).with_name("benchmark-local-payroll.py")))
    root = Path(tempfile.mkdtemp(prefix="kernel-source-batches-", dir=".tmp"))
    result = {
        "program_version": PROGRAM_VERSION,
        "scales": [],
        "source_sha256": shared["source_hashes"](),
    }
    for size in (100, 500):
        scale, reference = (
            {"employees": size, "measured_phase": "wage_registration_after_source_setup"},
            None,
        )
        for mode in ("individual", "batch"):
            engine = shared["CountingEngine"](
                shared["CountingStore"].create(
                    root / f"{size}-{mode}.sqlite",
                    production_bundle(),
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
            revisions, logical_employees, sources = {}, {}, []

            def save_source(
                subject, fact, *, engine=engine, evidence=evidence, revisions=revisions
            ):
                result = engine.save_fact(
                    fact.kind,
                    subject,
                    fact.model_dump(mode="json"),
                    evidence=(evidence,),
                    expected_revision=0,
                    request_id="setup-" + subject,
                )
                revisions[subject] = result["revision"]

            for subject, fact in shared["policies"]():
                save_source(subject, fact)
            for employee in range(size):
                identity = Entities(engine).register_entity(
                    "person",
                    {"display_name": f"合成工资员工 {employee:04}"},
                    source="隔离批次性能样本中的明确员工",
                    evidence_digest=evidence,
                    request_id=f"employee-{employee:04}",
                )["entity_id"]
                logical_employees[identity] = f"employee-{employee:04}"
                generator = shared["source_facts"](employee, identity, revisions)
                for _ in range(2):
                    subject, fact = next(generator)
                    save_source(subject, fact)
                sources.extend(generator)
            scale["facts"] = len(sources)
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
                confirmed = []
                for row in connection.execute(
                    "SELECT id,subject_id FROM fact_revision ORDER BY subject_id"
                ):
                    data = engine.store.fact(connection, row["id"]).fact.model_dump(mode="json")
                    if "employee_id" in data:
                        data["employee_id"] = logical_employees[data["employee_id"]]
                    confirmed.append((row["subject_id"], canonical(data)))
            if reference is None:
                reference = confirmed
            assert reference == confirmed
            scale[mode]["same_typed_inputs_after_employee_resolution"] = True
            print(size, mode, round(scale[mode]["seconds"], 3), flush=True)
        result["scales"].append(scale)
    result["source_sha256_after"] = shared["source_hashes"]()
    Path("docs/local-source-batch-benchmark.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
