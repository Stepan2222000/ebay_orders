-- Stage 1 — фундамент в Postgres.
-- Накатывается через db/apply.sh ровно один раз; идемпотентность обеспечивает
-- applier поверх таблицы schema_migrations, поэтому сама миграция не делает
-- IF NOT EXISTS / DROP — её повторное применение должно ронять, чтобы не
-- замаскировать дрифт.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TYPE proc_status   AS ENUM ('pending','running','done','failed');
CREATE TYPE change_source AS ENUM ('screenshot','user_chat');

-- ── Чистовые таблицы ──────────────────────────────────────────────────────

CREATE TABLE orders (
    order_id          bigserial      PRIMARY KEY,
    order_number      text           NOT NULL UNIQUE,
    sold_by           text           NOT NULL,
    ordered_at        timestamptz,
    order_total_usd   numeric(14,2)  NOT NULL CHECK (order_total_usd >= 0),
    item_subtotal_usd numeric(14,2)           CHECK (item_subtotal_usd >= 0),
    shipping_usd      numeric(14,2)           CHECK (shipping_usd      >= 0),
    sales_tax_usd     numeric(14,2)           CHECK (sales_tax_usd     >= 0),
    delivery_status   text,
    delivered_date    date,
    arriving_by_date  text,
    shipping_service  text,
    is_untracked      boolean        NOT NULL DEFAULT false,
    created_at        timestamptz    NOT NULL DEFAULT now(),
    updated_at        timestamptz    NOT NULL DEFAULT now()
);

CREATE TABLE order_items (
    order_id            bigint         NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
    item_number         text           NOT NULL,
    item_title          text           NOT NULL,
    item_quantity       int            NOT NULL CHECK (item_quantity > 0),
    item_line_total_usd numeric(14,2)  NOT NULL CHECK (item_line_total_usd >= 0),
    PRIMARY KEY (order_id, item_number)
);

CREATE TABLE order_refunds (
    refund_id         bigserial      PRIMARY KEY,
    order_id          bigint         NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
    refund_amount_usd numeric(14,2)  NOT NULL CHECK (refund_amount_usd > 0),
    refund_date       date,
    refund_note       text
);

CREATE TABLE order_tracking_numbers (
    order_id        bigint NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
    tracking_number text   NOT NULL,
    PRIMARY KEY (order_id, tracking_number)
);
CREATE INDEX order_tracking_numbers_track_idx ON order_tracking_numbers(tracking_number);

-- ── Технические таблицы ──────────────────────────────────────────────────

