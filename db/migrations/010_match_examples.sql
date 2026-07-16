-- Примеры для переделки regex-правил и промпта (article_truth, этап 4-rework).
--
-- Пример рождается автоматически из ручной правки состава: «что агент видел и
-- решил → что должно было сматчиться по логике человека». Метка причины —
-- детерминированный классификатор в коде (не LLM):
--   rule     — правильный номер не проходит правила brands_mapping (дыра в правилах);
--   not_seen — номер покрыт правилами, но в прогоне его не было ни в подсказках,
--              ни в прочитанном агентом (не заметил на фото);
--   semantic — номер был агенту полностью доступен, выбрал другое (ошибка смысла);
--   unclear  — не классифицировалось однозначно.
--
-- input_context в agent_runs — вход прогона (кандидаты-подсказки + каталожный
-- контекст): без него классификация «был ли номер в подсказках» нечестна,
-- правила со временем меняются.
--
-- Накатывается через db/apply.sh ровно один раз (schema_migrations). Секретов нет.

ALTER TABLE agent_runs ADD COLUMN input_context jsonb;

CREATE TABLE match_examples (
    id             bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    item_number    text        NOT NULL REFERENCES items(item_number) ON DELETE CASCADE,
    kind           text        NOT NULL CHECK (kind IN ('rule', 'not_seen', 'semantic', 'unclear')),
    human_lines    jsonb       NOT NULL,   -- [{article, qty, canonical, part_id, kind}]
    agent_snapshot jsonb,                  -- {verdict, positions, near, input_context}
    run_id         bigint      REFERENCES agent_runs(id) ON DELETE SET NULL,
    note           text,
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX match_examples_kind_idx ON match_examples (kind, created_at DESC);
