"""Additive security DDL owned by the catalogue/company migration runner.

No constructor creates tables. Each statement executes inside the caller's
transaction; executescript is deliberately not used because it can commit it.
"""

CATALOG_DDL = (
    """CREATE TABLE security_owner(
        id TEXT PRIMARY KEY, singleton INTEGER NOT NULL UNIQUE CHECK(singleton=1),
        login_name TEXT NOT NULL, login_key TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL CHECK(status IN ('active','disabled')),
        password_hash TEXT NOT NULL CHECK(length(password_hash)=97
          AND password_hash LIKE '$argon2id$v=19$m=65536,t=3,p=4$%'),
        credential_version INTEGER NOT NULL CHECK(credential_version>=1),
        password_failures INTEGER NOT NULL CHECK(password_failures>=0),
        password_blocked_until INTEGER,
        recovery_failures INTEGER NOT NULL CHECK(recovery_failures>=0),
        recovery_blocked_until INTEGER,
        created_at INTEGER NOT NULL, password_changed_at INTEGER NOT NULL,
        last_authenticated_at INTEGER,
        CHECK(login_key=lower(trim(login_name))),
        CHECK(length(login_name) BETWEEN 3 AND 100
          AND login_name=trim(login_name) AND login_name NOT GLOB '*[^A-Za-z0-9._-]*'
          AND substr(login_name,1,1) GLOB '[A-Za-z0-9]')
    ) STRICT""",
    """CREATE TABLE security_session(
        id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES security_owner(id),
        secret_hash BLOB NOT NULL UNIQUE CHECK(length(secret_hash)=32),
        credential_version INTEGER NOT NULL CHECK(credential_version>=1),
        created_at INTEGER NOT NULL, last_seen_at INTEGER NOT NULL,
        idle_expires_at INTEGER NOT NULL, absolute_expires_at INTEGER NOT NULL,
        revoked_at INTEGER, revoke_reason TEXT,
        CHECK(last_seen_at>=created_at),
        CHECK(idle_expires_at>created_at AND idle_expires_at<=absolute_expires_at),
        CHECK((revoked_at IS NULL AND revoke_reason IS NULL)
          OR (revoked_at>=created_at AND revoke_reason IS NOT NULL))
    ) STRICT""",
    "CREATE INDEX security_session_owner ON security_session(owner_id)",
    """CREATE TABLE security_recovery(
        id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES security_owner(id),
        code_hash BLOB NOT NULL UNIQUE CHECK(length(code_hash)=32),
        credential_version INTEGER NOT NULL CHECK(credential_version>=1),
        created_at INTEGER NOT NULL, used_at INTEGER, invalidated_at INTEGER,
        CHECK(used_at IS NULL OR used_at>=created_at),
        CHECK(invalidated_at IS NULL OR invalidated_at>=created_at),
        CHECK(used_at IS NULL OR invalidated_at IS NULL)
    ) STRICT""",
    """CREATE UNIQUE INDEX security_recovery_current ON security_recovery(owner_id)
        WHERE used_at IS NULL AND invalidated_at IS NULL""",
    """CREATE TABLE security_audit(
        id INTEGER PRIMARY KEY, occurred_at INTEGER NOT NULL,
        owner_id TEXT, session_id TEXT, event TEXT NOT NULL,
        outcome TEXT NOT NULL CHECK(outcome IN ('succeeded','rejected','blocked')),
        reason TEXT, request_id TEXT NOT NULL
    ) STRICT""",
    """CREATE TRIGGER security_audit_no_update BEFORE UPDATE ON security_audit
        BEGIN SELECT RAISE(ABORT,'immutable security audit'); END""",
    """CREATE TRIGGER security_audit_no_delete BEFORE DELETE ON security_audit
        BEGIN SELECT RAISE(ABORT,'immutable security audit'); END""",
    """CREATE TABLE security_identity_import(
        source_catalog_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL UNIQUE
          REFERENCES security_owner(id), imported_at INTEGER NOT NULL
    ) STRICT""",
    """CREATE TRIGGER security_import_no_update BEFORE UPDATE ON security_identity_import
        BEGIN SELECT RAISE(ABORT,'immutable identity import'); END""",
    """CREATE TRIGGER security_import_no_delete BEFORE DELETE ON security_identity_import
        BEGIN SELECT RAISE(ABORT,'immutable identity import'); END""",
)

