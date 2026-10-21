"""Combine public adopted changes with independent current management inputs."""

from ai_accounting_modular.reading.public import PeriodRange, ReadingImpact


def applied(*, adoption, objects, facts, notes, issues, working_period_changed):
    return ReadingImpact(
        formal_changes=adoption.formal_changes,
        objects=tuple(sorted((obj["object_id"], obj["kind"]) for obj in objects)),
        facts=tuple(
            sorted(
                {(fact["business_id"], fact["kind"], fact["period"]) for fact in facts},
                key=lambda value: (value[0], value[1], value[2] or ""),
            )
        ),
        note_scopes=tuple(sorted(notes)),
        issue_scopes=tuple(sorted(issues)),
        working_period_changed=working_period_changed,
    )


def closed(period_from, period_to):
    return ReadingImpact(closure_ranges=(PeriodRange(period_from, period_to),))
