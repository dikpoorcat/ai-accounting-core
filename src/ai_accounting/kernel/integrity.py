"""Read-only accounting content checks against saved, never recalculated, sources.

Reference directories are checked last: their corruption cannot hide a source.
Missing historical adoption evidence is corruption under the single close contract.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections import defaultdict
from dataclasses import asdict

from .content_history_context import month_type, read_type, source_json_loads
from .content_history_context import source_canonical as canonical
from .content_history_context import source_checked as checked
from .content_history_context import source_digest as digest
from .content_history_context import source_sum_fen as sum_fen
from .contracts import KernelError
from .projections import require_projections
from .schema import sequence_model, table_name
from .storage import _snapshot_fact_hashes, _snapshot_fact_raws


def _invalid(component, ident, reason):
    raise KernelError(
        "content_integrity_failed",
        "已保存的核算内容或来源关系不一致",
        component=component,
        record_id=str(ident),
        reason=reason,
    )


def _object(raw, component, ident):
    try:
        value = source_json_loads(raw)
    except (ValueError, TypeError):
        _invalid(component, ident, "invalid_json")
    if not isinstance(value, dict):
        _invalid(component, ident, "invalid_object")
    return value


def _lines(lines, component, ident, *, opening=False):
    if not isinstance(lines, list):
        _invalid(component, ident, "invalid_lines")
    result = []
    for line in lines:
        if not isinstance(line, dict) or set(line) != {"account", "debit", "credit", "cashflow"}:
            _invalid(component, ident, "invalid_line_shape")
        account, debit, credit, cashflow = (
            line[key] for key in ("account", "debit", "credit", "cashflow")
        )
        if (
            not isinstance(account, str)
            or not account
            or type(debit) is not int
            or type(credit) is not int
            or debit < 0
            or credit < 0
            or bool(debit) == bool(credit)
            or cashflow is not None
            and (not isinstance(cashflow, str) or not cashflow)
            or opening
            and cashflow is not None
        ):
            _invalid(component, ident, "invalid_line")
        checked(debit)
        checked(credit)
        result.append((account, debit, credit, cashflow))
    if result and (
        len(result) < 2 or sum_fen(row[1] for row in result) != sum_fen(row[2] for row in result)
    ):
        _invalid(component, ident, "unbalanced_lines")
    return result


def _outcome(row):
    ident = row["id"]
    outcome = _object(row["outcome"], "calculation", ident)
    if digest(outcome) != row["digest"]:
        _invalid("calculation", ident, "result_digest_mismatch")
    if not isinstance(outcome.get("values"), dict) or not isinstance(outcome.get("balances"), list):
        _invalid("calculation", ident, "invalid_outcome_shape")
    _lines(outcome.get("lines"), "calculation", ident)
    opening = outcome.get("opening", False)
    opening_lines = outcome.get("opening_lines", [])
    if type(opening) is not bool or opening_lines and (not opening or outcome["lines"]):
        _invalid("calculation", ident, "invalid_opening")
    _lines(opening_lines, "calculation", ident, opening=True)
    for balance in outcome["balances"]:
        if (
            not isinstance(balance, dict)
            or set(balance) != {"category", "key", "amount"}
            or not isinstance(balance["category"], str)
            or not balance["category"]
            or not isinstance(balance["key"], str)
            or not balance["key"]
        ):
            _invalid("calculation", ident, "invalid_balance_effect")
        checked(balance["amount"])
    return outcome


def _rows(connection, table, key, identifiers):
    if identifiers is None:
        return list(connection.execute(f"SELECT * FROM {table}"))
    if not identifiers:
        return []
    return list(
        connection.execute(
            f"SELECT t.* FROM {table} t JOIN json_each(?) ids ON t.{key}=ids.value",
            (canonical(sorted(identifiers)),),
        )
    )


def _source_set(connection, calculation_ids):
    """Expand saved dependencies and voucher owners, not current scope matches."""
    if calculation_ids is None:
        return None
    return {
        row[0]
        for row in connection.execute(
            "WITH RECURSIVE lineage(id) AS (SELECT value FROM json_each(?) UNION "
            "SELECT d.upstream_id FROM lineage ids JOIN dependency_calculation d "
            "ON d.calculation_id=ids.id UNION "
            "SELECT v.calculation_id FROM lineage ids JOIN calculation c ON c.id=ids.id "
            "JOIN calculation_publication p ON p.calculation_id=c.id "
            "JOIN calculation o INDEXED BY calculation_subject ON o.subject_id=c.subject_id "
            "JOIN voucher_version v INDEXED BY voucher_calculation ON v.calculation_id=o.id "
            "WHERE v.voucher_id=p.voucher_id UNION "
            "SELECT original.calculation_id FROM lineage ids JOIN voucher_version v "
            "ON v.calculation_id=ids.id JOIN voucher_version original "
            "ON original.id=v.reverses_id UNION "
            "SELECT old.calculation_id FROM lineage ids JOIN calculation_publication p "
            "ON p.calculation_id=ids.id JOIN calculation_publication old "
            "ON old.id=p.previous_publication_id WHERE old.calculation_id IS NOT NULL UNION "
            "SELECT p.baseline_calculation_id FROM lineage ids JOIN calculation_publication p "
            "ON p.calculation_id=ids.id WHERE p.baseline_calculation_id IS NOT NULL) "
            "SELECT id FROM lineage",
            (canonical(sorted(set(calculation_ids))),),
        )
    }


def _check_facts(engine, connection, identifiers):
    rows = _rows(connection, "fact_revision", "id", identifiers)
    if identifiers is not None and len(rows) != len(identifiers):
        _invalid("fact", "*", "referenced_fact_missing")
    subject_kinds = {
        row[0]: row[1]
        for row in connection.execute(
            "SELECT s.id,s.kind FROM json_each(?) ids JOIN subject s ON s.id=ids.value",
            (canonical(sorted({row["subject_id"] for row in rows})),),
        )
    }
    fact_ids = [row["id"] for row in rows]
    raw_hashes = _snapshot_fact_hashes(engine.store, connection, fact_ids)
    raw_facts = _snapshot_fact_raws(engine.store, connection, fact_ids)
    missing_raw = [ident for ident in fact_ids if ident not in raw_facts]
    if missing_raw:
        raw_facts.update(engine.store.fact_data_many(connection, missing_raw))
    kinds = defaultdict(set)
    for row in rows:
        kinds[subject_kinds.get(row["subject_id"])].add(row["id"])
    verify_fact_child_order(engine, connection, kinds)
    facts = {}
    for row in rows:
        ident = row["id"]
        kind = subject_kinds.get(row["subject_id"])
        if kind not in engine.store.registry.models:
            _invalid("fact", ident, "unknown_fact_kind")
        raw = raw_facts[ident]
        if raw_hashes.get(ident) != row["digest"] and digest(raw) != row["digest"]:
            _invalid("fact", ident, "fact_digest_mismatch")
        if raw.get("period") != str(month_type().from_ordinal(row["period"])):
            _invalid("fact", ident, "fact_period_mismatch")
        facts[ident] = {**dict(row), "kind": kind, "data": raw}
    payload = canonical(sorted(facts))
    sealed = {
        r[0]
        for r in connection.execute(
            "SELECT s.fact_id FROM fact_seal s JOIN json_each(?) ids ON ids.value=s.fact_id",
            (payload,),
        )
    }
    if sealed != set(facts):
        _invalid("fact", "*", "fact_seal_missing")
    evidence_ids = set()
    for row in connection.execute(
        "SELECT e.fact_id,e.evidence_digest FROM fact_evidence e "
        "JOIN json_each(?) ids ON ids.value=e.fact_id",
        (payload,),
    ):
        facts[row[0]].setdefault("evidence", []).append(row[1].hex())
        evidence_ids.add(row[1])
    for fact in facts.values():
        fact.setdefault("evidence", [])
    return facts, evidence_ids


def verify_fact_child_order(engine, connection, kinds):
    """Check the stored sequence positions for these exact fact versions."""
    for kind, fact_ids in kinds.items():
        if kind not in engine.store.registry.models:
            _invalid("fact", "*", "unknown_fact_kind")
        model = engine.store.registry.models[kind]
        for name, field in model.model_fields.items():
            historical_fields = getattr(model, "_v1_fields", None)
            is_sequence = (
                historical_fields[name]["kind"] == "sequence"
                if historical_fields is not None
                else sequence_model(field.annotation) is not None
            )
            if not is_sequence:
                continue
            next_numbers = defaultdict(int)
            for child in connection.execute(
                f"SELECT c.revision_id,c.item_no FROM {table_name(kind)}_{name} c "
                "JOIN json_each(?) ids ON ids.value=c.revision_id ORDER BY c.revision_id,c.item_no",
                (canonical(sorted(fact_ids)),),
            ):
                if child["item_no"] != next_numbers[child["revision_id"]]:
                    _invalid("fact", child["revision_id"], "fact_child_order_mismatch")
                next_numbers[child["revision_id"]] += 1


def _check_evidence(connection, evidence_ids, *, verified=None):
    if evidence_ids is not None and not evidence_ids:
        return 0
    count = 0
    query = "SELECT digest,content FROM evidence"
    parameters = ()
    if evidence_ids is not None:
        query = (
            "SELECT e.digest,e.content FROM json_each(?) ids "
            "JOIN evidence e ON e.digest=unhex(ids.value)"
        )
        parameters = (canonical(sorted(value.hex() for value in evidence_ids)),)
    for row in connection.execute(query, parameters):
        if hashlib.sha256(row["content"]).digest() != row["digest"]:
            _invalid("evidence", row["digest"].hex(), "evidence_digest_mismatch")
        if verified is not None:
            verified.add(bytes(row["digest"]))
        count += 1
    if evidence_ids is not None and count != len(evidence_ids):
        _invalid("evidence", "*", "evidence_missing")
    return count


def _check_sources(engine, connection, calculation_ids=None, *, extra_fact_ids=()):
    identifiers = _source_set(connection, calculation_ids)
    calculations = {
        row["id"]: dict(row) for row in _rows(connection, "calculation", "id", identifiers)
    }
    if identifiers is not None and set(calculations) != identifiers:
        _invalid("calculation", "*", "referenced_calculation_missing")
    dependencies = defaultdict(set)
    dependency_facts = defaultdict(set)
    for row in _rows(connection, "dependency_calculation", "calculation_id", identifiers):
        dependencies[row["calculation_id"]].add(row["upstream_id"])
    for row in _rows(connection, "dependency_fact", "calculation_id", identifiers):
        dependency_facts[row["calculation_id"]].add(row["fact_id"])
    stored_reads = defaultdict(list)
    for row in _rows(connection, "dependency_scope", "calculation_id", identifiers):
        stored_reads[row["calculation_id"]].append(
            read_type()(
                row["source"],
                row["kind"],
                row["scope_key"],
                None
                if row["before_period"] == 119988
                else month_type().from_ordinal(row["before_period"]),
            )
        )
    fact_ids = {row["fact_id"] for row in calculations.values()}
    fact_ids.update(ident for values in dependency_facts.values() for ident in values)
    fact_ids.update(extra_fact_ids)
    facts, evidence_ids = _check_facts(
        engine, connection, fact_ids if identifiers is not None else None
    )
    verified_evidence = set()
    evidence_count = _check_evidence(
        connection, evidence_ids if identifiers is not None else None, verified=verified_evidence
    )
    seals = {
        row[0]
        for row in connection.execute(
            "SELECT s.calculation_id FROM json_each(?) ids JOIN calculation_seal s "
            "ON s.calculation_id=ids.value",
            (canonical(sorted(calculations)),),
        )
    }
    publications = {
        row["calculation_id"]: dict(row)
        for row in _rows(
            connection,
            "calculation_publication",
            "calculation_id",
            identifiers,
        )
        if row["calculation_id"] is not None
    }
    from .content_history_context import publication_reader

    publication_reader().verify_publication_chain(
        connection, subject_ids={row["subject_id"] for row in calculations.values()}
    )
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
    }
    member_ids = set()
    if "asset_batch_member" in tables:
        member_ids = {
            row["member_calculation_id"]
            for row in _rows(connection, "asset_batch_member", "member_calculation_id", identifiers)
        }
    for ident, row in calculations.items():
        if ident not in seals:
            _invalid("calculation", ident, "calculation_seal_missing")
        fact = facts.get(row["fact_id"])
        if fact is None or (row["subject_id"], row["kind"], row["period"]) != (
            fact["subject_id"],
            fact["kind"],
            fact["period"],
        ):
            _invalid("calculation", ident, "calculation_fact_identity_mismatch")
        if row["fact_id"] not in dependency_facts[ident]:
            _invalid("calculation", ident, "own_fact_dependency_missing")
        if not dependencies[ident] <= calculations.keys() or ident in dependencies[ident]:
            _invalid("calculation", ident, "invalid_calculation_dependency")
        if ident not in publications and ident not in member_ids:
            _invalid("calculation", ident, "publication_or_adoption_missing")
        row["decoded"] = _outcome(row)
        # The released calculation identity seals saved reads and version IDs.
        # dependency_fact additionally always contains the owning fact; that
        # fact may or may not also have been selected, hence two exact variants.
        versions = dependencies[ident] | (dependency_facts[ident] - {row["fact_id"]})
        payload = {
            "fact": row["fact_id"],
            "outcome": row["decoded"],
            "reads": [asdict(read) for read in sorted(stored_reads[ident], key=repr)],
            "program": row["program_version"],
        }
        first_key = "c_" + digest({**payload, "versions": sorted(versions)}).hex()
        if ident != first_key:
            second_key = "c_" + digest(
                {**payload, "versions": sorted(versions | {row["fact_id"]})}
            ).hex()
            if ident != second_key:
                _invalid("calculation", ident, "calculation_input_digest_mismatch")
    # Iterative graph traversal also handles long legitimate cumulative chains.
    completed, active = set(), set()
    for ident in calculations:
        stack = [(ident, False)]
        while stack:
            item, returning = stack.pop()
            if returning:
                active.discard(item)
                completed.add(item)
            elif item not in completed:
                if item in active:
                    _invalid("calculation", item, "dependency_cycle")
                active.add(item)
                stack.append((item, True))
                stack.extend((parent, False) for parent in dependencies[item])
    vouchers = {
        row["id"]: dict(row)
        for row in _rows(connection, "voucher_version", "calculation_id", identifiers)
    }
    numbered = {
        row[0]: row[1]
        for row in connection.execute(
            "SELECT v.id,v.number FROM json_each(?) ids JOIN voucher v ON v.id=ids.value",
            (
                canonical(
                    sorted(
                        {row["voucher_id"] for row in vouchers.values()}
                        | {
                            row["voucher_id"]
                            for row in publications.values()
                            if row["voucher_id"] is not None
                        }
                    )
                ),
            ),
        )
    }
    lines = defaultdict(list)
    payload = canonical(sorted(vouchers))
    for row in connection.execute(
        "SELECT l.* FROM json_each(?) ids JOIN voucher_line l ON ids.value=l.version_id "
        "ORDER BY l.version_id,l.line_no",
        (payload,),
    ):
        if row["line_no"] != len(lines[row["version_id"]]) + 1:
            _invalid("voucher", row["version_id"], "voucher_line_order_mismatch")
        lines[row["version_id"]].append(
            {key: row[key] for key in ("account", "debit", "credit", "cashflow")}
        )
    for ident, row in vouchers.items():
        actual = _lines(lines[ident], "voucher", ident)
        if not actual or sum_fen(line[1] for line in actual) != row["total"]:
            _invalid("voucher", ident, "voucher_total_mismatch")
        if row["voucher_id"] not in numbered:
            _invalid("voucher", ident, "voucher_number_missing")
        row["number"] = numbered[row["voucher_id"]]
        row["lines"] = lines[ident]
        publication = publications.get(row["calculation_id"])
        if publication is None or publication["posting_period"] != row["period"]:
            _invalid("voucher", ident, "voucher_publication_mismatch")
        if row["reverses_id"] is not None:
            original = vouchers.get(row["reverses_id"])
            if (
                original is None
                or original["period"] >= row["period"]
                or original["reverses_id"] is not None
            ):
                _invalid("voucher", ident, "invalid_reversal_source")
            expected = [
                (line["account"], line["credit"], line["debit"], line["cashflow"])
                for line in lines[original["id"]]
            ]
        else:
            if publication["voucher_id"] != row["voucher_id"]:
                _invalid("voucher", ident, "voucher_publication_identity_mismatch")
            expected = _lines(
                calculations[row["calculation_id"]]["decoded"]["lines"],
                "calculation",
                row["calculation_id"],
            )
        if actual != expected:
            _invalid("voucher", ident, "voucher_lines_mismatch")
    # Non-impact reviews may reuse a voucher produced by an older calculation.
    # A cleared result may retain its reserved number with no current version.
    represented = {
        (
            voucher["voucher_id"],
            calculations[voucher["calculation_id"]]["subject_id"],
            voucher["period"],
            canonical(voucher["lines"]),
        )
        for voucher in vouchers.values()
        if voucher["reverses_id"] is None
    }
    for ident, publication in publications.items():
        voucher_id = publication["voucher_id"]
        if voucher_id is not None and voucher_id not in numbered:
            _invalid("calculation", ident, "publication_voucher_missing")
        if (
            calculations[ident]["decoded"]["lines"]
            and (
                voucher_id,
                calculations[ident]["subject_id"],
                publication["posting_period"],
                canonical(calculations[ident]["decoded"]["lines"]),
            )
            not in represented
        ):
            _invalid("calculation", ident, "publication_lines_unrepresented")
    for ident, item in publications.items():
        if item["mode"] != "review_no_impact":
            continue
        previous = connection.execute(
            "SELECT calculation_id FROM calculation_publication WHERE id=?",
            (item["previous_publication_id"],),
        ).fetchone()[0]
        before, after = calculations[previous]["decoded"], calculations[ident]["decoded"]
        if any(
            before[field] != after[field]
            for field in ("lines", "balances", "opening", "opening_lines")
        ):
            _invalid("publication", ident, "no_impact_accounting_mismatch")
    for ident, calculation in calculations.items():
        if calculation["kind"] in {"asset_activation_batch", "asset_consumption_month"}:
            from .content_history_context import asset_membership_reader

            try:
                asset_membership_reader().frozen_members(connection, ident)
            except KernelError:
                _invalid("calculation", ident, "asset_membership_mismatch")
    return {
        "calculations": calculations,
        "facts": facts,
        "publications": publications,
        "dependencies": dependencies,
        "dependency_facts": dependency_facts,
        "vouchers": vouchers,
        "evidence_count": evidence_count,
        "verified_evidence": verified_evidence,
    }


def _totals(lines, component, ident):
    if not isinstance(lines, list):
        _invalid(component, ident, "invalid_trial_balance")
    totals = {}
    for line in lines:
        if not isinstance(line, dict) or not {"account", "debit", "credit"} <= line.keys():
            _invalid(component, ident, "invalid_trial_balance_line")
        account = line["account"]
        if not isinstance(account, str) or not account:
            _invalid(component, ident, "invalid_trial_balance_account")
        amounts = [checked(line["debit"]), checked(line["credit"])]
        if min(amounts) < 0:
            _invalid(component, ident, "invalid_trial_balance_amount")
        previous = totals.setdefault(account, [0, 0])
        previous[0] = checked(previous[0] + amounts[0])
        previous[1] = checked(previous[1] + amounts[1])
    return {key: value for key, value in totals.items() if value != [0, 0]}


def _merge(left, right):
    result = {key: list(value) for key, value in left.items()}
    for key, value in right.items():
        previous = result.setdefault(key, [0, 0])
        previous[0] = checked(previous[0] + value[0])
        previous[1] = checked(previous[1] + value[1])
    return {key: value for key, value in result.items() if value != [0, 0]}


def _opening_basis(source, manifest, period):
    """Read the explicitly adopted independent opening; never infer from totals."""
    ident = manifest["opening_calculation_id"]
    if ident is None:
        return {}
    row = source["calculations"].get(ident)
    if row is None or not row["decoded"].get("opening"):
        _invalid("close", period, "opening_adoption_mismatch")
    if row["kind"] == "opening_package":
        from .content_history_context import opening_adoption_reader

        opening = opening_adoption_reader()

        package = {
            **row,
            "period": str(month_type().from_ordinal(row["period"])),
            "outcome": row["decoded"],
            "fact_data": source["facts"][row["fact_id"]]["data"],
        }
        facts = {
            key: {**value, "period": str(month_type().from_ordinal(value["period"]))}
            for key, value in source["facts"].items()
        }
        contract = opening._package_contract(package, facts, source["dependency_facts"][ident])
        if contract is None:
            _invalid("close", period, "opening_package_content_mismatch")
        declarations, _ = contract
        adopted_details = {
            item["subject_id"]: source["calculations"][item["calculation_id"]]
            for item in manifest["adopted_results"]
            if source["calculations"][item["calculation_id"]]["kind"] in opening._DETAIL_KINDS
        }
        if set(adopted_details) != set(declarations):
            _invalid("close", period, "opening_member_adoption_mismatch")
        for subject, member in declarations.items():
            detail = adopted_details[subject]
            if (
                detail["fact_id"] != member["fact_id"]
                or detail["kind"] != member["kind"]
                or not opening._detail_shape(detail["decoded"])
                or detail["decoded"]["values"] != member["values"]
                or source["dependencies"][detail["id"]] != {ident}
            ):
                _invalid("close", period, "opening_member_adoption_mismatch")
    return _totals(row["decoded"].get("opening_lines", []), "calculation", ident)


class _FrozenAdoptionReads:
    """Supply the existing asset-adoption proof with already verified raw rows."""

    def __init__(self, source):
        self.source = source

    def prime_parents(self, identifiers):
        return None

    def parents(self, ident):
        return self.source["dependencies"][ident]

    def calculations(self, identifiers):
        result = {}
        for ident in identifiers:
            row = self.source["calculations"][ident]
            publication = self.source["publications"].get(ident)
            result[ident] = {
                **row,
                "outcome": row["decoded"],
                "period": str(month_type().from_ordinal(row["period"])),
                "posting_period": str(month_type().from_ordinal(publication["posting_period"]))
                if publication
                else None,
                "result_digest": row["digest"].hex(),
                "fact_data": self.source["facts"][row["fact_id"]]["data"],
            }
        return result


def _check_snapshot_references(connection, source, manifest, period):
    snapshot = manifest.get("management_snapshot")
    if snapshot is None:
        return
    if not isinstance(snapshot, dict):
        _invalid("close", period, "invalid_management_snapshot")
    if (
        "digest" in snapshot
        and snapshot["digest"]
        != digest({key: value for key, value in snapshot.items() if key != "digest"}).hex()
    ):
        _invalid("close", period, "management_snapshot_digest_mismatch")
    groups = (
        ("typed_facts", "fact_revision"),
        ("profiles", "display_profile_revision"),
        ("entity_profiles", "entity_profile_revision"),
        ("management", "management_revision"),
        ("payees", "payee_revision"),
        ("company_note", "company_note_revision"),
        ("commentary", "period_commentary_revision"),
        ("commentary_latest", "period_commentary_revision"),
    )
    for name, table in groups:
        if name not in snapshot:
            continue
        entries = snapshot.get(name, [])
        if entries is None:
            continue
        if name in {"company_note", "commentary", "commentary_latest"}:
            entries = [entries]
        if not isinstance(entries, list) or any(
            not isinstance(item, dict) or "id" not in item for item in entries
        ):
            _invalid("close", period, "invalid_management_reference")
        identities = {item["id"] for item in entries}
        records = {row["id"]: row for row in _rows(connection, table, "id", identities)}
        for entry in entries:
            record = records.get(entry["id"])
            if record is None:
                _invalid("close", period, "management_source_missing")
            for key, value in entry.items():
                if key == "content_validity" or key == "kind" and name == "typed_facts":
                    if (
                        key == "kind"
                        and name == "typed_facts"
                        and source["facts"][entry["id"]]["kind"] != value
                    ):
                        _invalid("close", period, "management_source_content_mismatch")
                    continue
                if key not in record.keys():
                    _invalid("close", period, "unsupported_management_reference")
                actual = record[key].hex() if isinstance(record[key], bytes) else record[key]
                if actual != value:
                    _invalid("close", period, "management_source_content_mismatch")


def _check_job_sources(engine, connection, source):
    """Frozen export plans have their own saved content digests."""
    from .read_indexes import JOB_KINDS, _job_references

    for row in connection.execute(
        "SELECT * FROM jobs WHERE kind IN (SELECT value FROM json_each(?))",
        (canonical(JOB_KINDS),),
    ):
        payload = _object(row["payload"], "job", row["id"])
        plan = payload.get("plan")
        if not isinstance(plan, dict) or not isinstance(plan.get("digest"), str):
            _invalid("job", row["id"], "unsupported_export_plan")
        omitted = {"digest"}
        if row["kind"] == "report_export":
            omitted.add("epochs")
        elif row["kind"] == "tax_import":
            omitted.add("status")
        if (
            plan["digest"]
            != digest({key: value for key, value in plan.items() if key not in omitted}).hex()
        ):
            _invalid("job", row["id"], "export_plan_digest_mismatch")
        if (plan.get("company_id"), plan.get("database_id")) != (
            engine.store.company_id,
            engine.store.database_id,
        ):
            _invalid("job", row["id"], "export_plan_identity_mismatch")
        for _, _, kind, ident, *_ in _job_references(row):
            if kind == "calculation" and ident not in source["calculations"]:
                _invalid("job", row["id"], "export_calculation_missing")
            if kind == "fact" and ident not in source["facts"]:
                _invalid("job", row["id"], "export_fact_missing")
            if (
                kind == "version"
                and ident not in source["facts"]
                and ident not in source["calculations"]
            ):
                _invalid("job", row["id"], "export_version_missing")


def _check_audit_sources(connection):
    from .provenance import _result
    from .read_indexes import AUDIT_ACTIONS

    for row in connection.execute(
        "SELECT a.id,a.payload,r.result FROM audit a LEFT JOIN request r ON r.id=a.request_id "
        "WHERE a.action IN (SELECT value FROM json_each(?))",
        (canonical(AUDIT_ACTIONS),),
    ):
        payload = _object(row["payload"], "audit", row["id"])
        if row["result"] is None or _result(payload) != _object(
            row["result"], "request", row["id"]
        ):
            _invalid("audit", row["id"], "audit_request_result_mismatch")


def _check_close_approval(row, manifest, period):
    """Tie the frozen receipt to the immutable, consumed company-local grant."""
    approval = manifest["approval"]
    if approval is None:
        return None
    versions = manifest["read_version"]
    preview_manifest = {**manifest, "approval": None}
    preview_digest = digest(
        [
            preview_manifest,
            versions["accounting"],
            versions["material"],
            versions["management"],
        ]
    ).hex()
    if (
        row is None
        or row["consumed_at"] is None
        or not row["confirmed_at"] <= row["consumed_at"] < row["expires_at"]
        or (
            row["catalog_instance_id"],
            row["company_id"],
            row["database_id"],
            row["period"],
            row["preview_digest"].hex(),
            row["accounting_epoch"],
            row["material_epoch"],
            row["management_epoch"],
            row["owner_id"],
            row["credential_version"],
            row["confirmed_at"],
        )
        != (
            approval["catalog_instance_id"],
            manifest["company_id"],
            manifest["database_id"],
            period,
            approval["preview_digest"],
            versions["accounting"],
            versions["material"],
            versions["management"],
            approval["owner_id"],
            approval["credential_version"],
            approval["confirmed_at"],
        )
        or approval["preview_digest"] != preview_digest
    ):
        _invalid("close", period, "close_approval_mismatch")
    return approval["approval_id"]


def _check_closes(
    engine,
    connection,
    source,
    *,
    through_period=None,
    verify_financial_position=True,
    require_index_marker=True,
    predecoded_closes=None,
    _return_closes=False,
):
    from .content_history_context import close_contract, close_reader

    decode_close = close_reader().decode_close
    direct_calculation_ids = close_contract().direct_calculation_ids

    previous_digest, previous_trial, previous_period = None, None, None
    previous_sequence = 0
    maximum_sequence = connection.execute(
        "SELECT coalesce(max(sequence),0) FROM calculation_publication"
    ).fetchone()[0]
    count = 0
    used_approval_ids = set()
    parameters = (through_period,) if through_period is not None else ()
    restriction = " WHERE period<=?" if through_period is not None else ""
    material_rules = dict(
        connection.execute(
            "SELECT period,rule_digest FROM material_close_rule" + restriction, parameters
        )
    )
    from .verified_close_archive import pack_verified_close, unpack_verified_close

    closes = []
    approval_ids = []
    coverage_inventory_inputs = []
    inventory_ids = set()
    owner_evidence = []
    boundaries = []
    close_rows = (
        connection.execute(
            "SELECT * FROM period_close" + restriction + " ORDER BY period", parameters
        )
        if predecoded_closes is None
        else (row for row, _ in predecoded_closes)
    )
    for index, row in enumerate(close_rows):
        period = row["period"]
        rule = material_rules.get(period)
        if not isinstance(rule, bytes) or len(rule) != 32:
            _invalid("close", period, "material_rule_identity_missing")
        saved = (
            decode_close(connection, row, require_marker=require_index_marker)
            if predecoded_closes is None
            else predecoded_closes[index][1]
        )
        manifest = unpack_verified_close(saved) if isinstance(saved, bytes) else saved
        closes.append((row, saved if isinstance(saved, bytes) else pack_verified_close(manifest)))
        if manifest["approval"] is not None:
            approval_ids.append(manifest["approval"]["approval_id"])
        coverage_inventory_inputs.append(
            (period, manifest["material_coverage"].get("inventory_versions"))
        )
        inventory_ids.update(manifest["inventories"].values())
        owner_evidence.append(manifest["owner_confirmation"])
        boundaries.append([period, manifest["publication_sequence"]])
        del manifest, saved
    if predecoded_closes is not None and (
        len(closes) != len(material_rules)
        or {row["period"] for row, _ in closes} != material_rules.keys()
    ):
        _invalid("close", "*", "predecoded_close_set_mismatch")
    approval_rows = (
        {
            row["id"]: row
            for row in connection.execute(
                "SELECT * FROM security_close_approval "
                "WHERE id IN (SELECT value FROM json_each(?))",
                (canonical(approval_ids),),
            )
        }
        if approval_ids
        else {}
    )
    coverage_inventory_references = []
    for period, references in coverage_inventory_inputs:
        if not isinstance(references, list):
            _invalid("close", period, "material_inventory_references_missing")
        for reference in references:
            if (
                not isinstance(reference, dict)
                or set(reference) != {"inventory_id", "period", "category", "content_digest"}
                or type(reference["inventory_id"]) is not int
                or not isinstance(reference["period"], str)
                or not isinstance(reference["category"], str)
                or not isinstance(reference["content_digest"], str)
            ):
                _invalid("close", period, "material_inventory_reference_invalid")
            coverage_inventory_references.append((period, reference))
    inventory_ids.update(
        reference["inventory_id"] for _, reference in coverage_inventory_references
    )
    del coverage_inventory_inputs
    inventories = {
        item["id"]: item for item in _rows(connection, "material_revision", "id", inventory_ids)
    }
    inventory_items = {ident: [] for ident in inventory_ids}
    if inventory_ids:
        for item in connection.execute(
            "SELECT i.inventory_id,i.evidence_digest FROM json_each(?) ids "
            "JOIN material_item i ON i.inventory_id=ids.value "
            "ORDER BY i.inventory_id,i.evidence_digest",
            (canonical(sorted(inventory_ids)),),
        ):
            inventory_items[item["inventory_id"]].append(item["evidence_digest"].hex())
    from .content_history_context import inventory_reference

    for period, reference in coverage_inventory_references:
        inventory = inventories.get(reference["inventory_id"])
        if (
            inventory is None
            or inventory_reference()(inventory, inventory_items[reference["inventory_id"]])
            != reference
        ):
            _invalid("close", period, "material_inventory_reference_mismatch")
    evidence_ids = {bytes.fromhex(value) for value in owner_evidence} | {
        bytes(item["evidence_digest"]) for item in inventories.values()
    }
    del owner_evidence
    _check_evidence(connection, evidence_ids - source["verified_evidence"])
    expected_adoptions_by_period, current_vouchers_by_period = defaultdict(set), defaultdict(set)
    boundaries = canonical(boundaries)
    for item in connection.execute(
        "SELECT json_extract(b.value,'$[0]'),p.calculation_id FROM json_each(?) b "
        "JOIN calculation_publication p ON p.posting_period=json_extract(b.value,'$[0]') "
        "WHERE p.sequence<=json_extract(b.value,'$[1]') AND p.calculation_id IS NOT NULL "
        "AND NOT EXISTS(SELECT 1 FROM calculation_publication later "
        "WHERE later.previous_publication_id=p.id "
        "AND later.sequence<=json_extract(b.value,'$[1]'))",
        (boundaries,),
    ):
        expected_adoptions_by_period[item[0]].add(item[1])
    for item in connection.execute(
        "SELECT v.period,h.version_id FROM json_each(?) b "
        "JOIN voucher_version v ON v.period=json_extract(b.value,'$[0]') "
        "JOIN voucher_current h ON h.version_id=v.id",
        (boundaries,),
    ):
        current_vouchers_by_period[item[0]].add(item[1])
    verified_position_prefix = []
    for row, packed in closes:
        manifest = unpack_verified_close(packed)
        period = row["period"]
        if (
            (manifest["period"], manifest["company_id"], manifest["database_id"])
            != (
                str(month_type().from_ordinal(period)),
                engine.store.company_id,
                engine.store.database_id,
            )
            or manifest["previous_close_digest"] != previous_digest
            or manifest["previous_close_period"] != previous_period
        ):
            _invalid("close", period, "manifest_identity_or_chain_mismatch")
        approval = manifest["approval"]
        approval_id = _check_close_approval(
            approval_rows.get(approval["approval_id"]) if approval is not None else None,
            manifest,
            period,
        )
        if approval_id is not None:
            if approval_id in used_approval_ids:
                _invalid("close", period, "duplicate_close_approval")
            used_approval_ids.add(approval_id)
        if not previous_sequence <= manifest["publication_sequence"] <= maximum_sequence:
            _invalid("close", period, "publication_boundary_mismatch")
        previous_sequence = manifest["publication_sequence"]
        members = direct_calculation_ids(manifest)
        if not members <= source["calculations"].keys():
            _invalid("close", period, "manifest_source_missing")
        adopted = {item["calculation_id"]: item for item in manifest["adopted_results"]}
        for ident, declaration in adopted.items():
            calculation = source["calculations"][ident]
            publication = source["publications"].get(ident)
            if publication is None or (
                publication["id"],
                calculation["subject_id"],
                calculation["fact_id"],
                calculation["digest"].hex(),
                str(month_type().from_ordinal(calculation["period"])),
                publication["posting_period"],
            ) != (
                declaration["publication_id"],
                declaration["subject_id"],
                declaration["fact_id"],
                declaration["result_digest"],
                declaration["source_period"],
                period,
            ):
                _invalid("close", period, "direct_adoption_source_mismatch")
            outcome = calculation["decoded"]
            role = (
                "asset_batch_owner"
                if calculation["kind"] in {"asset_activation_batch", "asset_consumption_month"}
                else "opening_basis"
                if outcome["opening"]
                else "journal_basis"
                if outcome["lines"]
                else "state_only"
            )
            if declaration["role"] != role:
                _invalid("close", period, "direct_adoption_role_mismatch")
        if set(adopted) != expected_adoptions_by_period[period]:
            _invalid("close", period, "direct_adoption_set_mismatch")
        voucher_ids, voucher_lines = set(), []
        for reference in manifest["vouchers"]:
            voucher = source["vouchers"].get(reference["id"])
            if (
                voucher is None
                or voucher["period"] != period
                or any(
                    reference[key] != voucher[key]
                    for key in (
                        "id",
                        "voucher_id",
                        "calculation_id",
                        "total",
                        "number",
                        "reverses_id",
                    )
                )
            ):
                _invalid("close", period, "manifest_voucher_content_mismatch")
            basis = adopted.get(reference["adopted_calculation_id"])
            owner = source["calculations"][voucher["calculation_id"]]
            if (
                basis is None
                or basis["subject_id"] != owner["subject_id"]
                or reference["result_digest"] != basis["result_digest"]
            ):
                _invalid("close", period, "voucher_adoption_mismatch")
            basis_publication = source["publications"][basis["calculation_id"]]
            if (
                voucher["reverses_id"] is None
                and basis_publication["voucher_id"] != voucher["voucher_id"]
            ):
                _invalid("close", period, "voucher_adoption_mismatch")
            voucher_ids.add(voucher["id"])
            voucher_lines.extend(voucher["lines"])
        if voucher_ids != current_vouchers_by_period[period]:
            _invalid("close", period, "manifest_voucher_set_mismatch")
        batch_owners = {
            ident for ident, item in adopted.items() if item["role"] == "asset_batch_owner"
        }
        declared_batches = set()
        for adoption in manifest["asset_batch_adoptions"]:
            if not isinstance(adoption, dict) or set(adoption) != {
                "owner_calculation_id",
                "membership_digest",
            }:
                _invalid("close", period, "asset_adoption_identity_mismatch")
            ident = adoption["owner_calculation_id"]
            if ident not in batch_owners or ident in declared_batches:
                _invalid("close", period, "asset_adoption_identity_mismatch")
            if (
                source["calculations"][ident]["decoded"]["values"]["membership_digest"]
                != adoption["membership_digest"]
            ):
                _invalid("close", period, "asset_adoption_digest_mismatch")
            declared_batches.add(ident)
        if batch_owners != declared_batches:
            _invalid("close", period, "asset_adoption_set_mismatch")
        _check_snapshot_references(connection, source, manifest, period)
        for references in manifest["readiness"].values():
            if not isinstance(references, dict) or set(references) != {"facts", "calculations"}:
                _invalid("close", period, "readiness_references_missing")
            for field in ("facts", "calculations"):
                if (
                    not isinstance(references[field], list)
                    or not set(references[field]) <= source[field].keys()
                ):
                    _invalid("close", period, "readiness_source_missing")
        from .content_history_context import material_categories

        if set(manifest["inventories"]) != set(material_categories()):
            _invalid("close", period, "material_inventory_set_mismatch")
        for category, inventory_id in manifest["inventories"].items():
            inventory = inventories.get(inventory_id)
            if inventory is None or (inventory["period"], inventory["category"]) != (
                period,
                category,
            ):
                _invalid("close", period, "material_inventory_source_mismatch")
        coverage = manifest["material_coverage"]
        saved_coverage = {
            key: value
            for key, value in coverage.items()
            if key not in {"status", "coverage_digest"}
        } | {"issues": []}
        if (
            coverage.get("status") != "complete"
            or coverage.get("coverage_digest") != digest(saved_coverage).hex()
            or not isinstance(coverage.get("fact_ids"), list)
            or not set(coverage["fact_ids"]) <= source["facts"].keys()
        ):
            _invalid("close", period, "material_coverage_mismatch")
        from .content_history_context import asset_card_reader

        reads = _FrozenAdoptionReads(source)
        try:
            asset_card_reader().prove_asset_card_adoptions(
                reads,
                close_period=period,
                manifest=manifest,
                metadata=reads.calculations(members),
                independent_proofs={
                    ident: {"basis": "manifest_voucher_root"}
                    for ident in {reference["calculation_id"] for reference in manifest["vouchers"]}
                    | {reference["adopted_calculation_id"] for reference in manifest["vouchers"]}
                },
            )
        except KernelError:
            _invalid("close", period, "asset_card_adoption_mismatch")
        trial = _totals(manifest["trial_balance"], "close", period)
        if len(trial) != len(manifest["trial_balance"]):
            # Zero totals may be represented by a balanced but empty account only once.
            if len({item["account"] for item in manifest["trial_balance"]}) != len(
                manifest["trial_balance"]
            ):
                _invalid("close", period, "duplicate_trial_account")
        if sum_fen(value[0] for value in trial.values()) != sum_fen(
            value[1] for value in trial.values()
        ):
            _invalid("close", period, "unbalanced_trial_balance")
        if previous_trial is None:
            baseline = _opening_basis(source, manifest, period)
        else:
            if manifest["opening_calculation_id"] is not None:
                _invalid("close", period, "opening_after_first_close")
            baseline = previous_trial
        if _merge(baseline, _totals(voucher_lines, "close", period)) != trial:
            _invalid("close", period, "trial_balance_source_mismatch")
        from .content_history_context import owner_review_reader

        owner_reader = owner_review_reader()
        from . import close_review_integrity_v1

        if (
            owner_reader is close_review_integrity_v1
            and verify_financial_position
            and connection.in_transaction
        ):
            from .position_v1 import _verified_close_prefix
            from .verified_source_lease import verified_source_lease

            # This prefix comes only from closes whose source, adoption, trial,
            # and storage proofs have passed above in this read transaction.
            verified_position_prefix.append((row, manifest))
            with verified_source_lease(connection):
                owner_reader.verify_owner_review_integrity(
                    connection,
                    engine,
                    manifest,
                    verify_financial_position=True,
                    _verified_closes=_verified_close_prefix(connection, verified_position_prefix),
                )
            verified_position_prefix[-1] = (
                row,
                {
                    "opening_calculation_id": manifest["opening_calculation_id"],
                    "vouchers": manifest["vouchers"],
                },
            )
        else:
            owner_reader.verify_owner_review_integrity(
                connection,
                engine,
                manifest,
                verify_financial_position=verify_financial_position,
            )
        previous_digest, previous_trial, previous_period = (
            row["digest"].hex(),
            trial,
            manifest["period"],
        )
        count += 1
        del manifest, coverage, saved_coverage
    approval_parameters = (through_period,) if through_period is not None else ()
    approval_restriction = " AND period<=?" if through_period is not None else ""
    consumed_approval_ids = {
        row[0]
        for row in connection.execute(
            "SELECT id FROM security_close_approval WHERE consumed_at IS NOT NULL"
            + approval_restriction,
            approval_parameters,
        )
    }
    if consumed_approval_ids != used_approval_ids:
        _invalid("close", "*", "orphaned_close_approval")
    if _return_closes:
        if not connection.in_transaction:
            _invalid("close", "*", "decoded_close_reuse_requires_transaction")
        from .verified_close_archive import VerifiedCloseArchive

        return count, [], VerifiedCloseArchive(connection, closes)
    return count, []


def _check_heads(connection):
    for table, target, head_id, key in (
        ("fact_current", "fact_revision", "fact_id", "subject_id"),
        ("calculation_current", "calculation", "calculation_id", "subject_id"),
        ("voucher_current", "voucher_version", "version_id", "voucher_id"),
    ):
        if connection.execute(
            f"SELECT 1 FROM {table} h LEFT JOIN {target} s ON s.id=h.{head_id} "
            f"WHERE s.id IS NULL OR h.{key}<>s.{key} LIMIT 1"
        ).fetchone():
            _invalid("heads", table, "current_identity_mismatch")


def verify_integrity(engine, connection, *, include_projections=True, include_indexes=True):
    """Check all preserved versions in the caller's snapshot, including old ones."""
    if include_projections and connection.in_transaction:
        from .verified_source_lease import verified_source_lease

        with verified_source_lease(connection):
            return _verify_integrity_snapshot(
                engine,
                connection,
                include_projections=include_projections,
                include_indexes=include_indexes,
            )
    return _verify_integrity_snapshot(
        engine,
        connection,
        include_projections=include_projections,
        include_indexes=include_indexes,
    )


