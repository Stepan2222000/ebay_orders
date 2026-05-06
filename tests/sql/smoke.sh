#!/usr/bin/env bash
# Stage 1 smoke test. Walks every CHECK / CONSTRAINT TRIGGER on the live DB.
# Each negative case runs in its own transaction so a failed COMMIT doesn't
# poison the rest. The whole run leaves no rows behind (rollback at end of
# every case + a final cleanup that is also rolled back).
set -uo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DB="${PROJECT_ROOT}/scripts/db.sh"

PASS=0
FAIL=0

# Helper SHA: 64 hex chars, deterministic, prefixed.
sha() { printf '%-064s' "$1" | tr ' ' '0' | tr 'A-Z' 'a-z'; }

# expect_ok <case-id> <description> <SQL>
expect_ok() {
    local id="$1" desc="$2" sql="$3"
    if "${DB}" -v ON_ERROR_STOP=1 -q -c "$sql" >/tmp/smoke.out 2>/tmp/smoke.err; then
        echo "OK   case ${id} — ${desc}"
        PASS=$((PASS+1))
    else
        echo "FAIL case ${id} — ${desc}"
        echo "    err: $(tr '\n' ' ' < /tmp/smoke.err)"
        FAIL=$((FAIL+1))
    fi
}

# expect_err <case-id> <description> <expected-substring> <SQL>
expect_err() {
    local id="$1" desc="$2" needle="$3" sql="$4"
    if "${DB}" -v ON_ERROR_STOP=1 -q -c "$sql" >/tmp/smoke.out 2>/tmp/smoke.err; then
        echo "FAIL case ${id} — ${desc} (expected error, got success)"
        FAIL=$((FAIL+1))
    else
        if grep -q -- "$needle" /tmp/smoke.err; then
            echo "OK   case ${id} — ${desc}  [error matched: ${needle}]"
            PASS=$((PASS+1))
        else
            echo "FAIL case ${id} — ${desc}"
            echo "    expected substring: ${needle}"
            echo "    got: $(tr '\n' ' ' < /tmp/smoke.err)"
            FAIL=$((FAIL+1))
        fi
    fi
}

S1=$(sha A)  # 64 hex zeros prefixed by 'a'
S2=$(sha B)
S3=$(sha C)

# Clean any leftovers from prior runs (every case rolls back, but be safe).
"${DB}" -q -c "
TRUNCATE order_change_log RESTART IDENTITY;
DELETE FROM order_screenshot_links;
DELETE FROM order_refunds;
DELETE FROM order_items;
DELETE FROM orders;
DELETE FROM raw_ocr;
DELETE FROM screenshots;
DELETE FROM chat_messages;
DELETE FROM chat_sessions;
" >/dev/null

# ──────────────────────────────────────────────────────────────────
# Case 1: ON CONFLICT DO NOTHING idempotency on screenshots.
# ──────────────────────────────────────────────────────────────────
expect_ok 1 "screenshots ON CONFLICT DO NOTHING keeps first bytes" "
BEGIN;
INSERT INTO screenshots (sha256, bytes, mime, byte_size)
VALUES ('${S1}', E'\\\\xdeadbeef', 'image/png', 4);
INSERT INTO screenshots (sha256, bytes, mime, byte_size)
VALUES ('${S1}', E'\\\\xcafebabe', 'image/png', 4)
  ON CONFLICT (sha256) DO NOTHING;
DO \$\$
DECLARE n int; b bytea;
BEGIN
  SELECT count(*), max(bytes) INTO n, b FROM screenshots WHERE sha256='${S1}';
  IF n <> 1 OR encode(b,'hex') <> 'deadbeef' THEN
    RAISE EXCEPTION 'expected single deadbeef row, got % bytes=%', n, encode(b,'hex');
  END IF;
END\$\$;
ROLLBACK;
"

# ──────────────────────────────────────────────────────────────────
# Case 2: sha256 wrong format → domain check fails.
# ──────────────────────────────────────────────────────────────────
expect_err 2 "sha256_hex domain rejects bad format" "violates check constraint" "
BEGIN;
INSERT INTO screenshots (sha256, bytes, mime, byte_size)
VALUES ('NOT-A-SHA', E'\\\\x01', 'image/png', 1);
ROLLBACK;
"

# ──────────────────────────────────────────────────────────────────
# Case 3: byte_size mismatch.
# ──────────────────────────────────────────────────────────────────
expect_err 3 "byte_size must equal octet_length(bytes)" "screenshots_byte_size_matches" "
BEGIN;
INSERT INTO screenshots (sha256, bytes, mime, byte_size)
VALUES ('${S2}', E'\\\\xdeadbeef', 'image/png', 999);
ROLLBACK;
"

# ──────────────────────────────────────────────────────────────────
# Case 4: non-image mime rejected.
# ──────────────────────────────────────────────────────────────────
expect_err 4 "mime must start with image/" "screenshots_mime_is_image" "
BEGIN;
INSERT INTO screenshots (sha256, bytes, mime, byte_size)
VALUES ('${S2}', E'\\\\x01', 'application/pdf', 1);
ROLLBACK;
"

