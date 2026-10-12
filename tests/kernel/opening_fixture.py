"""An explicit evidenced formation boundary for isolated synthetic companies."""

from ai_accounting.kernel.domains.opening import CATEGORIES, OpeningPackage


def adopt_empty_opening(engine, *, period="2026-01", subject="synthetic-opening"):
    proof = engine.register_evidence(
        "合成公司本期开始记账，期初各类资产、负债及权益均明确为零，资料完整。".encode(),
        "text/plain", "synthetic zero opening confirmation", request_id=subject + "-proof",
    )["digest"]
    fact = OpeningPackage(
        period=period, package_id=subject, counts=dict.fromkeys(CATEGORIES.values(), 0),
        members=(), completeness_confirmed=True,
    )
    engine.save_fact(
        fact.kind, subject, fact.model_dump(mode="json"), evidence=(proof,),
        expected_revision=0, request_id=subject + "-save",
    )
    preview = engine.preview([subject])
    return engine.confirm(
        [subject], preview_digest=preview["digest"], epochs=preview["epochs"],
        request_id=subject + "-adopt",
    )