CREATE TABLE screenshots (
    sha256       bytea       PRIMARY KEY CHECK (octet_length(sha256) = 32),
    byte_size    int         NOT NULL CHECK (byte_size > 0),
    mime_type    text        NOT NULL,
    bytes        bytea       NOT NULL,
    ocr_status   proc_status NOT NULL DEFAULT 'pending',
    agent_status proc_status NOT NULL DEFAULT 'pending',
    last_error   text,
    order_id     bigint               REFERENCES orders(order_id) ON DELETE SET NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX screenshots_ocr_pending_idx   ON screenshots(created_at) WHERE ocr_status   = 'pending';
CREATE INDEX screenshots_agent_pending_idx ON screenshots(created_at) WHERE agent_status = 'pending';
CREATE INDEX screenshots_order_id_idx      ON screenshots(order_id);

CREATE TABLE raw_ocr (
    sha256   bytea       PRIMARY KEY REFERENCES screenshots(sha256) ON DELETE CASCADE,
    model    text        NOT NULL,
    raw_json jsonb       NOT NULL,
    ocr_at   timestamptz NOT NULL DEFAULT now()
);

-- Сессия одна и постоянная — фиксируем единственную строку через CHECK.
CREATE TABLE chat_sessions (
    session_id text        PRIMARY KEY DEFAULT 'default' CHECK (session_id = 'default'),
    created_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO chat_sessions(session_id) VALUES ('default');

CREATE TABLE chat_messages (
    message_id bigserial   PRIMARY KEY,
    session_id text        NOT NULL REFERENCES chat_sessions(session_id) ON DELETE CASCADE,
    role       text        NOT NULL CHECK (role IN ('user','assistant','system')),
    parts      jsonb       NOT NULL CHECK (jsonb_typeof(parts) = 'array'),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX chat_messages_session_created_idx ON chat_messages(session_id, created_at);

-- ── pg_trgm индексы для нечёткой сверки агентом стадии B ──────────────────

CREATE INDEX orders_sold_by_trgm_idx     ON orders      USING GIN (sold_by    gin_trgm_ops);
CREATE INDEX order_items_title_trgm_idx  ON order_items USING GIN (item_title gin_trgm_ops);

-- ── Журнал изменений чистовых полей ──────────────────────────────────────

CREATE TABLE order_change_log (
    id         bigserial   PRIMARY KEY,
    table_name text        NOT NULL,
    op         text        NOT NULL CHECK (op IN ('INSERT','UPDATE','DELETE')),
    source     change_source,
    ts         timestamptz NOT NULL DEFAULT now(),
    old_row    jsonb,
    new_row    jsonb
);
CREATE INDEX order_change_log_ts_idx ON order_change_log(ts);

CREATE OR REPLACE FUNCTION audit_trg() RETURNS trigger AS $$
DECLARE
    src      text          := current_setting('app.source', true);
    src_enum change_source := NULL;
BEGIN
    IF src IS NOT NULL AND src <> '' THEN
        src_enum := src::change_source;
    END IF;
    INSERT INTO order_change_log(table_name, op, source, old_row, new_row)
    VALUES (
        TG_TABLE_NAME,
        TG_OP,
        src_enum,
        CASE WHEN TG_OP IN ('UPDATE','DELETE') THEN to_jsonb(OLD) END,
        CASE WHEN TG_OP IN ('INSERT','UPDATE') THEN to_jsonb(NEW) END
    );
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_orders_audit  AFTER INSERT OR UPDATE OR DELETE ON orders
    FOR EACH ROW EXECUTE FUNCTION audit_trg();
CREATE TRIGGER trg_items_audit   AFTER INSERT OR UPDATE OR DELETE ON order_items
    FOR EACH ROW EXECUTE FUNCTION audit_trg();
CREATE TRIGGER trg_refunds_audit AFTER INSERT OR UPDATE OR DELETE ON order_refunds
    FOR EACH ROW EXECUTE FUNCTION audit_trg();
CREATE TRIGGER trg_tracks_audit  AFTER INSERT OR UPDATE OR DELETE ON order_tracking_numbers
    FOR EACH ROW EXECUTE FUNCTION audit_trg();

-- ── updated_at для orders ────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_orders_touch BEFORE UPDATE ON orders
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- ── Проверка сумм заказа на закрытии транзакции ──────────────────────────
-- CHECK не может быть DEFERRABLE в Postgres, поэтому используется
-- CONSTRAINT TRIGGER ... DEFERRABLE INITIALLY DEFERRED.

CREATE OR REPLACE FUNCTION orders_invariants_check() RETURNS trigger AS $$
DECLARE
    oid        bigint;
    o          orders%ROWTYPE;
    s_items    numeric(14,2);
    has_tracks boolean;
BEGIN
    oid := COALESCE(NEW.order_id, OLD.order_id);
    SELECT * INTO o FROM orders WHERE order_id = oid;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    -- 1. sum(items.line_total) = item_subtotal_usd, если subtotal задан
    IF o.item_subtotal_usd IS NOT NULL THEN
        SELECT COALESCE(SUM(item_line_total_usd), 0)
          INTO s_items
          FROM order_items WHERE order_id = oid;
        IF s_items <> o.item_subtotal_usd THEN
            RAISE EXCEPTION
                'order %: sum(order_items.item_line_total_usd)=% but orders.item_subtotal_usd=%',
                o.order_number, s_items, o.item_subtotal_usd;
        END IF;
    END IF;

    -- 2. components -> total, только если все три компонента видны
    IF o.item_subtotal_usd IS NOT NULL
       AND o.shipping_usd      IS NOT NULL
       AND o.sales_tax_usd     IS NOT NULL THEN
        IF (o.item_subtotal_usd + o.shipping_usd + o.sales_tax_usd) <> o.order_total_usd THEN
            RAISE EXCEPTION
                'order %: item_subtotal+shipping+tax=% but order_total_usd=%',
                o.order_number,
                o.item_subtotal_usd + o.shipping_usd + o.sales_tax_usd,
                o.order_total_usd;
        END IF;
    END IF;

    -- 3. is_untracked = true исключает наличие tracking_numbers
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

CREATE CONSTRAINT TRIGGER trg_orders_invariants
    AFTER INSERT OR UPDATE ON orders
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION orders_invariants_check();

CREATE CONSTRAINT TRIGGER trg_items_invariants
    AFTER INSERT OR UPDATE OR DELETE ON order_items
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION orders_invariants_check();

CREATE CONSTRAINT TRIGGER trg_tracks_invariants
    AFTER INSERT OR UPDATE OR DELETE ON order_tracking_numbers
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION orders_invariants_check();
