"""Open verified export bytes only when the host can deliver them."""

from contextlib import AbstractContextManager

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.files.public import VerifiedDownload


def save(application, source: AbstractContextManager[VerifiedDownload]):
    if application.save_export is None:
        raise ContractError("unsupported", "当前实例未装配本机桌面交付。")
    with source as download:
        return {
            **application.save_export(download),
            "company_id": download.company_id,
            "database_id": download.database_id,
            "task_id": download.task_id,
        }
