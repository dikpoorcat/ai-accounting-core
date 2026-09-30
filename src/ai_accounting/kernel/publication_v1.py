"""Released v1 immutable publication identity and chain verification."""

from .contracts import KernelError
from .history_encoding_v1 import canonical, digest

CONTENT_FIELDS = (
    "sequence", "subject_id", "previous_publication_id", "calculation_id",
    "mode", "posting_period", "baseline_calculation_id", "voucher_id",
)


def _invalid(ident, reason):
    raise KernelError(
        "content_integrity_failed", "正式发布关系不完整或不一致",
        component="publication", record_id=ident, reason=reason,
    )


def verify_record(row):
    if row["id"] != "p_" + digest({key: row[key] for key in CONTENT_FIELDS}).hex():
        _invalid(row["id"], "publication_digest")


def active_tranches(connection, subject_ids=None):
    restriction = (
        " WHERE subject_id IN (SELECT value FROM json_each(?))" if subject_ids is not None else ""
    )
    parameters = (canonical(sorted(subject_ids)),) if subject_ids is not None else ()
    active = {}
    for record in connection.execute(
        "SELECT * FROM calculation_publication" + restriction + " ORDER BY sequence",
        parameters,
    ):
        row = dict(record)
        verify_record(row)
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
        else:
            _invalid(row["id"], "unknown_mode")
    return [row for segments in active.values() for row in segments]


def verify_publication_chain(connection, subject_ids=None):
    restriction = (
        " WHERE subject_id IN (SELECT value FROM json_each(?))" if subject_ids is not None else ""
    )
    parameters = (canonical(sorted(subject_ids)),) if subject_ids is not None else ()
    rows = [
        dict(row) for row in connection.execute(
            "SELECT * FROM calculation_publication" + restriction + " ORDER BY sequence",
            parameters,
        )
    ]
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
