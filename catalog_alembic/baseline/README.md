# Catalog schema baseline

`0001_catalog_baseline_v2` reads `postgresql.sql` to create the complete catalog,
identity, company-routing, and automatic close-backup schema. The asset contains no
owner credentials, sessions, company registrations, or business facts. The migration
adds only a newly generated (or explicitly supplied) catalog instance identifier.

This baseline supports empty PostgreSQL 17 databases only and intentionally has no
automatic downgrade.
