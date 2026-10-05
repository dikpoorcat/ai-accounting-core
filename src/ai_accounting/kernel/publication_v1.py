"""Released v1 immutable publication identity and chain verification."""

from dataclasses import asdict

from .contracts import KernelError
from .history_encoding_v1 import canonical, digest, sum_fen
from .history_types_v1 import Read, YearMonth
from .stored_json_v1 import loads_unique

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


def _normal_publication(connection, publication):
    """A fixed v1 review adopts the last non-review publication's version."""
    visited = set()
    while publication["mode"] == "review_no_impact":
        if publication["id"] in visited:
            _invalid(publication["id"], "review_chain_cycle")
        visited.add(publication["id"])
        previous = connection.execute(
            "SELECT * FROM calculation_publication WHERE id=?",
            (publication["previous_publication_id"],),
        ).fetchone()
        if previous is None:
            _invalid(publication["id"], "missing_predecessor")
        verify_record(previous)
        if (
            previous["mode"] == "withdrawn"
            or previous["calculation_id"] is None
            or previous["sequence"] >= publication["sequence"]
            or any(previous[field] != publication[field] for field in (
                "subject_id", "posting_period", "voucher_id", "baseline_calculation_id",
            ))
        ):
            _invalid(publication["id"], "review_moved_accounting")
        publication = previous
    if publication["mode"] not in {"initial", "open_replace", "closed_correction"}:
        _invalid(publication["id"], "unknown_mode")
    return publication


def _verify_cleared_heads(connection, identifiers):
    """Read only exceptional v1 bodies; this does not publish a body proof."""
    rows = list(connection.execute(
        "SELECT c.id,c.outcome,c.digest FROM json_each(?) ids "
        "LEFT JOIN calculation c ON c.id=ids.value",
        (canonical(sorted(identifiers)),),
    ))
    if {row["id"] for row in rows} != identifiers:
        _invalid("*", "current_calculation_missing")
    for row in rows:
        try:
            outcome = loads_unique(row["outcome"])
            valid = digest(outcome) == row["digest"]
        except (ValueError, TypeError, OverflowError) as exc:
            raise KernelError(
                "content_integrity_failed", "已保存的 v1 核算结果有重复字段或格式错误",
                component="publication", record_id=row["id"], reason="invalid_clear_outcome",
            ) from exc
        if not valid:
            _invalid(row["id"], "clear_outcome_digest")
        if (not isinstance(outcome, dict) or not isinstance(outcome.get("lines"), list)
                or outcome["lines"]):
            _invalid(row["id"], "nonzero_publication_head_missing")


def verify_open_voucher_heads(connection, through_period, *, posting_period=None, subject_ids=None):
    """Fixed v1 forward checks for terminal publications in the actual open scope.

    Reversal expectations come independently from exact correction publications
    and strictly decoded sealed baselines. Complete source content and frozen
    adoption remain the separate full-source verifier's responsibilities.
    """
    restrictions = [
        "p.posting_period<=?",
        "p.posting_period>(SELECT coalesce(max(period),-1) FROM period_close)",
        "NOT EXISTS(SELECT 1 FROM calculation_publication later "
        "WHERE later.previous_publication_id=p.id)",
    ]
    parameters = [through_period]
    if posting_period is not None:
        restrictions.append("p.posting_period=?")
        parameters.append(posting_period)
    if subject_ids is not None:
        restrictions.append("p.subject_id IN (SELECT value FROM json_each(?))")
        parameters.append(canonical(sorted(set(subject_ids))))
    publications = list(connection.execute(
        "SELECT p.*,a.calculation_id current_id,c.id calculation_exists,"
        "c.subject_id calculation_subject,c.kind calculation_kind,c.period source_period,"
        "f.id fact_exists,f.subject_id fact_subject,f.period fact_period,s.kind fact_kind,"
        "h.version_id,v.id version_exists,v.voucher_id version_voucher,"
        "v.calculation_id version_calculation,v.period version_period,v.reverses_id "
        "FROM calculation_publication p "
        "LEFT JOIN calculation_current a ON a.subject_id=p.subject_id "
        "LEFT JOIN calculation c ON c.id=p.calculation_id "
        "LEFT JOIN fact_revision f ON f.id=c.fact_id LEFT JOIN subject s ON s.id=f.subject_id "
        "LEFT JOIN voucher_current h ON h.voucher_id=p.voucher_id "
        "LEFT JOIN voucher_version v ON v.id=h.version_id "
        "WHERE " + " AND ".join(restrictions), parameters,
    ))
    active, cleared = [], set()
    for row in publications:
        verify_record(row)
        if row["mode"] == "withdrawn":
            if (row["calculation_id"] is not None or row["voucher_id"] is not None
                    or row["current_id"] is not None):
                _invalid(row["id"], "withdrawn_current_head")
            continue
        if (
            row["calculation_exists"] is None or row["fact_exists"] is None
            or row["current_id"] != row["calculation_id"]
            or row["calculation_subject"] != row["subject_id"]
            or (row["calculation_subject"], row["calculation_kind"], row["source_period"])
            != (row["fact_subject"], row["fact_kind"], row["fact_period"])
        ):
            _invalid(row["id"], "current_source_identity")
        normal = _normal_publication(connection, row)
        active.append(row)
        if row["voucher_id"] is None:
            continue
        if row["version_id"] is None:
            cleared.add(row["calculation_id"])
        elif (
            row["version_exists"] is None
            or row["version_voucher"] != row["voucher_id"]
            or row["version_period"] != row["posting_period"]
            or row["version_calculation"] != normal["calculation_id"]
            or row["reverses_id"] is not None
        ):
            _invalid(row["id"], "current_voucher_head_mismatch")
    if cleared:
        _verify_cleared_heads(connection, cleared)
    if not active:
        return
    verify_open_correction_publication_heads(connection, active)


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


