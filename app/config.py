from pydantic import field_validator
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
