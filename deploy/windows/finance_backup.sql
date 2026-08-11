\set ON_ERROR_STOP on

-- Run only on a dedicated local PostgreSQL cluster, while connected to the one
-- private-pilot database as its deployment owner. The explicit psql variable prevents
-- this script from silently changing CONNECT defaults on a shared cluster:
--   psql --set=finance_dedicated_local_cluster=on ... -f finance_backup.sql
-- This file intentionally does not accept, generate, print, or persist a password.
\if :{?finance_dedicated_local_cluster}
\else
\echo 'FINANCE_BACKUP_DEDICATED_LOCAL_CLUSTER_ACK_REQUIRED'
\quit
\endif
\if :finance_dedicated_local_cluster
\else
\echo 'FINANCE_BACKUP_DEDICATED_LOCAL_CLUSTER_ACK_REQUIRED'
\quit
\endif

BEGIN;

DO $role$
DECLARE
    attributes record;
    memberships text[];
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'finance_backup') THEN
        CREATE ROLE finance_backup
            LOGIN
            INHERIT
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            NOREPLICATION
            NOBYPASSRLS;
    ELSE
        SELECT rolcanlogin, rolinherit, rolsuper, rolcreatedb, rolcreaterole,
               rolreplication, rolbypassrls
          INTO attributes
          FROM pg_catalog.pg_roles
         WHERE rolname = 'finance_backup';
        IF attributes.rolcanlogin IS NOT TRUE
           OR attributes.rolinherit IS NOT TRUE
           OR attributes.rolsuper IS NOT FALSE
           OR attributes.rolcreatedb IS NOT FALSE
           OR attributes.rolcreaterole IS NOT FALSE
           OR attributes.rolreplication IS NOT FALSE
           OR attributes.rolbypassrls IS NOT FALSE THEN
            RAISE EXCEPTION 'FINANCE_BACKUP_ROLE_PRIVILEGES_INVALID';
        END IF;
        SELECT coalesce(array_agg(parent.rolname ORDER BY parent.rolname), ARRAY[]::text[])
          INTO memberships
          FROM pg_catalog.pg_auth_members AS membership
          JOIN pg_catalog.pg_roles AS member_role ON member_role.oid = membership.member
          JOIN pg_catalog.pg_roles AS parent ON parent.oid = membership.roleid
         WHERE member_role.rolname = 'finance_backup';
        IF NOT (memberships <@ ARRAY['pg_monitor', 'pg_read_all_data']::text[]) THEN
            RAISE EXCEPTION 'FINANCE_BACKUP_ROLE_MEMBERSHIP_INVALID';
        END IF;
    END IF;
END
$role$;

DO $grant$
BEGIN
    EXECUTE format(
        'GRANT CONNECT ON DATABASE %I TO finance_backup',
        current_database()
    );
END
$grant$;

-- PostgreSQL grants CONNECT on the maintenance databases to PUBLIC by default.
-- A dedicated finance cluster removes those defaults so finance_backup can connect
-- only to the current finance database. The acknowledgement above deliberately makes
-- this script unsuitable for an unreviewed shared-cluster run.
REVOKE CONNECT ON DATABASE postgres FROM PUBLIC;
REVOKE CONNECT ON DATABASE template1 FROM PUBLIC;
REVOKE CONNECT ON DATABASE postgres FROM finance_backup;
REVOKE CONNECT ON DATABASE template1 FROM finance_backup;

-- pg_read_all_data supplies read-only access to all present and future tables,
-- views, and sequences. pg_monitor is required to prove the runtime role has no
-- remaining sessions after the formal Windows service has stopped.
GRANT pg_read_all_data TO finance_backup;
GRANT pg_monitor TO finance_backup;

DO $verify$
DECLARE
    memberships text[];
    connect_databases text[];
BEGIN
    SELECT coalesce(array_agg(parent.rolname ORDER BY parent.rolname), ARRAY[]::text[])
      INTO memberships
      FROM pg_catalog.pg_auth_members AS membership
      JOIN pg_catalog.pg_roles AS member_role ON member_role.oid = membership.member
      JOIN pg_catalog.pg_roles AS parent ON parent.oid = membership.roleid
     WHERE member_role.rolname = 'finance_backup';
    IF memberships <> ARRAY['pg_monitor', 'pg_read_all_data']::text[] THEN
        RAISE EXCEPTION 'FINANCE_BACKUP_ROLE_MEMBERSHIP_INVALID';
    END IF;
    SELECT coalesce(array_agg(datname ORDER BY datname), ARRAY[]::text[])
      INTO connect_databases
      FROM pg_catalog.pg_database
     WHERE datallowconn
       AND has_database_privilege('finance_backup', oid, 'CONNECT');
    IF connect_databases <> ARRAY[current_database()]::text[] THEN
        RAISE EXCEPTION 'FINANCE_BACKUP_ROLE_CONNECT_PRIVILEGES_INVALID';
    END IF;
END
$verify$;

-- Keep explicit object-creation privileges off the dedicated backup identity.
REVOKE CREATE ON SCHEMA public FROM finance_backup;

COMMIT;
