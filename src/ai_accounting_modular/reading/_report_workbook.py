"""Fill the retained tax workbook from a complete formal snapshot, preserving XML."""

from __future__ import annotations

import calendar
import hashlib
import io
import re
import zipfile
from datetime import date
from importlib.resources import files
from xml.sax.saxutils import escape, quoteattr

from ai_accounting_modular.contracts.public import canonical_json
from ai_accounting_modular.files.public import ProductionError

TEMPLATE_VERSION = 1
TEMPLATE_SHA256 = "bd83011100c52143b3b9a5cf5e13ba4dbe1899191bb1395bd92a5ce0f0c6f7fd"
TEMPLATE_MAX_FEN = 999_999_999_999_900


def check_text(identity):
    for key in ("legal_name", "credit_code"):
        if any(
            not (
                ord(char) in (9, 10, 13)
                or 0x20 <= ord(char) <= 0xD7FF
                or 0xE000 <= ord(char) <= 0xFFFD
                or 0x10000 <= ord(char) <= 0x10FFFF
            )
            for char in identity[key]
        ):
            raise ProductionError(
                "report_text_unrepresentable",
                "公司名称或识别号含Excel不支持的字符，不能原样生成此模板。",
                retryable=False,
            )


def _template_bytes():
    raw = files(__package__).joinpath("_templates/tax-bureau-v1.xlsx").read_bytes()
    if hashlib.sha256(raw).hexdigest() != TEMPLATE_SHA256:
        raise ProductionError(
            "report_template_invalid", "报表模板字节与固定版本不一致。", retryable=False
        )
    return raw


def _input(plan):
    snapshot, organization = plan["snapshot"], plan["organization"]
    check_text(organization)
    first, last = snapshot["scope"]["period_from"], snapshot["scope"]["period_to"]
    columns = {
        "balance_sheet": (("ending", "ending_fen"), ("beginning", "beginning_fen")),
        "profit_statement": (("current", "current_fen"), ("year_to_date", "year_to_date_fen")),
        "cash_flow_statement": (("current", "current_fen"), ("year_to_date", "year_to_date_fen")),
    }
    return {
        "organization": {
            "name": organization["legal_name"],
            "taxpayer_identification_number": organization["credit_code"],
        },
        "period": {
            "quarter_start": first + "-01",
            "quarter_end": last + f"-{calendar.monthrange(int(last[:4]), int(last[5:]))[1]:02d}",
        },
        "statements": {
            table: {
                number: {target: row[source]["amount_fen"] for source, target in pairs}
                for number, row in snapshot[table].items()
            }
            for table, pairs in columns.items()
        },
    }


def _identity(xml, plan, task_id):
    binding = canonical_json(
        {
            **{
                key: plan[key]
                for key in (
                    "company_id",
                    "database_id",
                    "scope_head",
                    "variant",
                    "template_version",
                )
            },
            "task_id": task_id,
        }
    )
    property_xml = (
        '<property fmtid="{D5CDD505-2E9C-101B-9397-08002B2CF9AE}" '
        'pid="4" name="AccountingReport"><vt:lpwstr>' + escape(binding) + "</vt:lpwstr></property>"
    )
    return xml.replace("</Properties>", property_xml + "</Properties>")


def _excel_serial(value: date) -> int:
    return value.toordinal() - date(1899, 12, 30).toordinal()


def _yuan_text(fen: int) -> str:
    yuan, cents = divmod(abs(fen), 100)
    return f"{'-' if fen < 0 else ''}{yuan}.{cents:02d}"


def _cell_span(xml: str, reference: str) -> tuple[int, int, str, str]:
    start_match = re.search(
        rf"<c\b(?=[^>]*\br={quoteattr(reference)}(?:\s|/?>))[^>]*>",
        xml,
    )
    if start_match is None:
        raise ValueError(f"FINANCIAL_STATEMENT_TEMPLATE_CELL_MISSING:{reference}")
    start_tag = start_match.group(0)
    if start_tag.endswith("/>"):
        return start_match.start(), start_match.end(), start_tag, ""
    closing = xml.find("</c>", start_match.end())
    if closing < 0:
        raise ValueError(f"FINANCIAL_STATEMENT_TEMPLATE_CELL_INVALID:{reference}")
    return start_match.start(), closing + len("</c>"), start_tag, xml[start_match.end() : closing]


def _cell_start_tag(start_tag: str, *, cell_type: str | None) -> str:
    tag = re.sub(r"\s+t=(?:\"[^\"]*\"|'[^']*')", "", start_tag)
    tag = tag[:-2] if tag.endswith("/>") else tag[:-1]
    if cell_type is not None:
        tag += f" t={quoteattr(cell_type)}"
    return tag + ">"


def _formula_xml(body: str) -> str:
    match = re.search(r"<f\b[^>]*(?:/>|>.*?</f>)", body, flags=re.DOTALL)
    return match.group(0) if match is not None else ""


def _replace_cell(
    xml: str,
    reference: str,
    *,
    value_xml: str,
    cell_type: str | None,
) -> str:
    start, end, start_tag, body = _cell_span(xml, reference)
    replacement = (
        _cell_start_tag(start_tag, cell_type=cell_type) + _formula_xml(body) + value_xml + "</c>"
    )
    return xml[:start] + replacement + xml[end:]


def _set_numeric(xml: str, reference: str, value: str) -> str:
    return _replace_cell(
        xml,
        reference,
        value_xml=f"<v>{escape(value)}</v>",
        cell_type=None,
    )


