-- Stage B v3 — мягкая проверка денежных инвариантов.
--
-- До этой миграции orders_invariants_check сравнивал суммы строго через <>.
-- Это ломало случай иностранной валюты (CAD/GBP/EUR), где модель пересчитывает
-- компоненты по курсу из видимого USD-якоря в Payment info: округления eBay
-- дают ±1–3 цента дрейфа, и заказ не сохранялся.
--
-- Допуск зафиксирован как 0.05 USD (см. [[правила денег USD]]). Триггер
-- оставлен ровно на тех же таблицах с тем же DEFERRABLE-поведением.

CREATE OR REPLACE FUNCTION orders_invariants_check() RETURNS trigger AS $$
DECLARE
    oid        bigint;
    o          orders%ROWTYPE;
    s_items    numeric(14,2);
    has_tracks boolean;
    tol        constant numeric(14,2) := 0.05;
BEGIN
    oid := COALESCE(NEW.order_id, OLD.order_id);
    SELECT * INTO o FROM orders WHERE order_id = oid;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    -- 1. sum(items.line_total) ≈ item_subtotal_usd
    IF o.item_subtotal_usd IS NOT NULL THEN
        SELECT COALESCE(SUM(item_line_total_usd), 0)
          INTO s_items
          FROM order_items WHERE order_id = oid;
        IF abs(s_items - o.item_subtotal_usd) > tol THEN
            RAISE EXCEPTION
                'order %: sum(order_items.item_line_total_usd)=% but orders.item_subtotal_usd=% (diff > $%)',
                o.order_number, s_items, o.item_subtotal_usd, tol;
        END IF;
    END IF;

    -- 2. components ≈ total
    IF o.item_subtotal_usd IS NOT NULL
       AND o.shipping_usd      IS NOT NULL
       AND o.sales_tax_usd     IS NOT NULL THEN
        IF abs((o.item_subtotal_usd + o.shipping_usd + o.sales_tax_usd) - o.order_total_usd) > tol THEN
            RAISE EXCEPTION
                'order %: item_subtotal+shipping+tax=% but order_total_usd=% (diff > $%)',
                o.order_number,
                o.item_subtotal_usd + o.shipping_usd + o.sales_tax_usd,
                o.order_total_usd, tol;
        END IF;
    END IF;

    -- 3. is_untracked = true исключает наличие tracking_numbers (не денежная проверка, без допуска)
    IF o.is_untracked THEN
        SELECT EXISTS(SELECT 1 FROM order_tracking_numbers WHERE order_id = oid)
          INTO has_tracks;
        IF has_tracks THEN
            RAISE EXCEPTION
                'order %: is_untracked=true but tracking_numbers exist',
                o.order_number;
        END IF;
    END IF;

    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