def verify_open_correction_heads(connection, calculations, publications, vouchers):
    """Check expected reversals only after complete source/chain/line verification.

    The supplied maps are the caller's strictly verified source scope. This
    helper creates neither a source proof nor a cache and never loads bodies.
    Preserved publications independently determine required versions, so a
    deleted reversal cannot disappear from its own expected set.
    """
    if not calculations:
        return
    by_id = {row["id"]: row for row in publications.values()}
    closed_through = connection.execute(
        "SELECT coalesce(max(period),-1) FROM period_close"
    ).fetchone()[0]
    current = list(connection.execute(
        "SELECT h.subject_id,h.calculation_id FROM json_each(?) ids "
        "JOIN calculation_current h ON h.calculation_id=ids.value",
        (canonical(sorted(calculations)),),
    ))
    expected_heads = {}

    def previous(publication):
        prior = by_id.get(publication["previous_publication_id"])
        if prior is None or prior["subject_id"] != publication["subject_id"]:
            _invalid(publication["id"], "correction_predecessor_missing")
        return prior

    def require_version(ident, voucher_id, calculation_id, period, reverses_id):
        actual = vouchers.get(ident)
        if actual is None or (
            actual["id"], actual["voucher_id"], actual["calculation_id"],
            actual["period"], actual["reverses_id"],
        ) != (ident, voucher_id, calculation_id, period, reverses_id):
            _invalid(ident, "correction_voucher_source_missing")
        return actual

    for head in current:
        terminal = publications.get(head["calculation_id"])
        if terminal is None or terminal["posting_period"] <= closed_through:
            continue
        if terminal["subject_id"] != head["subject_id"]:
            _invalid(terminal["id"], "correction_current_identity")
        path, visited = [], set()
        root = terminal
        while root["mode"] in {"review_no_impact", "open_replace"}:
            if root["id"] in visited:
                _invalid(root["id"], "correction_chain_cycle")
            visited.add(root["id"])
            path.append(root)
            root = previous(root)
        if root["mode"] != "closed_correction":
            continue
        baseline = calculations.get(root["baseline_calculation_id"])
        if baseline is None:
            _invalid(root["id"], "correction_baseline_source_missing")
        if not baseline["decoded"]["lines"]:
            # A cleared baseline has no posted version to reverse, even when
            # its publication retains an older reserved logical voucher.
            continue
        prior = previous(root)
        if prior["calculation_id"] != root["baseline_calculation_id"]:
            _invalid(root["id"], "correction_baseline_identity")
        original_publication = prior
        reviewed = set()
        while original_publication["mode"] == "review_no_impact":
            if original_publication["id"] in reviewed:
                _invalid(original_publication["id"], "correction_chain_cycle")
            reviewed.add(original_publication["id"])
            original_publication = previous(original_publication)
        if prior["voucher_id"] is None:
            _invalid(root["id"], "correction_baseline_voucher_missing")
        original_id = "v_" + digest([
            prior["voucher_id"], original_publication["calculation_id"],
            prior["posting_period"], None,
        ]).hex()
        original = require_version(
            original_id, prior["voucher_id"], original_publication["calculation_id"],
            prior["posting_period"], None,
        )
        reverse_voucher = root["calculation_id"] + ":reverse"
        reverse_calculation = root["calculation_id"]
        posting = root["posting_period"]
        for later in reversed(path):
            if later["posting_period"] != posting:
                if later["mode"] != "open_replace":
                    _invalid(later["id"], "correction_review_moved_period")
                reverse_calculation = later["calculation_id"]
            posting = later["posting_period"]
        reverse_id = "v_" + digest([
            reverse_voucher, reverse_calculation, posting, original_id,
        ]).hex()
        reversal = require_version(
            reverse_id, reverse_voucher, reverse_calculation, posting, original_id,
        )
        if reversal["total"] != original["total"]:
            _invalid(reverse_id, "correction_reversal_total_mismatch")
        expected_heads[reverse_voucher] = reverse_id
    if not expected_heads:
        return
    heads = dict(connection.execute(
        "SELECT ids.value,h.version_id FROM json_each(?) ids "
        "LEFT JOIN voucher_current h ON h.voucher_id=ids.value",
        (canonical(sorted(expected_heads)),),
    ))
    if heads != expected_heads:
        _invalid("*", "correction_current_reversal_missing")


