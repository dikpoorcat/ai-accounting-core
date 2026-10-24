"""The new read uses the resident's existing browser-session boundary."""

import json

from .conftest import native_login
from .test_transport import browser, http


def test_company_note_get_requires_existing_owner_session_and_post_stays_unavailable(local):
    runtime, company, _ = local
    runtime.start()
    path = "/api/dashboard/company-note?company_id=" + company.company_id
    assert http(runtime, "GET", path)[0] == 401
    native_login(runtime)
    cookies = browser(runtime)
    status, raw, _ = http(runtime, "GET", path, cookies=cookies)
    assert status == 200
    assert json.loads(raw) == {
        "company": {
            "company_id": company.company_id,
            "database_id": company.database_id,
            "credit_code": company.credit_code,
            "name": company.legal_name,
        },
        "description": None,
    }
    assert http(runtime, "POST", path, {}, cookies=cookies)[0] == 404