def _verify_integrity_snapshot(engine, connection, *, include_projections, include_indexes):
    try:
        ignored_tables = set()
        if not include_indexes:
            ignored_tables.update(
                ("read_index_source", "close_reference", "job_reference", "audit_reference")
            )
            ignored_tables.update(
                (
                    "entity_reference_recorded",
                    "entity_reference_current",
                    "discovery_fact_history",
                    "discovery_fact_current",
                )
            )
        if not include_projections:
            ignored_tables.update(
                (
                    "monthly_account",
                    "monthly_cashflow",
                    "balance",
                    "opening_account",
                    "period_balance",
                    "settlement_change",
                    "settlement_projection_seal",
                    "settlement_freeze_root",
                    "settlement_freeze_block",
                    "settlement_freeze_ref",
                    "settlement_state_revision",
                    "period_balance_seal",
                    "period_balance_freeze_root",
                    "period_balance_freeze_bucket",
                    "period_balance_freeze_row",
                    "report_line_source",
                    "report_line_source_seal",
                    "duplicate_freeze_root",
                    "duplicate_freeze_directory",
                    "duplicate_freeze_bucket",
                    "material_watch_root",
                    "material_watch_directory",
                    "material_watch_bucket",
                    "report_party_delta",
                    "report_party_month_seal",
                    "report_party_checkpoint",
                    "report_party_checkpoint_seal",
                    "report_semantic_line",
                    "report_semantic_seal",
                    "report_period_flow",
                    "report_classification_directory",
                    "report_classification_node",
                    "report_open_contribution",
                )
            )
        checks = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        if any(
            message != "ok"
            and message not in {f"CHECK constraint failed in {table}" for table in ignored_tables}
            for message in checks
        ):
            _invalid("sqlite", "*", "sqlite_integrity_check_failed")
        if any(
            row[0] not in ignored_tables for row in connection.execute("PRAGMA foreign_key_check")
        ):
            _invalid("sqlite", "*", "foreign_key_check_failed")
        _check_heads(connection)
        from .content_history_context import duplicate_reader, journal_reader, material_watch_reader

        journal_reader().verify_journal(connection)
        source = _check_sources(engine, connection)
        from .content_history_context import report_open_contribution_reader

        report_open_contribution_reader().compare_open_contributions(
            engine, connection, check_bodies=include_projections, verified_source=source
        )
        close_result = _check_closes(
            engine,
            connection,
            source,
            verify_financial_position=include_projections and include_indexes,
            require_index_marker=include_indexes,
            _return_closes=include_projections and connection.in_transaction,
        )
        close_count, limitations = close_result[:2]
        decoded_closes = close_result[2] if len(close_result) == 3 else None
        _check_job_sources(engine, connection, source)
        _check_audit_sources(connection)
        from .entities import verify_entities
        from .identity_corrections import verify_identity_corrections

        verify_entities(connection)
        verify_identity_corrections(engine, connection)
        duplicate_reader().verify_duplicate_checks(connection)
        if include_projections:
            duplicate_reader().compare_duplicate_freeze(connection, engine)
            material_watch_reader().require_material_watch(
                engine, connection, _verified_closes=decoded_closes
            )
            from .content_history_context import (
                balance_freeze_reader,
                report_classification_directory_reader,
                report_flow_reader,
                report_reader,
                report_semantics_reader,
                settlement_projection_reader,
                settlement_reader,
            )

            require_projections(connection, verified_calculations=source["calculations"])
            if connection.in_transaction:
                from contextlib import nullcontext

                from .verified_source_lease import (
                    verified_calculation_source,
                    verified_source_lease,
                )

                lease_scope = (
                    nullcontext()
                    if decoded_closes is not None
                    else verified_source_lease(connection)
                )
                with lease_scope:
                    settlement = settlement_projection_reader().require_settlement_projection(
                        engine,
                        connection,
                        verified_calculations=source["calculations"],
                        _return_verified=True,
                    )
                    settlement_reader().require_frozen_settlement_projection(
                        engine,
                        connection,
                        verified_calculations=source["calculations"],
                        _verified_projection=settlement,
                    )
                    reports = report_reader().require_report_projection(
                        engine,
                        connection,
                        _verified_closes=decoded_closes,
                        _return_verified=True,
                    )
                    semantics = report_semantics_reader().require_report_semantics(
                        engine,
                        connection,
                        _verified_closes=decoded_closes,
                        _verified_source=verified_calculation_source(connection, source),
                        _return_verified=True,
                    )
                    flow_result = report_flow_reader().require_report_flow(
                        engine,
                        connection,
                        _verified_closes=decoded_closes,
                        _verified_reports=reports,
                        _verified_semantics=semantics,
                    )
                    report_classification_directory_reader().require_classification_directory(
                        engine,
                        connection,
                        _verified_closes=decoded_closes,
                        _expected_flows=flow_result["expected_rows"],
                    )
            else:
                settlement_projection_reader().require_settlement_projection(
                    engine, connection, verified_calculations=source["calculations"]
                )
                settlement_reader().require_frozen_settlement_projection(
                    engine, connection, verified_calculations=source["calculations"]
                )
                report_reader().require_report_projection(engine, connection)
                report_semantics_reader().require_report_semantics(engine, connection)
                flow_result = report_flow_reader().require_report_flow(engine, connection)
                report_classification_directory_reader().require_classification_directory(
                    engine, connection, _expected_flows=flow_result["expected_rows"]
                )
            balance_freeze_reader().compare_balance_freeze(connection)
        if include_indexes:
            from .discovery_indexes import verify_discovery_indexes
            from .entity_references import verify_entity_references
            from .read_indexes import _verified_closes_for_indexes, verify_read_indexes

            verify_entity_references(connection, registry=engine.store.registry)
            verify_discovery_indexes(connection)

            if connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE name='read_index_source'"
            ).fetchone():
                if decoded_closes is None:
                    verify_read_indexes(connection)
                else:
                    verify_read_indexes(
                        connection,
                        _verified_closes=_verified_closes_for_indexes(
                            connection, decoded_closes
                        ),
                    )
        return {
            "status": "verified",
            "coverage": {
                "sources": "verified",
                "historical_adoption": "verified",
                "projections": "verified" if include_projections else "not_checked",
                "read_indexes": "verified" if include_indexes else "not_checked",
            },
            "limitations": limitations,
            "counts": {
                "facts": len(source["facts"]),
                "calculations": len(source["calculations"]),
                "vouchers": len(source["vouchers"]),
                "closes": close_count,
                "evidence": source["evidence_count"],
            },
        }
    except KernelError:
        raise
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, sqlite3.Error) as exc:
        raise KernelError(
            "content_integrity_failed",
            "已保存的核算内容无法按其存储合同核验",
            component="content",
            record_id="*",
            reason="invalid_stored_content",
        ) from exc


