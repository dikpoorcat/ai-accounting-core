"""Small native response contract for bounded accounting source discovery.

The existing typed fact body remains source JSON. Its business registration
contract is discovered separately rather than expanded into this read contract.
Omitted profile fields remain omitted; validation creates no management facts.
"""

from __future__ import annotations

from typing import Annotated, Literal, NotRequired

from pydantic import ConfigDict, Field, JsonValue, TypeAdapter
from typing_extensions import TypedDict

from .entities import DisplayDate, EntityKind
from .response_types import Version1
from .types import EvidenceDigest, YearMonth

Count = Annotated[int, Field(ge=0)]
Revision = Annotated[int, Field(ge=1)]
AccountType = Literal["bank", "cash", "platform"]


class ContextObject(TypedDict):
    __pydantic_config__ = ConfigDict(strict=True, extra="forbid")


class ContextEpochs(ContextObject):
    accounting: Count
    material: Count
    management: Count


class ContextCompanyIdentity(ContextObject):
    company_id: str
    taxpayer_id: str
    database_id: str


class ContextDatabaseFormat(ContextObject):
    family: str
    kind: Literal["company"]
    status: Literal["draft", "released"]
    version: Count
    fingerprint: EvidenceDigest


class ContextCompanyNote(ContextObject):
    revision: Count
    text: str
    digest: EvidenceDigest | None
    evidence_digest: EvidenceDigest | None


class ContextCompanyBackground(ContextObject):
    identity: ContextCompanyIdentity
    database_format: ContextDatabaseFormat
    company_note: ContextCompanyNote
    epochs: ContextEpochs
    source_kinds: list[str]
    note_semantics: str


class ContextAdoption(ContextObject):
    basis: Literal["asset_batch_member", "direct_publication"]
    publication_id: str
    calculation_id: str
    posting_period: YearMonth
    mode: Literal["initial", "open_replace", "closed_correction", "review_no_impact", "withdrawn"]
    owner_calculation_id: str | None
    current: bool


class ContextIdentityMatch(ContextObject):
    entity_id: str
    role: str
    path: str
    identity_match: Literal["current"]


class ContextSourceFact(ContextObject):
    fact_id: str
    subject_id: str
    revision: Revision
    kind: str
    period: YearMonth
    data: dict[str, JsonValue]
    evidence: list[EvidenceDigest]
    is_current: bool
    superseded: bool
    pending: bool
    calculation_id: str | None
    adoption: ContextAdoption | None
    identity_matches: list[ContextIdentityMatch]


class ContextCalculationDependencies(ContextObject):
    fact_ids: list[str]
    calculation_ids: list[str]


class ContextBusinessReference(ContextObject):
    path: str
    role: str
    value: str


class ContextFact(ContextSourceFact):
    calculation_dependencies: ContextCalculationDependencies | None
    business_references: list[ContextBusinessReference]


class ContextFactReference(ContextObject):
    subject_id: str
    fact_id: str
    kind: str


class ContextOriginal(ContextObject):
    digest: EvidenceDigest
    name: str
    media_type: str
    byte_size: Count


class ContextMaterial(ContextObject):
    source: ContextSourceFact
    original: ContextOriginal
    received_period: YearMonth
    page_related_fact_refs: list[ContextFactReference]
    period_semantics: str


class ContextEntityProfile(ContextObject):
    id: str
    entity_id: str
    revision: Revision
    source: str
    digest: EvidenceDigest
    evidence_digest: EvidenceDigest | None
    entity_kind: EntityKind
    account_type: AccountType | None
    display_name: NotRequired[str | None]
    display_number: NotRequired[str | None]
    external_identifiers: NotRequired[dict[str, str]]
    active: NotRequired[bool]
    purpose: NotRequired[str | None]
    note: NotRequired[str | None]
    employment_start: NotRequired[DisplayDate | None]
    employment_end: NotRequired[DisplayDate | None]
    employment_status: NotRequired[Literal["active", "inactive", "unknown"]]
    category_label: NotRequired[str | None]
    rights_description: NotRequired[str | None]
    useful_life_basis: NotRequired[str | None]


class ContextEntity(ContextObject):
    entity_id: str
    kind: EntityKind
    account_type: AccountType | None
    profile: ContextEntityProfile


class WorkContextResponse(ContextObject):
    schema_version: Version1
    company_id: str
    period: YearMonth
    work_area: Literal["bank", "payroll", "transactions", "tax", "assets", "financing"]
    company_context: ContextCompanyBackground
    read_repair_revision: Count
    sort: Literal["period_desc_fact_id_desc"]
    items: list[ContextFact]
    materials: list[ContextMaterial]
    entities: list[ContextEntity]
    has_more: bool
    next_cursor: str | None
    page_semantics: str


WORK_CONTEXT_ADAPTER = TypeAdapter(WorkContextResponse)
