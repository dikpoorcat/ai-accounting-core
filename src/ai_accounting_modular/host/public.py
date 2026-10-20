"""Assemble delivered modules exclusively through their public factories."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ai_accounting_modular.application import public as application
from ai_accounting_modular.dashboard import public as dashboard
from ai_accounting_modular.domains.assets import public as assets
from ai_accounting_modular.domains.financing import public as financing
from ai_accounting_modular.domains.operations import public as operations
from ai_accounting_modular.domains.people import public as people
from ai_accounting_modular.domains.taxes import public as taxes
from ai_accounting_modular.files import public as files
from ai_accounting_modular.identity import public as identity
from ai_accounting_modular.ledger import public as ledger
from ai_accounting_modular.ledger.opening import public as opening
from ai_accounting_modular.ledger.reporting import public as reporting
from ai_accounting_modular.money import public as money
from ai_accounting_modular.reading import public as reading
from ai_accounting_modular.records import public as records
from ai_accounting_modular.storage.public import Storage

__all__ = ["Kernel", "create_kernel"]


@dataclass(frozen=True)
class Kernel:
    storage: Storage
    application: application.Application
    identity: identity.Identity
    ledger: ledger.Ledger
    records: records.Records
    money: money.Money
    files: files.Files
    reading: reading.Reading
    dashboard: dashboard.Dashboard

    def close(self):
        self.files.close()


def create_kernel(
    catalog_path: Path | str, *, file_producers: Mapping[str, files.FileProducer] | None = None
) -> Kernel:
    owners = (
        application,
        identity,
        ledger,
        opening,
        reporting,
        records,
        operations,
        people,
        assets,
        taxes,
        financing,
        money,
        files,
        reading,
    )
    storage = Storage(catalog_path, repositories=[owner.repository_factory() for owner in owners])
    identities, bookkeeping = identity.create_identity(), ledger.create_ledger()
    materials = records.create_records(lambda read: storage.company_metadata(read).company_root)
    funds = money.create_money(ledger=bookkeeping, identity=identities)
    asset_accounting = assets.create_assets()
    domains = (
        operations.create_operations(),
        people.create_people(),
        asset_accounting,
        taxes.create_taxes(),
        financing.create_financing(),
        financing.create_investment(),
        funds,
        opening.create_opening(asset_accounting=asset_accounting),
        reporting.create_reporting(),
    )

    def mark_reading_targets(impact, unit):
        return read_models.mark_targets(impact, unit)

    app = application.create_application(
        storage=storage,
        identity=identities,
        ledger=bookkeeping,
        records=materials,
        domains=domains,
        money=funds,
        mark_reading_targets=mark_reading_targets,
    )
    read_models = reading.create_reading(
        storage=storage, ledger=bookkeeping, records=materials, businesses=app
    )
    return Kernel(
        storage,
        app,
        identities,
        bookkeeping,
        materials,
        funds,
        files.create_files(storage, {} if file_producers is None else file_producers),
        read_models,
        dashboard.create_dashboard(read_models),
    )