def verify_publication(engine, connection, calculation_ids):
    """Verify the actual frozen inputs and voucher sources of affected results."""
    try:
        source = _check_sources(engine, connection, calculation_ids)
        return {
            "status": "verified",
            "calculations": len(source["calculations"]),
            "vouchers": len(source["vouchers"]),
        }
    except KernelError:
        raise
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, sqlite3.Error) as exc:
        raise KernelError(
            "content_integrity_failed",
            "本次发布来源核验失败",
            component="publication",
            record_id="*",
            reason="invalid_stored_content",
        ) from exc


def verify_sources(engine, connection, *, calculation_ids=(), fact_ids=(), _return_facts=False):
    """Validate saved inputs; optionally return this call's checked fact rows."""
    try:
        requested_facts = set(fact_ids)
        # One verification owns the union of dependency facts and explicitly
        # requested facts. Its evidence union is read once as well. No proof
        # survives this call or bypasses either side of a publication write.
        source = _check_sources(
            engine, connection, calculation_ids, extra_fact_ids=requested_facts
        )
        if not requested_facts <= source["facts"].keys():
            _invalid("fact", "*", "referenced_fact_missing")
        if _return_facts:
            return {ident: source["facts"][ident] for ident in sorted(requested_facts)}
        return {
            "status": "verified",
            "calculations": len(source["calculations"]),
            "vouchers": len(source["vouchers"]),
            "facts": len(requested_facts),
        }
    except KernelError:
        raise
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, sqlite3.Error) as exc:
        raise KernelError(
            "content_integrity_failed",
            "本次来源核验失败",
            component="source",
            record_id="*",
            reason="invalid_stored_content",
        ) from exc


