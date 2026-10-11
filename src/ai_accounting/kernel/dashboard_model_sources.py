"""Exact raw hits of an authenticated dashboard model, never a full verifier.

The model's production receipt certifies the complete selection and aggregate.
This reader checks actual displayed sources without decoding unseen ancestors.
Independent source verification retains its original recursive semantics.
"""

from __future__ import annotations

from collections import defaultdict

from .calculation_identity import calculation_identity_digest, calculation_identity_prefix
from .content_history_context import month_type, read_type
from .contracts import KernelError, read_identity_payload
from .types import canonical


def _invalid():
    return KernelError("content_integrity_failed", "看板命中来源或凭证内容不一致",
                       component="dashboard_read_model")


def verify_model_sources(
    engine, connection, *, calculation_ids=(), fact_ids=(), through_period=None,
    normalized_lines=None, expected_sources=None, expected_facts=None,
):
    """Check exact bodies, seals and input identities after model authentication.

    Dependency identities participate in the saved calculation ID. Their raw
    bodies, evidence files and publication ancestors are not displayed hits.
    This private model consumer cannot substitute for verify_sources.
    The optional output retains line normalization only for its caller's local
    physical comparison, and is updated only after the entire batch succeeds.
    """
    from .integrity import _check_facts, _outcome_with_lines, _rows

    identifiers = set(calculation_ids)
    calculations = {row["id"]: dict(row) for row in _rows(
        connection, "calculation", "id", identifiers,
    )}
    if calculations.keys() != identifiers:
        raise _invalid()
    fact_identifiers = set(fact_ids)
    own_facts = fact_identifiers | {row["fact_id"] for row in calculations.values()}
    facts, _evidence_ids = _check_facts(engine, connection, own_facts)
    # Bind the model to these actual headers, not another independent read of
    # the same IDs. Every raw fact, seal and object check above still runs.
    if expected_sources is not None and {
        (ident, row["digest"].hex(), row["fact_id"], facts[row["fact_id"]]["digest"].hex())
        for ident, row in calculations.items()
    } != set(expected_sources):
        raise _invalid()
    if expected_facts is not None and {
        (ident, facts[ident]["digest"].hex()) for ident in fact_identifiers
    } != set(expected_facts):
        raise _invalid()
    sealed = {row[0] for row in connection.execute(
        "SELECT s.calculation_id FROM json_each(?) ids JOIN calculation_seal s "
        "ON s.calculation_id=ids.value", (canonical(sorted(identifiers)),),
    )}
    if sealed != identifiers:
        raise _invalid()
    dependencies, fact_dependencies, reads = defaultdict(set), defaultdict(set), defaultdict(list)
    read_model, month_model = read_type(), month_type()
    for row in _rows(connection, "dependency_calculation", "calculation_id", identifiers):
        dependencies[row["calculation_id"]].add(row["upstream_id"])
    for row in _rows(connection, "dependency_fact", "calculation_id", identifiers):
        fact_dependencies[row["calculation_id"]].add(row["fact_id"])
    for row in _rows(connection, "dependency_scope", "calculation_id", identifiers):
        reads[row["calculation_id"]].append(read_model(
            row["source"], row["kind"], row["scope_key"],
            None if row["before_period"] == 119988
            else month_model.from_ordinal(row["before_period"]),
        ))
    outcomes = {}
    checked_lines = {}
    for ident, row in calculations.items():
        fact = facts[row["fact_id"]]
        if (row["subject_id"], row["kind"], row["period"]) != (
            fact["subject_id"], fact["kind"], fact["period"],
        ) or row["fact_id"] not in fact_dependencies[ident] or ident in dependencies[ident]:
            raise _invalid()
        outcome, outcome_encoding, checked_lines[ident] = _outcome_with_lines(row)
        versions = dependencies[ident] | (fact_dependencies[ident] - {row["fact_id"]})
        identity = {
            "fact_id": row["fact_id"], "outcome_encoding": outcome_encoding,
            "reads": [read_identity_payload(read) for read in sorted(reads[ident], key=repr)],
            "program": row["program_version"],
            "encode": canonical,
        }
        identity["prefix"] = calculation_identity_prefix(**identity)
        first_key = "c_" + calculation_identity_digest(**identity, versions=sorted(versions)).hex()
        if ident != first_key:
            second_key = "c_" + calculation_identity_digest(
                **identity, versions=sorted(versions | {row["fact_id"]}),
            ).hex()
            if ident != second_key:
                raise _invalid()
        outcomes[ident] = outcome
    _verify_current_hit_adoptions(connection, calculations, through_period=through_period)
    if normalized_lines is not None:
        normalized_lines.update(checked_lines)
    return outcomes


