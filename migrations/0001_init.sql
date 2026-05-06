-- Stage 1 — foundation schema and Postgres-side validation.
-- See docs/SPEC.yaml (source of truth) and docs/PLAN.md.

BEGIN;

-- ============================================================
-- 1. Extensions
-- ============================================================
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ============================================================
-- 2. Domains and enums
-- ============================================================
CREATE DOMAIN money_usd_nonneg AS numeric(12, 2)
    CHECK (VALUE >= 0);

CREATE DOMAIN sha256_hex AS char(64)
    CHECK (VALUE ~ '^[0-9a-f]{64}$');

CREATE TYPE ocr_status_t   AS ENUM ('pending', 'running', 'done', 'failed');
CREATE TYPE agent_status_t AS ENUM ('pending', 'running', 'done', 'failed');

-- ============================================================
-- 3. Helpers
-- ============================================================
CREATE FUNCTION arr_uniq(a text[]) RETURNS boolean
    LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT AS $$
    SELECT cardinality(a) = (SELECT count(DISTINCT x) FROM unnest(a) x)
$$;

CREATE FUNCTION set_updated_at() RETURNS trigger
    LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END
$$;

-- ============================================================
-- 4. Technical layer
-- ============================================================
CREATE TABLE screenshots (
    sha256       sha256_hex      PRIMARY KEY,
    bytes        bytea           NOT NULL,
    mime         text            NOT NULL,
    byte_size    integer         NOT NULL,
    ocr_status   ocr_status_t    NOT NULL DEFAULT 'pending',
    agent_status agent_status_t  NOT NULL DEFAULT 'pending',
    error_text   text,
    created_at   timestamptz     NOT NULL DEFAULT now(),
    updated_at   timestamptz     NOT NULL DEFAULT now(),
    CONSTRAINT screenshots_byte_size_matches CHECK (byte_size = octet_length(bytes)),
    CONSTRAINT screenshots_mime_is_image     CHECK (mime LIKE 'image/%')
);

CREATE TRIGGER screenshots_set_updated_at
    BEFORE UPDATE ON screenshots
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE INDEX screenshots_pending_ocr_idx
    ON screenshots (sha256)
    WHERE ocr_status = 'pending';

CREATE INDEX screenshots_pending_agent_idx
    ON screenshots (sha256)
    WHERE agent_status = 'pending';

CREATE TABLE raw_ocr (
    sha256         sha256_hex   PRIMARY KEY REFERENCES screenshots(sha256) ON DELETE CASCADE,
    model          text         NOT NULL,
    payload        jsonb        NOT NULL,
    recognized_at  timestamptz  NOT NULL DEFAULT now()
);

CREATE TABLE chat_sessions (
    session_id  uuid         PRIMARY KEY,
    started_at  timestamptz  NOT NULL DEFAULT now()
);

CREATE TABLE chat_messages (
    id          bigserial    PRIMARY KEY,
    session_id  uuid         NOT NULL REFERENCES chat_sessions(session_id) ON DELETE CASCADE,
    role        text         NOT NULL CHECK (role IN ('user', 'assistant')),
    content     text,
    parts       jsonb,
    created_at  timestamptz  NOT NULL DEFAULT now()
);

CREATE INDEX chat_messages_session_idx
    ON chat_messages (session_id, created_at);

-- ============================================================
-- 5. Cleanroom layer
-- ============================================================
CREATE TABLE orders (
    order_number       text              PRIMARY KEY,
    sold_by            text              NOT NULL,
    ordered_at         timestamptz,
    order_total_usd    money_usd_nonneg  NOT NULL,
    item_subtotal_usd  money_usd_nonneg,
    shipping_usd       money_usd_nonneg,
    sales_tax_usd      money_usd_nonneg,
    delivery_status    text,
    delivered_date     date,
    arriving_by_date   text,
    shipping_service   text,
    tracking_numbers   text[]            NOT NULL DEFAULT '{}',
    is_untracked       boolean,
    created_at         timestamptz       NOT NULL DEFAULT now(),
    updated_at         timestamptz       NOT NULL DEFAULT now(),
    deleted_at         timestamptz,
    deleted_reason     text,
    CONSTRAINT orders_tracking_unique
        CHECK (arr_uniq(tracking_numbers)),
    CONSTRAINT orders_untracked_no_trackings
        CHECK (is_untracked IS NOT TRUE OR cardinality(tracking_numbers) = 0),
    CONSTRAINT orders_soft_delete_pair
        CHECK ((deleted_at IS NULL) = (deleted_reason IS NULL))
);

CREATE TRIGGER orders_set_updated_at
    BEFORE UPDATE ON orders
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE INDEX orders_sold_by_trgm_idx
    ON orders USING gin (sold_by gin_trgm_ops);

CREATE TABLE order_items (
    id                    bigserial         PRIMARY KEY,
    order_number          text              NOT NULL REFERENCES orders(order_number) ON DELETE CASCADE,
    item_number           text              NOT NULL,
    item_title            text              NOT NULL,
    item_quantity         integer           NOT NULL CHECK (item_quantity > 0),
    item_line_total_usd   money_usd_nonneg  NOT NULL,
    UNIQUE (order_number, item_number)
);

CREATE INDEX order_items_order_idx
    ON order_items (order_number);

CREATE TABLE order_refunds (
    id                  bigserial         PRIMARY KEY,
    order_number        text              NOT NULL REFERENCES orders(order_number) ON DELETE CASCADE,
    refund_amount_usd   money_usd_nonneg  NOT NULL,
    refund_date         date,
    refund_note         text
);

