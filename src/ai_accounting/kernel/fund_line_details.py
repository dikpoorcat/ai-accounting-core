"""Formal account attribution beside immutable parent-account journal lines.

Attribution is determined by exact typed source fields and journal roles, never
by searching for a matching amount or by rewriting historical journal rows.
"""

from __future__ import annotations

import json
from collections import defaultdict
from functools import cache

from .account_definitions import ACCOUNT_NAMES, CASH_ACCOUNTS
from .contracts import KernelError
from .types import YearMonth, canonical, checked, digest

CONTRACT = "ai-accounting-kernel/2/fund-line-details/1"
FUND_LINE_DETAILS_DDL = """
CREATE TABLE fund_line_detail(
 version_id TEXT NOT NULL REFERENCES voucher_version(id),line_no INTEGER NOT NULL CHECK(line_no>0),
 entity_id TEXT NOT NULL REFERENCES entity(id),detail_code TEXT NOT NULL,parent_account TEXT NOT NULL,
 basis_calculation_id TEXT NOT NULL REFERENCES calculation(id),source_fact_id TEXT NOT NULL REFERENCES fact_revision(id),
 rule_version INTEGER NOT NULL CHECK(rule_version=1),digest BLOB NOT NULL CHECK(length(digest)=32),
 PRIMARY KEY(version_id,line_no),FOREIGN KEY(version_id,line_no) REFERENCES voucher_line(version_id,line_no)) STRICT;
CREATE INDEX fund_line_detail_account ON fund_line_detail(entity_id,version_id,line_no);
CREATE TABLE opening_fund_line_detail(
 calculation_id TEXT NOT NULL REFERENCES calculation(id),line_no INTEGER NOT NULL CHECK(line_no>0),
 entity_id TEXT NOT NULL REFERENCES entity(id),detail_code TEXT NOT NULL,parent_account TEXT NOT NULL,
 basis_calculation_id TEXT NOT NULL REFERENCES calculation(id),source_fact_id TEXT NOT NULL REFERENCES fact_revision(id),
 rule_version INTEGER NOT NULL CHECK(rule_version=1),digest BLOB NOT NULL CHECK(length(digest)=32),
 PRIMARY KEY(calculation_id,line_no)) STRICT;
CREATE TABLE fund_detail_activation(id INTEGER PRIMARY KEY CHECK(id=1),first_period INTEGER NOT NULL) STRICT;
CREATE TABLE fund_detail_opening(
 first_period INTEGER NOT NULL,entity_id TEXT NOT NULL REFERENCES entity(id),detail_code TEXT NOT NULL,
 parent_account TEXT NOT NULL,amount INTEGER NOT NULL,source_period INTEGER NOT NULL,
 source_digest BLOB NOT NULL CHECK(length(source_digest)=32),digest BLOB NOT NULL CHECK(length(digest)=32),
 PRIMARY KEY(first_period,entity_id)) STRICT;
"""  # noqa: E501 - preserve the exact exported schema SQL.
for _table in (
    "fund_line_detail",
    "opening_fund_line_detail",
    "fund_detail_activation",
    "fund_detail_opening",
):
    for _event in ("UPDATE", "DELETE"):
        FUND_LINE_DETAILS_DDL += (
            f"CREATE TRIGGER immutable_{_table}_{_event} BEFORE {_event} ON {_table} "
            "BEGIN SELECT RAISE(ABORT,'immutable fund detail attribution'); END;\n"
        )

_SINGLE = {
    "funding": ("1002", "bank_account_id"),
    "cash_funding": ("1001", "cash_account_id"),
    "platform_funding": ("1012", "platform_account_id"),
    "payment": ("1002", "bank_account_id"),
    "cash_payment": ("1001", "cash_account_id"),
    "platform_payment": ("1012", "platform_account_id"),
    "payroll_reserve_payment": ("1002", "bank_account_id"),
    "bank_income": ("1002", "bank_account_id"),
    "loan_drawdown": ("1002", "bank_account_id"),
    "platform_expense_confirmation": ("1012", "platform_account_id"),
    "opening_bank": ("1002", "bank_account_id"),
    "opening_cash": ("1001", "cash_account_id"),
}
_TYPES = {"1001": "cash", "1002": "bank", "1012": "platform"}


