-- Фото товаров — галерея объявления eBay + ручные снимки, привязка к item_number.
--
-- Лёгкий путь (не через ebay_data): ссылки eBay тянет ebay_library.fetch_image_urls,
-- файлы лежат в MinIO, здесь — только индекс. Дизайн: docs/ITEM_PHOTOS.md.
--
-- Ключ — item_number (id объявления). НЕ FK на items: строки items создаёт
-- save_order (после сборки), а фото триггерятся раньше (на OCR-done). FK плодил бы
-- сирот и портил счётчики матчинга.
--
-- Накатывается через db/apply.sh ровно один раз (schema_migrations). Секретов нет.

-- Статус скачивания eBay-галереи по номеру (идемпотентность инлайн-триггера).
CREATE TABLE listing_photos (
    item_number text        PRIMARY KEY,
    ebay_status text        NOT NULL DEFAULT 'pending'
        CHECK (ebay_status IN ('pending','done','failed')),
    attempts    int         NOT NULL DEFAULT 0,
    last_error  text,                                  -- причина failed / последнего сбоя
    fetched_at  timestamptz,                           -- когда дошли до терминального статуса
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX listing_photos_pending_idx ON listing_photos(item_number)
    WHERE ebay_status = 'pending';

-- Сама галерея: и eBay, и ручные. Одна строка = одно фото.
CREATE TABLE item_photos (
    id          bigserial   PRIMARY KEY,
    item_number text        NOT NULL,
    source      text        NOT NULL CHECK (source IN ('ebay','manual')),
    idx         int         NOT NULL DEFAULT 0,        -- порядок внутри источника
    s3_url      text        NOT NULL,                  -- полный публичный URL в MinIO
    url_hash    bytea       NOT NULL,                  -- md5(ebay_url) | md5(bytes) — дедуп
    ebay_url    text,                                  -- только source='ebay' (трассировка)
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (item_number, source, url_hash)
);
CREATE INDEX item_photos_item_idx ON item_photos(item_number);
