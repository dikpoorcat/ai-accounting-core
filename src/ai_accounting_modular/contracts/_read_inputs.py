"""Explicit process-local inputs of an owner projection, without page names."""

from dataclasses import dataclass, fields

_FLAGS = {"movements", "all_results", "posted_results", "all_obligations"}


@dataclass(frozen=True)
class ReadInputs:
    """Selectors discover new rows; identities bind the rows actually consumed.

    Formal business identities also subscribe to incoming resource uses. Current
    follow-up identities are separate from the projection's as-posted stock.
    Current Fact identities cover only deliberately current management inputs.
    Result selectors distinguish accumulated stock, the economic business month,
    and the actual adoption month. Movement rows use their real occurrence month;
    account selectors separately carry balances into quiet later months.
    """

    result_kinds: tuple[str, ...] = ()
    business_result_kinds: tuple[str, ...] = ()
    posted_result_kinds: tuple[str, ...] = ()
    account_codes: tuple[str, ...] = ()
    obligation_categories: tuple[str, ...] = ()
    cost_categories: tuple[str, ...] = ()
    business_ids: tuple[str, ...] = ()
    current_business_ids: tuple[str, ...] = ()
    current_fact_ids: tuple[str, ...] = ()
    fact_kinds: tuple[str, ...] = ()
    object_ids: tuple[str, ...] = ()
    object_kinds: tuple[str, ...] = ()
    note_scopes: tuple[str, ...] = ()
    movements: bool = False
    all_results: bool = False
    posted_results: bool = False
    all_obligations: bool = False

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if field.name in _FLAGS:
                if type(value) is not bool:
                    raise TypeError("read input flags must be bool")
            elif not isinstance(value, tuple) or any(
                not isinstance(item, str) or not item for item in value
            ):
                raise TypeError("read input selectors must be tuples of nonempty strings")

    def merge(self, *others):
        values = (self, *others)
        if any(not isinstance(value, ReadInputs) for value in values):
            raise TypeError("owner read inputs must use ReadInputs")
        return ReadInputs(
            **{
                field.name: any(getattr(value, field.name) for value in values)
                if field.name in _FLAGS
                else tuple(
                    sorted({item for value in values for item in getattr(value, field.name)})
                )
                for field in fields(self)
            }
        )

    def wire(self):
        return {
            field.name: getattr(self, field.name)
            if field.name in _FLAGS
            else sorted(set(getattr(self, field.name)))
            for field in fields(self)
        }

    @classmethod
    def from_wire(cls, value):
        if isinstance(value, dict) and "note_scopes" not in value:
            # Older projections could not consume per-business descriptions.
            value = {**value, "note_scopes": []}
        names = {field.name for field in fields(cls)}
        if not isinstance(value, dict) or set(value) != names:
            raise ValueError("projection read inputs are incomplete")
        if any(not isinstance(item, list) for key, item in value.items() if key not in _FLAGS):
            raise ValueError("projection read selectors must be arrays")
        return cls(**{key: item if key in _FLAGS else tuple(item) for key, item in value.items()})
