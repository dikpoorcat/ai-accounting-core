--
-- PostgreSQL database dump
--


-- Dumped from database version 17.10
-- Dumped by pg_dump version 17.10

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: finance_catalog_identity_audit_append_only(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_catalog_identity_audit_append_only() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    RAISE EXCEPTION 'IDENTITY_AUDIT_APPEND_ONLY';
END;
$$;


--
-- Name: finance_catalog_owner_guard(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_catalog_owner_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'IDENTITY_OWNER_DELETE_FORBIDDEN';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'active' OR NEW.credential_version <> 1
           OR NEW.password_failed_attempts <> 0 OR NEW.recovery_failed_attempts <> 0
           OR NEW.password_throttled_until IS NOT NULL
           OR NEW.recovery_throttled_until IS NOT NULL THEN
            RAISE EXCEPTION 'IDENTITY_OWNER_INITIAL_STATE_INVALID';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id OR NEW.org_id IS DISTINCT FROM OLD.org_id
       OR NEW.singleton_key IS DISTINCT FROM OLD.singleton_key
       OR NEW.login_name IS DISTINCT FROM OLD.login_name
       OR NEW.login_name_normalized IS DISTINCT FROM OLD.login_name_normalized
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'IDENTITY_OWNER_IMMUTABLE_FIELD';
    END IF;
    IF OLD.status = 'disabled' AND NEW.status IS DISTINCT FROM OLD.status THEN
        RAISE EXCEPTION 'IDENTITY_OWNER_REACTIVATION_FORBIDDEN';
    END IF;
    IF NEW.password_hash IS DISTINCT FROM OLD.password_hash THEN
        IF NEW.credential_version <> OLD.credential_version + 1
           OR NEW.password_changed_at <= OLD.password_changed_at THEN
            RAISE EXCEPTION 'IDENTITY_CREDENTIAL_ROTATION_INVALID';
        END IF;
    ELSIF NEW.credential_version IS DISTINCT FROM OLD.credential_version
          OR NEW.password_changed_at IS DISTINCT FROM OLD.password_changed_at THEN
        RAISE EXCEPTION 'IDENTITY_CREDENTIAL_ROTATION_INVALID';
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: finance_catalog_recovery_guard(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_catalog_recovery_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'IDENTITY_RECOVERY_HISTORY_DELETE_FORBIDDEN';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.used_at IS NOT NULL OR NEW.invalidated_at IS NOT NULL
           OR NOT EXISTS (
                SELECT 1 FROM owner_accounts owner
                 WHERE owner.id = NEW.owner_account_id AND owner.org_id = NEW.org_id
                   AND owner.status = 'active'
                   AND owner.credential_version = NEW.credential_version) THEN
            RAISE EXCEPTION 'IDENTITY_RECOVERY_INITIAL_STATE_INVALID';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id OR NEW.org_id IS DISTINCT FROM OLD.org_id
       OR NEW.owner_account_id IS DISTINCT FROM OLD.owner_account_id
       OR NEW.code_sha256 IS DISTINCT FROM OLD.code_sha256
       OR NEW.credential_version IS DISTINCT FROM OLD.credential_version
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'IDENTITY_RECOVERY_IMMUTABLE_FIELD';
    END IF;
    IF (OLD.used_at IS NOT NULL AND NEW.used_at IS DISTINCT FROM OLD.used_at)
       OR (OLD.invalidated_at IS NOT NULL
           AND NEW.invalidated_at IS DISTINCT FROM OLD.invalidated_at) THEN
        RAISE EXCEPTION 'IDENTITY_RECOVERY_TERMINAL_STATE_IMMUTABLE';
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: finance_catalog_session_guard(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.finance_catalog_session_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'IDENTITY_SESSION_HISTORY_DELETE_FORBIDDEN';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.revoked_at IS NOT NULL OR NEW.revoke_reason IS NOT NULL
           OR NOT EXISTS (
                SELECT 1 FROM owner_accounts owner
                 WHERE owner.id = NEW.owner_account_id AND owner.org_id = NEW.org_id
                   AND owner.status = 'active'
                   AND owner.credential_version = NEW.credential_version) THEN
            RAISE EXCEPTION 'IDENTITY_SESSION_INITIAL_STATE_INVALID';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id OR NEW.org_id IS DISTINCT FROM OLD.org_id
       OR NEW.owner_account_id IS DISTINCT FROM OLD.owner_account_id
       OR NEW.secret_sha256 IS DISTINCT FROM OLD.secret_sha256
       OR NEW.credential_version IS DISTINCT FROM OLD.credential_version
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.absolute_expires_at IS DISTINCT FROM OLD.absolute_expires_at THEN
        RAISE EXCEPTION 'IDENTITY_SESSION_IMMUTABLE_FIELD';
    END IF;
    IF OLD.revoked_at IS NOT NULL
       AND (NEW.revoked_at IS DISTINCT FROM OLD.revoked_at
            OR NEW.revoke_reason IS DISTINCT FROM OLD.revoke_reason) THEN
        RAISE EXCEPTION 'IDENTITY_SESSION_TERMINAL_STATE_IMMUTABLE';
    END IF;
    RETURN NEW;
END;
$$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: accounting_period_close_backups; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.accounting_period_close_backups (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    close_id uuid NOT NULL,
    period_id uuid NOT NULL,
    period_month character varying(7) NOT NULL,
    database_identity uuid NOT NULL,
    location_version_id uuid NOT NULL,
    status character varying(20) NOT NULL,
    attempt_count integer NOT NULL,
    archive_file text,
    archive_sha256 character varying(64),
    manifest_sha256 character varying(64),
    error_code character varying(100),
    requested_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    completed_at timestamp with time zone,
    CONSTRAINT ck_close_backup_attempt_count CHECK ((attempt_count >= 0)),
    CONSTRAINT ck_close_backup_completion CHECK (((((status)::text = 'completed'::text) AND (archive_file IS NOT NULL) AND (archive_sha256 IS NOT NULL) AND (manifest_sha256 IS NOT NULL) AND (error_code IS NULL) AND (completed_at IS NOT NULL)) OR (((status)::text <> 'completed'::text) AND (archive_file IS NULL) AND (archive_sha256 IS NULL) AND (manifest_sha256 IS NULL)))),
    CONSTRAINT ck_close_backup_period_month CHECK (((length((period_month)::text) = 7) AND (substr((period_month)::text, 5, 1) = '-'::text))),
    CONSTRAINT ck_close_backup_status CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'running'::character varying, 'completed'::character varying, 'failed'::character varying])::text[])))
);


