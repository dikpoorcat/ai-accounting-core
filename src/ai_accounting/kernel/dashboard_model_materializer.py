"""Produce complete, typed owner read content from one authoritative snapshot.

This module never persists or authenticates a read model. The transaction owner
must anchor its output independently. Group summaries certify the complete
aggregate; their member bodies remain a separate, exact read responsibility.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass

from .contracts import KernelError
from .dashboard_model_dates import ModelDateBasis, make_date_basis
from .dashboard_model_filters import decorate_keys
from .dashboard_pages import SORT_PROFILES, seal_collections
from .response_contracts import validate_response
from .types import YearMonth, canonical

SourceBinding = tuple[str, str, str, str]
FactBinding = tuple[str, str]
SeekKey = str | int


@dataclass(frozen=True)
class MaterializedBlock:
    key: str
    kind: str
    payload: dict
    keys: tuple[tuple[SeekKey], ...] = ()
    sources: tuple[SourceBinding, ...] = ()
    voucher_version_ids: tuple[str, ...] = ()
    total_count: int | None = None
    filtered_count: int | None = None
    item_sources: tuple[tuple[SourceBinding, ...], ...] = ()
    item_voucher_version_ids: tuple[tuple[str, ...], ...] = ()
    fact_sources: tuple[FactBinding, ...] = ()
    item_fact_sources: tuple[tuple[FactBinding, ...], ...] = ()


@dataclass(frozen=True)
class CollectionIndex:
    page: str
    variant: str
    section: str
    keys: tuple[SeekKey, ...]
    total_count: int
    filtered_count: int
    sort_profile: str | None
    filters: dict[str, str | None]


@dataclass(frozen=True)
class MaterializedModel:
    period: str
    as_of: str
    snapshot_version: str
    blocks: tuple[MaterializedBlock, ...]
    collections: tuple[CollectionIndex, ...]
    unmaterialized: tuple[str, ...] = ()
    date_basis: ModelDateBasis | None = None


def _bindings(snap, identifiers):
    identifiers = set(identifiers)
    if not identifiers:
        return ()
    cache = snap.semantic_cache.setdefault("model_source_bindings", {})
    missing = identifiers - cache.keys()
    rows = (
        tuple(
            (row["id"], row["digest"].hex(), row["fact_id"], row["fact_digest"].hex())
            for row in snap.connection.execute(
                "SELECT c.id,c.digest,c.fact_id,f.digest fact_digest "
                "FROM json_each(?) ids JOIN calculation c ON c.id=ids.value "
                "JOIN fact_revision f ON f.id=c.fact_id ORDER BY c.id",
                (canonical(sorted(missing)),),
            )
        )
        if missing
        else ()
    )
    if {row[0] for row in rows} != missing:
        raise KernelError("content_integrity_failed", "看板读取内容的精确来源缺失")
    cache.update((row[0], row) for row in rows)
    return tuple(cache[ident] for ident in sorted(identifiers))


def _subject_sources(snap, subjects, *, kinds=None, posting_period=None):
    if not subjects:
        return {}
    cache = snap.semantic_cache.setdefault("model_subject_sources", {})
    scope = (tuple(sorted(kinds)) if kinds is not None else None, posting_period)
    missing = {subject for subject in subjects if (scope, subject) not in cache}
    if missing:
        selected = snap.calculations.selected(
            subjects=missing,
            kinds=kinds,
            posting_period=posting_period,
        )
        cache.update(((scope, subject), selected.get(subject)) for subject in missing)
    return {subject: value for subject in subjects if (value := cache[scope, subject]) is not None}


def _employee_sources(snap, items):
    """Bind displayed monthly payroll; historical unpaid totals are certified aggregates."""
    from .dashboard import _workforce_payroll_rows

    cache = snap.semantic_cache.get("model_month_payroll_sources")
    if cache is None:
        cache = {}
        for row in _workforce_payroll_rows(snap):
            calc = row["basis"]
            ident = calc["fact"]["data"]["employee_id"]
            cache.setdefault(ident, set()).add(calc["id"])
        snap.semantic_cache["model_month_payroll_sources"] = cache
    return {source for item in items for source in cache.get(item["employee_id"], ())}


def _asset_sources(snap, items):
    """Bind the selected card's acquisition and current lifecycle, not every charge."""
    from .dashboard import _asset_card_sources

    sources = snap.semantic_cache.get("model_asset_card_sources")
    if sources is None:
        sources = _asset_card_sources(snap, activation_identities=True)
        snap.semantic_cache["model_asset_card_sources"] = sources
    return {
        calc["id"]
        for item in items
        for kind in ("acquisitions", "activations", "disposals")
        if (calc := sources[kind].get(item["asset_id"])) is not None
    }


