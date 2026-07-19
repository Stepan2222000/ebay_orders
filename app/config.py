"""Настройки приложения. Читаются из окружения / .env один раз при импорте."""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

# OpenAI-совместимый прокси gpt55. Effort кодируется суффиксом имени модели
# (cursor-gpt55(low|medium|high)) — отдельного поля reasoning прокси не требует.
_DEFAULT_BASE_URL = "http://2.27.20.221:8317/v1"


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
    agent_model: str = "gpt-5.6-terra"  # агент — стадия B
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

    # снапшоты текстов листинга (article_truth/SPEC.md §5): конкуренция PDP-фетчей,
    # потолок ретраев транзиента, период отложенной сверки титулов в воркере
    snapshot_concurrency: int = 6
    snapshot_max_attempts: int = 3
    snapshot_reconcile_period_s: float = 10.0

    # истина по артикулам (article_truth/SPEC.md §6): модель одна, без фолбэка.
    # TRUTH_WRITE=1 — боевая запись (item_parts/статусы/карточки), иначе сухой
    # режим (только agent_runs, dry_run=true).
    truth_model: str = "gpt-5.6-luna"
    truth_write: bool = False
    truth_concurrency: int = 3
    truth_max_attempts: int = 3           # failed-прогонов на один отпечаток
    truth_max_photos: int = 10
    truth_max_tokens: int = 4000
    truth_llm_timeout_s: float = 240.0
    truth_poll_s: float = 15.0            # быстрый цикл: листинги без прогонов
    truth_recheck_period_s: float = 3600.0  # пересчёт отпечатков нефинальных

    # едущие экземпляры (article_truth/SPEC.md §10): запись в parts_uchet
    # ПРЯМЫМ подключением (FDW-запись валится на audit-триггерах, SPEC §14).
    # transit_since — дата запуска этапа 5: авто-создание только для заказов,
    # появившихся в базе с этой даты (старый хвост — разовый backfill руками).
    uchet_pg_dsn: str = "postgresql://admin:Password123@parts_uchet:5432/parts_uchet"
    transit_since: str = "2026-07-18"
    transit_poll_s: float = 60.0

    # контур 2 (SPEC §12): чтение маркировки экземпляров с наших фото.
    # INSTANCE_TRUTH: off — петля не запускается (дефолт; первый прогон — руками
    # частями, ворота включения за пользователем), dry — журнал без записи,
    # write — боевой. Фото экземпляров — parts_photos (MinIO публичен).
    instance_mode: str = "off"
    photos_pg_dsn: str = "postgresql://admin:Password123@parts_photos:5432/parts_photos"
    photos_minio_base: str = "http://2.27.20.221:9000/parts-photos"
    instance_poll_s: float = 120.0
    instance_concurrency: int = 2


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
        snapshot_concurrency=int(os.environ.get("SNAPSHOT_CONCURRENCY", 6)),
        truth_write=os.environ.get("TRUTH_WRITE") == "1",
        truth_concurrency=int(os.environ.get("TRUTH_CONCURRENCY", 3)),
        uchet_pg_dsn=os.environ.get("UCHET_PG_DSN")
            or "postgresql://admin:Password123@parts_uchet:5432/parts_uchet",
        transit_since=os.environ.get("TRANSIT_SINCE", "2026-07-18"),
        instance_mode=os.environ.get("INSTANCE_TRUTH", "off"),
        photos_pg_dsn=os.environ.get("PHOTOS_PG_DSN")
            or "postgresql://admin:Password123@parts_photos:5432/parts_photos",
    )


settings = load()
