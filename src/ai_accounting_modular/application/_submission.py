"""Normalized process input and outcomes, independent of the bounded AI wire."""

from dataclasses import dataclass

from ai_accounting_modular.contracts.public import canonical_json, loads_json, validate_contract


@dataclass(frozen=True)
class Submission:
    company_id: str
    command_id: str
    mutations: tuple = ()
    objects: tuple = ()
    sources: tuple = ()
    notes: tuple = ()
    unresolved: tuple | None = None
    unresolved_scope: str | None = None
    expected_scope_head: int | None = None
    correction_period: str | None = None
    explanation: str | None = None

    @classmethod
    def from_request(cls, request):
        value = loads_json(canonical_json(request))
        return cls(
            **{key: value[key] for key in ("company_id", "command_id")},
            **{
                key: tuple(value.get(key, ()))
                for key in ("mutations", "objects", "sources", "notes")
            },
            unresolved=tuple(value["unresolved"]) if "unresolved" in value else None,
            **{
                key: value.get(key)
                for key in (
                    "unresolved_scope",
                    "expected_scope_head",
                    "correction_period",
                    "explanation",
                )
            },
        )

    def validate(self):
        for value in (self.company_id, self.command_id):
            validate_contract("core.schema.json", "Id", value)
        for field, definition in (
            ("mutations", "Mutation"),
            ("objects", "Object"),
            ("sources", "Source"),
            ("notes", "NoteChange"),
        ):
            for value in getattr(self, field):
                validate_contract("core.schema.json", definition, value)
        if self.correction_period is not None:
            validate_contract("core.schema.json", "Month", self.correction_period)


@dataclass(frozen=True)
class ExecutionOutcome:
    """Effects computed in the caller's transaction, before wire receipt formatting."""

    heads: tuple
    affected_periods: tuple
    objects: tuple
    notes: tuple
    issue_scopes: tuple
    unresolved: tuple
    changed: bool
    accounting_changed: bool
    reading_changed: bool