def _presentation_sources(snap, identifiers):
    """Exact obligation bodies used by movement labels retain saved identities."""
    identifiers = set(identifiers)
    if not identifiers:
        return identifiers
    resolutions = snap.reads.relations_many(identifiers)
    for ident, resolution in resolutions.items():
        balance_keys = {
            item["key"] for item in snap.reads.calculation(ident)["outcome"].get("balances", ())
        }
        identifiers.update(
            item["source_calculation_id"]
            for item in resolution.get("obligations", ())
            if item.get("key") in balance_keys and item.get("source_calculation_id")
        )
    return identifiers


def _statement_proofs(snap, items):
    """Bind the actual shown extraction independently of its adopted calculation."""
    if not items:
        return (), ()
    directory = snap.semantic_cache.get("model_statement_directory")
    if directory is None:
        fact_ids = snap.fact_ids_of_kind("bank_statement", period=snap.period)
        statements = {
            row["subject_id"]: dict(row)
            for row in snap.connection.execute(
                "SELECT f.id,f.subject_id,f.digest FROM json_each(?) ids "
                "JOIN fact_revision f ON f.id=ids.value",
                (canonical(sorted(fact_ids)),),
            )
        }
        reconciliation_facts = snap.fact_ids_of_kind("bank_reconciliation", period=snap.period)
        candidates, reconciliation_ids = {}, {}
        for row in snap.connection.execute(
            "SELECT f.id,f.subject_id,r.statement_id FROM json_each(?) ids "
            "JOIN fact_bank_reconciliation r ON r.revision_id=ids.value "
            "JOIN fact_revision f ON f.id=r.revision_id",
            (canonical(sorted(reconciliation_facts)),),
        ):
            candidates.setdefault(row["statement_id"], set()).add(row["subject_id"])
            reconciliation_ids[row["subject_id"]] = row["id"]
        reconciliations = _subject_sources(
            snap,
            {subject for subjects in candidates.values() for subject in subjects},
            kinds={"bank_reconciliation"},
            posting_period=snap.period,
        )
        adopted_statements = _subject_sources(
            snap,
            statements,
            kinds={"bank_statement"},
            posting_period=snap.period,
        )
        pending = (
            {
                row[0]
                for row in snap.connection.execute(
                    "SELECT DISTINCT subject_id FROM pending WHERE subject_id IN "
                    "(SELECT value FROM json_each(?))",
                    (canonical(sorted(set(statements) | set(reconciliation_ids))),),
                )
            }
            if not snap.close
            else set()
        )
        directory = (
            statements,
            candidates,
            reconciliations,
            reconciliation_ids,
            adopted_statements,
            pending,
        )
        snap.semantic_cache["model_statement_directory"] = directory
    (statements, candidates, reconciliations, reconciliation_ids, adopted_statements, pending) = (
        directory
    )
    identifiers, facts = set(), set()
    for item in items:
        subject, item_no = item["id"].rsplit(":", 1)
        statement = statements.get(subject)
        if statement is None:
            raise KernelError("content_integrity_failed", "看板原始资金行缺少实际事实")
        facts.add((statement["id"], statement["digest"].hex()))
        selected_statement = adopted_statements.get(subject)
        if (
            selected_statement is None
            or selected_statement["fact_id"] != statement["id"]
            or subject in pending
            or len(candidates.get(subject, ())) != 1
        ):
            continue
        # A matched source can precede a later no-impact head. Read the exact
        # saved reconciliation dependency, as the existing presenter does.
        for reconciliation_subject in candidates.get(subject, ()):
            calc = reconciliations.get(reconciliation_subject)
            if (
                calc is None
                or calc["fact_id"] != reconciliation_ids[reconciliation_subject]
                or reconciliation_subject in pending
                or not snap.reads.calculation(calc["id"])["outcome"]["values"].get("balanced")
            ):
                continue
            identifiers.add(calc["id"])
            identifiers.update(
                row[0]
                for row in snap.connection.execute(
                    "SELECT DISTINCT c.id FROM fact_bank_statement_entries e "
                    "JOIN fact_bank_reconciliation_matches m ON m.reference=e.reference "
                    "AND m.revision_id=? JOIN calculation c ON c.kind=m.source_kind "
                    "AND c.subject_id=m.source_id JOIN dependency_calculation d "
                    "ON d.calculation_id=? AND d.upstream_id=c.id "
                    "WHERE e.revision_id=? AND e.item_no=?",
                    (calc["fact_id"], calc["id"], statement["id"], int(item_no)),
                )
            )
    return _bindings(snap, identifiers), tuple(sorted(facts))


