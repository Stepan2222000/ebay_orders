#!/usr/bin/env bash
# Stage 4 UI smoke через agent-browser.
# Подразумевает что FastAPI на :3051, Next.js dev на :3050 уже запущены и
# подключены к чистой dev-БД (или к БД с тестовыми данными — каждый сценарий
# чистит то, что ему нужно).

set -euo pipefail

cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
export PGPASSWORD="$POSTGRES_PASSWORD"

API="http://localhost:3051/api"
UI="http://localhost:3050"
SESSION="${EO_AGENT_SESSION:-eo}"
ART="tmp/stage4_ui"
mkdir -p "$ART"

PSQL=(psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" -X -tA)

ab() { agent-browser --session "$SESSION" "$@"; }
ev() { ab eval --stdin; }

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "  ok    $1"; }
fail() { FAIL=$((FAIL+1)); echo "  FAIL  $1"; }
section() { echo; echo "── $1 ──"; }
truncate_db() {
    "${PSQL[@]}" -c "TRUNCATE TABLE chat_messages, order_change_log, raw_ocr, screenshots, order_items, order_refunds, order_tracking_numbers, orders RESTART IDENTITY CASCADE" >/dev/null
}
expect() {
    local what="$1" ; shift
    local got="$1" ; shift
    local want="$1" ; shift || true
    if [[ "$got" == "$want" ]]; then ok "$what"; else fail "$what  expected=<$want> got=<$got>"; fi
}
expect_contains() {
    local what="$1" got="$2" needle="$3"
    if [[ "$got" == *"$needle"* ]]; then ok "$what"; else fail "$what  needle=<$needle> got=<$got>"; fi
}

# ── 0. подготовка
section "T0 baseline"
truncate_db
ab open "$UI" >/dev/null
ab wait --load networkidle >/dev/null
sleep 1
ab screenshot "$ART/00_initial.png" >/dev/null
got=$(ab eval 'getComputedStyle(document.body).backgroundColor' | tr -d '"')
expect "body на dark surface" "$got" "rgb(38, 38, 37)"

# ── 1. layout: dark, заголовок Fraunces
section "T1 layout"
got=$(ab eval 'getComputedStyle(document.querySelector("aside")).backgroundColor' | tr -d '"')
expect "sidebar product-sidebar" "$got" "rgb(34, 33, 29)"
got=$(ab eval 'getComputedStyle(document.querySelector("h1")).fontFamily.includes("Fraunces")' | tr -d '"')
expect "h1 Fraunces" "$got" "true"

# ── 2. EventSource: real-time счётчики
section "T2 SSE counters"
res=$(ev <<'JS'
(async () => {
  return await new Promise(resolve => {
    const es = new EventSource("http://localhost:3051/api/status/stream");
    const got = [];
    es.onmessage = e => got.push(e.data.length > 0);
    setTimeout(() => { resolve(got.length >= 1 ? "got_initial" : "no_messages"); es.close(); }, 2500);
  });
})();
JS
)
expect "SSE initial" "$(echo "$res" | tr -d '"')" "got_initial"