def _verify_current_hit_adoptions(connection, calculations, *, through_period):
    """Check returned current heads, never replace an old basis with today's head."""
    if not calculations:
        return
    from .asset_batch_models import MEMBER_KINDS, OWNER_KINDS
    from .query_reads import _verify_model_current_publication_voucher_heads

    selected = list(connection.execute(
        "SELECT c.id source_id,c.subject_id source_subject,c.kind source_kind,p.*,"
        "h.calculation_id current_id,"
        "EXISTS(SELECT 1 FROM period_close z WHERE z.period=p.posting_period) closed,"
        "EXISTS(SELECT 1 FROM calculation_publication n "
        "WHERE n.previous_publication_id=p.id) has_successor "
        "FROM json_each(?) ids JOIN calculation c ON c.id=ids.value "
        "LEFT JOIN calculation_publication p ON p.calculation_id=c.id "
        "LEFT JOIN calculation_current h ON h.subject_id=c.subject_id",
        (canonical(sorted(calculations)),),
    ))
    publications, members = {}, {}
    for row in selected:
        if row["id"] is None:
            if row["source_kind"] not in MEMBER_KINDS:
                raise _invalid()
            members[row["source_id"]] = row
            continue
        if row["subject_id"] != row["source_subject"]:
            raise _invalid()
        if (through_period is not None and row["posting_period"] > through_period
                or row["closed"]):
            continue
        if row["has_successor"]:
            if row["current_id"] == row["source_id"]:
                raise _invalid()
            continue
        if row["current_id"] != row["source_id"]:
            raise _invalid()
        publications[row["id"]] = row
    if members:
        # A member has no standalone publication. Its exact immutable membership
        # locates the owner; only an open, current owner creates a current-head
        # obligation. Frozen/older members are not rebound to a newer owner.
        found = set()
        for row in connection.execute(
            "SELECT m.*,c.kind owner_kind,c.subject_id owner_subject,c.period owner_period,"
            "p.*,h.calculation_id owner_current,"
            "EXISTS(SELECT 1 FROM calculation_seal s "
            "WHERE s.calculation_id=c.id) owner_sealed,"
            "EXISTS(SELECT 1 FROM dependency_calculation d "
            "WHERE d.calculation_id=c.id AND d.upstream_id=m.member_calculation_id) dependent,"
            "EXISTS(SELECT 1 FROM period_close z WHERE z.period=p.posting_period) closed,"
            "EXISTS(SELECT 1 FROM calculation_publication n "
            "WHERE n.previous_publication_id=p.id) has_successor "
            "FROM json_each(?) ids JOIN asset_batch_member m "
            "ON m.member_calculation_id=ids.value "
            "LEFT JOIN calculation c ON c.id=m.owner_calculation_id "
            "LEFT JOIN calculation_publication p ON p.calculation_id=c.id "
            "LEFT JOIN calculation_current h ON h.subject_id=c.subject_id",
            (canonical(sorted(members)),),
        ):
            ident = row["member_calculation_id"]
            source = calculations[ident]
            if (row["owner_kind"] not in OWNER_KINDS or not row["owner_sealed"]
                    or not row["dependent"] or row["id"] is None
                    or row["subject_id"] != row["owner_subject"]
                    or row["owner_period"] != source["period"]
                    or source["kind"] != (
                        "asset_activation" if row["owner_kind"] == "asset_activation_batch"
                        else "asset_consumption"
                    )
                    or (row["member_subject_id"], row["member_fact_id"], row["result_digest"])
                    != (source["subject_id"], source["fact_id"], source["digest"])):
                raise _invalid()
            found.add(ident)
            if (row["closed"]
                    or through_period is not None and row["posting_period"] > through_period):
                continue
            if row["has_successor"]:
                if row["owner_current"] == row["owner_calculation_id"]:
                    raise _invalid()
                continue
            if (row["owner_current"] != row["owner_calculation_id"]
                    or members[ident]["current_id"] != ident):
                raise _invalid()
            publications[row["id"]] = row
        if found != members.keys():
            raise _invalid()
    _verify_model_current_publication_voucher_heads(connection, publications.values())