--
-- Name: catalog_metadata; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.catalog_metadata (
    singleton_key integer NOT NULL,
    catalog_instance_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_catalog_metadata_singleton CHECK ((singleton_key = 1))
);


--
-- Name: catalog_metadata_singleton_key_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.catalog_metadata_singleton_key_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: catalog_metadata_singleton_key_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.catalog_metadata_singleton_key_seq OWNED BY public.catalog_metadata.singleton_key;


--
-- Name: close_backup_location_versions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.close_backup_location_versions (
    id uuid NOT NULL,
    version integer NOT NULL,
    backup_directory text NOT NULL,
    idempotency_key character varying(200) NOT NULL,
    request_payload_hash character varying(64) NOT NULL,
    confirmation_note text NOT NULL,
    owner_account_id uuid NOT NULL,
    owner_session_id uuid NOT NULL,
    owner_credential_version integer NOT NULL,
    executor_kind character varying(30) NOT NULL,
    executor_name character varying(100) NOT NULL,
    executor_version character varying(100) NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    org_id uuid,
    CONSTRAINT ck_close_backup_location_credential_version CHECK ((owner_credential_version >= 1)),
    CONSTRAINT ck_close_backup_location_request_hash CHECK ((length((request_payload_hash)::text) = 64)),
    CONSTRAINT ck_close_backup_location_version CHECK ((version >= 1))
);


