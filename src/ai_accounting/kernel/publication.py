"""Immutable publication chains and the posting rule shared by all publishers."""

from .contracts import KernelError
from .types import YearMonth, canonical, digest

CONTENT_FIELDS = (
    "sequence",
    "subject_id",
    "previous_publication_id",
    "calculation_id",
    "mode",
    "posting_period",
    "baseline_calculation_id",
    "voucher_id",
)


def _invalid(ident, reason):
    raise KernelError(
        "content_integrity_failed",
        "正式发布关系不完整或不一致",
        component="publication",
        record_id=ident,
        reason=reason,
    )


def verify_record(row):
    """Verify a bounded publication record without loading its calculation."""
    if row["id"] != "p_" + digest({key: row[key] for key in CONTENT_FIELDS}).hex():
        _invalid(row["id"], "publication_digest")


def chain_rows(connection, subject_ids=None):
    restriction = (
        " WHERE subject_id IN (SELECT value FROM json_each(?))" if subject_ids is not None else ""
    )
    parameters = (canonical(sorted(subject_ids)),) if subject_ids is not None else ()
    return [
        dict(r)
        for r in connection.execute(
            "SELECT * FROM calculation_publication" + restriction + " ORDER BY sequence", parameters
        )
    ]


def head(connection, subject_id):
    return heads(connection, [subject_id]).get(subject_id)


def heads(connection, subject_ids):
    rows = connection.execute(
        "SELECT p.*,c.period AS source_period FROM calculation_publication p "
        "JOIN json_each(?) ids ON p.subject_id=ids.value "
        "LEFT JOIN calculation c ON c.id=p.calculation_id WHERE "
        "NOT EXISTS(SELECT 1 FROM calculation_publication n "
        "WHERE n.previous_publication_id=p.id)",
        (canonical(sorted(subject_ids)),),
    )
    return {row["subject_id"]: dict(row) for row in rows}


def route(
    source_period, previous, closed_through, requested=None, *, no_impact=False, explicit=True
):
    """Return the complete decision; calling time never determines posting time."""
    requested = YearMonth(requested).ordinal if requested is not None else None
    if previous and previous["mode"] != "withdrawn":
        if no_impact:
            posting, mode = previous["posting_period"], "review_no_impact"
        elif previous["posting_period"] <= closed_through:
            posting, mode = requested, "closed_correction"
            if (
                requested is not None
                and source_period > closed_through
                and requested != source_period
            ):
                raise KernelError("posting_period_conflict", "所属月仍开放，更正须记入该所属月")
        else:
            # An open correction tranche keeps its original month, even if the
            # subject's original accounting month is already frozen.
            moved_to_open = (
                source_period > closed_through and source_period != previous["source_period"]
            )
            posting = source_period if moved_to_open else previous["posting_period"]
            mode = "open_replace"
    else:
        posting = requested if source_period <= closed_through else source_period
        mode = "initial"
    if posting is None:
        raise KernelError("posting_period_required", "所属月或原结果已关账，须明确指定开放入账月")
    if mode != "review_no_impact" and posting <= closed_through:
        raise KernelError("closed_period", "实际入账月份必须处于开放期")
    # An automatically recalculated open dependant retains its own month. The
    # requested month selects a closed correction; it cannot move that dependant.
    # Explicit roots still reject a conflicting month, including standalone review.
    constrained = (
        explicit
        or mode == "closed_correction"
        or (mode == "initial" and source_period <= closed_through)
    )
    if requested is not None and requested != posting and constrained:
        raise KernelError(
            "posting_period_conflict",
            "指定入账月与业务应采用的月份冲突",
            posting_period=str(YearMonth.from_ordinal(posting)),
        )
    baseline = (
        previous["calculation_id"]
        if mode == "closed_correction"
        else previous["baseline_calculation_id"]
        if previous and mode != "initial"
        else None
    )
    return {
        "source_period": source_period,
        "posting_period": posting,
        "mode": mode,
        "previous_publication_id": previous["id"] if previous else None,
        "baseline_calculation_id": baseline,
    }


