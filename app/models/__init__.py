"""Model package. Importing it registers every table on `Base.metadata`, which
is what `alembic/env.py` autogenerates against and what the app imports.
"""

from app.enums import (
    AnchorTarget,
    BatchState,
    ConformanceVerdict,
    ConsentPurpose,
    GrantedVia,
    Role,
)
from app.models.anchor import MerkleAnchor
from app.models.audit import AuditLog, ConsentRecord
from app.models.auth import IdempotencyKey, RefreshToken
from app.models.batch import HoneyBatch
from app.models.identity import Farmer, User
from app.models.stages import (
    ApiaryLocation,
    ApiaryRecord,
    BatchMetadata,
    CodexConformance,
    DistributionRecord,
    HarvestRecord,
    LabResult,
    PackagingRecord,
    ProcessRecord,
)

__all__ = [
    "AnchorTarget",
    "ApiaryLocation",
    "ApiaryRecord",
    "AuditLog",
    "BatchMetadata",
    "BatchState",
    "CodexConformance",
    "ConformanceVerdict",
    "ConsentPurpose",
    "ConsentRecord",
    "DistributionRecord",
    "Farmer",
    "GrantedVia",
    "HarvestRecord",
    "HoneyBatch",
    "IdempotencyKey",
    "LabResult",
    "MerkleAnchor",
    "PackagingRecord",
    "ProcessRecord",
    "RefreshToken",
    "Role",
    "User",
]