def _error(reason, identity=None):
    raise KernelError(
        "content_integrity_failed",
        "资金明细科目归属与正式来源不一致",
        component="fund_line_details",
        record_id=identity,
        reason=reason,
    )


@cache
def _source_models():
    from .service import default_registry

    return default_registry().models


def _exact_fact(connection, kind, fact_id):
    from .storage import Store

    header = connection.execute(
        "SELECT f.*,s.kind FROM fact_revision f JOIN subject s ON s.id=f.subject_id "
        "JOIN fact_seal z ON z.fact_id=f.id WHERE f.id=?",
        (fact_id,),
    ).fetchone()
    if header is None or header["kind"] != kind:
        _error("missing_exact_fact", fact_id)
    data = Store._fact_data(connection, fact_id, _source_models()[kind], kind)
    if digest(data) != header["digest"] or data["period"] != str(
        YearMonth.from_ordinal(header["period"])
    ):
        _error("exact_fact_digest", fact_id)
    return data


def bindings(kind, fact, lines):
    """Return explicit 1-based line roles for one exact fact/outcome."""
    result = {}
    for number, line in enumerate(lines, 1):
        parent = line["account"]
        if parent not in CASH_ACCOUNTS:
            continue
        entity = None
        if kind in _SINGLE:
            expected, field = _SINGLE[kind]
            if parent == expected:
                entity = fact.get(field)
        elif kind == "funds_transfer" and parent == "1002":
            field = "destination_bank_account_id" if line["debit"] else "source_bank_account_id"
            entity = fact.get(field)
        elif kind in ("cash_bank_transfer", "bank_platform_transfer"):
            field = {
                "1002": "bank_account_id",
                "1001": "cash_account_id",
                "1012": "platform_account_id",
            }.get(parent)
            entity = fact.get(field) if field else None
        elif kind in ("managed_reserve_expense", "managed_reserve_refund"):
            field = {
                "1002": "bank_account_id",
                "1001": "cash_account_id",
                "1012": "platform_account_id",
            }[parent]
            entity = fact.get(field)
        if not entity:
            _error("unmapped_typed_funds_line", f"{kind}:{number}")
        result[number] = (parent, entity)
    return result


def _source(connection, calculation_id):
    row = connection.execute("SELECT * FROM calculation WHERE id=?", (calculation_id,)).fetchone()
    if row is None:
        _error("missing_calculation", calculation_id)
    outcome = json.loads(row["outcome"])
    if digest(outcome) != row["digest"]:
        _error("calculation_digest", calculation_id)
    kind = row["kind"]
    # Only controlled registered kind names are used to select fact storage.
    if kind not in _SINGLE and kind not in {
        "funds_transfer",
        "cash_bank_transfer",
        "bank_platform_transfer",
        "managed_reserve_expense",
        "managed_reserve_refund",
        "opening_basis_correction",
    }:
        return row, outcome, {}
    return row, outcome, _exact_fact(connection, kind, row["fact_id"])


def _read_fact(connection, kind, fact_id, source_reads):
    if source_reads is None:
        return _exact_fact(connection, kind, fact_id)
    value = source_reads.raw_facts((fact_id,))[fact_id]
    if value["kind"] != kind:
        _error("exact_fact_kind", fact_id)
    return value["data"]


def _read_source(connection, calculation_id, source_reads):
    if source_reads is None:
        return _source(connection, calculation_id)
    value = source_reads.raw_calculations((calculation_id,))[calculation_id]
    return value, value["outcome"], value["fact_data"]