--
-- Name: company_lifecycle_actions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.company_lifecycle_actions (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    action_type character varying(40) NOT NULL,
    idempotency_key character varying(200) NOT NULL,
    request_payload_hash character varying(64) NOT NULL,
    status character varying(20) NOT NULL,
    input_facts json NOT NULL,
    calculation_hash character varying(64),
    error_code character varying(100),
    owner_account_id uuid NOT NULL,
    owner_session_id uuid NOT NULL,
    owner_credential_version integer NOT NULL,
    executor_kind character varying(30) NOT NULL,
    executor_name character varying(100) NOT NULL,
    executor_version character varying(100) NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    completed_at timestamp with time zone,
    CONSTRAINT ck_company_lifecycle_action_type CHECK (((action_type)::text = ANY ((ARRAY['create'::character varying, 'profile_change'::character varying, 'status_change'::character varying, 'import'::character varying])::text[]))),
    CONSTRAINT ck_company_lifecycle_hashes CHECK (((length((request_payload_hash)::text) = 64) AND ((calculation_hash IS NULL) OR (length((calculation_hash)::text) = 64)))),
    CONSTRAINT ck_company_lifecycle_status CHECK (((status)::text = ANY ((ARRAY['started'::character varying, 'completed'::character varying, 'failed'::character varying])::text[])))
);


--
-- Name: company_registry; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.company_registry (
    org_id uuid NOT NULL,
    database_name character varying(80) NOT NULL,
    database_identity uuid NOT NULL,
    status character varying(30) NOT NULL,
    display_name character varying(200) NOT NULL,
    taxpayer_identification_number character varying(18) NOT NULL,
    profile_effective_from date NOT NULL,
    filing_cycle character varying(20) NOT NULL,
    urban_maintenance_rate numeric(6,5) NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    archived_at timestamp with time zone,
    is_primary boolean NOT NULL,
    CONSTRAINT ck_company_registry_archive_state CHECK (((((status)::text = 'archived'::text) AND (archived_at IS NOT NULL)) OR (((status)::text <> 'archived'::text) AND (archived_at IS NULL)))),
    CONSTRAINT ck_company_registry_database_name CHECK ((((database_name)::text = 'finance'::text) OR ((database_name)::text ~ '^finance_company_[0-9a-f]{32}$'::text))),
    CONSTRAINT ck_company_registry_filing_cycle CHECK (((filing_cycle)::text = ANY ((ARRAY['monthly'::character varying, 'quarterly'::character varying])::text[]))),
    CONSTRAINT ck_company_registry_status CHECK (((status)::text = ANY ((ARRAY['provisioning'::character varying, 'active'::character varying, 'changing'::character varying, 'archived'::character varying, 'attention_required'::character varying])::text[]))),
    CONSTRAINT ck_company_registry_urban_rate CHECK ((urban_maintenance_rate = ANY (ARRAY[0.07, 0.05, 0.01])))
);


--
-- Name: identity_audit_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.identity_audit_events (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    owner_account_id uuid,
    session_id uuid,
    event_type character varying(50) NOT NULL,
    outcome character varying(20) NOT NULL,
    reason_code character varying(100),
    request_correlation_id uuid NOT NULL,
    occurred_at timestamp with time zone NOT NULL
);


--
-- Name: owner_accounts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.owner_accounts (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    singleton_key integer NOT NULL,
    login_name character varying(100) NOT NULL,
    login_name_normalized character varying(100) NOT NULL,
    status character varying(20) NOT NULL,
    password_hash character varying(512) NOT NULL,
    credential_version integer NOT NULL,
    password_failed_attempts integer NOT NULL,
    password_throttled_until timestamp with time zone,
    recovery_failed_attempts integer NOT NULL,
    recovery_throttled_until timestamp with time zone,
    last_authenticated_at timestamp with time zone,
    password_changed_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_owner_account_credential_version CHECK ((credential_version >= 1)),
    CONSTRAINT ck_owner_account_login_name CHECK ((((length((login_name)::text) >= 3) AND (length((login_name)::text) <= 100)) AND ((login_name)::text = TRIM(BOTH FROM login_name)))),
    CONSTRAINT ck_owner_account_login_normalized CHECK (((login_name_normalized)::text = lower(TRIM(BOTH FROM login_name)))),
    CONSTRAINT ck_owner_account_password_failures CHECK ((password_failed_attempts >= 0)),
    CONSTRAINT ck_owner_account_password_hash CHECK (((length((password_hash)::text) = 97) AND ((password_hash)::text ~~ '$argon2id$v=19$m=65536,t=3,p=4$%'::text))),
    CONSTRAINT ck_owner_account_recovery_failures CHECK ((recovery_failed_attempts >= 0)),
    CONSTRAINT ck_owner_account_singleton CHECK ((singleton_key = 1)),
    CONSTRAINT ck_owner_account_status CHECK (((status)::text = ANY ((ARRAY['active'::character varying, 'disabled'::character varying])::text[])))
);