# ── 3. upload pack — счётчики бегут, карточки появляются
section "T3 upload"
truncate_db
ab open "$UI" >/dev/null
ab wait --load networkidle >/dev/null
sleep 1
shopt -s nullglob
files=( test_photos/*.png )
if (( ${#files[@]} == 0 )); then files=( last_photos/*.png ); fi
files=( "${files[@]:0:4}" )
args=()
for f in "${files[@]}"; do args+=( "-F" "files=@$f" ); done
curl -s -X POST "$API/screenshots" "${args[@]}" >/dev/null
n=${#files[@]}

# ждём пока в БД станет n=$n done
for i in {1..120}; do
    cnt=$("${PSQL[@]}" -c "SELECT count(*) FROM screenshots WHERE ocr_status='done'")
    [[ "$cnt" == "$n" ]] && break
    sleep 1
done
expect "OCR done в БД" "$cnt" "$n"

# ждём пока в UI появятся карточки
for i in {1..30}; do
    got_cards=$(ab eval 'document.querySelectorAll("[data-testid=screenshot-card]").length' | tr -d '"')
    [[ "$got_cards" == "$n" ]] && break
    sleep 1
done
expect "$n карточек в sidebar" "$got_cards" "$n"
ab screenshot "$ART/03_cards.png" >/dev/null

# ── 4. detail modal
section "T4 detail modal"
ab eval 'document.querySelector("[data-testid=screenshot-card]").click()' >/dev/null
sleep 1
got=$(ab eval 'document.querySelector("[data-testid=detail-modal]") ? "open" : "closed"' | tr -d '"')
expect "detail modal открыт" "$got" "open"
ab screenshot "$ART/04_detail.png" >/dev/null
# закрываем
ab eval 'document.querySelector("[data-testid=detail-modal]").click()' >/dev/null
sleep 0.5
got=$(ab eval 'document.querySelector("[data-testid=detail-modal]") ? "open" : "closed"' | tr -d '"')
expect "detail modal закрылся" "$got" "closed"

# ── 5. delete: карточка исчезает, raw_ocr каскад, заказ цел
section "T5 delete cascade"
sha=$("${PSQL[@]}" -c "SELECT encode(sha256,'hex') FROM screenshots ORDER BY created_at LIMIT 1")
orders_before=$("${PSQL[@]}" -c "SELECT count(*) FROM orders")
curl -s -X DELETE "$API/screenshots/$sha" >/dev/null
sleep 1
sc_after=$("${PSQL[@]}" -c "SELECT count(*) FROM screenshots WHERE encode(sha256,'hex')='$sha'")
ro_after=$("${PSQL[@]}" -c "SELECT count(*) FROM raw_ocr WHERE encode(sha256,'hex')='$sha'")
orders_after=$("${PSQL[@]}" -c "SELECT count(*) FROM orders")
expect "screenshot удалён"        "$sc_after" "0"
expect "raw_ocr каскад"           "$ro_after" "0"
expect "заказ цел"                "$orders_after" "$orders_before"

# ── 6. real-time push: счётчик уменьшился без reload
section "T6 SSE after delete"
sleep 1
ui_done=$(ab eval 'JSON.parse(document.querySelector("[data-testid=stats]").dataset.json).done' | tr -d '"')
db_done=$("${PSQL[@]}" -c "SELECT count(*) FROM screenshots WHERE ocr_status='done'")
expect "stats.done == db.done" "$ui_done" "$db_done"

# ── 7. theme toggle: light + persistent (после перезагрузки)
section "T7 theme"
ab eval 'document.querySelector("[data-testid=theme-toggle]").click()' >/dev/null
sleep 0.3
got=$(ab eval 'document.documentElement.dataset.theme' | tr -d '"')
expect "light после toggle" "$got" "light"
got=$(ab eval 'getComputedStyle(document.body).backgroundColor' | tr -d '"')
expect "body cream"  "$got" "rgb(250, 249, 245)"
ab screenshot "$ART/07_light.png" >/dev/null
ab open "$UI" >/dev/null
ab wait --load networkidle >/dev/null
sleep 1
got=$(ab eval 'document.documentElement.dataset.theme' | tr -d '"')
expect "light persists" "$got" "light"
# вернёмся в dark
ab eval 'document.querySelector("[data-testid=theme-toggle]").click()' >/dev/null

# ── 8. coral-only-CTA визуальная регрессия
section "T8 coral discipline"
got=$(ev <<'JS'
(() => {
  const coral = "rgb(204, 120, 92)";
  const offenders = [];
  document.querySelectorAll("button,a").forEach(el => {
    const c = getComputedStyle(el).backgroundColor;
    if (c === coral && !el.dataset.testid?.startsWith("send")) {
      offenders.push((el.dataset.testid || el.tagName) + ":" + c);
    }
  });
  return offenders.length ? offenders.join(",") : "ok";
})();
JS
)
got=$(echo "$got" | tr -d '"')
expect "coral только на send" "$got" "ok"

# ── 9. dedup при повторной загрузке
section "T9 dedup"
truncate_db
sleep 0.5
files=( test_photos/*.png last_photos/*.png )
files=( "${files[@]:0:2}" )
args=()
for f in "${files[@]}"; do args+=( "-F" "files=@$f" ); done
first=$(curl -s -X POST "$API/screenshots" "${args[@]}")
second=$(curl -s -X POST "$API/screenshots" "${args[@]}")
n_dup=$(echo "$second" | python3 -c 'import json,sys; print(sum(1 for s in json.load(sys.stdin)["screenshots"] if s["status"]=="duplicate"))')
expect "повторная загрузка → duplicate" "$n_dup" "${#files[@]}"

# ── 10. reset chat
section "T10 reset chat"
"${PSQL[@]}" -c "INSERT INTO chat_messages(session_id,role,parts) VALUES ('default','user','[{\"type\":\"text\",\"text\":\"hi\"}]')" >/dev/null
"${PSQL[@]}" -c "INSERT INTO chat_messages(session_id,role,parts) VALUES ('default','assistant','[{\"type\":\"text\",\"text\":\"hello\"}]')" >/dev/null
ab open "$UI" >/dev/null
ab wait --load networkidle >/dev/null
sleep 1
n=$(ab eval 'document.querySelectorAll("[data-role=user], [data-role=assistant]").length' | tr -d '"')
expect "история подгрузилась" "$n" "2"
ab eval 'document.querySelector("[data-testid=reset-chat]").click()' >/dev/null
sleep 0.3
ab eval 'document.querySelector("[data-testid=confirm-yes]").click()' >/dev/null
for i in {1..10}; do
    n=$(ab eval 'document.querySelectorAll("[data-role=user], [data-role=assistant]").length' | tr -d '"')
    [[ "$n" == "0" ]] && break
    sleep 0.5
done
expect "после reset чат пустой" "$n" "0"
sc=$("${PSQL[@]}" -c "SELECT count(*) FROM screenshots")
or=$("${PSQL[@]}" -c "SELECT count(*) FROM orders")
expect "скриншоты не тронуты" "$sc" "${#files[@]}"
ms=$("${PSQL[@]}" -c "SELECT count(*) FROM chat_messages")
expect "chat_messages пуст" "$ms" "0"

# ── 11. connection lost banner
section "T11 connection banner"
# `uv run` запускает дочерний процесс python — нам нужен именно он, не wrapper.
api_pid=$(pgrep -f "python.* -m app api" | grep -v "uv run" | head -1 || true)
if [[ -z "$api_pid" ]]; then
    api_pid=$(lsof -nP -iTCP:3051 -sTCP:LISTEN -t 2>/dev/null | head -1 || true)
fi
if [[ -n "$api_pid" ]]; then
    kill -STOP "$api_pid"
    found=""
    for i in {1..15}; do
        v=$(ab eval 'document.querySelector("[data-testid=connection-banner]") ? "yes" : "no"' | tr -d '"')
        if [[ "$v" == "yes" ]]; then found=yes; break; fi
        sleep 1
    done
    expect "banner появился" "${found:-no}" "yes"
    ab screenshot "$ART/11_offline.png" >/dev/null
    kill -CONT "$api_pid"
    found=""
    for i in {1..20}; do
        v=$(ab eval 'document.querySelector("[data-testid=connection-banner]") ? "yes" : "no"' | tr -d '"')
        if [[ "$v" == "no" ]]; then found=yes; break; fi
        sleep 1
    done
    expect "banner ушёл после reconnect" "${found:-no}" "yes"
else
    fail "не нашли pid uvicorn для kill -STOP"
fi

# ── итог
echo
echo "================="
echo " passed: $PASS"
echo " failed: $FAIL"
echo "================="
[[ $FAIL -eq 0 ]]
