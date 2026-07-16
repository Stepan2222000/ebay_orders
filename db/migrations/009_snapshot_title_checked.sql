-- Отметка выполненной сверки титулов (article_truth/SPEC.md §5).
--
-- Снапшот обычно завершается раньше, чем save_order создаст items-строку,
-- поэтому сверка OCR-титула с PDP-титулом — отложенный шаг (воркер, периодически).
-- title_checked_at ставится после сверки (совпало → items.item_title перезаписан
-- PDP-формой; нет → карточка title_mismatch). Сбрасывается при refetch.
--
-- Накатывается через db/apply.sh ровно один раз (schema_migrations). Секретов нет.

ALTER TABLE item_snapshots ADD COLUMN title_checked_at timestamptz;

CREATE INDEX item_snapshots_unchecked_idx ON item_snapshots (item_number)
    WHERE status = 'done' AND title_checked_at IS NULL;
