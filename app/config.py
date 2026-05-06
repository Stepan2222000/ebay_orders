from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    PGHOST: str
    PGPORT: int = 5432
    PGUSER: str
    PGDATABASE: str
    POSTGRES_PASSWORD: str

    OPENROUTER_API_KEY: str
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"

    STAGE_A_MODEL: str = "moonshotai/kimi-k2.6"
    STAGE_A_CONCURRENCY: int = Field(10, ge=1, le=64)
    STAGE_A_POLL_SECONDS: float = 0.5
    STAGE_A_TIMEOUT_SECONDS: int = 240


settings = Settings()