# ──────────────────────────────────────────────────────────────────
# Case 5: full happy-path order + items + refund + link + change log.
# ──────────────────────────────────────────────────────────────────
expect_ok 5 "order+items+refund+link COMMIT, change_log populated" "
BEGIN;
SET LOCAL app.source = 'screenshot';
INSERT INTO screenshots (sha256, bytes, mime, byte_size)
VALUES ('${S1}', E'\\\\x01', 'image/png', 1);
INSERT INTO orders (order_number, sold_by, order_total_usd, item_subtotal_usd, shipping_usd, sales_tax_usd)
VALUES ('ORD-A', 'shop1', 110.00, 100.00, 5.00, 5.00);
INSERT INTO order_items (order_number, item_number, item_title, item_quantity, item_line_total_usd)
VALUES ('ORD-A', 'I-1', 'thing one', 1, 60.00),
       ('ORD-A', 'I-2', 'thing two', 2, 40.00);
INSERT INTO order_refunds (order_number, refund_amount_usd, refund_date, refund_note)
VALUES ('ORD-A', 3.00, '2026-04-01', 'partial');
INSERT INTO order_screenshot_links (order_number, sha256) VALUES ('ORD-A', '${S1}');
DO \$\$
DECLARE
  n_orders int; n_items int; n_refunds int; n_log int; v_source text;
BEGIN
  SELECT count(*) INTO n_log FROM order_change_log WHERE order_number='ORD-A';
  SELECT count(*) INTO n_orders FROM order_change_log WHERE target_table='orders' AND order_number='ORD-A';
  SELECT count(*) INTO n_items  FROM order_change_log WHERE target_table='order_items' AND order_number='ORD-A';
  SELECT count(*) INTO n_refunds FROM order_change_log WHERE target_table='order_refunds' AND order_number='ORD-A';
  SELECT max(source) INTO v_source FROM order_change_log WHERE order_number='ORD-A';
  IF n_orders <> 1 OR n_items <> 2 OR n_refunds <> 1 THEN
    RAISE EXCEPTION 'change_log counts wrong: orders=%, items=%, refunds=%', n_orders, n_items, n_refunds;
  END IF;
  IF v_source <> 'screenshot' THEN
    RAISE EXCEPTION 'expected source=screenshot, got %', v_source;
  END IF;
END\$\$;
ROLLBACK;
"

# ──────────────────────────────────────────────────────────────────
# Case 6: items sum < item_subtotal_usd → DEFERRED check fails on COMMIT.
# ──────────────────────────────────────────────────────────────────
expect_err 6 "sum(items) <> item_subtotal_usd fails on COMMIT" "item subtotal mismatch" "
BEGIN;
INSERT INTO orders (order_number, sold_by, order_total_usd, item_subtotal_usd)
VALUES ('ORD-B', 'shop1', 50.00, 50.00);
INSERT INTO order_items (order_number, item_number, item_title, item_quantity, item_line_total_usd)
VALUES ('ORD-B', 'I-1', 'thing', 1, 30.00);
COMMIT;
"

# ──────────────────────────────────────────────────────────────────
# Case 7: subtotal+shipping+tax <> total → fails on COMMIT.
# ──────────────────────────────────────────────────────────────────
expect_err 7 "subtotal+shipping+tax <> order_total_usd fails on COMMIT" "total mismatch" "
BEGIN;
INSERT INTO orders (order_number, sold_by, order_total_usd, item_subtotal_usd, shipping_usd, sales_tax_usd)
VALUES ('ORD-C', 'shop1', 200.00, 100.00, 10.00, 10.00);
INSERT INTO order_items (order_number, item_number, item_title, item_quantity, item_line_total_usd)
VALUES ('ORD-C', 'I-1', 'thing', 1, 100.00);
COMMIT;
"

# ──────────────────────────────────────────────────────────────────
# Case 8: item_subtotal NULL → no sum check, COMMIT.
# ──────────────────────────────────────────────────────────────────
expect_ok 8 "NULL item_subtotal disables sum check" "
BEGIN;
INSERT INTO orders (order_number, sold_by, order_total_usd, item_subtotal_usd)
VALUES ('ORD-D', 'shop1', 99.99, NULL);
INSERT INTO order_items (order_number, item_number, item_title, item_quantity, item_line_total_usd)
VALUES ('ORD-D', 'I-1', 'thing', 1, 10.00);
COMMIT;
DELETE FROM order_items WHERE order_number='ORD-D';
DELETE FROM orders WHERE order_number='ORD-D';
"

# ──────────────────────────────────────────────────────────────────
# Case 9: is_untracked=true with non-empty tracking_numbers → reject.
# ──────────────────────────────────────────────────────────────────
expect_err 9 "is_untracked=true forbids non-empty tracking_numbers" "orders_untracked_no_trackings" "
BEGIN;
INSERT INTO orders (order_number, sold_by, order_total_usd, is_untracked, tracking_numbers)
VALUES ('ORD-E', 'shop1', 10.00, true, ARRAY['1Z']);
ROLLBACK;
"