def _correction_baseline_total(row):
    """Interpret only the exact sealed baseline needed to decide reversal absence."""
    try:
        outcome = loads_unique(row["outcome"])
        if digest(outcome) != row["digest"]:
            _invalid(row["id"], "correction_baseline_digest")
        lines = outcome.get("lines") if isinstance(outcome, dict) else None
        if not isinstance(lines, list):
            _invalid(row["id"], "correction_baseline_lines")
        debits, credits = [], []
        for line in lines:
            if not isinstance(line, dict) or set(line) != {
                "account", "debit", "credit", "cashflow",
            }:
                _invalid(row["id"], "correction_baseline_lines")
            debit, credit, cashflow = line["debit"], line["credit"], line["cashflow"]
            if (
                not isinstance(line["account"], str) or not line["account"]
                or type(debit) is not int or type(credit) is not int
                or debit < 0 or credit < 0 or bool(debit) == bool(credit)
                or cashflow is not None and (not isinstance(cashflow, str) or not cashflow)
            ):
                _invalid(row["id"], "correction_baseline_lines")
            debits.append(debit)
            credits.append(credit)
        total = sum_fen(debits)
        if total != sum_fen(credits) or lines and len(lines) < 2:
            _invalid(row["id"], "correction_baseline_lines")
        return total, outcome
    except (ValueError, TypeError, OverflowError) as exc:
        raise KernelError(
            "content_integrity_failed", "开放更正的冻结基线正文无法严格核验",
            component="publication", record_id=row["id"], reason="correction_baseline_json",
        ) from exc




def _correction_baseline_totals(connection, rows):
    """Bind baseline bytes to the immutable calculation ID using exact saved inputs."""
    encoded = canonical(sorted(rows))
    dependencies = {ident: set() for ident in rows}
    dependency_facts = {ident: set() for ident in rows}
    reads = {ident: [] for ident in rows}
    for row in connection.execute(
        "SELECT d.calculation_id,d.upstream_id,c.id found FROM json_each(?) ids "
        "JOIN dependency_calculation d ON d.calculation_id=ids.value "
        "LEFT JOIN calculation c ON c.id=d.upstream_id", (encoded,),
    ):
        if row["found"] is None or row["upstream_id"] == row["calculation_id"]:
            _invalid(row["calculation_id"], "correction_baseline_dependency")
        dependencies[row["calculation_id"]].add(row["upstream_id"])
    for row in connection.execute(
        "SELECT d.calculation_id,d.fact_id,f.id found FROM json_each(?) ids "
        "JOIN dependency_fact d ON d.calculation_id=ids.value "
        "LEFT JOIN fact_revision f ON f.id=d.fact_id", (encoded,),
    ):
        if row["found"] is None:
            _invalid(row["calculation_id"], "correction_baseline_dependency")
        dependency_facts[row["calculation_id"]].add(row["fact_id"])
    for row in connection.execute(
        "SELECT d.* FROM json_each(?) ids JOIN dependency_scope d ON d.calculation_id=ids.value",
        (encoded,),
    ):
        try:
            reads[row["calculation_id"]].append(Read(
                row["source"], row["kind"], row["scope_key"],
                None if row["before_period"] == 119988
                else YearMonth.from_ordinal(row["before_period"]),
            ))
        except (ValueError, TypeError) as exc:
            raise KernelError(
                "content_integrity_failed", "开放更正的冻结基线读取范围无效",
                component="publication", record_id=row["calculation_id"],
                reason="correction_baseline_scope",
            ) from exc
    totals = {}
    for ident, row in rows.items():
        total, outcome = _correction_baseline_total(row)
        if row["fact_id"] not in dependency_facts[ident]:
            _invalid(ident, "correction_baseline_own_fact_missing")
        versions = dependencies[ident] | (dependency_facts[ident] - {row["fact_id"]})
        payload = {
            "fact": row["fact_id"], "outcome": outcome,
            "reads": [asdict(read) for read in sorted(reads[ident], key=repr)],
            "program": row["program_version"],
        }
        first_key = "c_" + digest({**payload, "versions": sorted(versions)}).hex()
        if ident != first_key and ident != "c_" + digest({
            **payload, "versions": sorted(versions | {row["fact_id"]}),
        }).hex():
            _invalid(ident, "correction_baseline_input_digest")
        totals[ident] = total
    return totals