def _row_keys(snap, page, section, items):
    if page == "brief":
        return [
            int(item["number"])
            if section == "vouchers"
            else item["key" if section == "activity" else "id"]
            for item in items
        ]
    if page == "funds":
        from .dashboard_funds import FUND_TYPES

        categories = {value: key for key, value in FUND_TYPES.items()}
        if section == "accounts":
            return [categories[item["type"]] + ":" + item["account_id"] for item in items]
        return [item["fund_id" if section == "investment_products" else "id"] for item in items]
    if section == "employees":
        return [item["employee_id"] for item in items]
    if section == "labor_workers":
        return [item["person_id"] for item in items]
    if section == "assets":
        return [item["asset_id"] for item in items]
    # The public labor/project row exposes a business ID; its original seek key
    # is the selected calculation ID. Do not substitute a mutable current head.
    kinds = (
        {"labor", "labor_accrual", "labor_project_cost"}
        if page == "employees"
        else {
            "project_cost",
            "labor_project_cost",
        }
    )
    chosen = _subject_sources(
        snap,
        {item["source_id"] for item in items},
        kinds=kinds,
        posting_period=snap.period if page == "employees" else None,
    )
    return [chosen[item["source_id"]]["id"] for item in items]


def _funds_voucher_rows(snap, section, items):
    """Bind a posted effect to its existing number, exact basis and local slot."""
    from .dashboard_funds import FUND_TYPES

    if not items:
        return ()
    by_number = snap.semantic_cache.get("model_funds_voucher_numbers")
    if by_number is None:
        headers = snap.month_journal.authenticated_headers()
        by_number = {row["number"]: row for row in headers}
        if len(by_number) != len(headers):
            raise KernelError("content_integrity_failed", "资金流水凭证编号不唯一")
        snap.semantic_cache["model_funds_voucher_numbers"] = by_number
    rows = {}
    for item in items:
        try:
            parts = item["id"].split(":")
            number = int(parts[0])
            row = by_number[number]
            if parts[0] != f"{number:012d}" or parts[1] != row["basis_calculation_id"]:
                raise ValueError("posted effect identity")
            calculation = snap.reads.calculation(parts[1])
            if section == "movements":
                position = int(parts[2])
                effects = [effect for effect in calculation["outcome"]["balances"]
                           if effect["category"] in {"bank", "cash", "platform"}
                           and effect["amount"]]
                effect = effects[position]
                sign = -1 if row["reverses_id"] else 1
                if (len(parts) != 3 or position < 0 or parts[2] != f"{position:06d}"
                        or effect["key"] != item["account_id"]
                        or FUND_TYPES[effect["category"]] != item["account_type"]
                        or sign * effect["amount"] != item["signed_amount_fen"]
                        or item["correction"] != (sign < 0)):
                    raise ValueError("posted money effect")
            elif parts[2] == "settlement":
                position = int(parts[3])
                settlements = calculation["outcome"]["values"]["settlements"]
                if (len(parts) != 4 or position < 0 or parts[3] != f"{position:06d}"
                        or position >= len(settlements)):
                    raise ValueError("posted investment settlement")
            elif len(parts) != 3 or parts[2] != "confirmation":
                raise ValueError("posted investment effect")
        except (KeyError, ValueError, TypeError, IndexError) as exc:
            raise KernelError("content_integrity_failed", "资金流水缺少精确入账凭证") from exc
        rows[row["id"]] = row
    return tuple(rows.values())


