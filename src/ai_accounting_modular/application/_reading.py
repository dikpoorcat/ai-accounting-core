"""Notify rebuildable reads using actual formal changes, never the whole submission."""

from ai_accounting_modular.reading.public import PeriodRange, ReadingImpact


def _key(reference):
    return reference["business_id"], reference["result_slot"]


def applied(*, changes, before, adoption, heads, previous_heads, objects, sources, notes, issues):
    prior = {_key(reference): reference for reference in before}
    after = {_key(reference): reference for reference in adoption.results}
    slots = {
        key
        for key in prior.keys() | after.keys()
        if ((prior[key]["result_id"], prior[key]["generation"]) if key in prior else None)
        != ((after[key]["result_id"], after[key]["generation"]) if key in after else None)
    }
    current, posted, periods = set(), set(), set()
    for key in slots:
        old, new = prior.get(key), after.get(key)
        for reference in (old, new):
            if reference is not None:
                current.add(reference["business_period"])
        if old is not None and old["state"] == "open":
            posted.add(old["posting_period"])
        if new is not None:
            posted.add(new["posting_period"])
    business_ids = {change.business_id for change in changes if change.changed}
    business_ids.update(key[0] for key in slots)
    families = set()
    for business_id in business_ids:
        for head in (previous_heads.get(business_id), heads.get(business_id)):
            if head is not None:
                families.add(head["family"])
                if head["period"] is not None:
                    periods.add(head["period"])
    return ReadingImpact(
        changes={
            "families": sorted(families),
            "business_ids": sorted(business_ids),
            "object_ids": sorted(objects),
            "periods": sorted(periods | current | posted),
            "slots": [
                {"business_id": business_id, "slot": slot} for business_id, slot in sorted(slots)
            ],
        },
        current_ranges=tuple(PeriodRange(period, period) for period in sorted(current)),
        posted_ranges=tuple(PeriodRange(period, period) for period in sorted(posted)),
        source_ids=tuple(sorted(sources)),
        note_scopes=tuple(sorted(notes)),
        issue_scopes=tuple(sorted(issues)),
    )


def closed(period_from, period_to):
    return ReadingImpact(
        changes={
            "families": [],
            "business_ids": [],
            "object_ids": [],
            "periods": [period_to],
            "slots": [],
        },
        posted_ranges=(PeriodRange(period_from, period_to),),
    )
