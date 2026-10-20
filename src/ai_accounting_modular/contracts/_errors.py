"""Errors shared by public module boundaries."""

from __future__ import annotations

from collections.abc import Sequence

from ._wire_types import ErrorResponse, FactIssue


class ContractError(ValueError):
    """A typed failure; missing business facts are distinct from bad inputs."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        fact_issues: Sequence[FactIssue] = (),
        diagnostic_id: str | None = None,
    ) -> None:
        if not code or not message:
            raise ValueError("Contract errors require a code and message")
        if code == "needs_information" and not fact_issues:
            raise ValueError("needs_information requires actual fact issues")
        super().__init__(message)
        self.code = code
        self.message = message
        self.fact_issues = tuple(fact_issues)
        self.diagnostic_id = diagnostic_id

    def to_response(self) -> ErrorResponse:
        response: ErrorResponse = {
            "status": "rejected",
            "code": self.code,
            "message": self.message,
        }
        if self.fact_issues:
            response["fact_issues"] = list(self.fact_issues)
        if self.diagnostic_id is not None:
            response["diagnostic_id"] = self.diagnostic_id
        return response