CREATE UNIQUE INDEX order_refunds_dedup_idx
    ON order_refunds (
        order_number,
        refund_amount_usd,
        COALESCE(refund_date, '1970-01-01'::date),
        COALESCE(refund_note, '')
    );

CREATE TABLE order_screenshot_links (
    order_number  text         NOT NULL REFERENCES orders(order_number)  ON DELETE CASCADE,
    sha256        sha256_hex   NOT NULL REFERENCES screenshots(sha256)   ON DELETE RESTRICT,
    linked_at     timestamptz  NOT NULL DEFAULT now(),
    PRIMARY KEY (order_number, sha256)
);

CREATE INDEX order_screenshot_links_sha_idx
    ON order_screenshot_links (sha256);

CREATE TABLE order_change_log (
    id            bigserial    PRIMARY KEY,
    changed_at    timestamptz  NOT NULL DEFAULT now(),
    target_table  text         NOT NULL CHECK (target_table IN ('orders', 'order_items', 'order_refunds')),
    order_number  text,
    op            text         NOT NULL CHECK (op IN ('INSERT', 'UPDATE', 'DELETE')),
    source        text,
    before        jsonb,
    after         jsonb
);

CREATE INDEX order_change_log_order_idx
    ON order_change_log (order_number, changed_at DESC);

-- ============================================================
-- 6. Audit trigger (one function, three triggers)
-- ============================================================
CREATE FUNCTION change_log_write() RETURNS trigger
    LANGUAGE plpgsql AS $$
DECLARE
    v_order_number text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        v_order_number := COALESCE(OLD.order_number, NULL);
    ELSE
        v_order_number := COALESCE(NEW.order_number, NULL);
    END IF;

    INSERT INTO order_change_log (target_table, order_number, op, source, before, after)
    VALUES (
        TG_TABLE_NAME,
        v_order_number,
        TG_OP,
        NULLIF(current_setting('app.source', true), ''),
        CASE WHEN TG_OP IN ('UPDATE', 'DELETE') THEN to_jsonb(OLD) END,
        CASE WHEN TG_OP IN ('INSERT', 'UPDATE') THEN to_jsonb(NEW) END
    );

    RETURN COALESCE(NEW, OLD);
END
$$;

CREATE TRIGGER orders_change_log
    AFTER INSERT OR UPDATE OR DELETE ON orders
    FOR EACH ROW EXECUTE FUNCTION change_log_write();

CREATE TRIGGER order_items_change_log
    AFTER INSERT OR UPDATE OR DELETE ON order_items
    FOR EACH ROW EXECUTE FUNCTION change_log_write();

CREATE TRIGGER order_refunds_change_log
    AFTER INSERT OR UPDATE OR DELETE ON order_refunds
    FOR EACH ROW EXECUTE FUNCTION change_log_write();

-- ============================================================
-- 7. Money sum invariants (DEFERRABLE constraint trigger)
-- See SPEC.yaml [[правила проверки сумм#формула]] and #строки-товаров.
-- Checks fire only when the relevant fields are NOT NULL — system never invents.
-- ============================================================
CREATE FUNCTION orders_check_sums(p_order_number text) RETURNS void
    LANGUAGE plpgsql AS $$
DECLARE
    v_total      money_usd_nonneg;
    v_subtotal   money_usd_nonneg;
    v_shipping   money_usd_nonneg;
    v_tax        money_usd_nonneg;
    v_items_sum  money_usd_nonneg;
BEGIN
    SELECT order_total_usd, item_subtotal_usd, shipping_usd, sales_tax_usd
      INTO v_total, v_subtotal, v_shipping, v_tax
      FROM orders
     WHERE order_number = p_order_number;

    -- Order may be gone (DELETE cascade); nothing to check.
    IF v_total IS NULL THEN
        RETURN;
    END IF;

    -- Subtotal vs item lines: only when subtotal is known.
    IF v_subtotal IS NOT NULL THEN
        SELECT COALESCE(sum(item_line_total_usd), 0)
          INTO v_items_sum
          FROM order_items
         WHERE order_number = p_order_number;

        IF v_items_sum <> v_subtotal THEN
            RAISE EXCEPTION
                'order % item subtotal mismatch: sum(items)=% but item_subtotal_usd=%',
                p_order_number, v_items_sum, v_subtotal
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    -- Total decomposition: only when ALL three components are known.
    IF v_subtotal IS NOT NULL AND v_shipping IS NOT NULL AND v_tax IS NOT NULL THEN
        IF (v_subtotal + v_shipping + v_tax) <> v_total THEN
            RAISE EXCEPTION
                'order % total mismatch: subtotal(%)+shipping(%)+tax(%)=% but order_total_usd=%',
                p_order_number, v_subtotal, v_shipping, v_tax,
                (v_subtotal + v_shipping + v_tax), v_total
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
END
$$;

CREATE FUNCTION orders_sum_trigger() RETURNS trigger
    LANGUAGE plpgsql AS $$
BEGIN
    PERFORM orders_check_sums(COALESCE(NEW.order_number, OLD.order_number));
    RETURN NULL;
END
$$;

CREATE FUNCTION order_items_sum_trigger() RETURNS trigger
    LANGUAGE plpgsql AS $$
BEGIN
    PERFORM orders_check_sums(COALESCE(NEW.order_number, OLD.order_number));
    RETURN NULL;
END
$$;

CREATE CONSTRAINT TRIGGER orders_check_sums_tg
    AFTER INSERT OR UPDATE ON orders
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION orders_sum_trigger();

CREATE CONSTRAINT TRIGGER order_items_check_sums_tg
    AFTER INSERT OR UPDATE OR DELETE ON order_items
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION order_items_sum_trigger();

COMMIT;