def verify_open_correction_publication_heads(connection, publications):
    """Prove expected reversal heads from an already authenticated terminal scope.

    Only correction ancestors' headers, exact baseline bodies and saved input
    IDs/scopes are read. The immutable calculation ID independently binds bytes.
    This does not prove all source content, frozen adoption, or voucher lines, and
    publishes no body proof/cache. Complete-source verification has its separate
    API. Return exact checked reversal IDs for the caller's existing voucher proof.
    """
    candidates = {
        row["id"]: row for row in publications
        if row["baseline_calculation_id"] is not None
    }
    if not candidates:
        return set()
    closed_through = connection.execute(
        "SELECT coalesce(max(period),-1) FROM period_close"
    ).fetchone()[0]
    candidates = {
        ident: row for ident, row in candidates.items()
        if row["posting_period"] > closed_through
    }
    if not candidates:
        return set()
    # Phase 0 follows the current open tranche to its correction root. Phase 1
    # follows only the root predecessor's reviews to the original version owner.
    # UNION bounds even a damaged cycle; Python then rejects nondecreasing links.
    rows = connection.execute(
        "WITH RECURSIVE chain(terminal,id,previous_id,mode,phase) AS ("
        "SELECT p.id,p.id,p.previous_publication_id,p.mode,0 FROM json_each(?) ids "
        "JOIN calculation_publication p ON p.id=ids.value UNION "
        "SELECT chain.terminal,p.id,p.previous_publication_id,p.mode,"
        "CASE WHEN chain.mode='closed_correction' THEN 1 ELSE chain.phase END "
        "FROM chain JOIN calculation_publication p ON p.id=chain.previous_id "
        "WHERE (chain.phase=0 AND chain.mode IN "
        "('review_no_impact','open_replace','closed_correction')) "
        "OR (chain.phase=1 AND chain.mode='review_no_impact')) "
        "SELECT chain.terminal,p.*,c.id calculation_exists,c.subject_id calculation_subject,"
        "c.kind calculation_kind,c.period source_period,"
        "f.id fact_exists,f.subject_id fact_subject,f.period fact_period,s.kind fact_kind,"
        "EXISTS(SELECT 1 FROM calculation_seal z WHERE z.calculation_id=c.id) "
        "calculation_sealed,EXISTS(SELECT 1 FROM fact_seal z WHERE z.fact_id=f.id) fact_sealed "
        "FROM chain JOIN calculation_publication p ON p.id=chain.id "
        "LEFT JOIN calculation c ON c.id=p.calculation_id "
        "LEFT JOIN fact_revision f ON f.id=c.fact_id LEFT JOIN subject s ON s.id=f.subject_id",
        (canonical(sorted(candidates)),),
    )
    by_terminal = {ident: {} for ident in candidates}
    checked = set()
    for row in rows:
        if row["id"] not in checked:
            verify_record(row)
            if (
                row["calculation_exists"] is None or row["fact_exists"] is None
                or not row["calculation_sealed"] or not row["fact_sealed"]
                or row["calculation_subject"] != row["subject_id"]
                or (row["calculation_subject"], row["calculation_kind"], row["source_period"])
                != (row["fact_subject"], row["fact_kind"], row["fact_period"])
            ):
                _invalid(row["id"], "correction_source_identity")
            checked.add(row["id"])
        by_terminal[row["terminal"]][row["id"]] = row
    plans = []
    for ident, terminal in candidates.items():
        chain = by_terminal[ident]
        actual = chain.get(ident)
        if actual is None or any(actual[key] != terminal[key] for key in CONTENT_FIELDS):
            _invalid(ident, "correction_terminal_missing")

        def previous(current, chain=chain):
            prior = chain.get(current["previous_publication_id"])
            if (
                prior is None or prior["subject_id"] != current["subject_id"]
                or prior["sequence"] >= current["sequence"] or prior["mode"] == "withdrawn"
            ):
                _invalid(current["id"], "correction_predecessor_missing")
            if current["mode"] == "closed_correction":
                if (
                    current["baseline_calculation_id"] != prior["calculation_id"]
                    or current["posting_period"] <= prior["posting_period"]
                    or prior["posting_period"] > closed_through
                ):
                    _invalid(current["id"], "correction_baseline_identity")
            elif current["baseline_calculation_id"] != prior["baseline_calculation_id"]:
                _invalid(current["id"], "correction_segment_baseline")
            if current["mode"] == "review_no_impact" and (
                current["posting_period"], current["voucher_id"],
            ) != (prior["posting_period"], prior["voucher_id"]):
                _invalid(current["id"], "correction_review_moved_accounting")
            return prior

        root, path = actual, []
        while root["mode"] in {"review_no_impact", "open_replace"}:
            path.append(root)
            root = previous(root)
        if root["mode"] != "closed_correction" or root["posting_period"] <= closed_through:
            _invalid(ident, "correction_root_missing")
        prior = previous(root)
        owner = prior
        while owner["mode"] == "review_no_impact":
            owner = previous(owner)
        if owner["mode"] not in {"initial", "open_replace", "closed_correction"}:
            _invalid(owner["id"], "correction_baseline_owner_missing")
        reverse_calculation, posting = root["calculation_id"], root["posting_period"]
        for later in reversed(path):
            if later["posting_period"] != posting:
                if (
                    later["mode"] != "open_replace" or later["posting_period"] <= closed_through
                    or later["posting_period"] != later["source_period"]
                ):
                    _invalid(later["id"], "correction_review_moved_period")
                reverse_calculation = later["calculation_id"]
            posting = later["posting_period"]
        plans.append((root, prior, owner, reverse_calculation, posting))
    baselines = {root["baseline_calculation_id"] for root, *_ in plans}
    baseline_rows = {
        row["id"]: row for row in connection.execute(
            "SELECT c.id,c.subject_id,c.fact_id,c.program_version,c.outcome,c.digest "
            "FROM json_each(?) ids "
            "LEFT JOIN calculation c ON c.id=ids.value",
            (canonical(sorted(baselines)),),
        )
    }
    if baseline_rows.keys() != baselines:
        _invalid("*", "correction_baseline_source_missing")
    totals = _correction_baseline_totals(connection, baseline_rows)
    expected, reverse_ids = {}, set()
    for root, prior, owner, reverse_calculation, posting in plans:
        total = totals[root["baseline_calculation_id"]]
        if not total:
            # A legitimate clear can retain an old reserved logical voucher.
            continue
        if prior["voucher_id"] is None:
            _invalid(root["id"], "correction_baseline_voucher_missing")
        original_id = "v_" + digest([
            prior["voucher_id"], owner["calculation_id"], prior["posting_period"], None,
        ]).hex()
        expected[original_id] = (
            original_id, prior["voucher_id"], owner["calculation_id"],
            prior["posting_period"], None, total,
        )
        logical = root["calculation_id"] + ":reverse"
        reverse_id = "v_" + digest([
            logical, reverse_calculation, posting, original_id,
        ]).hex()
        expected[reverse_id] = (
            reverse_id, logical, reverse_calculation, posting, original_id, total,
        )
        reverse_ids.add(reverse_id)
    if not expected:
        return set()
    actual = {
        row["requested_id"]: row for row in connection.execute(
            "SELECT ids.value requested_id,v.*,r.id logical_exists,h.version_id current_id "
            "FROM json_each(?) ids LEFT JOIN voucher_version v ON v.id=ids.value "
            "LEFT JOIN voucher r ON r.id=v.voucher_id "
            "LEFT JOIN voucher_current h ON h.voucher_id=v.voucher_id",
            (canonical(sorted(expected)),),
        )
    }
    for ident, expected_header in expected.items():
        row = actual.get(ident)
        if row is None or row["logical_exists"] is None or (
            row["id"], row["voucher_id"], row["calculation_id"],
            row["period"], row["reverses_id"], row["total"],
        ) != expected_header:
            _invalid(ident, "correction_voucher_source_missing")
        if ident in reverse_ids and row["current_id"] != ident:
            _invalid(ident, "current_reversal_head_mismatch")
    return reverse_ids
