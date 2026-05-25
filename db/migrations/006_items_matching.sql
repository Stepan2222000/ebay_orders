-- Stage B — привязка eBay-листингов к деталям каталога smart.
--
-- Листинг (item_number) выносим в отдельную сущность `items`: один листинг = один
-- товар, переиспользуется во всех заказах с тем же item_number. item_title уезжает
-- из order_items сюда (читателей order_items.item_title в api/frontend нет; в
-- OCR-схеме app/schema.py поле остаётся — это вход save_order).
--
-- Привязка item -> деталь(и) каталога: item_parts, 0..N строк на листинг (бандлы).
-- part_id = smart.parts.id; кросс-БД FK невозможен, целостность держим на уровне
-- приложения/матчера. Матчинг — по regex-правилам из brands_mapping (через FDW,
-- см. db/setup_fdw.sql).
--
-- Накатывается через db/apply.sh ровно один раз (schema_migrations). Секретов нет.

CREATE TABLE items (
    item_number  text        PRIMARY KEY,          -- eBay listing id, стабилен
    item_title   text        NOT NULL,             -- канонический титул листинга
    match_status text        NOT NULL DEFAULT 'pending'
        CHECK (match_status IN ('pending','linked','needs_review','no_article','not_in_catalog')),
    matched_at   timestamptz,                       -- когда последний раз менялся разбор
    match_note   text,                              -- кандидаты бандла / причина review
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX items_match_status_idx ON items(match_status) WHERE match_status <> 'linked';

CREATE TABLE item_parts (
    item_number     text        NOT NULL REFERENCES items(item_number) ON DELETE CASCADE,
    part_id         text        NOT NULL,           -- = smart.parts.id (кросс-БД, не FK)
    quantity        int         NOT NULL DEFAULT 1 CHECK (quantity > 0),
    matched_article text,                            -- каким артикулом сматчилось (трассировка)
    match_method    text        NOT NULL CHECK (match_method IN ('regex_exact','agent','human')),
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (item_number, part_id)
);
CREATE INDEX item_parts_part_idx ON item_parts(part_id);

-- Бэкофилл listing-уровня из order_items (титулы у дублей идентичны — проверено).
INSERT INTO items(item_number, item_title)
SELECT DISTINCT ON (item_number) item_number, item_title
  FROM order_items
 ORDER BY item_number;

-- order_items: теряет item_title (уехал в items), получает FK на items.
ALTER TABLE order_items
    ADD CONSTRAINT order_items_item_fk FOREIGN KEY (item_number) REFERENCES items(item_number);
ALTER TABLE order_items DROP COLUMN item_title;

-- Провенанс привязок несёт сам item_parts (matched_article, match_method) +
-- items.matched_at/match_note. В order_change_log матчинг НЕ дублируем —
-- он про чистовые поля заказа.