def _outcome_bindings(
    connection,
    kind,
    fact,
    outcome,
    section,
    *,
    fact_sources=None,
    calculation_sources=None,
    source_reads=None,
):
    if kind == "opening_basis_correction" and section == "lines":
        result, offset, reconstructed = {}, 0, []
        for adopted in outcome["values"]["members"]:
            binding_id = adopted["binding_fact_id"]
            binding = (fact_sources or {}).get(binding_id) or _read_fact(
                connection, "opening_identity_binding", binding_id, source_reads
            )
            package_id = adopted["package_calculation_id"]
            if binding["package_calculation_id"] != package_id:
                _error("opening_correction_package", binding_id)
            package_values = (calculation_sources or {}).get(package_id)
            if package_values is None:
                _, package_outcome, _ = _read_source(connection, package_id, source_reads)
                package_values = package_outcome["values"]
            members = [
                item
                for item in package_values["members"]
                if item["fact_id"] == binding["source_fact_id"]
            ]
            if len(members) != 1:
                _error("opening_correction_member", binding_id)
            member = members[0]
            original = member["opening_lines"]
            reverse = adopted["action"] == "supersede"
            reconstructed.extend(
                {
                    **line,
                    "debit": line["credit"] if reverse else line["debit"],
                    "credit": line["debit"] if reverse else line["credit"],
                    "cashflow": None,
                }
                for line in original
            )
            source = {}
            if member["kind"] in ("opening_bank", "opening_cash"):
                source = (fact_sources or {}).get(member["fact_id"]) or _read_fact(
                    connection, member["kind"], member["fact_id"], source_reads
                )
            for number, (parent, entity) in bindings(member["kind"], source, original).items():
                result[offset + number] = (parent, entity, member["fact_id"])
            offset += len(original)
        if reconstructed != list(outcome[section]):
            _error("opening_correction_line_order")
        return result
    if kind != "opening_package" or section != "opening_lines":
        return {
            number: (parent, entity, None)
            for number, (parent, entity) in bindings(kind, fact, outcome.get(section, [])).items()
        }
    result, offset, member_lines = {}, 0, []
    for member in outcome["values"]["members"]:
        lines = member["opening_lines"]
        member_lines.extend(lines)
        source = {}
        if member["kind"] in ("opening_bank", "opening_cash"):
            source = (fact_sources or {}).get(member["fact_id"]) or _read_fact(
                connection, member["kind"], member["fact_id"], source_reads
            )
        for number, (parent, entity) in bindings(member["kind"], source, lines).items():
            result[offset + number] = (parent, entity, member["fact_id"])
        offset += len(lines)
    if member_lines != list(outcome[section]):
        _error("opening_member_line_order")
    return result


def preview_fund_details(
    connection, kind, fact, outcome, *, fact_sources=None, calculation_sources=None
):
    from .fund_accounts import has_fund_definitions, require_fund_account

    result = {"lines": [], "opening_lines": []}
    if not has_fund_definitions(connection):
        return result
    for section in result:
        for number, (parent, entity, _) in _outcome_bindings(
            connection,
            kind,
            fact,
            outcome,
            section,
            fact_sources=fact_sources,
            calculation_sources=calculation_sources,
        ).items():
            subject = require_fund_account(connection, entity, _TYPES[parent])
            result[section].append(
                {
                    "line_no": number,
                    "entity_id": entity,
                    "detail_code": subject["detail_code"],
                    "parent_account": parent,
                }
            )
    return result


def _expected(connection, calculation_id, lines, *, opening=False, source_reads=None):
    from .fund_accounts import require_fund_account

    calculation, outcome, fact = _read_source(connection, calculation_id, source_reads)
    original = outcome.get("opening_lines" if opening else "lines", [])
    if list(lines) != original:
        _error("posted_lines_differ_from_source", calculation_id)
    assignments = _outcome_bindings(
        connection,
        calculation["kind"],
        fact,
        outcome,
        "opening_lines" if opening else "lines",
        source_reads=source_reads,
    )
    movements = defaultdict(int)
    output = []
    for number, (parent, entity, source_fact_id) in assignments.items():
        subject = require_fund_account(connection, entity, _TYPES[parent])
        if subject["parent_account"] != parent:
            _error("parent_account_mismatch", entity)
        movements[(_TYPES[parent], entity)] += (
            lines[number - 1]["debit"] - lines[number - 1]["credit"]
        )
        output.append(
            {
                "line_no": number,
                "entity_id": entity,
                "detail_code": subject["detail_code"],
                "parent_account": parent,
                "basis_calculation_id": calculation_id,
                "source_fact_id": source_fact_id or calculation["fact_id"],
                "rule_version": 1,
            }
        )
    effects = defaultdict(int)
    for effect in outcome["balances"]:
        if effect["category"] in _TYPES.values():
            effects[(effect["category"], effect["key"])] += effect["amount"]
    if {key: value for key, value in movements.items() if value} != {
        key: value for key, value in effects.items() if value
    }:
        _error("funds_effect_mismatch", calculation_id)
    return output