def verify_model_voucher_lines(connection, voucher_ids, outcomes, *, normalized_lines=None):
    """Prove the displayed physical lines and any exact reversal original."""
    vouchers = {row["id"]: dict(row) for row in connection.execute(
        "SELECT v.* FROM json_each(?) ids JOIN voucher_version v ON v.id=ids.value",
        (canonical(sorted(voucher_ids)),),
    )}
    if vouchers.keys() != set(voucher_ids):
        raise _invalid()
    return _verify_model_voucher_rows(
        connection, vouchers, outcomes, normalized_lines=normalized_lines,
    )


def _verify_model_voucher_rows(connection, vouchers, outcomes, *, normalized_lines=None):
    """Consume actual selected SQLite headers; retain all physical line checks.

    The model assembler already read v.* in its exact ID selection. Its local
    headers are values, not a content/adoption success flag. The ID entry point
    above keeps its independent lookup for other callers.
    """
    from .integrity import _lines
    from .types import checked

    vouchers = dict(vouchers)
    original_ids = {row["reverses_id"] for row in vouchers.values() if row["reverses_id"]}
    missing = original_ids - vouchers.keys()
    vouchers.update((row["id"], dict(row)) for row in connection.execute(
        "SELECT v.* FROM json_each(?) ids JOIN voucher_version v ON v.id=ids.value",
        (canonical(sorted(missing)),),
    ))
    if not original_ids <= vouchers.keys():
        raise _invalid()
    lines = defaultdict(list)
    for row in connection.execute(
        "SELECT l.* FROM json_each(?) ids JOIN voucher_line l ON l.version_id=ids.value "
        "ORDER BY l.version_id,l.line_no", (canonical(sorted(vouchers)),),
    ):
        if row["line_no"] != len(lines[row["version_id"]]) + 1:
            raise _invalid()
        lines[row["version_id"]].append({
            key: row[key] for key in ("account", "debit", "credit", "cashflow")
        })
    for ident, row in vouchers.items():
        actual = _lines(lines[ident], "voucher", ident)
        if not actual or checked(sum(line[1] for line in actual)) != row["total"]:
            raise _invalid()
        if row["reverses_id"]:
            original = vouchers[row["reverses_id"]]
            if original["reverses_id"] or original["period"] >= row["period"]:
                raise _invalid()
            expected = [(line["account"], line["credit"], line["debit"], line["cashflow"])
                        for line in lines[original["id"]]]
        else:
            outcome = outcomes.get(row["calculation_id"])
            if outcome is None or (
                normalized_lines is not None and row["calculation_id"] not in normalized_lines
            ):
                raise _invalid()
            expected = (
                normalized_lines[row["calculation_id"]]
                if normalized_lines is not None
                else _lines(outcome["lines"], "calculation", row["calculation_id"])
            )
        if actual != expected:
            raise _invalid()
    return {ident: {**row, "lines": lines[ident]} for ident, row in vouchers.items()}
