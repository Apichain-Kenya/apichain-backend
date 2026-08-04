"""Bounded, stable enumerations stored as native PostgreSQL enums.

Only genuinely stable value sets live here (roles, batch states, consent
purposes). Open-ended vocabularies that grow every phase — `audit_log.action`
and `audit_log.subject_type` — are plain strings validated at the app layer,
so adding a new action never needs an `ALTER TYPE` migration.

This module sits at `app/` rather than inside `app/models/` because it is
shared vocabulary, not an ORM model: `app.config` types `anchor_target` with
`AnchorTarget` (P2-A), and importing it from the model package would run
`app/models/__init__.py` → `app.database` → back into `app.config`. Models
still re-export these names, so `from app.models import Role` keeps working.
"""

import enum


class Role(enum.StrEnum):
    """The four canonical actors plus admin (04 §5.3)."""

    farmer = "farmer"
    field_officer = "field_officer"
    operator = "operator"
    lab_officer = "lab_officer"
    admin = "admin"


class BatchState(enum.StrEnum):
    """The six-state honey batch lifecycle (04 §5.2). No state skipping."""

    CREATED = "CREATED"
    HARVESTED = "HARVESTED"
    PROCESSED = "PROCESSED"
    LAB_VERIFIED = "LAB_VERIFIED"
    PACKAGED = "PACKAGED"
    DISTRIBUTED = "DISTRIBUTED"


class ConsentPurpose(enum.StrEnum):
    """Consent events captured at enrollment or upload (04 §5.2)."""

    data_processing = "data_processing"
    document_upload = "document_upload"
    sms_notifications = "sms_notifications"
    email_notifications = "email_notifications"
    photo_publish = "photo_publish"


class GrantedVia(enum.StrEnum):
    """How a consent grant was captured (04 §5.2)."""

    onboarder = "onboarder"
    farmer_self = "farmer_self"


class AnchorTarget(enum.StrEnum):
    """Where a Merkle root is published (04 §5.2).

    The target is settled: OpenTimestamps to Bitcoin (05 §6.3). `polygon` is a
    reserved fallback (06 §9) — the value exists so switching would not need a
    migration; no code path in v2 consumes it.
    """

    opentimestamps = "opentimestamps"
    polygon = "polygon"
