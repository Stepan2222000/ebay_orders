-- brands_mapping DB (2.27.20.221:5411) — нормализация эталонных брендов.
--
-- ВНИМАНИЕ: это миграция ОТДЕЛЬНОЙ, общей БД brands_mapping, а не ebay_orders.
-- У brands_mapping нет своей системы миграций (schema_migrations); применяется
-- вручную один раз. Файл лежит здесь как durable-запись, т.к. ebay_orders —
-- основной потребитель этих данных через postgres_fdw.
--
-- Применено: 2026-05-25. Неразрушающее, аддитивное:
--   - brand_aliases (alias, canonical) сохраняет форму — старые потребители ок;
--   - добавлен FK brand_aliases.canonical -> brands.canonical;
--   - brands — единый реестр эталонных брендов, на него ссылаются все;
--   - article_match_rules — дом для regex-правил извлечения артикулов.
--
-- Набор canonical совпадает с smart.brands и smart.part_brands (13 брендов,
-- проверено). Кросс-БД FK к smart невозможен — синхрон по соглашению.

CREATE TABLE brands (
    canonical    text PRIMARY KEY,     -- эталон: MERCRUISER, BRP, VOLVO, ...
    display_name text                  -- для UI/отчётов (опц.)
);

INSERT INTO brands(canonical)
SELECT DISTINCT canonical FROM brand_aliases ORDER BY 1;

ALTER TABLE brand_aliases
    ADD CONSTRAINT brand_aliases_canonical_fk
    FOREIGN KEY (canonical) REFERENCES brands(canonical);

-- Правила извлечения артикула из eBay-титула. В рантайме матчер гоняет ВСЕ
-- enabled-правила (бренд из титула НЕ определяется); canonical — для
-- организации/трассировки. Кандидат = склейка capture-групп regex'а.
CREATE TABLE article_match_rules (
    name       text PRIMARY KEY,       -- человекочитаемый id правила
    canonical  text NOT NULL REFERENCES brands(canonical),
    find_regex text NOT NULL,          -- regex извлечения кандидата
    note       text NOT NULL,          -- алгоритм матчинга словами
    enabled    boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);
