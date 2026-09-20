"""Explicit real-object directory rows for synthetic accounting unit tests."""

from ai_accounting.kernel.entities import EntityProfile
from ai_accounting.kernel.types import canonical, digest


def seed_entities(engine, definitions):
    """Seed named synthetic IDs without weakening production entity checks."""
    definitions = tuple(definitions)
    if not definitions:
        return
    with engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        for entity_id, kind, account_type in definitions:
            profile = EntityProfile().model_dump(mode="json")
            existing = connection.execute(
                "SELECT kind,account_type FROM entity WHERE id=?", (entity_id,)
            ).fetchone()
            if existing is not None:
                assert tuple(existing) == (
                    kind,
                    account_type,
                ), "synthetic entity has conflicting roles"
                continue
            connection.execute(
                "INSERT INTO entity(id,kind,account_type) VALUES(?,?,?)",
                (entity_id, kind, account_type),
            )
            connection.execute(
                "INSERT INTO entity_profile_revision"
                "(id,entity_id,revision,content,source,evidence_digest,digest) "
                "VALUES(?,?,1,?,'synthetic test fixture',NULL,?)",
                (
                    "test-profile-" + entity_id,
                    entity_id,
                    canonical(profile),
                    digest([entity_id, 1, profile, "synthetic test fixture", None]),
                ),
            )
        connection.commit()


def seed_fact_entities(engine, fact):
    """Provision declared objects for pure accounting fixtures with named fake IDs.

    Tests call this explicitly before their business command. Identity/API tests
    use register_entity instead; this helper never supplies missing fact fields.
    """
    from ai_accounting.kernel.entity_references import references_for

    definitions = {}
    with engine.store.connection(read_only=True) as connection:
        for reference in references_for(fact, "synthetic-source"):
            if reference["reference_type"] != "entity":
                continue
            identifier = reference["entity_id"]
            existing = connection.execute(
                "SELECT kind,account_type FROM entity WHERE id=?", (identifier,)
            ).fetchone()
            if existing is not None:
                assert not reference["kinds"] or existing["kind"] in reference["kinds"]
                assert existing["account_type"] == reference["account_type"]
                continue
            assert reference["kinds"], "internal bindings require a pre-registered target"
            definitions[identifier] = (identifier, reference["kinds"][0], reference["account_type"])
    seed_entities(engine, definitions.values())


def seed_registration_entities(engine, kind, data):
    """Typed positive setup; invalid facts are left for the tested public boundary."""
    from pydantic import ValidationError

    try:
        fact = engine.store.registry.models[kind].model_validate_json(canonical(data))
    except (ValidationError, KeyError):
        return
    seed_fact_entities(engine, fact)


def save_entity_display_profile(engine, profile, *, expected_revision, request_id):
    """Explicit entity setup for presentation tests; revisions remain real."""
    from ai_accounting.kernel.display import Display
    from ai_accounting.kernel.entities import Entities

    data = dict(profile)
    kind = data.pop("kind")
    if kind == "business":
        return Display(engine).save_display_profile(
            profile, expected_revision=expected_revision, request_id=request_id
        )
    entity_id = data.pop("entity_id")
    source = data.pop("source")
    evidence = data.pop("evidence_digest", None)
    entity_kind = {"employee": "person", "counterparty": "organization"}.get(kind, kind)
    with engine.store.connection(read_only=True) as connection:
        current = connection.execute("SELECT kind FROM entity WHERE id=?", (entity_id,)).fetchone()
    if current is None:
        seed_entities(
            engine, [(entity_id, entity_kind, "bank" if kind == "fund_account" else None)]
        )
    # The synthetic directory starts with one blank profile revision. Preserve
    # the caller's expected version relative to that explicit starting point.
    revision = expected_revision + 1
    saved = Entities(engine).update_entity_profile(
        entity_id,
        data,
        source=source,
        evidence_digest=evidence,
        expected_revision=revision,
        request_id=request_id,
    )
    return dict(saved, id=saved["profile_id"])
