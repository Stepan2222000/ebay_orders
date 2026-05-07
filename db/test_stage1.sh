#!/usr/bin/env bash
# Полный тест Stage 1: схема + инварианты + журнал + pg_trgm.
# Каждый тест подаётся живой БД, считается pass/fail, в конце — итог.

set -uo pipefail

cd "$(dirname "$0")/.."
set -a; . ./.env; set +a

PSQL=(psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" -X -q -v ON_ERROR_STOP=1)
PSQL_RAW=(psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" -X -q)
export PGPASSWORD="$POSTGRES_PASSWORD"

PASS=0
FAIL=0
FAILED_NAMES=()

bold()   { printf '\033[1m%s\033[0m' "$*"; }
green()  { printf '\033[32m%s\033[0m' "$*"; }
red()    { printf '\033[31m%s\033[0m' "$*"; }
yellow() { printf '\033[33m%s\033[0m' "$*"; }

ok()   { printf '  %s %s\n'   "$(green '✓')" "$1"; PASS=$((PASS+1)); }
fail() { printf '  %s %s\n'   "$(red '✗')"   "$1"; FAIL=$((FAIL+1)); FAILED_NAMES+=("$1"); shift; [[ $# -gt 0 ]] && printf '    %s\n' "$@"; }

clean_state() {
    "${PSQL[@]}" <<'SQL' >/dev/null
DELETE FROM orders;
DELETE FROM raw_ocr;
DELETE FROM screenshots;
DELETE FROM chat_messages;
TRUNCATE order_change_log RESTART IDENTITY;
SQL
}

# Прогоняет SQL и возвращает 0 при успехе, 1 при ошибке.
run_sql() {
    "${PSQL[@]}" <<<"$1" >/dev/null 2>&1
}

# Прогоняет SQL и ОЖИДАЕТ ошибку. Возвращает 0 если действительно упало,
# и опционально проверяет, что stderr содержит подстроку.
run_sql_expect_fail() {
    local sql="$1" needle="${2:-}"
    local err
    err=$( { "${PSQL[@]}" <<<"$sql" >/dev/null; } 2>&1 ) && return 1
    if [[ -n "$needle" ]] && ! [[ "$err" == *"$needle"* ]]; then
        echo "expected error to contain: $needle"
        echo "got: $err"
        return 1
    fi
    return 0
}

scalar() {
    "${PSQL[@]}" -tA <<<"$1"
}

# ═══ schema sanity ═══════════════════════════════════════════════════════
section_schema() {
    bold '── schema sanity'; echo
    local missing=0 t
    for t in orders order_items order_refunds order_tracking_numbers \
             screenshots raw_ocr chat_sessions chat_messages \
             order_change_log schema_migrations; do
        local n; n=$(scalar "SELECT 1 FROM information_schema.tables WHERE table_name='$t' AND table_schema='public';")
        if [[ "$n" == "1" ]]; then
            :
        else
            missing=1
            fail "table $t missing"
        fi
    done
    [[ $missing -eq 0 ]] && ok "all 10 tables exist"

    local ext; ext=$(scalar "SELECT 1 FROM pg_extension WHERE extname='pg_trgm';")
    [[ "$ext" == "1" ]] && ok "pg_trgm installed" || fail "pg_trgm missing"

    local enums; enums=$(scalar "SELECT count(*) FROM pg_type WHERE typname IN ('proc_status','change_source');")
    [[ "$enums" == "2" ]] && ok "enums proc_status & change_source exist" || fail "enums missing ($enums/2)"

    local trgs; trgs=$(scalar "SELECT count(*) FROM pg_trigger WHERE tgname IN ('trg_orders_invariants','trg_items_invariants','trg_tracks_invariants') AND tgdeferrable;")
    [[ "$trgs" == "3" ]] && ok "3 deferrable invariant triggers exist" || fail "deferrable triggers missing ($trgs/3)"

    local sess; sess=$(scalar "SELECT session_id FROM chat_sessions;")
    [[ "$sess" == "default" ]] && ok "chat_sessions has single 'default' row" || fail "chat_sessions wrong ($sess)"
}

# ═══ happy path ══════════════════════════════════════════════════════════
section_happy() {
    bold '── happy path'; echo
    clean_state

    if run_sql "
BEGIN;
SET LOCAL app.source = 'screenshot';
INSERT INTO orders(order_number, sold_by, ordered_at, order_total_usd,
                   item_subtotal_usd, shipping_usd, sales_tax_usd,
                   delivery_status, delivered_date, shipping_service)
VALUES ('123-456-7890','BestSeller','2026-04-01 10:00+00',
        165.00, 150.00, 5.00, 10.00,
        'Delivered','2026-04-05','USPS Ground Advantage');
INSERT INTO order_items(order_id, item_number, item_title, item_quantity, item_line_total_usd)
SELECT order_id,'I-001','Wireless mouse',1,90.00 FROM orders WHERE order_number='123-456-7890';
INSERT INTO order_items(order_id, item_number, item_title, item_quantity, item_line_total_usd)
SELECT order_id,'I-002','USB cable',2,60.00 FROM orders WHERE order_number='123-456-7890';
INSERT INTO order_tracking_numbers(order_id, tracking_number)
SELECT order_id,'9400111899560123456789' FROM orders WHERE order_number='123-456-7890';
INSERT INTO order_refunds(order_id, refund_amount_usd, refund_date, refund_note)
SELECT order_id, 5.00, '2026-04-10', 'goodwill' FROM orders WHERE order_number='123-456-7890';
COMMIT;
"; then ok "insert order + 2 items + tracking + refund commits"; else fail "happy path commit failed"; return; fi

    local cnt
    cnt=$(scalar "SELECT count(*) FROM order_items   WHERE order_id=(SELECT order_id FROM orders WHERE order_number='123-456-7890');")
    [[ "$cnt" == "2" ]] && ok "items: 2 saved" || fail "items: $cnt expected 2"

    cnt=$(scalar "SELECT count(*) FROM order_refunds WHERE order_id=(SELECT order_id FROM orders WHERE order_number='123-456-7890');")
    [[ "$cnt" == "1" ]] && ok "refund saved" || fail "refunds: $cnt expected 1"

    cnt=$(scalar "SELECT count(*) FROM order_tracking_numbers WHERE order_id=(SELECT order_id FROM orders WHERE order_number='123-456-7890');")
    [[ "$cnt" == "1" ]] && ok "tracking number saved" || fail "tracking: $cnt expected 1"

    cnt=$(scalar "SELECT count(*) FROM order_change_log WHERE source='screenshot';")
    [[ "$cnt" == "5" ]] && ok "audit log: 5 INSERT rows with source='screenshot'" || fail "audit log: $cnt expected 5"
}

# ═══ deferred invariants — bad sums ══════════════════════════════════════
section_bad_sums() {
    bold '── deferred sum invariants'; echo
    clean_state

    if run_sql_expect_fail "
BEGIN;
INSERT INTO orders(order_number, sold_by, order_total_usd,
                   item_subtotal_usd, shipping_usd, sales_tax_usd)
VALUES ('B-1','S',999.00,100.00,1.00,1.00);
INSERT INTO order_items(order_id, item_number, item_title, item_quantity, item_line_total_usd)
SELECT order_id,'I','x',1,100.00 FROM orders WHERE order_number='B-1';
COMMIT;
" "item_subtotal+shipping+tax"; then ok "components ≠ total → ERROR at COMMIT"; else fail "should reject components ≠ total"; fi

    [[ "$(scalar "SELECT count(*) FROM orders WHERE order_number='B-1';")" == "0" ]] \
        && ok "rolled back: B-1 not saved" || fail "B-1 leaked through"

    if run_sql_expect_fail "
BEGIN;
INSERT INTO orders(order_number, sold_by, order_total_usd, item_subtotal_usd)
VALUES ('B-2','S',100.00,100.00);
INSERT INTO order_items(order_id, item_number, item_title, item_quantity, item_line_total_usd)
SELECT order_id,'I','x',1,42.00 FROM orders WHERE order_number='B-2';
COMMIT;
" "sum(order_items.item_line_total_usd)"; then ok "sum(items) ≠ subtotal → ERROR at COMMIT"; else fail "should reject sum(items) ≠ subtotal"; fi
}

# ═══ partial data is OK ══════════════════════════════════════════════════
section_partial() {
    bold '── partial data is allowed'; echo
    clean_state

    if run_sql "
BEGIN;
INSERT INTO orders(order_number, sold_by, order_total_usd, item_subtotal_usd)
VALUES ('P-1','S',100.00,100.00);
INSERT INTO order_items(order_id, item_number, item_title, item_quantity, item_line_total_usd)
SELECT order_id,'I-1','x',1,100.00 FROM orders WHERE order_number='P-1';
COMMIT;
"; then ok "subtotal=total without shipping/tax → OK"; else fail "rejected unexpectedly"; fi

    if run_sql "
BEGIN;
INSERT INTO orders(order_number, sold_by, order_total_usd) VALUES ('P-2','S',50.00);
COMMIT;
"; then ok "order without subtotal/items → OK (Stage A may have missed components)"; else fail "rejected"; fi
}

# ═══ is_untracked vs tracking_numbers ════════════════════════════════════
section_untracked() {
    bold '── is_untracked invariant'; echo
    clean_state

    if run_sql_expect_fail "
BEGIN;
INSERT INTO orders(order_number, sold_by, order_total_usd, is_untracked)
VALUES ('U-1','S',10.00,true);
INSERT INTO order_tracking_numbers(order_id, tracking_number)
SELECT order_id,'1Z999' FROM orders WHERE order_number='U-1';
COMMIT;
" "is_untracked=true but tracking_numbers exist"; then ok "is_untracked=true + tracking → ERROR"; else fail "should reject"; fi

    if run_sql "
BEGIN;
INSERT INTO orders(order_number, sold_by, order_total_usd, is_untracked)
VALUES ('U-2','S',10.00,true);
COMMIT;
"; then ok "is_untracked=true without tracking → OK"; else fail "rejected unexpectedly"; fi
}

# ═══ forbidden columns physically don't exist ════════════════════════════
section_forbidden() {
    bold '── forbidden columns are absent'; echo

    local cols
    cols=$(scalar "
SELECT count(*) FROM information_schema.columns
WHERE table_schema='public'
  AND column_name IN ('currency','payment_method_brand','payment_method_last4',
                      'payment_payer_name','payment_amount_usd','payment_at',
                      'paid_date','tracking_available_date',
                      'estimated_delivery_from','estimated_delivery_to',
                      'carrier','page_type');")
    [[ "$cols" == "0" ]] && ok "no forbidden columns in any table" || fail "found $cols forbidden columns"

    if run_sql_expect_fail "INSERT INTO orders(order_number,sold_by,order_total_usd,currency) VALUES ('X','Y',1,'USD');" \
        "column \"currency\""; then ok "INSERT into orders.currency rejected"; else fail "missing column not rejected"; fi
}

# ═══ row-level CHECK — negative amounts, zero quantity, scale ════════════
section_row_checks() {
    bold '── per-row CHECK constraints'; echo
    clean_state

    if run_sql_expect_fail "INSERT INTO orders(order_number,sold_by,order_total_usd) VALUES ('N-1','S',-1);" \
        "violates check"; then ok "order_total_usd < 0 rejected immediately"; else fail "negative total accepted"; fi

    run_sql "INSERT INTO orders(order_number,sold_by,order_total_usd) VALUES ('Q-1','S',10);" >/dev/null
    if run_sql_expect_fail "INSERT INTO order_items(order_id,item_number,item_title,item_quantity,item_line_total_usd)
                            SELECT order_id,'I','x',0,10 FROM orders WHERE order_number='Q-1';" \
        "violates check"; then ok "item_quantity=0 rejected immediately"; else fail "qty=0 accepted"; fi

    if run_sql_expect_fail "INSERT INTO order_refunds(order_id,refund_amount_usd)
                            SELECT order_id,0 FROM orders WHERE order_number='Q-1';" \
        "violates check"; then ok "refund_amount_usd=0 rejected"; else fail "refund=0 accepted"; fi

    if run_sql "INSERT INTO orders(order_number,sold_by,order_total_usd,shipping_usd) VALUES ('F-1','S',10.00,0.00);"; then
        ok "shipping_usd=0.00 (Free) accepted"
    else fail "Free shipping rejected"; fi
}

# ═══ DELETE cascade ══════════════════════════════════════════════════════
section_cascade() {
    bold '── DELETE cascade & screenshot SET NULL'; echo
    clean_state

    run_sql "
BEGIN;
SET LOCAL app.source='screenshot';
INSERT INTO orders(order_number,sold_by,order_total_usd) VALUES ('D-1','S',10.00);
INSERT INTO order_items(order_id,item_number,item_title,item_quantity,item_line_total_usd)
SELECT order_id,'I','x',1,10.00 FROM orders WHERE order_number='D-1';
INSERT INTO order_refunds(order_id,refund_amount_usd)
SELECT order_id,1.00 FROM orders WHERE order_number='D-1';
INSERT INTO order_tracking_numbers(order_id,tracking_number)
SELECT order_id,'T-1' FROM orders WHERE order_number='D-1';
INSERT INTO screenshots(sha256,byte_size,mime_type,bytes,order_id)
SELECT decode(repeat('aa',32),'hex'), 100, 'image/png', '\xdeadbeef', order_id
FROM orders WHERE order_number='D-1';
INSERT INTO raw_ocr(sha256,model,raw_json) VALUES (decode(repeat('aa',32),'hex'),'kimi','{}'::jsonb);
COMMIT;
" >/dev/null

    if run_sql "BEGIN; SET LOCAL app.source='user_chat'; DELETE FROM orders WHERE order_number='D-1'; COMMIT;"; then
        ok "DELETE order succeeded"
    else fail "DELETE order failed"; fi

    [[ "$(scalar "SELECT count(*) FROM order_items   WHERE order_id NOT IN (SELECT order_id FROM orders);")" == "0" ]] \
        && ok "order_items cascade cleaned" || fail "items leaked"
    [[ "$(scalar "SELECT count(*) FROM order_refunds WHERE order_id NOT IN (SELECT order_id FROM orders);")" == "0" ]] \
        && ok "order_refunds cascade cleaned" || fail "refunds leaked"
    [[ "$(scalar "SELECT count(*) FROM order_tracking_numbers WHERE order_id NOT IN (SELECT order_id FROM orders);")" == "0" ]] \
        && ok "order_tracking_numbers cascade cleaned" || fail "tracks leaked"
    [[ "$(scalar "SELECT count(*) FROM screenshots WHERE order_id IS NOT NULL;")" == "0" ]] \
        && ok "screenshots.order_id → NULL on order delete" || fail "screenshot.order_id not nulled"
    [[ "$(scalar "SELECT count(*) FROM screenshots;")" == "1" ]] \
        && ok "screenshot row itself preserved" || fail "screenshot disappeared"
    [[ "$(scalar "SELECT count(*) FROM raw_ocr;")"     == "1" ]] \
        && ok "raw_ocr preserved (tied to screenshot, not order)" || fail "raw_ocr lost"

    local del_count
    del_count=$(scalar "SELECT count(*) FROM order_change_log WHERE op='DELETE' AND source='user_chat';")
    [[ "$del_count" == "4" ]] \
        && ok "audit: 4 DELETE rows with source='user_chat'" || fail "audit DELETE: $del_count expected 4"
}

# ═══ screenshot delete cascades raw_ocr but keeps order ══════════════════
section_screenshot_delete() {
    bold '── screenshot delete cascades raw_ocr'; echo
    clean_state

    run_sql "
INSERT INTO orders(order_number,sold_by,order_total_usd) VALUES ('S-1','S',10.00);
INSERT INTO screenshots(sha256,byte_size,mime_type,bytes,order_id)
SELECT decode(repeat('bb',32),'hex'), 100, 'image/png', '\xcafe', order_id
FROM orders WHERE order_number='S-1';
INSERT INTO raw_ocr(sha256,model,raw_json) VALUES (decode(repeat('bb',32),'hex'),'kimi','{}'::jsonb);
" >/dev/null

    run_sql "DELETE FROM screenshots WHERE sha256=decode(repeat('bb',32),'hex');" >/dev/null

    [[ "$(scalar "SELECT count(*) FROM raw_ocr;")"  == "0" ]] && ok "raw_ocr cascaded with screenshot" || fail "raw_ocr leaked"
    [[ "$(scalar "SELECT count(*) FROM orders WHERE order_number='S-1';")" == "1" ]] \
        && ok "order S-1 still alive" || fail "order vanished"
}

# ═══ screenshot dedup ════════════════════════════════════════════════════
section_dedup() {
    bold '── screenshot dedup by sha256'; echo
    clean_state

    run_sql "INSERT INTO screenshots(sha256,byte_size,mime_type,bytes)
             VALUES (decode(repeat('cc',32),'hex'),10,'image/png','\xff');" >/dev/null

    if run_sql_expect_fail "INSERT INTO screenshots(sha256,byte_size,mime_type,bytes)
                            VALUES (decode(repeat('cc',32),'hex'),10,'image/png','\xff');" \
        "duplicate key"; then ok "second naked INSERT same sha256 → ERROR"; else fail "duplicate not rejected"; fi

    if run_sql "INSERT INTO screenshots(sha256,byte_size,mime_type,bytes)
                VALUES (decode(repeat('cc',32),'hex'),10,'image/png','\xff')
                ON CONFLICT(sha256) DO NOTHING;"; then
        ok "ON CONFLICT DO NOTHING upload-twice path works"
    else fail "ON CONFLICT path errored"; fi

    [[ "$(scalar "SELECT count(*) FROM screenshots;")" == "1" ]] \
        && ok "still one row after duplicate" || fail "duplicate leaked"

    if run_sql_expect_fail "INSERT INTO screenshots(sha256,byte_size,mime_type,bytes)
                            VALUES (decode(repeat('dd',16),'hex'),10,'image/png','\xff');" \
        "screenshots_sha256_check"; then ok "sha256 of wrong length rejected (CHECK)"; else fail "short sha accepted"; fi
}

# ═══ pg_trgm fuzzy match ═════════════════════════════════════════════════
section_trgm() {
    bold '── pg_trgm similarity'; echo

    local s; s=$(scalar "SELECT similarity('Best Buy','best-buy');")
    awk -v v="$s" 'BEGIN{ exit !(v+0 >= 0.3) }' \
        && ok "similarity('Best Buy','best-buy')=$s ≥ 0.3" \
        || fail "similarity too low: $s"

    [[ "$(scalar "SELECT 'Best Buy' % 'best-buy';")" == "t" ]] \
        && ok "operator % matches above default threshold" || fail "% match failed"

    local idx
    idx=$(scalar "SELECT count(*) FROM pg_indexes
                  WHERE indexname IN ('orders_sold_by_trgm_idx','order_items_title_trgm_idx');")
    [[ "$idx" == "2" ]] && ok "GIN trigram indexes installed" || fail "trgm indexes missing ($idx/2)"
}

# ═══ audit only on clean tables ══════════════════════════════════════════
section_audit_scope() {
    bold '── audit log only on clean tables'; echo
    clean_state

    run_sql "
INSERT INTO screenshots(sha256,byte_size,mime_type,bytes,ocr_status)
VALUES (decode(repeat('ee',32),'hex'),10,'image/png','\xff','running');
UPDATE screenshots SET ocr_status='done' WHERE sha256=decode(repeat('ee',32),'hex');
INSERT INTO chat_messages(session_id,role,parts) VALUES ('default','user','[{\"type\":\"text\",\"text\":\"hi\"}]'::jsonb);
" >/dev/null

    [[ "$(scalar "SELECT count(*) FROM order_change_log;")" == "0" ]] \
        && ok "no audit rows for screenshots/raw_ocr/chat changes" || fail "leaked rows"
}

# ═══ source NULL when SET LOCAL not used ═════════════════════════════════
section_audit_source_null() {
    bold '── audit source NULL without SET LOCAL'; echo
    clean_state

    run_sql "INSERT INTO orders(order_number,sold_by,order_total_usd) VALUES ('A-1','S',10);" >/dev/null
    [[ "$(scalar "SELECT count(*) FROM order_change_log WHERE source IS NULL;")" == "1" ]] \
        && ok "audit row with source=NULL written" || fail "missing audit row"
}

# ═══ chat parts must be jsonb array ══════════════════════════════════════
section_chat_parts() {
    bold '── chat_messages.parts must be jsonb array'; echo
    clean_state

    if run_sql_expect_fail "INSERT INTO chat_messages(session_id,role,parts) VALUES ('default','user','{\"type\":\"text\"}'::jsonb);" \
        "violates check"; then ok "object instead of array rejected"; else fail "object accepted"; fi

    if run_sql_expect_fail "INSERT INTO chat_messages(session_id,role,parts) VALUES ('default','tool','[]'::jsonb);" \
        "violates check"; then ok "role outside (user|assistant|system) rejected"; else fail "tool role accepted"; fi
}

# ═══ chat reset keeps orders & screenshots ═══════════════════════════════
section_chat_reset() {
    bold '── chat reset is independent of orders'; echo
    clean_state

    run_sql "
INSERT INTO orders(order_number,sold_by,order_total_usd) VALUES ('C-1','S',10);
INSERT INTO screenshots(sha256,byte_size,mime_type,bytes)
VALUES (decode(repeat('ff',32),'hex'),10,'image/png','\xff');
INSERT INTO chat_messages(session_id,role,parts) VALUES ('default','user','[]'::jsonb);
" >/dev/null

    run_sql "DELETE FROM chat_messages;" >/dev/null

    [[ "$(scalar "SELECT count(*) FROM orders WHERE order_number='C-1';")" == "1" ]] \
        && ok "order C-1 survived chat wipe" || fail "order lost"
    [[ "$(scalar "SELECT count(*) FROM screenshots;")" == "1" ]] \
        && ok "screenshot survived chat wipe" || fail "screenshot lost"
    [[ "$(scalar "SELECT count(*) FROM chat_messages;")" == "0" ]] \
        && ok "chat history empty" || fail "chat not empty"
    [[ "$(scalar "SELECT count(*) FROM chat_sessions;")" == "1" ]] \
        && ok "chat_sessions row preserved" || fail "session row lost"
}

# ═══ second chat_sessions row rejected ═══════════════════════════════════
section_session_singleton() {
    bold '── chat_sessions singleton'; echo

    if run_sql_expect_fail "INSERT INTO chat_sessions(session_id) VALUES ('other');" \
        "violates check"; then ok "non-default session_id rejected by CHECK"; else fail "second session accepted"; fi
}

# ═══ run all ═════════════════════════════════════════════════════════════
section_schema
section_happy
section_bad_sums
section_partial
section_untracked
section_forbidden
section_row_checks
section_cascade
section_screenshot_delete
section_dedup
section_trgm
section_audit_scope
section_audit_source_null
section_chat_parts
section_chat_reset
section_session_singleton

# Финальная очистка, чтобы база осталась пустой после прогона тестов.
clean_state

echo
if [[ $FAIL -eq 0 ]]; then
    bold "$(green "All $PASS checks passed.")"
    echo
    exit 0
else
    bold "$(red "$FAIL of $((PASS+FAIL)) checks failed:")"
    echo
    for n in "${FAILED_NAMES[@]}"; do printf '  - %s\n' "$n"; done
    exit 1
fi