# ──────────────────────────────────────────────────────────────────
# Case 10: duplicate tracking number inside array → reject.
# ──────────────────────────────────────────────────────────────────
expect_err 10 "tracking_numbers must be unique inside array" "orders_tracking_unique" "
BEGIN;
INSERT INTO orders (order_number, sold_by, order_total_usd, tracking_numbers)
VALUES ('ORD-F', 'shop1', 10.00, ARRAY['1Z','1Z']);
ROLLBACK;
"

# ──────────────────────────────────────────────────────────────────
# Case 11: deleted_at without deleted_reason → reject.
# ──────────────────────────────────────────────────────────────────
expect_err 11 "deleted_at requires deleted_reason and vice versa" "orders_soft_delete_pair" "
BEGIN;
INSERT INTO orders (order_number, sold_by, order_total_usd, deleted_at)
VALUES ('ORD-G', 'shop1', 10.00, now());
ROLLBACK;
"

# ──────────────────────────────────────────────────────────────────
# Case 12: duplicate refund (same order, amount, date, note) → reject.
# ──────────────────────────────────────────────────────────────────
expect_err 12 "duplicate refund row blocked by unique index" "order_refunds_dedup_idx" "
BEGIN;
INSERT INTO orders (order_number, sold_by, order_total_usd) VALUES ('ORD-H', 'shop1', 10.00);
INSERT INTO order_refunds (order_number, refund_amount_usd, refund_date, refund_note)
VALUES ('ORD-H', 1.00, '2026-01-01', 'r');
INSERT INTO order_refunds (order_number, refund_amount_usd, refund_date, refund_note)
VALUES ('ORD-H', 1.00, '2026-01-01', 'r');
ROLLBACK;
"

# ──────────────────────────────────────────────────────────────────
# Case 13: app.source not set → change_log.source is NULL, no error.
# ──────────────────────────────────────────────────────────────────
expect_ok 13 "app.source unset → log.source IS NULL, write succeeds" "
BEGIN;
INSERT INTO orders (order_number, sold_by, order_total_usd) VALUES ('ORD-I', 'shop1', 10.00);
DO \$\$
DECLARE v_source text; v_count int;
BEGIN
  SELECT source, count(*) OVER () INTO v_source, v_count FROM order_change_log WHERE order_number='ORD-I' LIMIT 1;
  IF v_source IS NOT NULL THEN
    RAISE EXCEPTION 'expected NULL source, got %', v_source;
  END IF;
  IF v_count = 0 THEN
    RAISE EXCEPTION 'expected at least one log row';
  END IF;
END\$\$;
ROLLBACK;
"

# ──────────────────────────────────────────────────────────────────
# Case 14: SKIP LOCKED hands off rows between two parallel sessions.
# ──────────────────────────────────────────────────────────────────
"${DB}" -q -c "
INSERT INTO screenshots (sha256, bytes, mime, byte_size) VALUES
  ('${S1}', E'\\\\x01', 'image/png', 1),
  ('${S2}', E'\\\\x02', 'image/png', 1);
" >/dev/null

# Session A holds the first row for ~3s, session B should pick the other.
"${DB}" -tAq -c "BEGIN; SELECT sha256 FROM screenshots WHERE ocr_status='pending' ORDER BY sha256 FOR UPDATE SKIP LOCKED LIMIT 1; SELECT pg_sleep(3); ROLLBACK;" >/tmp/skipA.out 2>&1 &
BG=$!
sleep 1
"${DB}" -tAq -c "BEGIN; SELECT sha256 FROM screenshots WHERE ocr_status='pending' ORDER BY sha256 FOR UPDATE SKIP LOCKED LIMIT 1; ROLLBACK;" >/tmp/skipB.out 2>&1
wait $BG

SESSA=$(grep -oE '^[0-9a-f]{64}$' /tmp/skipA.out | head -1)
SESSB=$(grep -oE '^[0-9a-f]{64}$' /tmp/skipB.out | head -1)
if [[ -n "$SESSA" && -n "$SESSB" && "$SESSA" != "$SESSB" ]]; then
    echo "OK   case 14 — SKIP LOCKED two sessions got distinct rows"
    PASS=$((PASS+1))
else
    echo "FAIL case 14 — SKIP LOCKED, sessA='${SESSA}' sessB='${SESSB}'"
    FAIL=$((FAIL+1))
fi

# Final cleanup of fixtures.
"${DB}" -q -c "
DELETE FROM order_change_log;
DELETE FROM order_screenshot_links;
DELETE FROM order_refunds;
DELETE FROM order_items;
DELETE FROM orders;
DELETE FROM raw_ocr;
DELETE FROM screenshots;
" >/dev/null

echo
echo "--- ${PASS} passed, ${FAIL} failed ---"
exit $FAIL