COMPANY_DDL = (
    """CREATE TABLE security_close_approval(
        id TEXT PRIMARY KEY, catalog_instance_id TEXT NOT NULL,
        company_id TEXT NOT NULL, database_id TEXT NOT NULL, period INTEGER NOT NULL,
        preview_digest BLOB NOT NULL CHECK(length(preview_digest)=32),
        accounting_epoch INTEGER NOT NULL CHECK(accounting_epoch>=0),
        material_epoch INTEGER NOT NULL CHECK(material_epoch>=0),
        management_epoch INTEGER NOT NULL CHECK(management_epoch>=0),
        owner_id TEXT NOT NULL, session_id TEXT NOT NULL,
        credential_version INTEGER NOT NULL CHECK(credential_version>=1),
        confirmed_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
        consumed_at INTEGER,
        CHECK(expires_at>confirmed_at),
        CHECK(consumed_at IS NULL OR consumed_at>=confirmed_at)
    ) STRICT""",
    """CREATE TRIGGER security_approval_sealed BEFORE UPDATE ON security_close_approval
        WHEN OLD.consumed_at IS NOT NULL OR NEW.consumed_at IS NULL
          OR NEW.id IS NOT OLD.id OR NEW.catalog_instance_id IS NOT OLD.catalog_instance_id
          OR NEW.company_id IS NOT OLD.company_id OR NEW.database_id IS NOT OLD.database_id
          OR NEW.period IS NOT OLD.period OR NEW.preview_digest IS NOT OLD.preview_digest
          OR NEW.accounting_epoch IS NOT OLD.accounting_epoch
          OR NEW.material_epoch IS NOT OLD.material_epoch
          OR NEW.management_epoch IS NOT OLD.management_epoch
          OR NEW.owner_id IS NOT OLD.owner_id OR NEW.session_id IS NOT OLD.session_id
          OR NEW.credential_version IS NOT OLD.credential_version
          OR NEW.confirmed_at IS NOT OLD.confirmed_at OR NEW.expires_at IS NOT OLD.expires_at
        BEGIN SELECT RAISE(ABORT,'sealed close approval'); END""",
    """CREATE TRIGGER security_approval_no_delete BEFORE DELETE ON security_close_approval
        BEGIN SELECT RAISE(ABORT,'immutable close approval'); END""",
)


# Additive catalogue v3 / company v4. Earlier released contracts remain intact.
CATALOG_DDL += (
    """CREATE TABLE security_close_batch(
        id TEXT PRIMARY KEY, catalog_instance_id TEXT NOT NULL,
        owner_id TEXT NOT NULL REFERENCES security_owner(id),
        session_id TEXT NOT NULL REFERENCES security_session(id),
        credential_version INTEGER NOT NULL CHECK(credential_version>=1),
        targets TEXT NOT NULL CHECK(json_valid(targets)),
        digest BLOB NOT NULL CHECK(length(digest)=32),
        confirmed_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
        CHECK(expires_at>confirmed_at)
    ) STRICT""",
    """CREATE TRIGGER security_close_batch_no_update BEFORE UPDATE ON security_close_batch
        BEGIN SELECT RAISE(ABORT,'immutable close batch'); END""",
    """CREATE TRIGGER security_close_batch_no_delete BEFORE DELETE ON security_close_batch
        BEGIN SELECT RAISE(ABORT,'retained close batch'); END""",
)
COMPANY_DDL += (
    """CREATE TABLE security_close_batch_receipt(
        batch_id TEXT PRIMARY KEY, catalog_instance_id TEXT NOT NULL,
        company_id TEXT NOT NULL, database_id TEXT NOT NULL,
        from_period INTEGER NOT NULL CHECK(from_period BETWEEN 0 AND 119987),
        through_period INTEGER NOT NULL CHECK(through_period BETWEEN from_period AND 119987),
        preview_digest BLOB NOT NULL CHECK(length(preview_digest)=32),
        target_digest BLOB NOT NULL CHECK(length(target_digest)=32),
        consumed_at INTEGER NOT NULL
    ) STRICT""",
    """CREATE TRIGGER security_close_batch_receipt_no_update
        BEFORE UPDATE ON security_close_batch_receipt
        BEGIN SELECT RAISE(ABORT,'immutable close batch receipt'); END""",
    """CREATE TRIGGER security_close_batch_receipt_no_delete
        BEFORE DELETE ON security_close_batch_receipt
        BEGIN SELECT RAISE(ABORT,'retained close batch receipt'); END""",
)


def _initialize(connection, statements):
    if not connection.in_transaction:
        raise RuntimeError("Security DDL requires the caller's migration transaction")
    for statement in statements:
        connection.execute(statement)


def initialize_catalog(connection):
    _initialize(connection, CATALOG_DDL)


def initialize_company(connection):
    _initialize(connection, COMPANY_DDL)