def append(connection, subject_id, calculation_id, decision, voucher_id):
    sequence = connection.execute(
        "SELECT coalesce(max(sequence),0)+1 FROM calculation_publication"
    ).fetchone()[0]
    values = {
        "sequence": sequence,
        "subject_id": subject_id,
        "previous_publication_id": decision["previous_publication_id"],
        "calculation_id": calculation_id,
        "mode": decision["mode"],
        "posting_period": decision["posting_period"],
        "baseline_calculation_id": decision["baseline_calculation_id"],
        "voucher_id": voucher_id,
    }
    ident = "p_" + digest(values).hex()
    connection.execute(
        "INSERT INTO calculation_publication(id,sequence,subject_id,previous_publication_id,"
        "calculation_id,"
        "mode,posting_period,baseline_calculation_id,voucher_id) VALUES(?,?,?,?,?,?,?,?,?)",
        (ident, *values.values()),
    )
    return ident


def active_tranches(connection, subject_ids=None):
    """Reconstruct effective segments without evaluating any historical fact."""
    active = {}
    for row in chain_rows(connection, subject_ids):
        segments = active.setdefault(row["subject_id"], [])
        if row["mode"] in ("initial", "closed_correction"):
            segments.append(row)
        elif row["mode"] in ("open_replace", "review_no_impact"):
            if not segments:
                _invalid(row["id"], "missing_segment")
            segments[-1] = row
        elif row["mode"] == "withdrawn":
            if not segments:
                _invalid(row["id"], "missing_segment")
            segments.pop()
    return [row for segments in active.values() for row in segments]


def verify_publication_chain(connection, subject_ids=None):
    rows = chain_rows(connection, subject_ids)
    calculations = {
        row[0]: row[1]
        for row in connection.execute(
            "SELECT id,subject_id FROM calculation WHERE id IN (SELECT value FROM json_each(?))",
            (canonical(sorted({row["calculation_id"] for row in rows if row["calculation_id"]})),),
        )
    }
    heads = dict(
        connection.execute(
            "SELECT subject_id,calculation_id FROM calculation_current WHERE subject_id IN "
            "(SELECT value FROM json_each(?))",
            (canonical(sorted({row["subject_id"] for row in rows})),),
        )
    )
    previous = {}
    for row in rows:
        prior = previous.get(row["subject_id"])
        if row["previous_publication_id"] != (prior["id"] if prior else None):
            _invalid(row["id"], "chain_fork_or_gap")
        verify_record(row)
        if row["mode"] == "initial":
            if row["baseline_calculation_id"] or (prior and prior["mode"] != "withdrawn"):
                _invalid(row["id"], "initial_baseline")
        elif prior is None or prior["mode"] == "withdrawn":
            _invalid(row["id"], "missing_predecessor")
        elif row["mode"] == "closed_correction":
            if (
                row["baseline_calculation_id"] != prior["calculation_id"]
                or row["posting_period"] <= prior["posting_period"]
            ):
                _invalid(row["id"], "correction_baseline")
        elif row["baseline_calculation_id"] != prior["baseline_calculation_id"]:
            _invalid(row["id"], "segment_baseline")
        if row["mode"] == "review_no_impact" and (
            row["posting_period"] != prior["posting_period"]
            or row["voucher_id"] != prior["voucher_id"]
        ):
            _invalid(row["id"], "review_moved_accounting")
        if row["calculation_id"]:
            if calculations.get(row["calculation_id"]) != row["subject_id"]:
                _invalid(row["id"], "calculation_identity")
        elif row["mode"] != "withdrawn" or row["voucher_id"]:
            _invalid(row["id"], "missing_calculation")
        previous[row["subject_id"]] = row
    for subject, row in previous.items():
        if heads.get(subject) != row["calculation_id"]:
            _invalid(row["id"], "current_head")
    return {"subjects": len(previous)}
