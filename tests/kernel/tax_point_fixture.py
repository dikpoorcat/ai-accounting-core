"""Explicit fictional contractual tax-point facts for accounting scenarios."""

from material_fixture import supporting_text

from ai_accounting.kernel.tax_point_basis import TaxPointBasis
from ai_accounting.kernel.types import canonical, digest


def contract_tax_basis(engine, kind, subject, *, business_period, contract_period,
                       contract_date=None, reviewed_through=None):
    """The test supplies a contractual month; no management date is converted."""
    reviewed_through = reviewed_through or max(business_period, contract_period)
    original = {
        "business_kind": kind, "business_id": subject,
        "contract_payment_period": contract_period,
        "contract_payment_date": contract_date,
        "reviewed_through_period": reviewed_through,
        "earlier_invoice_and_receipt_candidates": [],
        "service_pattern": "single",
    }
    key = digest(original).hex()
    proof = engine.register_evidence(
        canonical(original).encode(), "text/plain", "合成合同付款及税点核查事实",
        request_id="contract-original-" + key,
    )["digest"]
    supporting_text(engine, proof, period=reviewed_through)
    source_id = "synthetic-support:" + proof
    with engine.store.connection(read_only=True) as connection:
        fact_id = connection.execute(
            "SELECT fact_id FROM fact_current WHERE subject_id=?", (source_id,),
        ).fetchone()[0]
        existing = connection.execute(
            "SELECT f.revision FROM fact_current c JOIN fact_revision f ON f.id=c.fact_id "
            "WHERE c.subject_id=?", ("tax-basis:" + subject,),
        ).fetchone()
        version = (
            engine.store.current_fact(connection, "tax-basis:" + subject) if existing else None
        )
    source = {
        "kind": "material_source_v2", "subject_id": source_id, "fact_id": fact_id,
        "evidence_digest": proof, "location": "synthetic-confirmation",
    }
    data = {
        "period": reviewed_through, "business_kind": kind, "business_id": subject,
        "service_pattern": "single", "written_contract_status": "payment_date_set",
        "contract_payment_period": contract_period, "contract_payment_date": contract_date,
        "contract_source": source, "reviewed_through_period": reviewed_through,
        "earlier_candidates_reviewed": True, "review_source": source,
    }
    expected = TaxPointBasis.model_validate_json(canonical(data))
    if version is not None and version.fact == expected and proof in version.evidence:
        return {"status": "reused", "subject_id": version.subject_id,
                "fact_id": version.id, "revision": version.revision}
    revision = existing[0] if existing else 0
    return engine.save_fact(
        "tax_point_basis", "tax-basis:" + subject, data,
        evidence=(proof,), expected_revision=revision,
        request_id=f"contract-basis-{key}-from-{revision}",
    )


def sourced_tax_data(engine, kind, subject, data):
    """Translate only a scenario's explicit contractual month, never fill a missing one."""
    data = dict(data)
    month = data.pop("tax_obligation_period", None)
    day = data.pop("tax_obligation_date", None)
    if month is not None:
        contract_tax_basis(
            engine, kind, subject, business_period=data["period"],
            contract_period=month, contract_date=day,
        )
    return data
