"""Pure, version-pinned three-statement tax-template rendering; no persistence imports."""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from datetime import date
from decimal import Decimal
from importlib import resources
from typing import Any
from xml.sax.saxutils import escape, quoteattr

ACCOUNTING_RULE_VERSION = "small-enterprise-statements-2013-v1"
ACCOUNTING_RULE_EFFECTIVE_FROM = "2013-01-01"
ACCOUNTING_RULE_SOURCE_URL = "https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852734144.pdf"
TEMPLATE_FILE_NAME = "财务报表报送与信息采集（小企业会计准则）月季报.xlsx"
TEMPLATE_SHA256 = "BD83011100C52143B3B9A5CF5E13BA4DBE1899191BB1395BD92A5CE0F0C6F7FD"
TEMPLATE_PROFILE = "small-enterprise-monthly-quarterly-user-2026-08-26"
TEMPLATE_MAX_FEN = 999_999_999_999_900

BALANCE_NAMES = {
    1: "货币资金",
    2: "短期投资",
    3: "应收票据",
    4: "应收账款",
    5: "预付账款",
    6: "应收股利",
    7: "应收利息",
    8: "其他应收款",
    9: "存货",
    10: "其中：原材料",
    11: "在产品",
    12: "库存商品",
    13: "周转材料",
    14: "其他流动资产",
    15: "流动资产合计",
    16: "长期债券投资",
    17: "长期股权投资",
    18: "固定资产原价",
    19: "减：累计折旧",
    20: "固定资产账面价值",
    21: "在建工程",
    22: "工程物资",
    23: "固定资产清理",
    24: "生产性生物资产",
    25: "无形资产",
    26: "开发支出",
    27: "长期待摊费用",
    28: "其他非流动资产",
    29: "非流动资产合计",
    30: "资产合计",
    31: "短期借款",
    32: "应付票据",
    33: "应付账款",
    34: "预收账款",
    35: "应付职工薪酬",
    36: "应交税费",
    37: "应付利息",
    38: "应付利润",
    39: "其他应付款",
    40: "其他流动负债",
    41: "流动负债合计",
    42: "长期借款",
    43: "长期应付款",
    44: "递延收益",
    45: "其他非流动负债",
    46: "非流动负债合计",
    47: "负债合计",
    48: "实收资本（或股本）",
    49: "资本公积",
    50: "盈余公积",
    51: "未分配利润",
    52: "所有者权益合计",
    53: "负债和所有者权益总计",
}

PROFIT_NAMES = {
    1: "营业收入",
    2: "营业成本",
    3: "税金及附加",
    4: "其中：消费税",
    5: "营业税",
    6: "城市维护建设税",
    7: "资源税",
    8: "土地增值税",
    9: "城镇土地使用税、房产税、车船税、印花税",
    10: "教育费附加、矿产资源补偿费、排污费",
    11: "销售费用",
    12: "其中：商品维修费",
    13: "广告费和业务宣传费",
    14: "管理费用",
    15: "其中：开办费",
    16: "业务招待费",
    17: "研究费用",
    18: "财务费用",
    19: "其中：利息费用（收入以负号填列）",
    20: "投资收益（损失以负号填列）",
    21: "营业利润",
    22: "营业外收入",
    23: "其中：政府补助",
    24: "营业外支出",
    25: "其中：坏账损失",
    26: "无法收回的长期债券投资损失",
    27: "无法收回的长期股权投资损失",
    28: "自然灾害等不可抗力因素造成的损失",
    29: "税收滞纳金",
    30: "利润总额",
    31: "所得税费用",
    32: "净利润",
}

CASH_FLOW_NAMES = {
    1: "销售产成品、商品、提供劳务收到的现金",
    2: "收到其他与经营活动有关的现金",
    3: "购买原材料、商品、接受劳务支付的现金",
    4: "支付的职工薪酬",
    5: "支付的税费",
    6: "支付其他与经营活动有关的现金",
    7: "经营活动产生的现金流量净额",
    8: "收回短期投资、长期债券投资和长期股权投资收到的现金",
    9: "取得投资收益收到的现金",
    10: "处置固定资产、无形资产和其他非流动资产收回的现金净额",
    11: "短期投资、长期债券投资和长期股权投资支付的现金",
    12: "购建固定资产、无形资产和其他非流动资产支付的现金",
    13: "投资活动产生的现金流量净额",
    14: "取得借款收到的现金",
    15: "吸收投资者投资收到的现金",
    16: "偿还借款本金支付的现金",
    17: "偿还借款利息支付的现金",
    18: "分配利润支付的现金",
    19: "筹资活动产生的现金流量净额",
    20: "现金净增加额",
    21: "期初现金余额",
    22: "期末现金余额",
}


def _template_bytes() -> bytes:
    data = (
        resources.files("ai_accounting")
        .joinpath("templates/financial_reports")
        .joinpath(TEMPLATE_FILE_NAME)
        .read_bytes()
    )
    if hashlib.sha256(data).hexdigest().upper() != TEMPLATE_SHA256:
        raise ValueError("FINANCIAL_STATEMENT_TEMPLATE_VERSION_MISMATCH")
    return data


def _excel_serial(value: date) -> int:
    return value.toordinal() - date(1899, 12, 30).toordinal()


def _yuan_text(fen: int) -> str:
    return format((Decimal(fen) / Decimal(100)).quantize(Decimal("0.00")), "f")


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
    escaped = escape(value)
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


def render_quarterly_template(data: dict[str, Any]) -> bytes:
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
        replacements["xl/workbook.xml"] = _enable_workbook_recalculation(
            source.read("xl/workbook.xml").decode("utf-8")
        ).encode("utf-8")
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as target:
            for info in source.infolist():
                target.writestr(info, replacements.get(info.filename, source.read(info.filename)))
    return output.getvalue()
