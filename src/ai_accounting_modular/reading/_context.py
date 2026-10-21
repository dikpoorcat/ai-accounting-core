"""The selected company is the only business database opened by context reads."""

import calendar

from ai_accounting_modular.contracts.public import YearMonth, validate_contract


def period(value, closed_through=None):
    year, month = map(int, value.split("-"))
    return {
        "key": value,
        "year": year,
        "month": month,
        "label": f"{year}年{month}月",
        "short_label": f"{month}月",
        "status": "closed" if closed_through and value <= closed_through else "open",
        "start_date": f"{value}-01",
        "end_date": f"{value}-{calendar.monthrange(year, month)[1]:02d}",
        "closed_at": None,
    }


def last_period(reader, read):
    # A configured start month alone is not evidence of established books.
    latest = reader.ledger.latest_period(read)
    if latest is None:
        return None
    details = reader.businesses.context(
        {"company_id": read.company_id, "note_scopes": [], "issue_scopes": []}, read
    )
    return details["company_identity"].get("working_period_to", latest)


def context(reader, scope):
    validate_contract("core.schema.json", "ContextScope", scope)
    companies = reader.storage.list_companies()
    selected = scope.get("company_id")
    if selected is None and companies:
        selected = companies[0].company_id
    result = {
        "companies": [
            {
                "company_id": c.company_id,
                "name": c.legal_name,
                "taxpayer_id": c.credit_code,
                "status": "active",
            }
            for c in companies
        ],
        "current_company": None,
        "periods": [],
        "quarters": [],
        "default_period": None,
        "default_quarter": None,
    }
    if selected is None:
        return result
    with reader.storage.read_unit(selected) as read:
        metadata = reader.storage.company_metadata(read)
        current = {
            "company_id": read.company_id,
            "name": metadata.legal_name,
            "taxpayer_id": metadata.credit_code,
            "status": "active",
        }
        result["current_company"] = current
        last = last_period(reader, read)
        if last is None:
            return result
        closed = reader.ledger.closed_through(read)
        values = [
            str(YearMonth.from_ordinal(index))
            for index in range(YearMonth(read.start_period).ordinal, YearMonth(last).ordinal + 1)
        ]
        result["periods"] = [period(value, closed) for value in reversed(values)]
        quarters = sorted(
            {(int(value[:4]), (int(value[5:]) - 1) // 3 + 1) for value in values}, reverse=True
        )
        result["quarters"] = [
            {
                "key": f"{year}-Q{quarter}",
                "year": year,
                "quarter": quarter,
                "label": f"{year}年第{quarter}季度",
            }
            for year, quarter in quarters
        ]
        result["default_period"] = result["periods"][0]["key"]
        result["default_quarter"] = result["quarters"][0]["key"]
    return result
