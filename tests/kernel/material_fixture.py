"""Explicit disposition of synthetic supporting text, never real business rows."""

from ai_accounting.kernel.materials import Materials


def supporting_text(engine, proof, *, period="2026-01"):
    source_id = "synthetic-support:" + proof
    with engine.store.connection(read_only=True) as connection:
        if connection.execute("SELECT 1 FROM subject WHERE id=?", (source_id,)).fetchone():
            return
        content = (
            connection.execute(
                "SELECT content FROM evidence WHERE digest=?", (bytes.fromhex(proof),)
            )
            .fetchone()[0]
            .decode("utf-8")
        )
    materials = Materials(engine)
    source = materials.receive(
        source_id,
        {
            "period": period,
            "evidence_digest": proof,
            "category": "transactions",
            "purpose": "supporting",
            "supporting_purpose": "测试手工构造的确认依据，不含原始业务表格或待处理业务行",
            "specification": {
                "format": "text",
                "all_pages_reviewed": True,
                "passages": [{"location": "synthetic-confirmation", "page": 1, "excerpt": content}],
            },
        },
        evidence=(proof,),
        expected_revision=0,
        request_id=source_id,
    )
    materials.resolve(
        "synthetic-resolution:" + proof,
        {
            "period": period,
            "source_id": source_id,
            "source_fact_id": source["fact_id"],
            "location": "synthetic-confirmation",
            "treatment": "supporting",
            "reason": "逐项确认该测试说明只作为支持依据，不代表另有业务项目",
        },
        evidence=(proof,),
        expected_revision=0,
        request_id="synthetic-resolution:" + proof,
    )
