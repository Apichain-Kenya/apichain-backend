from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.enums import AnchorTarget


class Settings(BaseSettings):
    """Application settings, read from the environment (.env in dev).

    Secrets never live in code; production reads from the DO secret store
    (Phase 5). Defaults here are dev-safe only.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Dev DB name is `apichain_v2`, distinct from the v1 `apichain` DB that also
    # lives on :5433 locally — so a stray local run fails fast instead of
    # silently reading/writing v1 data. Compose creates this DB in its own volume.
    database_url: str = "postgresql+psycopg://apichain:apichain@localhost:5433/apichain_v2"
    frontend_origins: str = "http://localhost:5173"

    # Auth (Phase 1). Real secret comes from the environment; this default is
    # dev-only and must never reach staging/prod (the DO secret store supplies
    # it in Phase 5). `kid` key-rotation is deferred to Phase 5 (08 D2).
    jwt_secret_key: str = "dev-only-insecure-change-me-in-env-32b+"
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 30
    bcrypt_rounds: int = 12

    # Audit-chain integrity check (P1-G). The scheduler is disabled in tests.
    scheduler_enabled: bool = True
    integrity_check_interval_seconds: int = 300

    # Anchoring (P2-A, 09 §4). `anchor_enabled` is a second switch on top of
    # `scheduler_enabled` so anchoring can be off in a dev or staging box that
    # has no outbound network while the integrity job keeps running.
    anchor_enabled: bool = True
    anchor_target: AnchorTarget = AnchorTarget.opentimestamps
    anchor_interval_seconds: int = 300  # dev; ~3600 in staging/prod via env
    # Bitcoin confirmation takes hours (06 §8), so polling for the upgrade
    # faster than hourly is pure waste.
    anchor_upgrade_interval_seconds: int = 3600
    anchor_max_rows: int = 10_000  # bounds one run's tree; the run stays contiguous
    ots_calendar_urls: str = "https://a.pool.opentimestamps.org,https://b.pool.opentimestamps.org"
    ots_timeout_seconds: int = 10

    # Media pipeline (P3b-D, 11 §7). Every default is either the real dev
    # service compose runs or fail-closed; a fake is always an explicit opt-in
    # and is stamped on every row it touches (11 D4).
    storage_backend: Literal["s3", "fake"] = "s3"
    s3_endpoint: str = "localhost:9000"
    # What a browser can reach. Presigned URLs bind their host, so they are
    # signed for this, never for the in-cluster endpoint.
    s3_public_endpoint: str = "localhost:9000"
    s3_bucket: str = "apichain-documents"
    s3_access_key: str = "apichain"
    s3_secret_key: str = "apichain-dev"
    s3_secure: bool = False
    signed_url_ttl_seconds: int = Field(default=300, ge=30, le=3600)
    max_file_bytes: int = 10 * 1024 * 1024  # 04 §5.6
    max_subject_bytes: int = 50 * 1024 * 1024  # 04 §5.6

    # Fail-closed: with no clamd reachable, uploads answer 503 and store
    # nothing. A box without Docker sets scanner_backend=fake on purpose.
    scanner_backend: Literal["clamd", "fake"] = "clamd"
    clamd_host: str = "localhost"
    clamd_port: int = 3310
    clamd_timeout_seconds: float = 30.0

    # Communications (P3b-D, 11 §8). SMS defaults to the log-only fake because
    # no Africa's Talking credentials exist; email defaults to compose Mailpit.
    sms_backend: Literal["africastalking", "fake"] = "fake"
    at_username: str = "sandbox"
    at_api_key: str = ""
    at_sender_id: str | None = None
    at_sandbox: bool = True
    at_timeout_seconds: float = 10.0
    email_backend: Literal["smtp", "fake"] = "smtp"
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_from: str = "ApiChain <no-reply@apichain.local>"
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_starttls: bool = False
    smtp_timeout_seconds: float = 10.0

    # Verification codes (P3b-G). The pepper keys the stored HMAC; like
    # jwt_secret_key, the default is dev-only and production reads it from the
    # secret store (Phase 5).
    verification_code_pepper: str = "dev-only-verification-pepper-change-me"

    # Milestone worker (P3b-H, 11 D8).
    comms_enabled: bool = True
    comms_interval_seconds: int = 60
    comms_lookback_hours: int = 72
    comms_batch_size: int = 50
    comms_max_attempts: int = 3
    comms_claim_timeout_seconds: int = 300

    @field_validator("ots_calendar_urls")
    @classmethod
    def _at_least_one_calendar(cls, value: str) -> str:
        if not [u.strip() for u in value.split(",") if u.strip()]:
            raise ValueError("ots_calendar_urls must list at least one calendar URL")
        return value

    @property
    def calendar_urls(self) -> list[str]:
        """The configured calendars. Submitting to several is redundancy, not
        consensus: one success is enough (09 §7)."""
        return [u.strip() for u in self.ots_calendar_urls.split(",") if u.strip()]


settings = Settings()
