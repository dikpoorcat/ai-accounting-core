"""Independent owner identity for the single local accounting service."""

from .approval import consume_close_approval
from .primitives import IdentityError
from .schema import CATALOG_DDL, COMPANY_DDL, initialize_catalog, initialize_company
from .service import Authority, LoginResult, RecoveryResult, SecurityService, credential_target

__all__ = [
    "Authority",
    "CATALOG_DDL",
    "COMPANY_DDL",
    "IdentityError",
    "LoginResult",
    "RecoveryResult",
    "SecurityService",
    "consume_close_approval",
    "credential_target",
    "initialize_catalog",
    "initialize_company",
]
