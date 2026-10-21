"""Observe complete public HTTP projections, outside any timed measurement."""

from ai_accounting_modular.contracts.public import canonical_json, validate_contract

from .._support import http, ready

PAGES = ("brief", "funds", "employees", "assets", "quarterly-report")


def parameters(company, month, page):
    if page == "quarterly-report":
        return {
            "company_id": company,
            "year": int(month[:4]),
            "quarter": (int(month[5:]) - 1) // 3 + 1,
        }
    return {"company_id": company, "period": month}


def checked(kernel, page, query):
    result = http(kernel, page, query)
    assert result["http_status"] == 200, result
    body = result["body"]
    body = body.get("response", body)
    validate_contract("dashboard-response-v2.json", "dashboard_" + page.replace("-", "_"), body)
    return body


def collection(kernel, route, query, name):
    """First 20, then 500; assert the complete filtered scope and every response Schema."""
    pages, responses, items, cursors = [], [], [], set()
    query = dict(query)
    while True:
        body = checked(kernel, route, query)
        part = body["data"]["collections"][name]
        page = part["page"]
        assert page["returned_count"] == len(part["items"])
        assert len(part["items"]) <= query.get("limit", 20)
        if pages:
            assert page["collection_version"] == pages[0]["page"]["collection_version"]
            assert page["filtered_count"] == pages[0]["page"]["filtered_count"]
            assert page["total_count"] == pages[0]["page"]["total_count"]
        pages.append(part)
        responses.append(body)
        items.extend(part["items"])
        if not page["has_more"]:
            assert page["next_cursor"] is None
            assert len(items) == page["filtered_count"]
            assert len({canonical_json(item) for item in items}) == len(items)
            return {"pages": pages, "responses": responses, "items": items}
        cursor = page["next_cursor"]
        assert cursor and cursor not in cursors
        cursors.add(cursor)
        query.update(cursor=cursor, limit=500)


def capture(kernel, company, month):
    """Defaults, full collections, actual groups, exact vouchers and business progress."""
    observed = {
        "context": checked(kernel, "context", {"company_id": company}),
        "defaults": {},
        "collections": {},
        "details": {},
        "filters": {},
    }
    for route in PAGES:
        query = parameters(company, month, route)
        ready(kernel, route, query)
        default = checked(kernel, route, query)
        observed["defaults"][route] = default
        if route == "quarterly-report":
            continue
        version = default["snapshot_version"]
        names = list(default["data"]["collections"])
        if route == "brief":
            names.append("vouchers")
        for name in names:
            scope = query | {"section": name, "expected_version": version}
            observed["collections"][route + "/" + name] = collection(kernel, route, scope, name)
        if route in ("employees", "assets"):
            field = "employee_filter" if route == "employees" else "asset_filter"
            observed["filters"][route + "/all"] = checked(kernel, route, query | {field: "all"})
        if route == "funds":
            for account in observed["collections"]["funds/accounts"]["items"]:
                selected = query | {
                    "movement_account_type": account["type"],
                    "movement_account_id": account["account_id"],
                    "section": "movements",
                    "expected_version": version,
                }
                observed["filters"]["funds/" + account["account_id"]] = collection(
                    kernel, route, selected, "movements"
                )
                if account["type"] == "bank":
                    observed["filters"]["statement/" + account["account_id"]] = collection(
                        kernel,
                        route,
                        query
                        | {
                            "section": "statements",
                            "statement_account_id": account["account_id"],
                            "expected_version": version,
                        },
                        "statements",
                    )
            observed["filters"]["funds/first"] = checked(
                kernel, route, query | {"movement_account_selection": "first"}
            )
        elif route == "employees":
            for section in ("employees", "labor_workers"):
                selected = query | {
                    "section": section,
                    "employee_filter": "all",
                    "expected_version": version,
                }
                people = collection(kernel, route, selected, section)
                observed["filters"]["employees/" + section] = people
                for person in people["items"]:
                    person_id = person["employee_id" if section == "employees" else "person_id"]
                    observed["filters"]["person/" + person_id] = checked(
                        kernel, route, query | {"employee_id": person_id}
                    )
                    if section == "labor_workers":
                        observed["filters"]["labor/" + person_id] = collection(
                            kernel,
                            route,
                            query
                            | {
                                "employee_id": person_id,
                                "section": "labor_sources",
                                "expected_version": version,
                            },
                            "labor_sources",
                        )
        elif route == "assets":
            for item in observed["collections"]["assets/assets"]["items"]:
                observed["filters"]["asset/" + item["asset_id"]] = checked(
                    kernel, route, query | {"asset_id": item["asset_id"]}
                )
    brief = observed["defaults"]["brief"]
    base = parameters(company, month, "brief") | {"expected_version": brief["snapshot_version"]}
    for section in ("activity", "open_items"):
        for group in observed["collections"]["brief/" + section]["items"]:
            scope = base | {"section": section, "group_key": group["group_key"]}
            detail = collection(kernel, "brief-group", scope, "members")
            observed["details"][section + "/" + group["group_key"]] = detail
            # Responses retain the complete page-local vouchers alongside their
            # members, independently of the voucher directory's own cursor.
            for member in detail["items"]:
                if member.get("subject_id"):
                    query = parameters(company, month, "brief") | {
                        "subject_id": member["subject_id"]
                    }
                    if (
                        member.get("detail_scope_key") is not None
                        and member.get("voucher_version_id") is not None
                    ):
                        query.update(
                            detail_scope_key=member["detail_scope_key"],
                            voucher_version_id=member["voucher_version_id"],
                        )
                    progress = checked(kernel, "business-status", query)
                    progress_query = query | {
                        "section": "settlement_events",
                        "expected_version": progress["snapshot_version"],
                    }
                    observed["details"]["progress/" + member.get("key", member.get("id", ""))] = {
                        "body": progress,
                        "events": collection(
                            kernel, "business-status", progress_query, "settlement_events"
                        ),
                    }
    for voucher in observed["collections"]["brief/vouchers"]["items"]:
        token = voucher["voucher_version_id"]
        observed["details"]["voucher/" + token] = checked(
            kernel, "brief", base | {"voucher_version_id": token}
        )
        observed["details"]["number/" + str(voucher["number"])] = checked(
            kernel, "brief", base | {"voucher_number": voucher["number"]}
        )
    return observed
