-- Аудит правок article_match_rules из UI ebay_orders (article_truth/SPEC.md §4).
-- Правила общие для нескольких проектов — каждая правка фиксируется.
-- Применяется вручную: psql -h 2.27.20.221 -p 5411 -U admin -d brands_mapping -1 -f этот_файл.

CREATE TABLE rule_audit (
    id         bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    rule_name  text        NOT NULL,
    action     text        NOT NULL CHECK (action IN
                           ('create', 'update', 'enable', 'disable', 'delete')),
    old_value  jsonb,
    new_value  jsonb,
    note       text,
    created_at timestamptz NOT NULL DEFAULT now()
);