def _page_sources(snap, page, section, items):
    """Return raw hit bindings; certified group aggregates have no member hits."""
    if page == "brief" and section != "vouchers":
        return (), ()
    vouchers = tuple(item["voucher_version_id"] for item in items) if page == "brief" else ()
    if vouchers:
        by_id = snap.semantic_cache.get("model_voucher_headers")
        if by_id is None:
            by_id = {row["id"]: row for row in snap.month_journal.authenticated_headers()}
            snap.semantic_cache["model_voucher_headers"] = by_id
        if not set(vouchers) <= by_id.keys():
            raise KernelError("content_integrity_failed", "看板采用的精确凭证缺失")
        identifiers = {
            by_id[ident][field]
            for ident in vouchers
            for field in ("basis_calculation_id", "voucher_calculation_id")
        }
        frozen = snap.month_journal.authenticated_frozen_adoptions(
            [by_id[ident] for ident in vouchers],
        )
        identifiers.update(
            item[field]
            for item in frozen.values()
            for field in ("calculation_id", "adopted_calculation_id")
        )
    elif page == "employees" and section == "employees":
        identifiers = _employee_sources(snap, items)
    elif page == "assets" and section == "assets":
        identifiers = _asset_sources(snap, items)
    elif page == "funds" and section in {"accounts", "investment_products"}:
        identifiers = set()
    elif page == "funds" and section in {"movements", "investment_events"}:
        identifiers = {item["id"].split(":", 2)[1] for item in items}
        if section == "movements":
            identifiers = _presentation_sources(snap, identifiers)
        else:
            for item in items:
                parts = item["id"].split(":")
                if parts[2] == "settlement":
                    calc = snap.reads.calculation(parts[1])
                    identifiers.add(
                        calc["outcome"]["values"]["settlements"][int(parts[3])][
                            "source_calculation"
                        ]
                    )
        posted = _funds_voucher_rows(snap, section, items)
        vouchers = tuple(row["id"] for row in posted)
        identifiers.update(row[field] for row in posted
                           for field in ("basis_calculation_id", "voucher_calculation_id"))
        frozen = snap.month_journal.authenticated_frozen_adoptions(posted)
        identifiers.update(item[field] for item in frozen.values()
                           for field in ("calculation_id", "adopted_calculation_id"))
    elif page == "funds" and section == "statements":
        bindings, _ = _statement_proofs(snap, items)
        return bindings, ()
    else:
        subjects = {item["subject_id"] for item in items if "subject_id" in item}
        subjects.update(item["source_id"] for item in items if "source_id" in item)
        identifiers = {calc["id"] for calc in _subject_sources(snap, subjects).values()}
    return _bindings(snap, identifiers), vouchers


def _native_frame(dashboard, snap, page, data, *, section=None, filters=None):
    versions = {"brief": 19, "funds": 9, "employees": 12, "assets": 10}
    filters = (
        dict(filters)
        if filters is not None
        else {
            "brief": {},
            "funds": {
                "movement_account_type": None,
                "movement_account_id": None,
                "statement_account_id": None,
            },
            "employees": {"employee_filter": "employment_active", "employee_id": None},
            "assets": {"asset_filter": "all", "asset_id": None, "project_id": None},
        }[page]
    )
    selected = data.get("selected_movement_account") if page == "funds" else None
    if selected:
        filters.update(
            movement_account_type=selected["type"], movement_account_id=selected["account_id"]
        )
    data = {
        **data,
        "collections": {
            name: {**collection, "page": dict(collection["page"])}
            for name, collection in data["collections"].items()
        },
    }
    seal_collections(snap, page, data, filters)
    value = {**dashboard._response(snap, data), "schema_version": versions[page]}
    if page == "brief":
        value["scope"] = section or "overview"
    validate_response("dashboard_" + page, value)
    return value


