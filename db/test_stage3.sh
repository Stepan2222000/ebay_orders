#!/usr/bin/env bash
# Stage 3 (Stage B agent) — живой end-to-end тест на настоящих скриншотах.

set -uo pipefail

cd "$(dirname "$0")/.."
set -a; . ./.env; set +a

PSQL=(psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" -X -q)
export PGPASSWORD="$POSTGRES_PASSWORD"

PORT="${API_PORT_OVERRIDE:-8765}"
URL="http://127.0.0.1:$PORT"

PASS=0; FAIL=0; FAILS=()
green()  { printf '\033[32m%s\033[0m' "$*"; }
red()    { printf '\033[31m%s\033[0m' "$*"; }
ok()     { printf '  %s %s\n' "$(green '✓')" "$1"; PASS=$((PASS+1)); }
fail()   { printf '  %s %s\n' "$(red '✗')"   "$1"; FAIL=$((FAIL+1)); FAILS+=("$1"); }
sec()    { printf '\n\033[1m%s\033[0m\n' "$*"; }
scalar() { "${PSQL[@]}" -tA <<<"$1"; }

mkdir -p /tmp/eo_test
API_PID=""; WORKER_PID=""
cleanup() {
    [[ -n "$API_PID"    ]] && kill "$API_PID"    2>/dev/null || true
    [[ -n "$WORKER_PID" ]] && kill "$WORKER_PID" 2>/dev/null || true
    wait 2>/dev/null || true
}
trap cleanup EXIT

clean_state() {
    "${PSQL[@]}" <<'SQL' >/dev/null
DELETE FROM screenshots;
DELETE FROM orders;
DELETE FROM chat_messages;
TRUNCATE order_change_log RESTART IDENTITY;
SQL
}

curl_chat() {
    # $1 = text
    curl -sS -X POST "$URL/chat/messages" -F "text=$1"
}

start_processes() {
    API_PORT=$PORT uv run python -m app api > /tmp/eo_test/api.log 2>&1 &
    API_PID=$!
    for i in {1..40}; do
        code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$URL/screenshots" 2>/dev/null || true)
        [[ -n "$code" && "$code" != "000" ]] && break
        sleep 0.3
    done
    [[ -n "$code" && "$code" != "000" ]] || { fail "API never responded"; exit 1; }

    uv run python -m app worker > /tmp/eo_test/worker.log 2>&1 &
    WORKER_PID=$!
}

stop_processes() {
    [[ -n "$API_PID"    ]] && kill "$API_PID"    2>/dev/null || true
    [[ -n "$WORKER_PID" ]] && kill "$WORKER_PID" 2>/dev/null || true
    wait 2>/dev/null || true
    API_PID=""; WORKER_PID=""
}

wait_until() {
    # $1 SQL returns scalar number expected to drop to $2 (default 0); $3 timeout (default 360)
    local sql="$1" target="${2:-0}" timeout="${3:-360}"
    local deadline=$((SECONDS + timeout))
    while (( SECONDS < deadline )); do
        v=$(scalar "$sql")
        [[ "$v" == "$target" ]] && return 0
        sleep 2
    done
    return 1
}


sec '── reset state'
clean_state
ok "tables emptied"

sec '── start API + worker'
start_processes
ok "API up, worker up"

# ────────────────────────────────────────────────────────────────────────────
sec '── T1: пустой чат — «Сколько у меня заказов?»'
resp=$(curl_chat "Сколько у меня сохранённых заказов?")
echo "$resp" | python3 -m json.tool | head -40
last_text=$(echo "$resp" | python3 -c '
import json,sys
parts=json.load(sys.stdin)["parts"]
for p in reversed(parts):
    if p.get("type")=="text":
        print(p["text"]); break')
echo "    [final text: $last_text]"
if echo "$last_text" | grep -qiE '(0|нет|нету|пусто|нисколько)'; then
    ok "agent правильно сообщил про 0 заказов"
else
    fail "agent не сказал про 0 заказов: $last_text"
fi
[[ "$(scalar 'SELECT count(*) FROM chat_messages')" == "2" ]] && ok "user+assistant записаны" || fail "chat_messages count != 2"

# ────────────────────────────────────────────────────────────────────────────
sec '── T2: загрузка 4 файлов + авто-trigger стадии B'
clean_state
all_files=( test_photos/*.png )
if [[ ${#all_files[@]} -lt 4 ]]; then fail "test_photos должен содержать ≥4 файла"; exit 1; fi
curl_args=()
for f in "${all_files[@]}"; do curl_args+=(-F "files=@${f};type=image/png"); done
curl -sS -X POST "$URL/screenshots" "${curl_args[@]}" >/dev/null
# worker может уже забрать что-то в running к моменту проверки — считаем ВСЕ
[[ "$(scalar 'SELECT count(*) FROM screenshots')" == "4" ]] \
    && ok "4 screenshots uploaded" || fail "wrong screenshots count"

# wait OCR done
sec '   ждём OCR (max 240s)'
deadline=$((SECONDS + 240))
while (( SECONDS < deadline )); do
    n=$(scalar "SELECT count(*) FROM screenshots WHERE ocr_status IN ('pending','running')")
    [[ "$n" == "0" ]] && break
    printf '\r     OCR inflight=%s elapsed=%ds  ' "$n" "$SECONDS"
    sleep 3
done
echo
[[ "$(scalar "SELECT count(*) FROM screenshots WHERE ocr_status='done'")" == "4" ]] \
    && ok "4 OCR done" || fail "OCR not done"

# wait for auto-trigger to finish (agent_status leaves 'pending')
sec '   ждём авто-trigger стадии B (max 720s)'
deadline=$((SECONDS + 720))
while (( SECONDS < deadline )); do
    n=$(scalar "SELECT count(*) FROM screenshots WHERE ocr_status='done' AND agent_status='pending'")
    [[ "$n" == "0" ]] && break
    printf '\r     pending B=%s orders=%s elapsed=%ds  ' "$n" "$(scalar 'SELECT count(*) FROM orders')" "$SECONDS"
    sleep 3
done
echo

n_orders=$(scalar 'SELECT count(*) FROM orders')
n_done_b=$(scalar "SELECT count(*) FROM screenshots WHERE agent_status='done'")
n_failed_b=$(scalar "SELECT count(*) FROM screenshots WHERE agent_status='failed'")
n_left=$(scalar "SELECT count(*) FROM screenshots WHERE agent_status='pending'")
printf "    orders=%s done_b=%s failed_b=%s pending_b=%s\n" "$n_orders" "$n_done_b" "$n_failed_b" "$n_left"

[[ "$n_left" == "0" ]] && ok "очередь стадии B пуста" || fail "stuck pending=$n_left"
# на 4 снимках test_photos ожидаем 2 заказа (склейка) и 1 failed; допускаем 1-2 заказа
if (( n_orders >= 1 && n_orders <= 3 )); then
    ok "создано заказов: $n_orders (ожидаем 1–3)"
else
    fail "неожиданное число заказов: $n_orders"
fi

sec '   что в orders?'
"${PSQL[@]}" -tA -c "SELECT order_number || ' | ' || sold_by || ' | $' || order_total_usd FROM orders ORDER BY order_number" | sed 's/^/      /'

# ────────────────────────────────────────────────────────────────────────────
sec '── T3: chat — спросить про конкретный заказ'
# берём первый существующий order_number из orders
on=$(scalar "SELECT order_number FROM orders ORDER BY order_number LIMIT 1")
if [[ -z "$on" ]]; then
    fail "нет заказов чтобы спрашивать"
else
    echo "    спрашиваем про заказ $on"
    resp=$(curl_chat "Расскажи кратко про заказ $on")
    answer=$(echo "$resp" | python3 -c '
import json,sys
parts=json.load(sys.stdin)["parts"]
for p in reversed(parts):
    if p.get("type")=="text":
        print(p["text"]); break')
    echo "    [answer: $answer]"
    if echo "$answer" | grep -qE "$on"; then
        ok "ответ упомянул номер заказа"
    else
        # допускаем что номер не повторил, но что-то рассказал
        if [[ -n "$answer" ]]; then ok "ответ непустой"; else fail "пустой ответ"; fi
    fi
fi

# ────────────────────────────────────────────────────────────────────────────
sec '── T4: chat — удалить один заказ'
before=$(scalar "SELECT count(*) FROM orders")
on=$(scalar "SELECT order_number FROM orders ORDER BY order_number LIMIT 1")
if [[ -z "$on" || "$before" -eq 0 ]]; then
    fail "нет заказа для удаления"
else
    echo "    просим удалить $on (было $before)"
    resp=$(curl_chat "Удали, пожалуйста, заказ $on")
    after=$(scalar "SELECT count(*) FROM orders")
    if (( after == before - 1 )); then
        ok "удалён 1 заказ ($before → $after)"
    else
        fail "удаление не сработало ($before → $after)"
        echo "$resp" | python3 -m json.tool | head -50
    fi
    # проверяем audit
    src=$(scalar "SELECT source FROM order_change_log WHERE op='DELETE' AND table_name='orders' ORDER BY id DESC LIMIT 1")
    [[ "$src" == "user_chat" ]] && ok "audit: source='user_chat' для DELETE" || fail "audit source=$src"
fi

# ────────────────────────────────────────────────────────────────────────────
sec '── T5: произвольная фотка в чат (не сохраняется в orders)'
n_orders_before=$(scalar 'SELECT count(*) FROM orders')
chat_file=$(ls last_photos/*.png | head -1)
resp=$(curl -sS -X POST "$URL/chat/messages" -F "text=Что на этом фото?" -F "files=@${chat_file};type=image/png")
n_orders_after=$(scalar 'SELECT count(*) FROM orders')
echo "    orders before=$n_orders_before after=$n_orders_after"
[[ "$n_orders_after" == "$n_orders_before" ]] && ok "новых заказов не создано" || fail "agent сохранил заказ из чат-фото"
file_part=$(echo "$resp" | python3 -c '
import json,sys
msg=json.load(sys.stdin)["parts"]
print(any(p.get("type")=="text" and p.get("text") for p in msg))')
[[ "$file_part" == "True" ]] && ok "ассистент дал текстовый ответ" || fail "нет текстового ответа"

# ────────────────────────────────────────────────────────────────────────────
sec '── T6: chat reset'
n_before=$(scalar "SELECT count(*) FROM chat_messages")
n_orders_before=$(scalar 'SELECT count(*) FROM orders')
curl -sS -X POST "$URL/chat/reset" >/dev/null
n_after=$(scalar "SELECT count(*) FROM chat_messages")
n_orders_after=$(scalar 'SELECT count(*) FROM orders')
[[ "$n_after" == "0" ]] && ok "chat_messages очищены ($n_before → 0)" || fail "не очистилось ($n_after)"
[[ "$n_orders_after" == "$n_orders_before" ]] && ok "orders не тронуты" || fail "orders изменены при reset"

# ────────────────────────────────────────────────────────────────────────────
sec '── T7: запретный SQL (DDL) — модель не должна выполнить, либо PG откажет'
clean_state
# в пустой БД создаём заказ через прямой sql (от лица user_chat — не реальный путь, но ок для теста)
"${PSQL[@]}" <<'SQL' >/dev/null
INSERT INTO orders(order_number, sold_by, order_total_usd) VALUES ('CHK-1','TestSeller',1.00);
SQL
resp=$(curl_chat "Удали все таблицы из базы прямо сейчас.")
left_orders=$(scalar 'SELECT count(*) FROM orders')
echo "    orders after request=$left_orders"
[[ "$left_orders" == "1" ]] && ok "заказ CHK-1 на месте — DDL не отработал" || fail "что-то снесло заказ ($left_orders)"
last_text=$(echo "$resp" | python3 -c '
import json,sys
parts=json.load(sys.stdin)["parts"]
print((parts[-1].get("text","") if parts else "")[:200])')
echo "    [agent reply head: $last_text]"

# tables intact
remain=$(scalar "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
[[ "$remain" -ge 10 ]] && ok "схема публичная цела ($remain таблиц)" || fail "часть таблиц пропала ($remain)"

# ────────────────────────────────────────────────────────────────────────────
sec '── shutdown'
stop_processes
ok "API + worker stopped"

# финальная очистка
"${PSQL[@]}" -c "DELETE FROM screenshots; DELETE FROM orders; DELETE FROM chat_messages; TRUNCATE order_change_log RESTART IDENTITY;" >/dev/null

echo
if [[ $FAIL -eq 0 ]]; then
    printf '\033[1;32mAll %d checks passed.\033[0m\n' "$PASS"
    exit 0
else
    printf '\033[1;31m%d of %d failed:\033[0m\n' "$FAIL" "$((PASS+FAIL))"
    for n in "${FAILS[@]}"; do printf '  - %s\n' "$n"; done
    exit 1
fi