def _voucher_expected(connection, version_id, *, source_reads=None):
    header = connection.execute(
        "SELECT * FROM voucher_version WHERE id=?", (version_id,)
    ).fetchone()
    if header is None:
        _error("missing_voucher", version_id)
    basis = header
    if header["reverses_id"]:
        basis = connection.execute(
            "SELECT * FROM voucher_version WHERE id=?", (header["reverses_id"],)
        ).fetchone()
        if basis is None:
            _error("missing_reversal_basis", version_id)
    lines = [
        dict(row)
        for row in connection.execute(
            "SELECT account,debit,credit,cashflow FROM voucher_line "
            "WHERE version_id=? ORDER BY line_no",
            (version_id,),
        )
    ]
    if header["reverses_id"]:
        lines = [{**line, "debit": line["credit"], "credit": line["debit"]} for line in lines]
    expected = _expected(connection, basis["calculation_id"], lines, source_reads=source_reads)
    return [{"version_id": version_id, **item} for item in expected]


def _persist(connection, table, rows):
    for row in rows:
        checksum = digest([CONTRACT, table, row])
        values = {**row, "digest": checksum}
        existing = connection.execute(
            f"SELECT * FROM {table} WHERE {next(iter(row))}=? AND line_no=?",
            (next(iter(row.values())), row["line_no"]),
        ).fetchone()
        if existing is not None:
            if dict(existing) != values:
                _error("existing_assignment_mismatch", str(row))
            continue
        connection.execute(
            f"INSERT INTO {table}({','.join(values)}) VALUES({','.join('?' for _ in values)})",
            tuple(values.values()),
        )


def store_voucher_fund_line_details(connection, version_id):
    if (
        connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='fund_line_detail'"
        ).fetchone()
        is None
    ):
        return
    _persist(connection, "fund_line_detail", _voucher_expected(connection, version_id))


def store_opening_fund_line_details(connection, calculation_id):
    if (
        connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='opening_fund_line_detail'"
        ).fetchone()
        is None
    ):
        return
    _, outcome, _ = _source(connection, calculation_id)
    if not outcome.get("opening"):
        return
    rows = _expected(connection, calculation_id, outcome.get("opening_lines", []), opening=True)
    _persist(
        connection,
        "opening_fund_line_detail",
        [{"calculation_id": calculation_id, **row} for row in rows],
    )


def _activation(connection):
    row = connection.execute(
        "SELECT first_period FROM fund_detail_activation WHERE id=1"
    ).fetchone()
    return row[0] if row else None


def _opening_digest(value):
    return digest(
        [CONTRACT, "opening", {**value, "source_digest": bytes(value["source_digest"]).hex()}]
    )


def verify_fund_line_details(
    connection,
    *,
    version_ids=None,
    calculation_ids=None,
    period=None,
    _source_reads=None,
):
    """Validate exact selected records. Historical pre-activation closes stay unchanged."""
    if (
        connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='fund_line_detail'"
        ).fetchone()
        is None
    ):
        return {"lines": [], "opening_lines": []}
    if _source_reads is not None:
        from .verified_source_reads import VerifiedSourceReads

        if (
            not isinstance(_source_reads, VerifiedSourceReads)
            or _source_reads.connection is not connection
        ):
            _error("verified_source_reader_identity")
        _source_reads._require_source()
    if version_ids is None:
        sql = "SELECT v.id FROM voucher_current h JOIN voucher_version v ON v.id=h.version_id"
        parameters = ()
        if period is not None:
            sql += " WHERE v.period=?"
            parameters = (period,)
        version_ids = [row[0] for row in connection.execute(sql, parameters)]
        if period is None:
            version_ids += [
                row[0]
                for row in connection.execute("SELECT DISTINCT version_id FROM fund_line_detail")
            ]
    if calculation_ids is None:
        sql = (
            "SELECT c.id FROM calculation_current h JOIN calculation c ON c.id=h.calculation_id "
            "WHERE json_extract(c.outcome,'$.opening')=1"
        )
        parameters = ()
        if period is not None:
            sql += " AND c.period=?"
            parameters = (period,)
        calculation_ids = [row[0] for row in connection.execute(sql, parameters)]
        if period is None:
            calculation_ids += [
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT calculation_id FROM opening_fund_line_detail"
                )
            ]
    first = _activation(connection)
    result = {"lines": [], "opening_lines": []}
    for table, identities, identity_field, section in (
        ("fund_line_detail", sorted(set(version_ids)), "version_id", "lines"),
        (
            "opening_fund_line_detail",
            sorted(set(calculation_ids)),
            "calculation_id",
            "opening_lines",
        ),
    ):
        for identity in identities:
            rows = [
                dict(row)
                for row in connection.execute(
                    f"SELECT * FROM {table} WHERE {identity_field}=? ORDER BY line_no", (identity,)
                )
            ]
            header_table = "voucher_version" if section == "lines" else "calculation"
            source_period = connection.execute(
                f"SELECT period FROM {header_table} WHERE id=?", (identity,)
            ).fetchone()
            if source_period is None:
                _error("missing_source", identity)
            if first is not None and source_period[0] < first and not rows:
                continue
            if section == "lines":
                expected = _voucher_expected(connection, identity, source_reads=_source_reads)
            else:
                _, outcome, _ = _read_source(connection, identity, _source_reads)
                expected = [
                    {"calculation_id": identity, **item}
                    for item in _expected(
                        connection,
                        identity,
                        outcome.get("opening_lines", []),
                        opening=True,
                        source_reads=_source_reads,
                    )
                ]
            expected = [{**row, "digest": digest([CONTRACT, table, row])} for row in expected]
            if rows != expected:
                _error("assignment_set_mismatch", identity)
            result[section].extend({**row, "digest": row["digest"].hex()} for row in rows)
    return result