def _set_text(xml: str, reference: str, value: str, *, formula_cache: bool = False) -> str:
    escaped = escape(value).replace("\r", "&#13;")
    if formula_cache:
        return _replace_cell(
            xml,
            reference,
            value_xml=f"<v>{escaped}</v>",
            cell_type="str",
        )
    return _replace_cell(
        xml,
        reference,
        value_xml=f"<is><t>{escaped}</t></is>",
        cell_type="inlineStr",
    )


def _enable_workbook_recalculation(xml: str) -> str:
    match = re.search(r"<calcPr\b[^>]*>", xml)
    if match is None:
        closing = xml.rfind("</workbook>")
        if closing < 0:
            raise ValueError("FINANCIAL_STATEMENT_TEMPLATE_WORKBOOK_INVALID")
        calc = '<calcPr calcMode="auto" fullCalcOnLoad="1" forceFullCalc="1"/>'
        return xml[:closing] + calc + xml[closing:]
    tag = match.group(0)
    self_closing = tag.endswith("/>")
    base = tag[:-2] if self_closing else tag[:-1]
    for name in ("calcMode", "fullCalcOnLoad", "forceFullCalc"):
        base = re.sub(rf"\s+{name}=(?:\"[^\"]*\"|'[^']*')", "", base)
    suffix = "/>" if self_closing else ">"
    updated = base + ' calcMode="auto" fullCalcOnLoad="1" forceFullCalc="1"' + suffix
    return xml[: match.start()] + updated + xml[match.end() :]


def render(plan, task_id):
    data = _input(plan)
    template = _template_bytes()
    organization = data["organization"]
    period = data["period"]
    balance = data["statements"]["balance_sheet"]
    profit = data["statements"]["profit_statement"]
    cash = data["statements"]["cash_flow_statement"]
    start = date.fromisoformat(period["quarter_start"])
    end = date.fromisoformat(period["quarter_end"])
    replacements: dict[str, bytes] = {}

    with zipfile.ZipFile(io.BytesIO(template), "r") as source:
        sheets = [
            source.read(f"xl/worksheets/sheet{index}.xml").decode("utf-8") for index in (1, 2, 3)
        ]
        sheets[0] = _set_text(sheets[0], "D3", organization["taxpayer_identification_number"])
        sheets[0] = _set_text(sheets[0], "H3", organization["name"])
        sheets[0] = _set_numeric(sheets[0], "D4", str(_excel_serial(start)))
        sheets[0] = _set_numeric(sheets[0], "H4", str(_excel_serial(end)))
        for index in (1, 2):
            sheets[index] = _set_text(
                sheets[index],
                "D3",
                organization["taxpayer_identification_number"],
                formula_cache=True,
            )
            sheets[index] = _set_text(sheets[index], "F3", organization["name"], formula_cache=True)
            sheets[index] = _set_numeric(sheets[index], "D4", str(_excel_serial(start)))
            sheets[index] = _set_numeric(sheets[index], "F4", str(_excel_serial(end)))

        balance_cells: dict[int, tuple[str, str]] = {}
        for line in range(1, 16):
            balance_cells[line] = (f"D{line + 6}", f"E{line + 6}")
        for line in range(16, 31):
            balance_cells[line] = (f"D{line + 7}", f"E{line + 7}")
        for line in range(31, 42):
            balance_cells[line] = (f"H{line - 24}", f"I{line - 24}")
        for line in range(42, 48):
            balance_cells[line] = (f"H{line - 23}", f"I{line - 23}")
        for line in range(48, 54):
            balance_cells[line] = (f"H{line - 16}", f"I{line - 16}")
        for line, (ending_cell, beginning_cell) in balance_cells.items():
            row = balance[str(line)]
            sheets[0] = _set_numeric(sheets[0], ending_cell, _yuan_text(int(row["ending_fen"])))
            sheets[0] = _set_numeric(
                sheets[0], beginning_cell, _yuan_text(int(row["beginning_fen"]))
            )

        for line in range(1, 33):
            row = profit[str(line)]
            sheets[1] = _set_numeric(sheets[1], f"D{line + 5}", _yuan_text(int(row["current_fen"])))
            sheets[1] = _set_numeric(
                sheets[1], f"E{line + 5}", _yuan_text(int(row["year_to_date_fen"]))
            )

        cash_rows = {
            **{line: line + 6 for line in range(1, 8)},
            **{line: line + 7 for line in range(8, 14)},
            **{line: line + 8 for line in range(14, 23)},
        }
        for line, sheet_row in cash_rows.items():
            row = cash[str(line)]
            sheets[2] = _set_numeric(
                sheets[2], f"D{sheet_row}", _yuan_text(int(row["current_fen"]))
            )
            sheets[2] = _set_numeric(
                sheets[2], f"E{sheet_row}", _yuan_text(int(row["year_to_date_fen"]))
            )
        for index, sheet in enumerate(sheets, start=1):
            replacements[f"xl/worksheets/sheet{index}.xml"] = sheet.encode("utf-8")
        replacements["docProps/custom.xml"] = _identity(
            source.read("docProps/custom.xml").decode("utf-8"), plan, task_id
        ).encode("utf-8")
        replacements["xl/workbook.xml"] = _enable_workbook_recalculation(
            source.read("xl/workbook.xml").decode("utf-8")
        ).encode("utf-8")
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as target:
            for info in source.infolist():
                target.writestr(info, replacements.get(info.filename, source.read(info.filename)))
    return output.getvalue()
