"""Настройки приложения. Читаются из окружения / .env один раз при импорте."""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    pg_host: str
    pg_port: int
    pg_user: str
    pg_password: str
    pg_database: str

    openrouter_api_key: str
    openrouter_model: str = "moonshotai/kimi-k2.6"
    openrouter_max_tokens: int = 16000
    openrouter_timeout_s: float = 120.0
    openrouter_ignore_providers: tuple[str, ...] = ("inceptron",)

    worker_concurrency: int = 10
    worker_idle_sleep_s: float = 0.5
    max_screenshot_bytes: int = 10 * 1024 * 1024  # 10 MiB


def load() -> Settings:
    return Settings(
        pg_host=os.environ["PGHOST"],
        pg_port=int(os.environ["PGPORT"]),
        pg_user=os.environ["PGUSER"],
        pg_password=os.environ["POSTGRES_PASSWORD"],
        pg_database=os.environ["PGDATABASE"],
        openrouter_api_key=os.environ["OPENROUTER_API_KEY"],
        worker_concurrency=int(os.environ.get("WORKER_CONCURRENCY", 10)),
    )


settings = load()