def build_model(dashboard, period, *, block_size=100):
    """Build five default pages and every continuation in one fresh snapshot.

    The caller supplies the trusted write transaction's snapshot provider and
    bypasses derived reads. There is no successful-proof reuse between builds.
    Quarterly figures use their complete native report algorithm with this same
    guarded transaction, including exact frozen sources for a closed quarter.
    """
    if type(block_size) is not int or not 1 <= block_size <= 500:
        raise ValueError("看板读取块大小必须为 1 至 500")
    period = str(YearMonth(period))
    from .dashboard import Dashboard, _assets, _employees, _funds
    from .dashboard_brief_groups import activity_group_page, vouchers_for_rows
    from .dashboard_open_groups import open_group_page

    blocks, indexes = [], []
    with dashboard._snapshot(period) as snap:
        if snap is None:
            raise KernelError("dashboard_period_not_found", "没有找到所选会计月份")

        @contextmanager
        def same_snapshot(_period):
            yield snap

        # Keep the caller and its provider untouched. The existing overview
        # algorithm produces all headlines once using this same source graph.
        producer = Dashboard(
            dashboard.engine, company_name=dashboard.company_name, companies=dashboard.companies
        )
        producer._snapshot = same_snapshot
        overview = producer.brief(period, limit=20, preparation="deferred")

        defaults = {"brief": overview}
        fund_filters = {
            "movement_account_type": None,
            "movement_account_id": None,
            "statement_account_id": None,
        }
        funds_data = _funds(snap, limit=20, filters=fund_filters, select_first_account=True)
        defaults["funds"] = _native_frame(producer, snap, "funds", funds_data)
        defaults["employees"] = _native_frame(
            producer,
            snap,
            "employees",
            _employees(
                snap,
                limit=20,
                employee_filter="employment_active",
            ),
        )
        defaults["assets"] = _native_frame(producer, snap, "assets", _assets(snap, limit=20))

        def add_default(page, payload, *, variant="default"):
            bindings, versions, fact_bindings = set(), set(), set()
            for section, collection in payload["data"]["collections"].items():
                hits, ids = _page_sources(snap, page, section, collection["items"])
                bindings.update(hits)
                versions.update(ids)
                if page == "funds" and section == "statements":
                    fact_bindings.update(_statement_proofs(snap, collection["items"])[1])
            validate_response("dashboard_" + page, payload)
            blocks.append(
                MaterializedBlock(
                    f"{page}/{variant}/overview/0",
                    "dashboard_" + page,
                    payload,
                    sources=tuple(sorted(bindings)),
                    voucher_version_ids=tuple(sorted(versions)),
                    fact_sources=tuple(sorted(fact_bindings)),
                )
            )

        for page, payload in defaults.items():
            add_default(page, payload)

        year, month = int(period[:4]), int(period[5:])
        quarterly = producer._quarterly_report_in_snapshot(
            year,
            (month - 1) // 3 + 1,
            connection=snap.connection,
            reads=snap.reads,
            as_of=snap.as_of,
        )
        validate_response("dashboard_quarterly_report", quarterly)
        # Every displayed figure is a certified aggregate. The page exposes no
        # raw fact or voucher body requiring an ancestor hit proof on each read.
        blocks.append(
            MaterializedBlock(
                "quarterly_report/default/overview/0",
                "dashboard_quarterly_report",
                quarterly,
            )
        )

        def collect(page, section, filters, fetch: Callable, *, variant="default"):
            cursor, ordinal, all_keys = None, 0, []
            counts = None
            while True:
                data = fetch(cursor)
                data["collections"] = {section: data["collections"][section]}
                collection = data["collections"][section]
                items, paging = collection["items"], collection["page"]
                keys = _row_keys(snap, page, section, items)
                next_counts = paging["total_count"], paging["filtered_count"]
                if counts is not None and next_counts != counts:
                    raise KernelError("content_integrity_failed", "看板完整集合范围发生变化")
                counts = next_counts
                sources, versions = _page_sources(snap, page, section, items)
                item_proofs = [_page_sources(snap, page, section, [item]) for item in items]
                item_facts = tuple(
                    _statement_proofs(snap, [item])[1]
                    if page == "funds" and section == "statements"
                    else ()
                    for item in items
                )
                blocks.append(
                    MaterializedBlock(
                        f"{page}/{variant}/{section}/{ordinal}",
                        "dashboard_" + page,
                        _native_frame(producer, snap, page, data, section=section, filters=filters),
                        keys=(
                            tuple(
                                (key, item["voucher_version_id"])
                                for key, item in zip(keys, items, strict=True)
                            )
                            if page == "brief" and section == "vouchers"
                            else decorate_keys(page, section, keys, items)
                        ),
                        sources=sources,
                        voucher_version_ids=versions,
                        total_count=counts[0],
                        filtered_count=counts[1],
                        item_sources=tuple(proof[0] for proof in item_proofs),
                        item_voucher_version_ids=tuple(proof[1] for proof in item_proofs),
                        fact_sources=tuple(
                            sorted({fact for facts in item_facts for fact in facts})
                        ),
                        item_fact_sources=item_facts,
                    )
                )
                all_keys.extend(keys)
                if not paging["has_more"]:
                    break
                following = paging["next_cursor"]
                if not keys or following != keys[-1] or following == cursor:
                    raise KernelError("content_integrity_failed", "看板集合分页位置不一致")
                cursor, ordinal = following, ordinal + 1
            if len(all_keys) != counts[1] or len(set(all_keys)) != len(all_keys):
                raise KernelError("content_integrity_failed", "看板完整集合成员缺失或重复")
            indexes.append(
                CollectionIndex(
                    page,
                    variant,
                    section,
                    tuple(all_keys),
                    counts[0],
                    counts[1],
                    SORT_PROFILES.get((page, section)),
                    dict(filters),
                )
            )

        def brief_collection(section, after):
            if section == "activity":
                collection = activity_group_page(snap, after=after, limit=block_size)
            elif section == "open_items":
                collection = open_group_page(snap, after=after, limit=block_size)["collection"]
            else:
                rows, paging = snap.month_journal.page(after or 0, block_size, include_lines=False)
                collection = {"items": vouchers_for_rows(snap, rows), "page": paging}
            return {"collections": {section: collection}}

        for section in ("activity", "open_items", "vouchers"):
            collect(
                "brief",
                section,
                {},
                lambda after, section=section: brief_collection(
                    section,
                    after,
                ),
            )

        for section in funds_data["collections"]:
            collect(
                "funds",
                section,
                fund_filters,
                lambda after, section=section: _funds(
                    snap,
                    sections={section},
                    cursors={section: after},
                    limit=block_size,
                    filters=dict(fund_filters),
                ),
            )
        all_fund_filters = {
            "movement_account_type": None,
            "movement_account_id": None,
            "statement_account_id": None,
        }
        all_funds_data = _funds(snap, limit=20, filters=dict(all_fund_filters))
        add_default(
            "funds",
            _native_frame(
                producer,
                snap,
                "funds",
                all_funds_data,
                filters=all_fund_filters,
            ),
            variant="all",
        )
        collect(
            "funds",
            "movements",
            all_fund_filters,
            lambda after: _funds(
                snap,
                sections={"movements"},
                cursors={"movements": after},
                limit=block_size,
                filters=dict(all_fund_filters),
            ),
            variant="all",
        )
        employee_filters = {"employee_filter": "employment_active", "employee_id": None}
        for section in defaults["employees"]["data"]["collections"]:
            collect(
                "employees",
                section,
                employee_filters,
                lambda after, section=section: _employees(
                    snap,
                    sections={section},
                    cursors={section: after},
                    limit=block_size,
                    employee_filter="employment_active",
                ),
            )
        all_employee_filters = {"employee_filter": "all", "employee_id": None}
        all_employees_data = _employees(snap, limit=20, employee_filter="all")
        add_default(
            "employees",
            _native_frame(
                producer,
                snap,
                "employees",
                all_employees_data,
                filters=all_employee_filters,
            ),
            variant="all",
        )
        collect(
            "employees",
            "employees",
            all_employee_filters,
            lambda after: _employees(
                snap,
                sections={"employees"},
                cursors={"employees": after},
                limit=block_size,
                employee_filter="all",
            ),
            variant="all",
        )
        asset_filters = {"asset_filter": "all", "asset_id": None, "project_id": None}
        for section in defaults["assets"]["data"]["collections"]:
            collect(
                "assets",
                section,
                asset_filters,
                lambda after, section=section: _assets(
                    snap,
                    sections={section},
                    cursors={section: after},
                    limit=block_size,
                ),
            )
        active_filters = {"asset_filter": "active", "asset_id": None, "project_id": None}
        active_data = _assets(snap, limit=20, asset_filter="active")
        add_default(
            "assets",
            _native_frame(
                producer,
                snap,
                "assets",
                active_data,
                filters=active_filters,
            ),
            variant="active",
        )
        for section in active_data["collections"]:
            collect(
                "assets",
                section,
                active_filters,
                lambda after, section=section: _assets(
                    snap,
                    sections={section},
                    cursors={section: after},
                    limit=block_size,
                    asset_filter="active",
                ),
                variant="active",
            )
        from .dashboard_model_group_details import materialize_group_blocks

        blocks.extend(materialize_group_blocks(producer, snap))
        return MaterializedModel(
            period,
            snap.as_of,
            snap.snapshot_version,
            tuple(blocks),
            tuple(indexes),
            date_basis=make_date_basis(
                snap.semantic_cache["model_employee_date_basis"],
                snap.as_of,
                snap.period,
            ),
        )