def verify_prepared_sources(engine, connection, prepared, *, new_fact_ids=()):
    """Check existing trace inputs before writing an entire publication batch."""
    prepared = tuple(prepared)
    planned = {item.calculation_id for item in prepared if item.calculation_id is not None}
    previous = {
        item.previous_calculation_id
        for item in prepared
        if item.previous_calculation_id is not None
    }
    calculations = set(previous)
    facts = {item.version.id for item in prepared}
    for item in prepared:
        for read, selected in item.context.trace().selections:
            (facts if read.source == "fact" else calculations).update(
                value.id for value in selected
            )
    stored_calculations = {
        row[0]
        for row in connection.execute(
            "SELECT c.id FROM json_each(?) ids JOIN calculation c ON c.id=ids.value",
            (canonical(sorted(calculations)),),
        )
    }
    if previous - stored_calculations or calculations - stored_calculations - planned:
        _invalid("publication", "*", "existing_calculation_source_missing")
    stored_facts = {
        row[0]
        for row in connection.execute(
            "SELECT f.id FROM json_each(?) ids JOIN fact_revision f ON f.id=ids.value",
            (canonical(sorted(facts)),),
        )
    }
    if facts - stored_facts - set(new_fact_ids):
        _invalid("publication", "*", "existing_fact_source_missing")
    return verify_sources(
        engine, connection, calculation_ids=stored_calculations, fact_ids=stored_facts
    )