--
-- Name: owner_recovery_codes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.owner_recovery_codes (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    owner_account_id uuid NOT NULL,
    code_sha256 character varying(64) NOT NULL,
    credential_version integer NOT NULL,
    created_at timestamp with time zone NOT NULL,
    used_at timestamp with time zone,
    invalidated_at timestamp with time zone,
    CONSTRAINT ck_owner_recovery_code_credential_version CHECK ((credential_version >= 1)),
    CONSTRAINT ck_owner_recovery_code_sha256 CHECK ((length((code_sha256)::text) = 64)),
    CONSTRAINT ck_owner_recovery_code_terminal_state CHECK (((used_at IS NULL) OR (invalidated_at IS NULL)))
);


--
-- Name: owner_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.owner_sessions (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    owner_account_id uuid NOT NULL,
    secret_sha256 character varying(64) NOT NULL,
    credential_version integer NOT NULL,
    created_at timestamp with time zone NOT NULL,
    last_seen_at timestamp with time zone NOT NULL,
    idle_expires_at timestamp with time zone NOT NULL,
    absolute_expires_at timestamp with time zone NOT NULL,
    revoked_at timestamp with time zone,
    revoke_reason character varying(50),
    CONSTRAINT ck_owner_session_absolute_expiry CHECK ((absolute_expires_at > created_at)),
    CONSTRAINT ck_owner_session_credential_version CHECK ((credential_version >= 1)),
    CONSTRAINT ck_owner_session_expiry_order CHECK ((idle_expires_at <= absolute_expires_at)),
    CONSTRAINT ck_owner_session_idle_expiry CHECK ((idle_expires_at > created_at)),
    CONSTRAINT ck_owner_session_last_seen CHECK ((last_seen_at >= created_at)),
    CONSTRAINT ck_owner_session_revocation_state CHECK ((((revoked_at IS NULL) AND (revoke_reason IS NULL)) OR ((revoked_at IS NOT NULL) AND (revoke_reason IS NOT NULL)))),
    CONSTRAINT ck_owner_session_secret_sha256 CHECK ((length((secret_sha256)::text) = 64))
);


--
-- Name: catalog_metadata singleton_key; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.catalog_metadata ALTER COLUMN singleton_key SET DEFAULT nextval('public.catalog_metadata_singleton_key_seq'::regclass);


--
-- Name: accounting_period_close_backups accounting_period_close_backups_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounting_period_close_backups
    ADD CONSTRAINT accounting_period_close_backups_pkey PRIMARY KEY (id);


--
-- Name: catalog_metadata catalog_metadata_catalog_instance_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.catalog_metadata
    ADD CONSTRAINT catalog_metadata_catalog_instance_id_key UNIQUE (catalog_instance_id);


--
-- Name: catalog_metadata catalog_metadata_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.catalog_metadata
    ADD CONSTRAINT catalog_metadata_pkey PRIMARY KEY (singleton_key);


--
-- Name: close_backup_location_versions close_backup_location_versions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.close_backup_location_versions
    ADD CONSTRAINT close_backup_location_versions_pkey PRIMARY KEY (id);


--
-- Name: company_lifecycle_actions company_lifecycle_actions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.company_lifecycle_actions
    ADD CONSTRAINT company_lifecycle_actions_pkey PRIMARY KEY (id);


--
-- Name: company_registry company_registry_database_identity_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.company_registry
    ADD CONSTRAINT company_registry_database_identity_key UNIQUE (database_identity);


--
-- Name: company_registry company_registry_database_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.company_registry
    ADD CONSTRAINT company_registry_database_name_key UNIQUE (database_name);


--
-- Name: company_registry company_registry_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.company_registry
    ADD CONSTRAINT company_registry_pkey PRIMARY KEY (org_id);


--
-- Name: identity_audit_events identity_audit_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.identity_audit_events
    ADD CONSTRAINT identity_audit_events_pkey PRIMARY KEY (id);


--
-- Name: owner_accounts owner_accounts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_accounts
    ADD CONSTRAINT owner_accounts_pkey PRIMARY KEY (id);


