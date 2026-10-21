from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import NAMESPACE_DNS, uuid4, uuid5

from ai_accounting_modular.application import public as application
from ai_accounting_modular.dashboard.public import create_dashboard
from ai_accounting_modular.domains.operations import public as operations
from ai_accounting_modular.domains.taxes import public as taxes
from ai_accounting_modular.identity import public as identity
from ai_accounting_modular.ledger import public as ledger
from ai_accounting_modular.ledger.opening import public as opening
from ai_accounting_modular.ledger.reporting import public as reporting
from ai_accounting_modular.money import public as money
from ai_accounting_modular.reading import public as reading
from ai_accounting_modular.records import public as records
from ai_accounting_modular.storage.public import CompanySpec, OwnerProfile, Storage, create_catalog


def uid(value):
    return str(uuid5(NAMESPACE_DNS, "modular-reading/" + value))


SOURCE, PARTY, BANK = (uid(value) for value in ("source", "party", "bank"))
NOW = datetime(2026, 10, 21, 12, 30, tzinfo=timezone(timedelta(hours=8)))


def source(name, text=None):
    text = text or f"合成资料{name}"
    return {
        "source_id": uid(name),
        "kind": "owner_statement",
        "text": text,
        "sha256": sha256(text.encode()).hexdigest(),
        "archive_month": "2026-01",
    }


def fact(name, kind, data, month="2026-01", on=None, sources=(SOURCE,)):
    value = {
        "business_id": uid(name),
        "kind": kind,
        "period": month,
        "evidence_ids": list(sources),
        "data": data,
    }
    if on:
        value["effective_on"] = on
    return value


def expense(name="expense", amount="10000", month="2026-01", sources=(SOURCE,)):
    return fact(
        name,
        "operations.expense",
        {
            "creditor_id": PARTY,
            "creditor_kind": "supplier",
            "amount_fen": amount,
            "expense_class": "administration",
            "report_details": [{"code": "management_other", "amount_fen": amount}],
        },
        month,
        sources=sources,
    )


def payment(expense_fact, amount="6000", name="payment", month="2026-01"):
    return fact(
        name,
        "money.movement",
        {
            "type": "settlement",
            "funds_account_id": BANK,
            "direction": "outflow",
            "amount_fen": amount,
            "counterparty_id": PARTY,
            "payment_method": "individual",
            "allocations": [
                {
                    "source": {"business_id": expense_fact["business_id"], "slot": "primary"},
                    "amount_fen": amount,
                }
            ],
        },
        month,
        on=month + "-15",
    )


def scope(company, quarter=1):
    return {
        "company_id": company,
        "period_from": f"2026-{quarter * 3 - 2:02d}",
        "period_to": f"2026-{quarter * 3:02d}",
        "view": "as_posted",
    }


@dataclass
class Book:
    storage: object
    app: object
    identity: object
    ledger: object
    records: object
    reading: object
    dashboard: object
    company: str

    def submit(self, *facts, mutations=(), **extra):
        result = self.app.accounting_apply(
            {
                "contract": "ai-accounting-modular/1",
                "company_id": self.company,
                "command_id": str(uuid4()),
                "mutations": [{"op": "create", "expected_seq": 0, "fact": value} for value in facts]
                + list(mutations),
                **extra,
            }
        )
        assert result.get("completion") == "complete", result
        return result

    def get(self, endpoint, **query):
        with self.dashboard.get(
            endpoint, [(name, str(value)) for name, value in query.items()]
        ) as result:
            return result.status, result.body

    def preview(self, quarter=1):
        return self.get(
            "/api/dashboard/quarterly-report", company_id=self.company, year=2026, quarter=quarter
        )

    def mark(self, month):
        from ai_accounting_modular.contracts.public import PageQuery
        from ai_accounting_modular.ledger.public import FormalChange

        with self.storage.write_unit(self.company) as unit:
            # Explicit rescheduling uses actual formal identities, not the party's ID.
            changes, cursor = [], None
            selected = {
                "company_id": self.company,
                "period_from": month,
                "period_to": month,
                "view": "as_posted",
                "as_of": month,
            }
            while True:
                page = self.ledger.query_adopted_directory(
                    selected, (), PageQuery(500, cursor), unit
                )
                for entry in page.items:
                    ref = entry.reference
                    if ref["posting_period"] == month:
                        changes.append(
                            FormalChange(
                                ref["business_id"],
                                ref["result_slot"],
                                (ref["business_period"],),
                                (ref["posting_period"],),
                                (entry.business_kind,),
                            )
                        )
                if page.next_cursor is None:
                    break
                cursor = page.next_cursor
            # A genuine business write makes next_head the committed sequence.
            obj = self.identity.get_batch([PARTY], unit)[0]
            data = {key: obj[key] for key in ("object_id", "kind", "name", "data")}
            data.update(name=obj["name"] + "新", expected_seq=obj["seq"])
            plan = self.identity.plan_change([data], identity.IdentityContext(unit, str(uuid4())))
            self.identity.apply_current(plan, unit)
            return self.reading.mark_targets(
                reading.ReadingImpact(formal_changes=tuple(changes)),
                unit,
            )


def assemble(root, *, established=True):
    catalog = root / "catalog.sqlite3"
    create_catalog(catalog, OwnerProfile(str(uuid4()), "合成负责人", "synthetic-password"))
    owners = (
        application,
        identity,
        ledger,
        opening,
        reporting,
        operations,
        taxes,
        money,
        records,
        reading,
    )
    storage = Storage(catalog, repositories=[owner.repository_factory() for owner in owners])
    company = str(uuid4())
    storage.create_company(
        CompanySpec(
            company,
            str(uuid4()),
            "913100000000000001",
            "合成读取验收公司",
            "2026-01",
            root / "company",
        )
    )
    identities, bookkeeping = identity.create_identity(), ledger.create_ledger()
    materials = records.create_records(lambda read: storage.company_metadata(read).company_root)
    funds = money.create_money(identity=identities, ledger=bookkeeping)

    def mark_reading_targets(impact, unit):
        return reads.mark_targets(impact, unit)

    app = application.create_application(
        storage=storage,
        identity=identities,
        ledger=bookkeeping,
        records=materials,
        money=funds,
        mark_reading_targets=mark_reading_targets,
        domains=[
            opening.create_opening(),
            reporting.create_reporting(),
            operations.create_operations(),
            taxes.create_taxes(),
            funds,
        ],
    )
    reads = reading.create_reading(
        storage=storage, ledger=bookkeeping, records=materials, businesses=app
    )
    book = Book(
        storage,
        app,
        identities,
        bookkeeping,
        materials,
        reads,
        create_dashboard(reads, now=lambda: NOW),
        company,
    )
    if established:
        book.submit(
            sources=[source("source")],
            objects=[
                {
                    "object_id": PARTY,
                    "kind": "organization",
                    "name": "合成交易方",
                    "expected_seq": 0,
                    "data": {},
                },
                {
                    "object_id": BANK,
                    "kind": "bank_account",
                    "name": "合成公司银行",
                    "expected_seq": 0,
                    "data": {
                        "bank_name": "合成银行",
                        "account_number": "test-1000",
                        "currency": "CNY",
                        "owner": "company",
                        "opened_period": "2026-01",
                        "zero_at_opening": True,
                    },
                },
            ],
        )
        book.submit(
            fact("opening", "opening.package", {"basis": "newly_established", "members": []})
        )
    return book