def snapshot_fund_details(connection, period, *, version_ids=None, calculation_ids=None):
    if (
        connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='fund_line_detail'"
        ).fetchone()
        is None
    ):
        return {
            "contract": CONTRACT,
            "period": period,
            "activation": None,
            "opening": [],
            "lines": [],
            "opening_lines": [],
        }
    selected = verify_fund_line_details(
        connection, version_ids=version_ids, calculation_ids=calculation_ids, period=period
    )
    from .fund_accounts import require_fund_account

    opening = []
    first = _activation(connection)
    balances, source_checksum = {}, None
    if first is not None:
        from .close_storage import verified_header
        from .period_balance_freeze import read_frozen_balances

        prior = connection.execute(
            "SELECT * FROM period_close WHERE period=?", (first - 1,)
        ).fetchone()
        if prior is not None:
            header = verified_header(connection, prior)
            source_checksum = header.logical_digest
            balances = {
                row["key"]: row["ending"]
                for row in read_frozen_balances(
                    connection, header, category=("bank", "cash", "platform")
                )
            }
        else:
            source_checksum = digest([CONTRACT, "new_company", first])
    for row in connection.execute(
        "SELECT * FROM fund_detail_opening ORDER BY first_period,entity_id"
    ):
        value = dict(row)
        checksum = value.pop("digest")
        if _opening_digest(value) != checksum:
            _error("opening_digest", row["entity_id"])
        definition = require_fund_account(connection, value["entity_id"])
        if (
            definition["detail_code"] != value["detail_code"]
            or definition["parent_account"] != value["parent_account"]
            or value["first_period"] != _activation(connection)
            or value["source_period"] != value["first_period"] - 1
            or value["source_digest"] != source_checksum
            or value["amount"] != balances.get(value["entity_id"], 0)
        ):
            _error("opening_identity", value["entity_id"])
        opening.append(
            {**value, "source_digest": value["source_digest"].hex(), "digest": checksum.hex()}
        )
    return {
        "contract": CONTRACT,
        "period": period,
        "activation": _activation(connection),
        "opening": opening,
        **selected,
    }


def fund_line_details(connection, version_ids):
    verified = verify_fund_line_details(connection, version_ids=version_ids, calculation_ids=[])
    return {(row["version_id"], row["line_no"]): row for row in verified["lines"]}