--
-- Name: owner_recovery_codes owner_recovery_codes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_recovery_codes
    ADD CONSTRAINT owner_recovery_codes_pkey PRIMARY KEY (id);


--
-- Name: owner_sessions owner_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_sessions
    ADD CONSTRAINT owner_sessions_pkey PRIMARY KEY (id);


--
-- Name: close_backup_location_versions uq_close_backup_location_org_idempotency; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.close_backup_location_versions
    ADD CONSTRAINT uq_close_backup_location_org_idempotency UNIQUE (org_id, idempotency_key);


--
-- Name: close_backup_location_versions uq_close_backup_location_org_version; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.close_backup_location_versions
    ADD CONSTRAINT uq_close_backup_location_org_version UNIQUE (org_id, version);


--
-- Name: accounting_period_close_backups uq_close_backup_org_close; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounting_period_close_backups
    ADD CONSTRAINT uq_close_backup_org_close UNIQUE (org_id, close_id);


--
-- Name: company_lifecycle_actions uq_company_lifecycle_org_idempotency; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.company_lifecycle_actions
    ADD CONSTRAINT uq_company_lifecycle_org_idempotency UNIQUE (org_id, action_type, idempotency_key);


--
-- Name: company_registry uq_company_registry_taxpayer_identification_number; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.company_registry
    ADD CONSTRAINT uq_company_registry_taxpayer_identification_number UNIQUE (taxpayer_identification_number);


--
-- Name: owner_accounts uq_owner_account_login_normalized; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_accounts
    ADD CONSTRAINT uq_owner_account_login_normalized UNIQUE (login_name_normalized);


--
-- Name: owner_accounts uq_owner_account_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_accounts
    ADD CONSTRAINT uq_owner_account_org_id UNIQUE (org_id, id);


--
-- Name: owner_accounts uq_owner_account_singleton; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_accounts
    ADD CONSTRAINT uq_owner_account_singleton UNIQUE (singleton_key);


--
-- Name: owner_recovery_codes uq_owner_recovery_code_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_recovery_codes
    ADD CONSTRAINT uq_owner_recovery_code_org_id UNIQUE (org_id, id);


--
-- Name: owner_recovery_codes uq_owner_recovery_code_sha256; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_recovery_codes
    ADD CONSTRAINT uq_owner_recovery_code_sha256 UNIQUE (code_sha256);


--
-- Name: owner_sessions uq_owner_session_execution_authority; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_sessions
    ADD CONSTRAINT uq_owner_session_execution_authority UNIQUE (org_id, owner_account_id, id, credential_version);


--
-- Name: owner_sessions uq_owner_session_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_sessions
    ADD CONSTRAINT uq_owner_session_org_id UNIQUE (org_id, id);


--
-- Name: owner_sessions uq_owner_session_secret_sha256; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_sessions
    ADD CONSTRAINT uq_owner_session_secret_sha256 UNIQUE (secret_sha256);


--
-- Name: ix_accounting_period_close_backups_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_accounting_period_close_backups_org_id ON public.accounting_period_close_backups USING btree (org_id);


--
-- Name: ix_close_backup_location_versions_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_close_backup_location_versions_org_id ON public.close_backup_location_versions USING btree (org_id);


--
-- Name: ix_company_lifecycle_actions_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_company_lifecycle_actions_org_id ON public.company_lifecycle_actions USING btree (org_id);


--
-- Name: ix_identity_audit_events_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_identity_audit_events_org_id ON public.identity_audit_events USING btree (org_id);


--
-- Name: ix_identity_audit_events_request_correlation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_identity_audit_events_request_correlation_id ON public.identity_audit_events USING btree (request_correlation_id);


--
-- Name: ix_owner_accounts_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_owner_accounts_org_id ON public.owner_accounts USING btree (org_id);


--
-- Name: ix_owner_recovery_codes_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_owner_recovery_codes_org_id ON public.owner_recovery_codes USING btree (org_id);


--
-- Name: ix_owner_recovery_codes_owner_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_owner_recovery_codes_owner_account_id ON public.owner_recovery_codes USING btree (owner_account_id);


--
-- Name: ix_owner_sessions_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_owner_sessions_org_id ON public.owner_sessions USING btree (org_id);


--
-- Name: ix_owner_sessions_owner_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_owner_sessions_owner_account_id ON public.owner_sessions USING btree (owner_account_id);


