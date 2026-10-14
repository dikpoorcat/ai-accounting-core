"""Fixed meanings of the supported filing fields, independent of consumer inputs."""

from .contracts import KernelError


def field_definition(field_id):
    section, name = field_id.split(".", 1)
    if section in {"financial_statement", "cit"}:
        definitions = {
            "operating_revenue": ("营业收入", "tax_exclusive", "1"),
            "operating_cost": ("营业成本", "accounting_amount", "2"),
            "profit_before_tax": ("利润总额，尚未扣除所得税费用", "accounting_amount", "18"),
        }
        if name not in definitions:
            raise KernelError("filing_field_unsupported", "尚未支持该财报取数字段")
        meaning, amount_basis, row = definitions[name]
        return {
            "meaning": meaning,
            "business_scope": "同期小企业利润表的同名字段；预缴采用本年累计金额",
            "amount_basis": amount_basis,
            "rounding_rule": "直接读取财报整数分金额，不再次计算或舍入",
            "form_field": "A200000." + row if section == "cit" else None,
        }
    if section != "vat_and_surtax":
        raise KernelError("filing_field_unsupported", "尚未支持该申报字段范围")
    definitions = {
        "net_sales_fen": ("本税期全部不含税销售额", "tax_exclusive", None),
        "taxable_sales_fen": ("本税期应税不含税销售额", "tax_exclusive", None),
        "exempt_sales_fen": ("本税期免税不含税销售额", "tax_exclusive", None),
        "accrued_vat_fen": ("业务来源已计提增值税合计", "tax_amount", None),
        "relief_fen": ("本税期来源已计提增值税的免税转回额", "tax_amount", None),
        "payable_vat_fen": ("本税期应纳增值税", "tax_amount", "20"),
        "urban_tax_fen": (
            "本税期减征后城市维护建设税计提额，不含预缴或已缴结算", "tax_amount", None,
        ),
        "education_tax_fen": (
            "本税期减征后教育费附加计提额，不含预缴或已缴结算", "tax_amount", None,
        ),
        "local_education_tax_fen": (
            "本税期减征后地方教育附加计提额，不含预缴或已缴结算", "tax_amount", None,
        ),
        "surtax_fen": ("本税期上述三项附加税费合计", "tax_amount", None),
        "row_1_fen": ("应征增值税不含税销售额（3%征收率）", "tax_exclusive", "1"),
        "row_9_fen": ("免税销售额", "tax_exclusive", "9"),
        "row_10_fen": ("小微企业免税销售额", "tax_exclusive", "10"),
        "row_15_fen": ("本期应纳税额", "tax_amount", "15"),
        "row_16_fen": ("本期应纳税额减征额", "tax_amount", "16"),
        "row_17_fen": ("本期免税额", "tax_amount", "17"),
        "row_18_fen": ("小微企业免税额", "tax_amount", "18"),
        "row_20_fen": ("应纳税额合计，第15栏减第16栏", "tax_amount", "20"),
        "row_21_fen": ("本期预缴税额", "tax_amount", "21"),
        "row_22_fen": ("本期应补退税额，第20栏减第21栏", "tax_amount", "22"),
        "row_23_fen": ("城市维护建设税本期应补退税额", "tax_amount", "23"),
        "row_24_fen": ("教育费附加本期应补退费额", "tax_amount", "24"),
        "row_25_fen": ("地方教育附加本期应补退费额", "tax_amount", "25"),
    }
    if name not in definitions:
        raise KernelError("filing_field_unsupported", "尚未支持该增值税或附加字段")
    meaning, amount_basis, row = definitions[name]
    return {
        "meaning": meaning,
        "business_scope": "已采用的同一税期核算结果；累计列只汇总完整季度，不重算累计免税限额",
        "amount_basis": amount_basis,
        "rounding_rule": (
            "业务含税拆分与税期计算均使用Decimal四舍五入至分；"
            "1%业务第15栏按3%、第16栏按2%分别舍入，第20栏相减；"
            "附加按该应纳增值税及已采用政策计算至分；"
            "有实际来源的税额直接采用实际整数分，计算仅作参考；取数复用正式采用结果"
        ),
        "form_field": "small_scale_vat." + row if row else None,
    }
