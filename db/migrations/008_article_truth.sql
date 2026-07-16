-- Истина по артикулам (article truth): снапшоты текстов листинга, прогоны
-- мультимодального агента, карточки разбора, игнор-лист «похожих на артикул»,
-- статус конфликта. Дизайн: article_truth/SPEC.md §4 (соседний репо).
--
-- item_snapshots — БЕЗ FK на items, по той же причине, что item_photos (007):
-- снапшот триггерится на OCR-done, раньше, чем save_order создаст items.
-- Сверка титулов и всё агентское — только после появления items-строки.
-- agent_runs и review_cards создаются заведомо после save_order — у них FK есть.
--
-- Накатывается через db/apply.sh ровно один раз (schema_migrations). Секретов нет.

-- Снапшот текстов листинга (SPEC §5): один на item_number, навсегда.
-- Ручная догрузка пишет specifics_raw/description сырым текстом, source='manual'.
CREATE TABLE item_snapshots (
    item_number   text        PRIMARY KEY,
    status        text        NOT NULL DEFAULT 'pending'
                              CHECK (status IN ('pending', 'done', 'failed')),
    source        text        CHECK (source IN ('ebay', 'manual')),
    title         text,
    condition     text,
    specifics     jsonb,      -- авто-парс PDP (ключ-значение)
    specifics_raw text,       -- ручная догрузка: как скопировано, без парсинга
    description   text,
    catalog_url   text,       -- 301 → ebay.com/p/{id}: ссылку храним, страницу не парсим
    attempts      integer     NOT NULL DEFAULT 0,
    last_error    text,
    fetched_at    timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX item_snapshots_pending_idx ON item_snapshots (item_number)
    WHERE status = 'pending';

-- Прогон агента (SPEC §6): храним все прогоны — аудит и идемпотентность.
-- dry_run=true — сухой режим (item_parts/статусы не трогались).
-- verdict — итог постобработки, чтобы очередь разбора не парсила сырой ответ.
CREATE TABLE agent_runs (
    id                bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    item_number       text        NOT NULL REFERENCES items(item_number) ON DELETE CASCADE,
    input_fingerprint text        NOT NULL,  -- хэш(фото + тексты + правила + каталожный контекст)
    model             text,
    status            text        NOT NULL DEFAULT 'queued'
                                  CHECK (status IN ('queued', 'running', 'done', 'failed')),
    dry_run           boolean     NOT NULL DEFAULT false,
    raw_response      jsonb,
    positions         jsonb,      -- разобранные позиции: article_read, канон, part_id, qty, sources
    near_articles     jsonb,
    contradictions    jsonb,
    qty_note          text,
    verdict           text        CHECK (verdict IS NULL OR verdict IN
                                  ('linked', 'not_in_catalog', 'no_article', 'conflict')),
    error             text,
    prompt_tokens     integer,
    completion_tokens integer,
    created_at        timestamptz NOT NULL DEFAULT now(),
    finished_at       timestamptz
);

CREATE INDEX agent_runs_item_idx  ON agent_runs (item_number, created_at DESC);
CREATE INDEX agent_runs_queue_idx ON agent_runs (created_at) WHERE status = 'queued';

-- Событийные карточки разбора (SPEC §4): статусные очереди (not_in_catalog,
-- no_article, «нужна догрузка», «не покрыто правилами») карточек НЕ плодят —
-- вычисляются из items/item_snapshots/agent_runs.
CREATE TABLE review_cards (
    id          bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind        text        NOT NULL CHECK (kind IN
                            ('title_mismatch', 'contradiction', 'human_disagreement',
                             'refund', 'truth_change')),
    item_number text        REFERENCES items(item_number) ON DELETE CASCADE,
    order_id    bigint      REFERENCES orders(order_id) ON DELETE CASCADE,
    payload     jsonb       NOT NULL DEFAULT '{}'::jsonb,
    status      text        NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
    resolution  text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz,
    CONSTRAINT review_cards_target_chk CHECK (item_number IS NOT NULL OR order_id IS NOT NULL)
);

CREATE INDEX review_cards_open_idx ON review_cards (kind, created_at) WHERE status = 'open';
-- Одна ОТКРЫТАЯ карточка типа на объект; resolved не мешает новой открытой.
CREATE UNIQUE INDEX review_cards_open_item_uniq ON review_cards (kind, item_number)
    WHERE status = 'open' AND item_number IS NOT NULL;
CREATE UNIQUE INDEX review_cards_open_order_uniq ON review_cards (kind, order_id)
    WHERE status = 'open' AND order_id IS NOT NULL;

-- «Похоже на артикул, но не артикул» — игнор навсегда (SPEC §4).
CREATE TABLE near_article_ignores (
    normalized text        PRIMARY KEY,   -- нормализованная форма (uppercase, без пробелов)
    reason     text,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- match_status: + 'conflict' (SPEC §4). Семантика значений — SPEC §4 п.5.
ALTER TABLE items DROP CONSTRAINT items_match_status_check;
ALTER TABLE items ADD CONSTRAINT items_match_status_check CHECK (match_status = ANY
    (ARRAY['pending'::text, 'linked'::text, 'needs_review'::text,
           'no_article'::text, 'not_in_catalog'::text, 'conflict'::text]));
