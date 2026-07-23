from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, read from the environment (.env in dev).

    Secrets never live in code; production reads from the DO secret store
    (Phase 5). Defaults here are dev-safe only.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://apichain:apichain@localhost:5433/apichain"
    frontend_origins: str = "http://localhost:5173"

    # Auth (Phase 1). Real secret comes from the environment; this default is
    # dev-only and must never reach staging/prod (the DO secret store supplies
    # it in Phase 5). `kid` key-rotation is deferred to Phase 5 (08 D2).
    jwt_secret_key: str = "dev-only-insecure-change-me"
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 30
    bcrypt_rounds: int = 12


settings = Settings()
