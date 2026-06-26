"""Настройки приложения. Читаются из окружения / .env один раз при импорте."""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

# OpenAI-совместимый прокси gpt55. Effort кодируется суффиксом имени модели
# (cursor-gpt55(low|medium|high)) — отдельного поля reasoning прокси не требует.
_DEFAULT_BASE_URL = "http://194.164.245.107:8317/v1"


@dataclass(frozen=True)
class Settings:
    pg_host: str
    pg_port: int
    pg_user: str
    pg_password: str
    pg_database: str

    openai_api_key: str
    openai_base_url: str = _DEFAULT_BASE_URL
    ocr_model: str = "claude-opus-4-8"   # транскрибация — стадия A (Claude vision)
    agent_model: str = "gpt-5.5"  # агент — стадия B
    llm_max_tokens: int = 16000
    llm_timeout_s: float = 120.0
    ocr_max_attempts: int = 3

    worker_concurrency: int = 10
    worker_idle_sleep_s: float = 0.5
    max_screenshot_bytes: int = 10 * 1024 * 1024  # 10 MiB

    # фото товаров: конкуренция фото-операций (eBay+MinIO) и сколько раз пробовать
    # номер до терминального failed (защита от «шторма» и от вечных ретраев)
    photo_concurrency: int = 6
    photo_max_attempts: int = 3
    manual_photo_bucket: str = "ebay-orders-my-photos"
    manual_photo_max_dim: int = 1600  # кап по длинной стороне


def load() -> Settings:
    return Settings(
        pg_host=os.environ["PGHOST"],
        pg_port=int(os.environ["PGPORT"]),
        pg_user=os.environ["PGUSER"],
        pg_password=os.environ["POSTGRES_PASSWORD"],
        pg_database=os.environ["PGDATABASE"],
        openai_api_key=os.environ["OPENAI_API_KEY"],
        openai_base_url=os.environ.get("OPENAI_BASE_URL") or _DEFAULT_BASE_URL,
        worker_concurrency=int(os.environ.get("WORKER_CONCURRENCY", 10)),
        photo_concurrency=int(os.environ.get("PHOTO_CONCURRENCY", 6)),
    )


settings = load()
