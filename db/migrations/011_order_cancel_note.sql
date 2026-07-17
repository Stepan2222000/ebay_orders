-- Отмена заказа и личные заметки (article_truth SPEC §10).
--
-- «Отменён» — вычисляемый признак из трёх источников: полный refund
-- (sum(order_refunds) >= order_total_usd, уже в данных со скринов), cancel-текст
-- в свободном delivery_status, и РУЧНАЯ пометка (cancelled_at) — для случая
-- «отменил только что, скрина ещё нет». Эффект отмены один: не ждём приезда
-- (едущие этапа 5, pending uchet/ebay_to_buy). Разбор истины НЕ меняется.
--
-- user_note — свободная заметка «для себя на будущее» («может приехать
-- неполное»); показывается в карточке листинга и в delivery-intake.
--
-- Накатывается через db/apply.sh. Секретов нет.

ALTER TABLE orders ADD COLUMN cancelled_at timestamptz;
ALTER TABLE orders ADD COLUMN cancel_note text;
ALTER TABLE orders ADD COLUMN user_note text;