--
-- Name: uq_company_registry_single_primary; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_company_registry_single_primary ON public.company_registry USING btree (is_primary) WHERE is_primary;


--
-- Name: uq_owner_recovery_code_current; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_owner_recovery_code_current ON public.owner_recovery_codes USING btree (owner_account_id) WHERE ((used_at IS NULL) AND (invalidated_at IS NULL));


--
-- Name: identity_audit_events catalog_identity_audit_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER catalog_identity_audit_append_only BEFORE DELETE OR UPDATE ON public.identity_audit_events FOR EACH ROW EXECUTE FUNCTION public.finance_catalog_identity_audit_append_only();


--
-- Name: owner_accounts catalog_owner_guard; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER catalog_owner_guard BEFORE INSERT OR DELETE OR UPDATE ON public.owner_accounts FOR EACH ROW EXECUTE FUNCTION public.finance_catalog_owner_guard();


--
-- Name: owner_recovery_codes catalog_recovery_guard; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER catalog_recovery_guard BEFORE INSERT OR DELETE OR UPDATE ON public.owner_recovery_codes FOR EACH ROW EXECUTE FUNCTION public.finance_catalog_recovery_guard();


--
-- Name: owner_sessions catalog_session_guard; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER catalog_session_guard BEFORE INSERT OR DELETE OR UPDATE ON public.owner_sessions FOR EACH ROW EXECUTE FUNCTION public.finance_catalog_session_guard();


--
-- Name: accounting_period_close_backups fk_close_backup_company; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounting_period_close_backups
    ADD CONSTRAINT fk_close_backup_company FOREIGN KEY (org_id) REFERENCES public.company_registry(org_id) ON DELETE RESTRICT;


--
-- Name: close_backup_location_versions fk_close_backup_location_company; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.close_backup_location_versions
    ADD CONSTRAINT fk_close_backup_location_company FOREIGN KEY (org_id) REFERENCES public.company_registry(org_id) ON DELETE RESTRICT;


--
-- Name: accounting_period_close_backups fk_close_backup_location_version; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounting_period_close_backups
    ADD CONSTRAINT fk_close_backup_location_version FOREIGN KEY (location_version_id) REFERENCES public.close_backup_location_versions(id) ON DELETE RESTRICT;


--
-- Name: company_lifecycle_actions fk_company_lifecycle_company; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.company_lifecycle_actions
    ADD CONSTRAINT fk_company_lifecycle_company FOREIGN KEY (org_id) REFERENCES public.company_registry(org_id) ON DELETE RESTRICT;


--
-- Name: identity_audit_events fk_identity_audit_company; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.identity_audit_events
    ADD CONSTRAINT fk_identity_audit_company FOREIGN KEY (org_id) REFERENCES public.company_registry(org_id) ON DELETE RESTRICT;


--
-- Name: identity_audit_events fk_identity_audit_org_account; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.identity_audit_events
    ADD CONSTRAINT fk_identity_audit_org_account FOREIGN KEY (org_id, owner_account_id) REFERENCES public.owner_accounts(org_id, id) ON DELETE RESTRICT;


--
-- Name: identity_audit_events fk_identity_audit_org_session; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.identity_audit_events
    ADD CONSTRAINT fk_identity_audit_org_session FOREIGN KEY (org_id, session_id) REFERENCES public.owner_sessions(org_id, id) ON DELETE RESTRICT;


--
-- Name: owner_accounts fk_owner_account_company; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_accounts
    ADD CONSTRAINT fk_owner_account_company FOREIGN KEY (org_id) REFERENCES public.company_registry(org_id) ON DELETE RESTRICT;


--
-- Name: owner_recovery_codes fk_owner_recovery_code_org_account; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_recovery_codes
    ADD CONSTRAINT fk_owner_recovery_code_org_account FOREIGN KEY (org_id, owner_account_id) REFERENCES public.owner_accounts(org_id, id) ON DELETE RESTRICT;


--
-- Name: owner_sessions fk_owner_session_org_account; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_sessions
    ADD CONSTRAINT fk_owner_session_org_account FOREIGN KEY (org_id, owner_account_id) REFERENCES public.owner_accounts(org_id, id) ON DELETE RESTRICT;


--
-- PostgreSQL database dump complete
--


