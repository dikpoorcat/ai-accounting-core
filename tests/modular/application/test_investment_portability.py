"""Six sourced investment profiles survive final-document rebuild and verified restore."""

from copy import deepcopy
from pathlib import Path

from ai_accounting_modular.domains.financing.public import create_investment
from ai_accounting_modular.host.public import create_offline_application
from ai_accounting_modular.ledger.opening.public import (
    OpeningCooperationTradeReference,
    OpeningInvestmentReference,
    opening_obligation_references,
)
from ai_accounting_modular.records.public import open_standard_bundle
from tests.modular.ledger.opening._support import (
    PARTY,
    PERSON,
    SOURCE,
    member,
)
from tests.modular.reading.test_brief_activity import activity, capture, vouchers
from tests.modular.reading.test_brief_open import members, page, publish

from ._investment_portability_case import apply, holdings_view, movement, reconcile, seed, snapshot
from ._investment_portability_case import invested as invested
from .test_isolated_targets import read_target
from .test_isolated_targets import request as isolated_request


def test_new_investment_facts_rebuild_from_standard_documents_with_exact_book_results(
    invested, tmp_path
):
    case, inputs = invested, seed(invested)
    with case.kernel.storage.read_unit(case.company) as read:
        metadata = case.kernel.storage.company_metadata(read)
        expected_report = snapshot(case.app, case.company, read)
        expected_holdings = holdings_view(case.app, case.company, read)
        assert expected_report["profit_statement"]["20"]["current"]["amount_fen"] == "152"
        assert {
            row: expected_report["balance_sheet"][row]["ending"]["amount_fen"]
            for row in ("1", "2", "6", "7", "8", "14", "16", "17", "28", "30")
        } == {
            "1": "4130",
            "2": "1210",
            "6": "10",
            "7": "112",
            "8": "78",
            "14": "1200",
            "16": "1200",
            "17": "1200",
            "28": "1100",
            "30": "10240",
        }
        assert {
            row: expected_report["cash_flow_statement"][row]["current"]["amount_fen"]
            for row in ("8", "9", "11")
        } == {"8": "110", "9": "20", "11": "1000"}
        assert len(expected_holdings["products"]) == 5
        assert expected_holdings["cash"] == {
            "payments_fen": 1000,
            "receipts_fen": 130,
            "income_receipts_fen": 20,
        }
        assert expected_holdings["cooperation"][0]["company_capital_fen"] == 78
        opening_result = next(
            result
            for result in case.app.ledger.get_results([inputs["package"]["business_id"]], read)
            if result["reference"]["result_slot"] == "main"
        )
        (content,) = case.app.ledger.get_adopted_contents([opening_result["reference"]], read)
        references = opening_obligation_references(content)
        assert sum(isinstance(item, OpeningInvestmentReference) for item in references) == 2
        assert sum(isinstance(item, OpeningCooperationTradeReference) for item in references) == 2
    bundle = open_standard_bundle(metadata.company_root / "standard/current.json")
    assert {
        fact["data"]["product_type"] for fact in bundle.facts if fact["kind"] == "investment.terms"
    } == {"money_fund", "bank_wealth", "other_fund", "bond", "equity"}
    assert any(fact["kind"] == "investment.cooperation_contract" for fact in bundle.facts)
    offline, target = create_offline_application(), tmp_path / "rebuilt"
    created = offline.accounting_isolated(
        isolated_request("rebuild", "run", bundle.manifest_path, target)
    )
    assert created["state"] == "created", created
    assert created["company_id"] != case.company
    with read_target(offline, target) as read:
        assert snapshot(offline, read.company_id, read) == expected_report
        assert holdings_view(offline, read.company_id, read) == expected_holdings
        assert offline.records.document_status(read)["state"] == "ready"


def test_verified_zip_restore_keeps_closed_investment_history_after_correction(invested, tmp_path):
    case, inputs = invested, seed(invested)
    reconcile(case)
    with case.kernel.storage.read_unit(case.company) as read:
        frozen_report = snapshot(case.app, case.company, read, view="as_posted")
    checked = case.check("2026-07")
    assert checked["ready"], str(checked)
    assert case.close(checked)["state"] == "closed"
    with case.kernel.storage.read_unit(case.company) as read:
        frozen = case.app.ledger.get_results([inputs["incomes"][-1]["business_id"]], read)
    corrected = deepcopy(inputs["incomes"][-1])
    corrected["data"]["amount_fen"] = "14"
    apply(
        case,
        correction_period="2026-08",
        mutations=[{"op": "replace", "expected_seq": 1, "fact": corrected}],
    )
    with case.kernel.storage.read_unit(case.company) as read:
        assert snapshot(case.app, case.company, read, view="as_posted") == frozen_report
        refs = [item["reference"] for item in frozen]
        assert case.app.ledger.get_results([], read, result_refs=refs) == frozen
        expected = case.app.ledger.get_results(inputs["ids"], read)
        expected_report = snapshot(case.app, case.company, read, month="2026-08")
        metadata = case.kernel.storage.company_metadata(read)
        expected_head = read.head
    pending = case.app.accounting_backup(
        {"company_id": case.company, "expected_database_id": metadata.database_id}
    )
    done = case.kernel.files.run(case.company, pending["task_id"])
    assert done["state"] == "succeeded", done
    package = Path(done["path"])
    offline, target = create_offline_application(), tmp_path / "restored"
    with package.open("rb") as stream:
        manifest = offline.backup_verifier(stream)
        assert manifest["database_head"] == expected_head
    created = offline.accounting_isolated(isolated_request("restore", "run", package, target))
    assert created["state"] == "created", created
    assert created["company_id"] == case.company and created["database_id"] == metadata.database_id
    with read_target(offline, target) as read:
        assert offline.ledger.closed_through(read) == "2026-07"
        assert offline.ledger.get_results(inputs["ids"], read) == expected
        assert offline.ledger.get_results([], read, result_refs=refs) == frozen
        assert snapshot(offline, case.company, read, view="as_posted") == frozen_report
        assert snapshot(offline, case.company, read, month="2026-08") == expected_report


