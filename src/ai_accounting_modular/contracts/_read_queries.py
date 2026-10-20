"""Explicit selections and bounded pages share a snapshot, never a connection."""

from dataclasses import dataclass
from typing import Literal

from ._errors import ContractError
from ._validation import validate_contract


@dataclass(frozen=True)
class QuerySelection:
    view: Literal["current", "as_posted"]
    as_of: str | None = None

    def __post_init__(self):
        if (
            not isinstance(self.view, str)
            or self.view not in {"current", "as_posted"}
            or (self.view == "current" and self.as_of is not None)
            or (self.view == "as_posted" and self.as_of is None)
        ):
            raise ContractError("invalid_input", "读取口径与正式采用截止月不匹配。")
        if self.as_of is not None:
            validate_contract("core.schema.json", "Month", self.as_of)


def query_selection(read_context, selection=None) -> QuerySelection:
    if selection is None:
        return QuerySelection(read_context.view, read_context.as_of)
    if not isinstance(selection, QuerySelection):
        raise ContractError("invalid_input", "查询须使用明确的读取口径。")
    return selection


@dataclass(frozen=True)
class PageQuery:
    limit: int = 20
    cursor: str | None = None
    expected_version: str | None = None

    def __post_init__(self):
        if type(self.limit) is not int or not 1 <= self.limit <= 500:
            raise ContractError("invalid_input", "查询每页数量须在1至500之间。")
        if self.cursor is not None and (not isinstance(self.cursor, str) or not self.cursor):
            raise ContractError("invalid_input", "分页游标须为非空的不透明字符串。")
        if self.expected_version is not None:
            validate_contract("core.schema.json", "Digest", self.expected_version)


@dataclass(frozen=True)
class ReadPage[T]:
    version: str
    total_count: int
    filtered_count: int
    items: tuple[T, ...]
    next_cursor: str | None = None

    def __post_init__(self):
        validate_contract("core.schema.json", "Digest", self.version)
        if (
            type(self.total_count) is not int
            or type(self.filtered_count) is not int
            or not self.total_count >= self.filtered_count >= len(self.items)
        ):
            raise ContractError("invalid_input", "查询数量须覆盖完整筛选范围与本页项目。")
        if self.next_cursor is not None and (
            not isinstance(self.next_cursor, str) or not self.next_cursor
        ):
            raise ContractError("invalid_input", "后续游标须为非空的不透明字符串。")
