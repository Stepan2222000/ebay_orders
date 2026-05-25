-- Stage B — происхождение трек-номера.
--
-- Единый источник правды для треков — order_tracking_numbers. Колонка source
-- различает, кто записал трек: 'ebay' — агент в ebay_orders (парсер/чат),
-- 'delivery' — наша логика delivery (дополнительные треки к существующим
-- заказам). Аудит «кто/когда» уже даёт триггер trg_tracks_audit через
-- order_change_log.new_row; added_by/added_at дублируют это в самой строке.
--
-- Существующие строки получают source='ebay' по дефолту — все они записаны
-- агентом из eBay. Накатывается один раз через db/apply.sh (schema_migrations).

ALTER TABLE order_tracking_numbers
    ADD COLUMN source     text        NOT NULL DEFAULT 'ebay',
    ADD COLUMN added_by   text,
    ADD COLUMN added_at   timestamptz NOT NULL DEFAULT now();

ALTER TABLE order_tracking_numbers
    ADD CONSTRAINT order_tracking_numbers_source_chk
        CHECK (source IN ('ebay', 'delivery'));
