from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, read from the environment (.env in dev).

    Secrets never live in code; production reads from the DO secret store
    (Phase 5). Defaults here are dev-safe only.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://apichain:apichain@localhost:5433/apichain"
    frontend_origins: str = "http://localhost:5173"


settings = Settings()
