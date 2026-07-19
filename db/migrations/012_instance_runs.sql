-- Контур 2 (article_truth SPEC §12): журнал прогонов агента по ЭКЗЕМПЛЯРАМ
-- uchet — чтение настоящей маркировки с наших складских фото (parts_photos),
-- фолбэк — реальные фото листинга-источника (правило «под вопросом»).
--
-- instance_id — parts_uchet.items.id (без FK — другая база). Идемпотентность
-- как у agent_runs: done-прогон с тем же отпечатком не повторяется; отпечаток
-- = живые фото коллажа + articles детали + промпт + модель, так что новое
-- фото маркировки само даёт перечитку.
--
-- Накатывается через db/apply.sh. Секретов нет.

CREATE TABLE instance_runs (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    instance_id        integer     NOT NULL,
    input_fingerprint  text        NOT NULL,
    status             text        NOT NULL DEFAULT 'queued'
                       CHECK (status IN ('queued', 'running', 'done', 'failed')),
    dry_run            boolean     NOT NULL DEFAULT false,
    verdict            text
                       CHECK (verdict IS NULL OR verdict IN
                              ('primary', 'tentative', 'no_article')),
    article_read       text,
    source             text
                       CHECK (source IS NULL OR source IN
                              ('our_photos', 'seller_photos')),
    seller_photos_real boolean,
    raw_response       jsonb,
    error              text,
    model              text,
    prompt_tokens      integer,
    completion_tokens  integer,
    created_at         timestamptz NOT NULL DEFAULT now(),
    finished_at        timestamptz
);

CREATE INDEX instance_runs_instance_idx ON instance_runs (instance_id, created_at DESC);
