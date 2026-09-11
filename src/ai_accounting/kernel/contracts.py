"""The only interface between pure business modules and the transaction kernel.

Facts have generated typed SQL tables; JSON is reserved for composite fields and
frozen calculation explanations. Journal lines are INTERNAL calculation output.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict

from .types import YearMonth, checked, sum_fen


class FrozenDict(dict):
    """JSON-compatible immutable explanation input, including nested mappings."""

    def _immutable(self, *args, **kwargs):
        raise TypeError("calculation input is immutable")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _immutable

    def __deepcopy__(self, memo):
        return self


def freeze(value):
    if isinstance(value, dict):
        return FrozenDict({key: freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    return value


class KernelError(ValueError):
    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code, self.details = code, details

    def response(self):
        return {"status": "rejected", "code": self.code, "message": str(self), **self.details}


class NeedsInformation(KernelError):
    def __init__(self, field: str, message: str, *, sources=(), precision=()):
        super().__init__("needs_information", message)
        self.issues = [
            {
                "field": field,
                "message": message,
                "semantics": "accounting",
                "reusable_sources": list(sources),
                "allowed_precision": list(precision),
            }
        ]

    def response(self):
        return {"status": "needs_information", "fact_issues": self.issues}


@dataclass(frozen=True, order=True)
class Read:
    """Select a current scope, or an immutable exact version using ``#version_id``."""

    source: Literal["fact", "calculation"]
    kind: str
    key: str
    before_period: YearMonth | None = None


@dataclass(frozen=True)
class Claim:
    key: str
    amount: int

    def __post_init__(self):
        if checked(self.amount) <= 0:
            raise ValueError("an obligation claim must be positive")


class Fact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    kind: ClassVar[str]
    lane: ClassVar[Literal["accounting", "material", "management"]] = "accounting"
    immutable: ClassVar[bool] = False
    immutable_fields: ClassVar[tuple[str, ...]] = ()
    identity_fields: ClassVar[tuple[str, ...]] = ()
    material_category: ClassVar[str | None] = None
    # Some internal facts are compiled from verified source bytes by a typed
    # command. They retain common versioning but are not arbitrary fact inputs.
    registration_command: ClassVar[str | None] = None
    # Explicit alternative names for the same source amount. Paths use fact.X_fen
    # or result.X_fen; equal values alone never establish an alias.
    material_amount_aliases: ClassVar[dict[str, str]] = {}
    # Explicit domain meaning, independent of whether a calculation has journal
    # lines. A published zero count may establish an idle period; only current,
    # reviewed calculation summaries qualify for this exception.
    business_activity: ClassVar[bool] = True
    activity_count_field: ClassVar[str | None] = None
    period: YearMonth

    def scopes(self) -> tuple[str, ...]:
        return (str(self.period),)

    def reads(self) -> tuple[Read, ...]:
        return ()

    def reads_for(self, subject_id: str) -> tuple[Read, ...]:
        """Declare identity-scoped reads without duplicating the public subject ID."""
        return self.reads()

    def scopes_for(self, subject_id: str) -> tuple[str, ...]:
        """Expose identity-scoped source categories without adding redundant fields."""
        return self.scopes()

    def claims(self) -> tuple[Claim, ...]:
        """Explicit typed allocation facts; independent of a business-specific save path."""
        return ()

    def validate_material_amount(
        self,
        amount_field: str,
        amount_fen: int,
        *,
        source_amounts: tuple[int, ...],
        source_directions: tuple[Literal["signed_net", "inflow", "outflow"] | None, ...] = (),
    ) -> None:
        """Pure constraints on unchanged originals and explicitly declared column directions."""

    def required_closed_periods(self) -> tuple[YearMonth, ...]:
        return ()


@dataclass(frozen=True)
class FactVersion:
    id: str
    subject_id: str
    revision: int
    fact: Fact
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class Calculation:
    id: str
    subject_id: str
    kind: str
    period: YearMonth
    values: dict[str, Any]
    fact_id: str
    result_digest: str = ""

    def __post_init__(self):
        object.__setattr__(self, "values", freeze(self.values))


