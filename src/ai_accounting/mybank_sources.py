"""Persist typed payment facts read by code from immutable original evidence.

These are payment preparation facts. Importing them does not amend a closed
payroll calculation, create a voucher, or assert that a payment has occurred.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from openpyxl import load_workbook
from sqlalchemy import select

from .material_reader import _xlsx_numeric_text, amount_fen, verify_material_bytes
from .material_service import lock_material_company
from .models import Evidence, MybankPaymentSourceVersion, Organization
from .mybank_export import canonical_hash

REGISTER_COLUMNS = {
    "报税工资": "tax_reported_salary_fen",
    "未报税劳务": "untaxed_labor_fen",
    "回扣报销1": "rebate_1_fen",
    "回扣报销2": "rebate_2_fen",
    "发票报销": "invoice_fen",
}


def _tables(raw, suffix):
    if suffix == ".xls":
        import xlrd

        book = xlrd.open_workbook(file_contents=raw)
        try:
            return [(s.name, [s.row_values(r) for r in range(s.nrows)]) for s in book.sheets()]
        finally:
            book.release_resources()
    if suffix == ".xlsx":
        book = load_workbook(io.BytesIO(raw), data_only=False)
        try:
            if any(c.data_type == "f" for s in book for row in s for c in row):
                raise ValueError("MYBANK_SOURCE_FORMULA_NOT_ALLOWED")
            numeric = _xlsx_numeric_text(raw)
            return [
                (
                    s.title,
                    [
                        [
                            numeric[(s.title, c.coordinate)]
                            if type(c.value) in (int, float)
                            else c.value
                            for c in row
                        ]
                        for row in s
                    ],
                )
                for s in book
            ]
        finally:
            book.close()
    if suffix == ".csv":
        return [("CSV", list(csv.reader(io.StringIO(raw.decode("utf-8-sig")))))]
    if suffix == ".md":
        rows = []
        for line in raw.decode("utf-8-sig").splitlines():
            if not line.strip().startswith("|"):
                continue
            row = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if all(set(cell) <= set("-: ") for cell in row):
                continue
            rows.append(row)
        return [("负责人原表", rows)]
    raise ValueError("MYBANK_SOURCE_FORMAT_UNSUPPORTED")


def read_payment_source(raw, suffix, kind, period):
    """Recognized original columns only; cumulative paid tax is not current tax."""
    output = []
    seen = set()
    for sheet, rows in _tables(raw, suffix):
        rows = [r for r in rows if any(v not in (None, "") for v in r)]
        if not rows:
            continue
        headers = [str(v or "").strip() for v in rows[0]]
        required = (
            ["姓名", "税款所属期起", "税款所属期止", "本期收入", "应补(退)税额"]
            if kind == "actual_tax"
            else ["姓名", "月份", *REGISTER_COLUMNS]
        )
        if any(headers.count(h) != 1 for h in required):
            raise ValueError("MYBANK_SOURCE_COLUMNS_MISSING_OR_DUPLICATE")
        columns = {h: headers.index(h) for h in required}
        for number, values in enumerate(rows[1:], 2):

            def value(label, columns=columns, values=values):
                index = columns[label]
                return values[index] if index < len(values) else None

            name = str(value("姓名") or "").strip()
            if not name or name in {"合计", "总计"}:
                raise ValueError("MYBANK_SOURCE_PERSON_REQUIRED")
            if kind == "actual_tax":
                start, end = str(value("税款所属期起")), str(value("税款所属期止"))
                if start[:7] != period or end[:7] != period:
                    raise ValueError("MYBANK_SOURCE_PERIOD_CONFLICT")
                fields = {
                    "tax_reported_salary_fen": value("本期收入"),
                    "actual_individual_income_tax_fen": value("应补(退)税额"),
                }
            else:
                month = str(value("月份") or "").strip()
                if month not in {period, f"{int(period[5:])}月"}:
                    raise ValueError("MYBANK_SOURCE_PERIOD_CONFLICT")
                fields = {field: value(label) for label, field in REGISTER_COLUMNS.items()}
            if name in seen:
                raise ValueError("MYBANK_SOURCE_DUPLICATE_PERSON")
            seen.add(name)
            row = {"name": name, "location": f"{sheet}!行{number}"}
            for field, raw_value in fields.items():
                if raw_value is None or str(raw_value).strip() == "":
                    raise ValueError(f"MYBANK_SOURCE_AMOUNT_MISSING:{name}:{field}")
                else:
                    number_fen = amount_fen(raw_value)
                if number_fen < 0:
                    raise ValueError(f"MYBANK_SOURCE_NEGATIVE_AMOUNT:{name}:{field}")
                row[field] = number_fen
            output.append(row)
    if not output:
        raise ValueError("MYBANK_SOURCE_EMPTY")
    return sorted(output, key=lambda row: row["name"])


class MybankPaymentSourceService:
    def __init__(self, session):
        self.session = session

    def latest(self, org_id, period, kind):
        row = self.session.scalar(
            select(MybankPaymentSourceVersion)
            .where(
                MybankPaymentSourceVersion.org_id == org_id,
                MybankPaymentSourceVersion.payroll_period == period,
                MybankPaymentSourceVersion.source_kind == kind,
            )
            .order_by(MybankPaymentSourceVersion.revision.desc())
            .limit(1)
        )
        if row:
            evidence = self.session.get(Evidence, row.evidence_id)
            if (
                evidence is None
                or evidence.org_id != org_id
                or evidence.sha256 != row.evidence_sha256
            ):
                raise ValueError("MYBANK_SOURCE_EVIDENCE_CHANGED")
            verify_material_bytes(evidence)
        return row

    def import_source(self, request):
        lock_material_company(self.session, request.org_id)
        if self.session.get(Organization, request.org_id) is None:
            raise ValueError("ORGANIZATION_NOT_FOUND")
        request_hash = canonical_hash(request.model_dump(mode="json"))
        old = self.session.scalar(
            select(MybankPaymentSourceVersion).where(
                MybankPaymentSourceVersion.org_id == request.org_id,
                MybankPaymentSourceVersion.idempotency_key == request.idempotency_key,
            )
        )
        if old:
            if old.request_hash != request_hash:
                raise ValueError("MYBANK_SOURCE_IDEMPOTENCY_CONFLICT")
            evidence = self.session.get(Evidence, old.evidence_id)
            if (
                evidence is None
                or evidence.org_id != request.org_id
                or evidence.sha256 != old.evidence_sha256
            ):
                raise ValueError("MYBANK_SOURCE_EVIDENCE_CHANGED")
            verify_material_bytes(evidence)
            return self.describe(old)
        current = self.latest(request.org_id, request.payroll_period, request.source_kind)
        if request.expected_revision != (current.revision if current else 0):
            raise ValueError("MYBANK_SOURCE_REVISION_CHANGED")
        evidence = self.session.get(Evidence, request.evidence_id)
        if evidence is None or evidence.org_id != request.org_id:
            raise ValueError("MYBANK_SOURCE_EVIDENCE_NOT_IN_COMPANY")
        rows = read_payment_source(
            verify_material_bytes(evidence),
            Path(evidence.original_name).suffix.lower(),
            request.source_kind,
            request.payroll_period,
        )
        row = MybankPaymentSourceVersion(
            org_id=request.org_id,
            payroll_period=request.payroll_period,
            source_kind=request.source_kind,
            revision=request.expected_revision + 1,
            evidence_id=evidence.id,
            evidence_sha256=evidence.sha256,
            idempotency_key=request.idempotency_key,
            request_hash=request_hash,
            content={"rows": rows},
        )
        self.session.add(row)
        self.session.flush()
        return self.describe(row)

    @staticmethod
    def describe(row):
        return {
            "status": "recorded",
            "revision": row.revision,
            "source_id": str(row.id),
            "source_kind": row.source_kind,
            "evidence_id": str(row.evidence_id),
            "evidence_sha256": row.evidence_sha256,
            "payroll_period": row.payroll_period,
            **row.content,
            "accounting_note": "保存代发来源事实，不更改工资凭证或记录实际付款。",
        }
