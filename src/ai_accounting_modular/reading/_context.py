"""The selected company is the only business database opened by context reads."""

import calendar

from ai_accounting_modular.contracts.public import ContractError, YearMonth, validate_contract


def company_note(reader, scope, query):
    """Current company prose is independent of accounting periods and projections."""
    validate_contract("company-note.schema.json", "CompanyNoteQuery", scope)
    if query != {}:
        raise ContractError("invalid_input", "公司说明不接受月份或其他筛选。")
    with reader.storage.read_unit(scope["company_id"]) as read:
        metadata = reader.storage.company_metadata(read)
        notes = reader.records.current_notes(("company",), read)
        description = None
        if notes:
            note = notes[0]
            # Records validates its stored format and digest.
            # Do not turn a damaged description into an honest-looking empty one.
            if note["state"] != "current" or not isinstance(note["text"], str):
                raise ContractError("storage_corrupt", "当前公司说明的正文状态不正确。")
            if note["text"].strip():
                description = {
                    "text_format": note.get("text_format", "plain_text"),
                    "text": note["text"],
                }
        return {
            "company": {
                "company_id": read.company_id,
                "database_id": read.database_id,
                "credit_code": metadata.credit_code,
                "name": metadata.legal_name,
            },
            "description": description,
        }


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
    scope = reader.businesses.work_scope(read)
    return scope.working_period_to if scope.has_formal_results else None


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
        work = reader.businesses.work_scope(read)
        if not work.has_formal_results:
            return result
        last, closed = work.working_period_to, work.closed_through
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
