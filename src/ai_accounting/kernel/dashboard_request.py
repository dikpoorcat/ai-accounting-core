"""Pure dashboard request checks shared by native reads and delivery gates."""

from typing import Literal, get_args

from .contracts import KernelError
from .dashboard_pages import validate_page
from .domains.money import FUNDS_ACCOUNT_TYPE_BY_BALANCE_CATEGORY
from .types import YearMonth

EmployeeFilter = Literal[
    "all", "in_period", "unknown", "ended", "employment_active", "employment_unpaid_leave",
    "employment_departed", "employment_unknown",
]


def validate_period(period: str | None) -> str | None:
    if period is None:
        return None
    try:
        return str(YearMonth(period))
    except ValueError as exc:
        raise KernelError("invalid_command", "会计月份格式不正确") from exc


def validate_request(
    page: str,
    *,
    period: str | None = None,
    year: int | None = None,
    quarter: int | None = None,
    limit: int = 20,
    section: str | None = None,
    cursor: str | None = None,
    expected_version: str | None = None,
    preparation: str = "deferred",
    voucher_version_id: str | None = None,
    voucher_number: int | None = None,
    movement_account_type: str | None = None,
    movement_account_id: str | None = None,
    statement_account_id: str | None = None,
    movement_account_selection: str = "all",
    employee_filter: str = "employment_active",
    employee_id: str | None = None,
    asset_filter: str = "all",
    asset_id: str | None = None,
    project_id: str | None = None,
) -> None:
    if page == "quarterly_report":
        if (type(year) is not int or not 1 <= year <= 9999
                or type(quarter) is not int or quarter not in range(1, 5)):
            raise KernelError("invalid_command", "报表年份或季度不正确")
    elif page not in {"brief", "funds", "employees", "assets"}:
        raise ValueError("unsupported dashboard request validator")
    if page not in {"brief", "quarterly_report"}:
        validate_page(page, section, cursor, limit)
    if preparation not in {"complete", "deferred"}:
        raise KernelError("invalid_command", "不支持的准备检查投影")
    if page == "quarterly_report":
        return
    if page == "brief":
        validate_page(page, section, cursor, limit)
        if section is not None and (voucher_version_id is not None or voucher_number is not None):
            raise KernelError("invalid_command", "精确业务定位不能与明细分区同时使用")
        if voucher_version_id is not None and voucher_number is not None:
            raise KernelError("invalid_command", "精确业务定位须只提供一个身份")
        if voucher_number is not None and (type(voucher_number) is not int or voucher_number < 1):
            raise KernelError("invalid_command", "业务编号须为正整数")
    elif page == "funds":
        if (movement_account_type is None) != (movement_account_id is None):
            raise KernelError("invalid_command", "资金账户筛选须同时提供账户类别与账户标识")
        if (movement_account_type is not None
                and movement_account_type not in FUNDS_ACCOUNT_TYPE_BY_BALANCE_CATEGORY.values()):
            raise KernelError("invalid_command", "不支持的资金账户类别")
        if movement_account_id == "" or statement_account_id == "":
            raise KernelError("invalid_command", "资金账户标识不能为空")
        if movement_account_selection not in {"all", "first"} or (
            movement_account_selection == "first"
            and (movement_account_type is not None or section is not None or cursor is not None)
        ):
            raise KernelError(
                "invalid_command", "自动选择首个资金账户只适用于未指定账户的完整首屏请求"
            )
    elif page == "employees":
        if employee_id == "":
            raise KernelError("invalid_command", "须指定有效 employee_id")
        if employee_filter not in get_args(EmployeeFilter):
            raise KernelError("invalid_command", "不支持的员工筛选")
    elif page == "assets":
        if asset_id == "" or project_id == "":
            raise KernelError("invalid_command", "须指定有效 asset_id 或 project_id")
        if asset_filter not in {"all", "active", "fixed", "intangible", "pending", "exited"}:
            raise KernelError("invalid_command", "不支持的资产筛选")
    validate_period(period)
