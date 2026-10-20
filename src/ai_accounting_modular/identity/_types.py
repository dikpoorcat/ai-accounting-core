"""Process-local identity plans, with immutable owner-interpreted contents."""

from __future__ import annotations

from dataclasses import dataclass

from ai_accounting_modular.contracts.public import CurrentObject, ReadContext, loads_json


@dataclass(frozen=True)
class IdentityContext:
    read_context: ReadContext
    command_id: str


@dataclass(frozen=True)
class PlannedObject:
    target_json: str
    expected_seq: int
    before_seq: int | None
    before_digest: str | None
    changed: bool

    @property
    def target(self) -> CurrentObject:
        return loads_json(self.target_json)


@dataclass(frozen=True)
class IdentityPlan:
    """Application may read targets and source uses; identity alone applies the plan."""

    company_id: str
    database_id: str
    head: int
    command_id: str
    _items: tuple[PlannedObject, ...]

    @property
    def objects(self) -> tuple[CurrentObject, ...]:
        """Submission overlay; each read returns detached contract values."""
        return tuple(item.target for item in self._items)

    @property
    def changed_ids(self) -> tuple[str, ...]:
        return tuple(item.target["object_id"] for item in self._items if item.changed)

    @property
    def source_uses(self) -> dict[str, tuple[str, ...]]:
        """Current source edges for records to save in the same unit of work."""
        return {
            item.target["object_id"]: tuple(item.target.get("evidence_ids", ()))
            for item in self._items
        }
