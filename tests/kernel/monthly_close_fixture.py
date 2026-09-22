"""Synthetic setup helpers; every close uses the production single-month path."""

from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods
from ai_accounting.kernel.types import YearMonth


def ready(engine, proof, first="2026-01", last="2026-03", *, omit=()):
    periods = Periods(engine)
    for ordinal in range(YearMonth(first).ordinal, YearMonth(last).ordinal + 1):
        month = str(YearMonth.from_ordinal(ordinal))
        for category in MATERIAL_CATEGORIES:
            if (month, category) not in omit:
                periods.inventory(
                    month,
                    category,
                    evidence=[],
                    expected=0,
                    no_business=True,
                    confirmation_evidence=proof,
                    request_id=f"inventory-{month}-{category}",
                )


def close_months(periods, proof, first="2026-01", last="2026-03", *, backup_directory=None):
    results = []
    for ordinal in range(YearMonth(first).ordinal, YearMonth(last).ordinal + 1):
        month = str(YearMonth.from_ordinal(ordinal))
        preview = periods.preview_close(month, owner_confirmation=proof)
        result = periods.close(
            month,
            owner_confirmation=proof,
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id=f"close-month-{month}",
            backup_directory=backup_directory,
        )
        results.append((preview, result))
    return results
