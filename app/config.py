from pydantic_settings import BaseSettings, SettingsConfigDict


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


settings = Settings()