@dataclass(frozen=True)
class Line:
    account: str
    debit: int = 0
    credit: int = 0
    cashflow: str | None = None

    def __post_init__(self):
        checked(self.debit)
        checked(self.credit)
        if (
            not self.account
            or self.debit < 0
            or self.credit < 0
            or bool(self.debit) == bool(self.credit)
        ):
            raise ValueError("a line has exactly one positive side and an account")


@dataclass(frozen=True)
class BalanceEffect:
    """Signed movements of a named receivable/payable/asset; keyed by stable identity."""

    key: str
    amount: int
    category: str = "payable"

    def __post_init__(self):
        checked(self.amount)


@dataclass(frozen=True)
class Outcome:
    lines: tuple[Line, ...]
    values: dict[str, Any]
    balances: tuple[BalanceEffect, ...] = ()
    explanation: tuple[dict, ...] = ()
    opening_lines: tuple[Line, ...] = ()
    opening: bool = False

    def __post_init__(self):
        if type(self.opening) is not bool or (self.opening_lines and not self.opening):
            raise ValueError("opening balances require an explicit opening calculation")
        if self.opening_lines and (self.lines or any(line.cashflow for line in self.opening_lines)):
            raise ValueError("opening balances cannot carry current-period activity or cash flow")
        if self.opening_lines and (
            len(self.opening_lines) < 2
            or sum_fen(x.debit for x in self.opening_lines)
            != sum_fen(x.credit for x in self.opening_lines)
        ):
            raise ValueError("unbalanced opening calculation")
        if self.lines and (
            len(self.lines) < 2
            or sum_fen(x.debit for x in self.lines) != sum_fen(x.credit for x in self.lines)
        ):
            raise ValueError("unbalanced calculation")


@dataclass
class Context:
    """Detached immutable inputs with explicit, including empty, read tracking."""

    selections: dict[Read, tuple[FactVersion | Calculation, ...]]
    used: set[Read] = field(default_factory=set)
    versions: set[str] = field(default_factory=set)

    def select(self, read: Read) -> tuple:
        if read not in self.selections:
            raise KernelError("undeclared_read", f"calculator did not declare {read}")
        self.used.add(read)
        result = self.selections[read]
        self.versions.update(item.id for item in result)
        return result

    def facts(self, kind: str, key: str) -> tuple[FactVersion, ...]:
        return self.select(Read("fact", kind, key))

    def calculations(self, kind: str, key: str) -> tuple[Calculation, ...]:
        return self.select(Read("calculation", kind, key))

    def one(self, kind: str, key: str) -> FactVersion:
        items = self.facts(kind, key)
        if not items:
            raise NeedsInformation(kind, "需要已确认的核算来源", sources=(key,))
        if len(items) != 1:
            raise KernelError("ambiguous_source", f"multiple {kind} sources for {key}")
        return items[0]


Evaluator = Callable[[FactVersion, Context], Outcome]


class Registry:
    def __init__(self):
        self.models: dict[str, type[Fact]] = {}
        self.evaluators: dict[str, Evaluator] = {}
        self.readiness: dict[str, tuple[Callable, Callable]] = {}
        self.snapshot_readiness: dict[str, Callable] = {}

    def register_snapshot_readiness(self, name: str, checker: Callable):
        """Register a read-model check run only on the detached read-only snapshot."""
        if name in self.snapshot_readiness:
            raise ValueError(f"duplicate snapshot readiness check {name}")
        self.snapshot_readiness[name] = checker

    def register_readiness(self, name: str, required_reads: Callable, evaluator: Callable):
        """Declare pure period obligations alongside a domain's calculators."""
        if name in self.readiness:
            raise ValueError(f"duplicate readiness check {name}")
        self.readiness[name] = (required_reads, evaluator)

    def register(self, model: type[Fact], evaluator: Evaluator | None = None):
        if model.kind in self.models:
            raise ValueError(f"duplicate fact kind {model.kind}")
        self.models[model.kind] = model
        if evaluator is not None:
            self.evaluators[model.kind] = evaluator

    def schemas(self) -> dict:
        return {
            kind: model.model_json_schema()
            | (
                {"x-registration-command": model.registration_command}
                if model.registration_command
                else {}
            )
            | (
                {"x-material-amount-aliases": model.material_amount_aliases}
                if model.material_amount_aliases
                else {}
            )
            for kind, model in self.models.items()
        }