def test_opening_investment_and_project_claims_have_public_reading_labels(invested):
    case = invested
    inputs = seed(case, settle=False)
    snapshot_ = publish(case, "2026-07")
    groups = page(case, snapshot_)["collections"]["open_items"]["items"]
    assert all(group["description"] != "未知业务" for group in groups)
    with case.kernel.storage.read_unit(case.company) as read:
        formal = next(
            result
            for result in case.app.ledger.get_results([inputs["package"]["business_id"]], read)
            if result["reference"]["result_slot"] == "main"
        )
        (content,) = case.app.ledger.get_adopted_contents([formal["reference"]], read)
        assert len(opening_obligation_references(content)) == 4
    expected = {
        "期初投资应收分配": "other_receivables",
        "期初投资应收利息": "other_receivables",
        "期初合作经营应收款": "customer_receivables",
        "期初合作经营应付款": "supplier_payables",
    }
    assert (
        expected.items()
        <= {group["description"]: group["category_key"] for group in groups}.items()
    )
    for group in groups:
        items = members(case, snapshot_, group["group_key"])["collections"]["members"]["items"]
        assert all(item["description"] != "未知业务" for item in items)


def test_reading_authorized_payment_preserves_actual_agent_and_original_creditor(invested):
    case, inputs = invested, seed(invested, settle=False)
    terms = deepcopy(inputs["terms"][0])
    terms["data"]["settlement_authorizations"] = [
        {
            "entry_key": "real_agent",
            "principal_party_id": PARTY,
            "agent_id": PERSON,
            "purposes": ["acquisition_payment"],
            "valid_from": "2026-07",
            "valid_through": "2026-07",
            "source": {"source_id": SOURCE},
            "limit_fen": "200",
        }
    ]
    paid = movement(inputs["purchases"][0], "primary", "200", direction="outflow")
    paid["data"]["counterparty_id"] = PERSON
    paid["data"]["allocations"][0]["authorization"] = {
        "business_id": terms["business_id"],
        "slot": "authorization:real_agent",
    }
    apply(
        case,
        objects=[
            {
                "object_id": PERSON,
                "kind": "person",
                "name": "合成实际代理人",
                "expected_seq": 1,
                "data": {},
            }
        ],
        mutations=[
            {"op": "replace", "expected_seq": 1, "fact": terms},
            {"op": "create", "expected_seq": 0, "fact": paid},
        ],
    )
    capture(case, "2026-07")
    cash = [item for item in activity(case, "2026-07")["items"] if item["group"] == "cash"]
    assert len(cash) == 1 and cash[0]["party"] == "合成实际代理人"
    paid_voucher = next(
        item
        for item in vouchers(case, "2026-07")["items"]
        if item["subject_id"] == paid["business_id"]
    )
    assert {party["id"] for line in paid_voucher["lines"] for party in line["parties"]} == {PARTY}
    assert all(line["party_state"] == "known" for line in paid_voucher["lines"] if line["parties"])


def test_investment_receipts_include_only_typed_opening_income_and_exclude_ordinary_claims(
    invested,
):
    case, inputs = invested, seed(invested, settle=False)
    package = deepcopy(inputs["package"])
    ordinary = member(
        {
            "type": "obligation",
            "nature": "customer_receivable",
            "counterparty_id": PARTY,
            "amount_fen": "17",
            "original_business_reference": "普通期初客户款",
        }
    )
    package["data"]["members"].append(ordinary)
    for item in package["data"]["members"]:
        if item["data"]["type"] == "equity":
            item["data"]["balance_fen"] = "10105"
    received = [
        movement(package, "member:" + ordinary["member_id"], "17"),
        movement(package, "member:" + inputs["receivables"][0]["member_id"], "7"),
    ]
    apply(
        case,
        mutations=[
            {"op": "replace", "expected_seq": 1, "fact": package},
            *[{"op": "create", "expected_seq": 0, "fact": item} for item in received],
        ],
    )
    owner = create_investment(ledger=case.app.ledger, identity=case.app.identity)
    with case.kernel.storage.read_unit(case.company) as read:
        activity_ = owner.investment_activity(
            {
                "company_id": case.company,
                "period_from": "2026-07",
                "period_to": "2026-07",
                "view": "current",
            },
            read,
        )
        receipts = [event for event in activity_.events if event.kind == "income_receipt"]
        assert activity_.actual_receipts_fen == 7
        assert len(receipts) == 1 and receipts[0].terms_id == inputs["terms"][0]["business_id"]
