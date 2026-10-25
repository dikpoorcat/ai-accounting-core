"""A single bounded financial basis across independently paged review collections."""

from ai_accounting_modular.contracts.public import (
    ContractError,
    PageQuery,
    ReadPage,
    validate_contract,
)

from . import _review_scope, _review_wire


def run(application, request):
    validate_contract("core.schema.json", "ReviewRequest", request)
    if application.money is None:
        raise ContractError("unsupported_business", "当前实例尚未装配账务核查所需的资金模块。")
    scope = _review_scope.scope_of(request)
    with application.storage.read_unit(request["company_id"]) as read:
        captured = (
            _review_scope.capture_find(application, scope, read)
            if scope["action"] == "find"
            else _review_scope.capture_inspect(
                application,
                scope,
                read,
                resuming=bool(request.get("expected_version") or request.get("cursor")),
            )
        )
        version = captured.version(read)
        collection = request["collection"]
        query = PageQuery(request.get("limit", 20), _review_wire.continuation(request, version))
        selection = captured.selections.get(
            "statement_rows" if collection == "links" else collection
        )
        if selection is None and query.cursor is not None:
            raise ContractError("invalid_input", "该范围没有此集合的后续页面。")
        ports = {
            "obligations": application.ledger.review_obligation_page,
            "settlements": application.ledger.review_settlement_page,
            "movements": application.money.review_movement_page,
            "statement_rows": application.money.review_row_page,
            "links": application.money.review_row_link_page,
        }
        page = (
            ports[collection](selection, query, read) if selection else ReadPage(version, 0, 0, ())
        )
        items = [
            _review_wire.wire(
                _review_wire.item(collection, value, captured.relationship(collection, value))
            )
            for value in page.items
        ]
        objects, sources = _review_wire.metadata(application, items, read)
        return validate_contract(
            "core.schema.json",
            "ReviewResult",
            {
                "company_id": read.company_id,
                "database_id": read.database_id,
                "scope": scope,
                "basis_version": version,
                "collection": collection,
                "items": items,
                "objects": objects,
                "sources": sources,
                "page": {
                    "total_count": page.total_count,
                    "filtered_count": page.filtered_count,
                    "returned_count": len(items),
                    "has_more": page.next_cursor is not None,
                    "next_cursor": _review_wire.cursor(version, collection, page.next_cursor),
                },
            },
        )