def display_fund_lines(connection, version_id, lines, *, details=None):
    from .entities import profiles_for_entities
    from .fund_accounts import require_fund_account

    if (
        connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='fund_line_detail'"
        ).fetchone()
        is None
    ):
        # Historical source archives retain their original exact schema.
        return [
            {
                **line,
                "code": line["account"],
                "account_name": ACCOUNT_NAMES.get(line["account"], "未配置名称的科目"),
            }
            for line in lines
        ]
    details = fund_line_details(connection, [version_id]) if details is None else details
    profiles = profiles_for_entities(
        connection, {value["entity_id"] for key, value in details.items() if key[0] == version_id}
    )
    names = {}
    for key, value in details.items():
        if key[0] != version_id or value["entity_id"] in names:
            continue
        entity_id = value["entity_id"]
        name = profiles.get(entity_id, {}).get("display_name")
        if not name:
            identity = require_fund_account(connection, entity_id)["identity"]
            if value["parent_account"] == "1002":
                name = (
                    identity["bank_name"] + "（尾号" + identity["bank_account_number"][-4:] + "）"
                )
            elif value["parent_account"] == "1012":
                name = identity["provider_name"] + "—" + identity["account_identifier"]
            else:
                name = identity["display_name"]
        names[entity_id] = name
    name_counts = defaultdict(int)
    for name in names.values():
        name_counts[name] += 1
    result = []
    for index, line in enumerate(lines, 1):
        value = details.get((version_id, line.get("line_no", index)))
        code, name = line["account"], ACCOUNT_NAMES.get(line["account"], "未配置名称的科目")
        if value:
            code = value["detail_code"]
            account_name = names[value["entity_id"]]
            name += "—" + account_name
            if name_counts[account_name] > 1:
                name += "（" + code + "）"
        result.append({**line, "code": code, "account_name": name})
    return result


def display_fund_line(connection, version_id, line):
    value = display_fund_lines(connection, version_id, [line])[0]
    return {"code": value["code"], "account": value["account_name"]}


def initialize_open_details(engine, connection, first_period):
    """Explicit upgrade: start with verified preceding per-account frozen balances."""
    from .close_storage import verified_header
    from .fund_accounts import require_fund_account
    from .integrity import verify_sources
    from .period_balance_freeze import read_frozen_balances

    first_period = (
        YearMonth(first_period).ordinal if isinstance(first_period, str) else first_period
    )
    existing = _activation(connection)
    if existing is not None:
        if existing != first_period:
            _error("activation_conflict")
        return snapshot_fund_details(connection, first_period)
    if connection.execute("SELECT 1 FROM period_close WHERE period>=?", (first_period,)).fetchone():
        _error("activation_period_closed")
    previous = connection.execute(
        "SELECT * FROM period_close WHERE period=?", (first_period - 1,)
    ).fetchone()
    source_digest = digest([CONTRACT, "new_company", first_period])
    balances = {}
    if previous is not None:
        header = verified_header(connection, previous)
        source_digest = header.logical_digest
        balances = {
            row["key"]: row["ending"]
            for row in read_frozen_balances(
                connection, header, category=("bank", "cash", "platform")
            )
        }
    elif connection.execute("SELECT 1 FROM period_close LIMIT 1").fetchone():
        _error("missing_previous_close")
    connection.execute("INSERT INTO fund_detail_activation VALUES(1,?)", (first_period,))
    for row in connection.execute("SELECT id FROM entity WHERE kind='fund_account' ORDER BY id"):
        subject = require_fund_account(connection, row[0])
        value = {
            "first_period": first_period,
            "entity_id": row[0],
            "detail_code": subject["detail_code"],
            "parent_account": subject["parent_account"],
            "amount": checked(balances.get(row[0], 0)),
            "source_period": first_period - 1,
            "source_digest": source_digest,
        }
        connection.execute(
            "INSERT INTO fund_detail_opening VALUES(?,?,?,?,?,?,?,?)",
            (*value.values(), _opening_digest(value)),
        )
    versions = [
        row[0]
        for row in connection.execute(
            "SELECT id FROM voucher_version WHERE period>=? ORDER BY period,id", (first_period,)
        )
    ]
    calculations = [
        row[0]
        for row in connection.execute(
            "SELECT id FROM calculation WHERE period>=? ORDER BY period,id", (first_period,)
        )
    ]
    bases = {
        row[0]
        for row in connection.execute(
            "SELECT CASE WHEN v.reverses_id IS NULL THEN v.calculation_id ELSE o.calculation_id "
            "END FROM voucher_version v LEFT JOIN voucher_version o ON o.id=v.reverses_id "
            "WHERE v.id IN(SELECT value FROM json_each(?))",
            (canonical(versions),),
        )
    }
    verify_sources(engine, connection, calculation_ids=set(calculations) | bases)
    for identity in versions:
        store_voucher_fund_line_details(connection, identity)
    for identity in calculations:
        store_opening_fund_line_details(connection, identity)
    return snapshot_fund_details(connection, first_period)
