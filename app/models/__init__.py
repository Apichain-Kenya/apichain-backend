"""Model package. Importing it registers every table on `Base.metadata`, which
is what `alembic/env.py` autogenerates against and what the app imports.
"""

from app.models.audit import AuditLog, ConsentRecord
from app.models.auth import IdempotencyKey, RefreshToken
from app.models.batch import HoneyBatch
from app.models.enums import BatchState, ConsentPurpose, GrantedVia, Role
from app.models.identity import Farmer, User

__all__ = [
    "AuditLog",
    "BatchState",
    "ConsentPurpose",
    "ConsentRecord",
    "Farmer",
    "GrantedVia",
    "HoneyBatch",
    "IdempotencyKey",
    "RefreshToken",
    "Role",
    "User",
]