def verify_close_integrity(engine, connection, period):
    """A new close cannot waive the independent all-account projection check."""
    if connection.in_transaction:
        return _verify_close_integrity_snapshot(engine, connection, period)
    connection.execute("BEGIN")
    try:
        return _verify_close_integrity_snapshot(engine, connection, period)
    finally:
        connection.rollback()


def _verify_close_integrity_snapshot(engine, connection, period):
    """Reuse verified close bodies only inside this one SQLite read snapshot."""
    from .verified_source_lease import verified_source_lease

    with verified_source_lease(connection):
        return _verify_close_integrity_leased(engine, connection, period)


def _verify_close_integrity_leased(engine, connection, period):
    from .content_history_context import duplicate_reader, journal_reader, material_watch_reader

    journal_reader().verify_journal(connection)
    month = month_type()(period).ordinal if isinstance(period, str) else period
    # Closed voucher heads are immutable; this proves the cumulative amounts
    # without substituting today's facts or calculators for historical results.
    ids = {
        row[0]
        for row in connection.execute(
            "SELECT c.id FROM calculation_current h JOIN calculation c ON c.id=h.calculation_id "
            "WHERE c.period<=? UNION SELECT v.calculation_id FROM voucher_current h "
            "JOIN voucher_version v ON v.id=h.version_id WHERE v.period<=?",
            (month, month),
        )
    }
    from .read_indexes import _close_references

    fact_ids, evidence_ids = set(), set()
    from .verified_close_archive import pack_verified_close

    packed_closes = []
    for row in connection.execute(
        "SELECT * FROM period_close WHERE period<=? ORDER BY period", (month,)
    ):
        from .content_history_context import close_reader

        decode_close = close_reader().decode_close

        manifest = decode_close(connection, row)
        packed_closes.append((row, pack_verified_close(manifest)))
        from .content_history_context import close_contract

        ids.update(close_contract().direct_calculation_ids(manifest))
        for _, _, kind, ident, _ in _close_references(row, manifest=manifest):
            if kind == "fact":
                fact_ids.add(ident)
            elif kind == "calculation":
                ids.add(ident)
        proof = manifest.get("owner_confirmation")
        if proof is not None:
            try:
                evidence_ids.add(bytes.fromhex(proof))
            except (TypeError, ValueError):
                _invalid("close", row["period"], "invalid_owner_evidence")
        del manifest
    try:
        source = _check_sources(engine, connection, ids, extra_fact_ids=fact_ids)
        _check_evidence(
            connection,
            evidence_ids - source["verified_evidence"],
            verified=source["verified_evidence"],
        )
        _, limitations, verified_closes = _check_closes(
            engine,
            connection,
            source,
            through_period=month,
            predecoded_closes=packed_closes,
            _return_closes=True,
        )
    except KernelError:
        raise
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, sqlite3.Error) as exc:
        raise KernelError(
            "content_integrity_failed",
            "关账采用的历史来源核验失败",
            component="close",
            record_id=str(period),
            reason="invalid_stored_content",
        ) from exc
    from contextlib import nullcontext

    from .verified_source_lease import verified_calculation_source

    with nullcontext():
        require_projections(
            connection, through_period=month, verified_calculations=source["calculations"]
        )
        from .content_history_context import settlement_projection_reader

        verified_settlement = settlement_projection_reader().require_settlement_projection(
            engine,
            connection,
            verified_calculations=source["calculations"],
            _return_verified=True,
        )
        from .content_history_context import settlement_reader

        settlement_reader().require_frozen_settlement_projection(
            engine,
            connection,
            verified_calculations=source["calculations"],
            _verified_projection=verified_settlement,
        )
        from .content_history_context import report_reader

        verified_reports = report_reader().require_report_projection(
            engine,
            connection,
            through_period=month,
            _verified_closes=verified_closes,
            _return_verified=True,
        )
        from .content_history_context import (
            balance_freeze_reader,
            report_flow_reader,
            report_semantics_reader,
        )

        verified_semantics = report_semantics_reader().require_report_semantics(
            engine,
            connection,
            through_period=month,
            _verified_closes=verified_closes,
            _verified_source=verified_calculation_source(connection, source),
            _return_verified=True,
        )
        flow_result = report_flow_reader().require_report_flow(
            engine,
            connection,
            through_period=month,
            _verified_closes=verified_closes,
            _verified_reports=verified_reports,
            _verified_semantics=verified_semantics,
        )
        from .content_history_context import report_classification_directory_reader

        report_classification_directory_reader().require_classification_directory(
            engine,
            connection,
            through_period=month,
            _verified_closes=verified_closes,
            _expected_flows=flow_result["expected_rows"],
        )
    balance_freeze_reader().compare_balance_freeze(connection)
    duplicate_reader().compare_duplicate_freeze(connection, engine)
    material_watch_reader().require_material_watch(
        engine, connection, through_period=month, _verified_closes=verified_closes
    )
    return {"status": "verified", "limitations": limitations}
