"""Version-scoped read rules for released company content verification.

Only the historical verifier enters this context. Current writes, previews,
and ordinary service reads continue to use the active package implementation.
"""

from contextlib import contextmanager
from contextvars import ContextVar

from . import history_encoding_v1, types

_version: ContextVar[int | None] = ContextVar("company_content_version", default=None)


@contextmanager
def historical_content(version: int):
    token = _version.set(version)
    try:
        yield
    finally:
        _version.reset(token)


def source_encoding():
    """Choose the fixed v1 source wire format only inside historical verification."""
    return history_encoding_v1 if _version.get() == 1 else types


def source_canonical(value):
    return source_encoding().canonical(value)


def source_digest(value):
    return source_encoding().digest(value)


def source_checked(value):
    return source_encoding().checked(value)


def source_sum_fen(values):
    return source_encoding().sum_fen(values)


def source_json_loads(raw):
    """Use the saved version's fixed duplicate-member interpretation."""
    if _version.get() == 1:
        from .stored_json_v1 import loads_unique
    else:
        from .stored_json import loads_unique

    return loads_unique(raw)


def close_reader():
    if _version.get() == 1:
        from . import close_storage_v1

        return close_storage_v1
    from . import close_storage

    return close_storage


def close_contract():
    if _version.get() == 1:
        from . import close_contract_v1

        return close_contract_v1
    from . import close_contract as current

    return current


def report_reader():
    if _version.get() == 1:
        from . import report_projection_v1

        return report_projection_v1
    from . import report_projection

    return report_projection


def settlement_reader():
    if _version.get() == 1:
        from . import settlement_freeze_v1

        return settlement_freeze_v1
    from . import settlement_freeze

    return settlement_freeze


def settlement_projection_reader():
    if _version.get() == 1:
        from . import settlement_projection_v1

        return settlement_projection_v1
    from . import settlement_projection

    return settlement_projection


def owner_review_reader():
    if _version.get() == 1:
        from . import close_review_integrity_v1

        return close_review_integrity_v1
    from . import close_review

    return close_review


def balance_freeze_reader():
    if _version.get() == 1:
        from . import period_balance_freeze_v1

        return period_balance_freeze_v1
    from . import period_balance_freeze

    return period_balance_freeze


def publication_reader():
    if _version.get() == 1:
        from . import publication_v1

        return publication_v1
    from . import publication

    return publication


def report_semantics_reader():
    if _version.get() == 1:
        from . import report_semantics_v1

        return report_semantics_v1
    from . import report_semantics

    return report_semantics


def report_flow_reader():
    if _version.get() == 1:
        from . import report_flow_v1

        return report_flow_v1
    from . import report_flow

    return report_flow


def report_classification_directory_reader():
    if _version.get() == 1:
        from . import report_classification_directory_v1

        return report_classification_directory_v1
    from . import report_classification_directory

    return report_classification_directory


def report_open_contribution_reader():
    if _version.get() == 1:
        from . import report_open_contribution_v1

        return report_open_contribution_v1
    from . import report_open_contribution

    return report_open_contribution


def journal_reader():
    if _version.get() == 1:
        from . import change_journal_v1

        return change_journal_v1
    from . import change_journal

    return change_journal


def duplicate_reader():
    if _version.get() == 1:
        from . import duplicate_freeze_v1

        return duplicate_freeze_v1
    from . import duplicate_freeze

    return duplicate_freeze


def material_watch_reader():
    if _version.get() == 1:
        from . import material_watch_v1

        return material_watch_v1
    from . import material_watch

    return material_watch


def month_type():
    if _version.get() == 1:
        from .history_types_v1 import YearMonth

        return YearMonth
    from .types import YearMonth

    return YearMonth


def read_type():
    if _version.get() == 1:
        from .history_types_v1 import Read

        return Read
    from .contracts import Read

    return Read


def inventory_reference():
    if _version.get() == 1:
        from .material_inspection_v1 import inventory_reference as v1_inventory_reference

        return v1_inventory_reference
    from .materials import _inventory_reference

    return _inventory_reference


def asset_card_reader():
    if _version.get() == 1:
        from . import asset_card_adoption_v1

        return asset_card_adoption_v1
    from . import asset_card_adoption

    return asset_card_adoption


def asset_membership_reader():
    if _version.get() == 1:
        from . import asset_membership_v1

        return asset_membership_v1
    from . import asset_batches

    return asset_batches


def opening_adoption_reader():
    if _version.get() == 1:
        from . import opening_adoption_v1

        return opening_adoption_v1
    from . import opening_adoption

    return opening_adoption


def material_categories():
    if _version.get() == 1:
        from .content_v1_semantics import MATERIAL_CATEGORIES

        return MATERIAL_CATEGORIES
    from .periods import MATERIAL_CATEGORIES

    return MATERIAL_CATEGORIES
