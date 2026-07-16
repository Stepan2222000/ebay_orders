# План реализации стадии B

Этот документ — про **как делать**, не про **что делать**. Что делать — в
`docs/SPEC.yaml` (контракт) и `docs/PLAN.md` (этапная нарезка). Здесь
только список конкретных файлов, последовательность шагов и проверки на
живой системе. Без дублирования контракта.

## Прежде чем писать код

Этот документ — отправная точка, не «истина последней инстанции».
Документация AI SDK и OpenRouter могут уже измениться, в `node_modules`
может стоять более свежая версия пакетов. Поэтому первым делом, ещё до
кода:

1. **Глубоко изучить актуальные источники** — через Exa MCP
   (`mcp__exa__web_search_exa`, `mcp__exa__web_fetch_exa`), при необходимости
   разделив работу на несколько субагентов параллельно. Особенно перепроверить:
   точный wire-формат UIMessageStream v1 (имена и поля чанков), как работают
   `useChat.onFinish` / `useChat.stop` / `useChat.status`, паттерн server-side
   persistence, формат tool-call дельт у OpenRouter и поведение
   `reasoning_details`, поведение `request.is_disconnected` в Starlette при
   обрыве клиента. Все ссылки внизу документа — стартовые, а не финальные.
2. **Прогнать ключевые механики в терминале** — поднять минимальный
   FastAPI-handler с одним заголовком `x-vercel-ai-ui-message-stream: v1`
   и одной парой чанков, скормить его реальному `useChat` и убедиться что
   собрал текст; сделать прямой curl на OpenRouter с `tool_calls` и
   увидеть delta-формат глазами; свериться с состоянием БД (`psql`
   через `.env`-coords). Не писать код «по памяти отчёта».
3. **Задавать вопросы пользователю.** Любое решение, которое не
   следует прямо из SPEC и не зафиксировано в этом файле, — не
   домысливать, а спросить. AskUserQuestion с предварительными ответами
   словами.

Без этого шага — сразу за код не садимся. Чем больше реальной механики
проверено перед стартом, тем меньше переписок потом.

## Как пишем

- **Без overengineering.** Пишем минимум, который реализует контракт из
  SPEC. Не добавляем абстракции, обёртки, конфиги «на будущее», классы
  где хватит функции. Если фича ещё не описана в SPEC — её не делаем.
- **Без глупых фолбеков.** Не пишем `try: ... except: pass`, не пишем
  «если этот endpoint не ответил — попробуем другой», не дублируем
  обработку ошибок в нескольких слоях. Ошибка должна доходить до
  пользователя в виде понятного сообщения, как описано в
  [[ответ пользователю#ошибка]].
- **Тестируем детально, в живой системе.** После каждого шага — проверка
  в терминале (curl, psql, лог-вывод), затем — UI-проверка через
  `agent-browser` (claude-in-chrome MCP), чтобы своими глазами видеть,
  что плашки разворачиваются, ответ стримится, Stop работает,
  refresh поднимает историю. Не «по памяти как должно быть».

---

## Стек и решения

Используем то, что уже стоит в репозитории:

- бэкенд — Python 3 + FastAPI + asyncpg, OpenRouter через httpx;
- фронт — Next.js 15 + AI SDK v6 (`ai@^6.0.0`) + `@ai-sdk/react@^3.0.0`,
  установка проверена в `frontend/node_modules`;
- стрим — официальный AI SDK UIMessageStream v1, формат и список
  чанков подтверждены прямым чтением `node_modules/ai/dist/index.d.ts`
  (см. ссылки внизу);
- модель стадии B — `openai/gpt-5.6-terra` через OpenRouter с
  `reasoning.effort=medium`, поведение в стриме проверено живым curl'ом;
- стадия A не трогается.

Ключевые решения, которые повлияют на код (полная мотивация — в SPEC):

- агент запускается только из POST `/api/chat`, никаких фоновых
  диспетчеров ([[Стадия B сборка заказа#запуск]]);
- `reasoning_details` в чат **не стримим** (правило «тихий чат»),
  но аккумулируем в памяти и пробрасываем в следующий шаг внутри
  одной сессии — для качества модели;
- tool-input даём сразу готовым (`tool-input-start` →
  `tool-input-available`), без посимвольного `tool-input-delta`;
- историю чата для модели читаем из БД на каждый POST, не из тела
  запроса — БД источник истины;
- assistant-message пишется одним INSERT'ом по завершении ответа
  ([[Стадия B сборка заказа#persistence]]);
- параллельные tool-вызовы — через семафор
  ([[параллельность tool-вызовов]]).

---

## Какие файлы появляются и зачем

**Новые:**

- `app/uimessage.py` — короткий self-rolled эмиттер UIMessageStream:
  набор хелперов, формирующих SSE-чанки нужных типов, плюс константы
  заголовков. Не зависит от FastAPI; чистые функции.
- `app/agent.py` — async-генератор стадии B. На входе: pool, http,
  готовый список сообщений для модели, число pending. Внутри —
  tool-loop поверх существующего `stream_chat_step` из
  `app/llm.py` (бывший `app/openrouter.py`). На выходе — последовательность UIMessageStream
  чанков (готовых dict'ов).

**Меняем:**

- `app/api.py` — добавляем `POST /api/chat`. Внутри:
  загрузка истории из БД, INSERT user-message, формирование
  системного промпта с числом pending, запуск `app/agent.py`, оборот
  чанков в SSE через `app/uimessage.py`, по окончании — INSERT
  assistant-message с собранными parts. Лишний `httpx.AsyncClient`
  возвращаем в `lifespan`.
- `app/prompt.py` — добавляем `SYSTEM_PROMPT_STAGE_B`. Текст —
  сборка из [[промпт стадии B]] (общие правила, обстановка,
  поведение, запреты). Подстановка `{pending_count}` — тонкий
  template, без шаблонизаторов.
- `app/tools.py` — не переписываем, но проверяем что
  `OPENAI_TOOLS`, `execute_save_order`, `execute_sql` остаются
  совместимы с новой обвязкой агента. Парсеры сумм/дат уже на месте.
- `frontend/components/ChatPane.tsx` — достаём `status` и `stop` из
  `useChat`, передаём в `Composer` как `busy` и `onStop`.
- `frontend/components/AssemblingIndicator.tsx` — два режима:
  во время стрима показывает «Обработано N/M · ошибок K», без
  стрима при `assembling > 0` — «Новых снимков: P», иначе скрыт
  ([[прогресс стадии B]]).

**Не трогаем:**

- стадия A целиком (`app/worker.py`, `app/ocr.py`);
- `app/llm.py` (бывший `app/openrouter.py`) — функция `stream_chat_step`
  универсальна, она уже даёт нужные нам события text/tool;
- `app/listener.py` (PgFanout) — нужен для сайдбар-стрима, в
  чат-эндпоинт не вмешивается;
- `frontend/lib/sse.tsx` — общий useSSE, без изменений.

---

## Порядок реализации

Каждый шаг заканчивается терминальной проверкой — без неё к
следующему не переходим.

### Шаг 1. UIMessageStream-эмиттер

В `app/uimessage.py` написать функции, формирующие dict'ы строго по
типам v6 (один тип — одна функция, имена «`text_start`», «`text_delta`»,
«`tool_input_start`», «`tool_input_available`», «`tool_output_available`»,
«`tool_output_error`», «`start`», «`finish`», `start_step`, `finish_step`).
Плюс хелпер `sse(d) -> str`, отдающий `data: {json}\n\n`. Плюс
константа `UI_MESSAGE_STREAM_HEADERS` ровно с теми пятью полями, что
в node_modules.

**Проверка:** запустить минимальный FastAPI handler, который кидает
`start → start-step → text-start → text-delta(«hi») → text-end →
finish-step → finish → [DONE]`. Открыть его в `useChat` — должно
показать «hi».

### Шаг 2. Системный промпт

В `app/prompt.py` добавить `SYSTEM_PROMPT_STAGE_B` с тремя блоками
по [[промпт стадии B]]: общие правила, обстановка, поведение,
запреты. Подставлять только число pending, ничего лишнего.

**Проверка:** unit-проверка `assert "Pending снимков: 0" in
SYSTEM_PROMPT_STAGE_B.format(pending_count=0)`.

### Шаг 3. Агент-генератор

В `app/agent.py` написать `async def stream_stage_b(pool, http,
messages_for_model, message_id) -> AsyncIterator[dict]`. Внутри
цикл:

1. Эмитим `start` (с `messageId`) → `start-step`.
2. Открываем `stream_chat_step(http, body, abort_check=…)`.
3. На каждый `text_delta` — открываем text-part (если ещё нет),
   шлём `text-delta`. На `step_finished` — `text-end` если был
   текст.
4. Если в step есть tool_calls — для каждого: эмитим `tool-input-start`
   и `tool-input-available` (input уже распарсенный JSON). Запускаем
   все tools параллельно через `asyncio.gather` + `Semaphore(40)`.
   По мере готовности шлём `tool-output-available` /
   `tool-output-error`. Результаты добавляем в `messages_for_model`
   как role=tool. `reasoning_details` из `step_finished`
   аккумулируем и кладём в assistant-msg для следующего шага.
5. Если tool_calls больше нет — эмитим `finish-step` → `finish` →
   возврат.

Stop через `CancelledError` — генератор просто пробрасывает; верхний
слой делает persistence. Внутри генератора — никаких try/finally,
никакой работы с БД, никакого знания про messageId формат.

**Проверка:** прогон через минимальный «harness»-скрипт без
HTTP — фабрикуем `messages_for_model = [{system}, {user "запиши
два"}]`, list(events). Видим ожидаемую последовательность чанков.

### Шаг 4. POST /api/chat

В `app/api.py`:

1. INSERT user-message в `chat_messages` (одна транзакция).
2. Прочитать историю из БД, отрисовать в формат для модели
   (text-only для assistant — без tool_calls/reasoning, но с
   text-parts). Прибавить системный промпт.
3. Сгенерить `messageId` (UUID) для start chunk.
4. В `StreamingResponse` гонять `app/agent.py:stream_stage_b`,
   оборачивая каждый dict в `sse(...)`, накапливая parts в локальном
   списке.
5. По завершении (онFinish) — INSERT assistant-message в
   `chat_messages` с собранными parts.
6. На `CancelledError` (или `request.is_disconnected()`) — добавить
   к accumulated parts финальный `{type:"text",text:"(прервано)"}` и
   INSERT'ить. Вокруг INSERT в except/finally — `anyio.CancelScope(shield=True)`,
   иначе async вызов получит повторный cancel.

**Проверка через curl:**

```bash
curl -N -X POST http://localhost:3051/api/chat \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"id":"u1","role":"user","parts":[{"type":"text","text":"привет"}]}]}'
```

Видим SSE-чанки, в БД появляется user-message и assistant-message.

### Шаг 5. Подключение фронта

В `ChatPane.tsx` достаём `status` и `stop` из `useChat`, передаём
в `Composer`. В `AssemblingIndicator.tsx` подключаем второй режим
«Обработано N/M» через тот же `useChat.status` (через React Context
от ChatPane или через перенос индикатора внутрь ChatInner).

**Проверка через agent-browser:** открываем localhost:3050, пишем
«привет», читаем DOM/консоль, проверяем что:
- статус useChat ползёт submitted → streaming → ready;
- кнопка Send превращается в Stop во время стрима;
- после refresh история подгружена из БД и отображается корректно.

### Шаг 6. Полный сценарий — через agent-browser

Проверяем не «вроде работает», а каждый сценарий руками в браузере
+ снимками консоли/сети + чтением БД. Где видим расхождение со SPEC —
правим код, не SPEC.

1. Дроп 5 скриншотов в сайдбар → стадия A их обрабатывает,
   сайдбар-счётчики бегут, в шапке чата появляется «Новых снимков: 5».
   Проверяем `read_console_messages`, `read_network_requests` для
   `/api/status/stream`.
2. Пишем в чат «обработай» → видим стрим: плашки `tool-save_order`
   появляются раскрытыми, через ~5с сворачиваются (но кликом
   раскрываются обратно — проверяем кликом), в конце сводка
   ([[ответ пользователю#сводка]]). В шапке — «Обработано N из M».
3. В БД появились новые `orders`/`order_items`, в `chat_messages` два
   row (user + assistant с tool-NAME parts).
4. Stop посреди ответа на 30 скриншотах → часть `orders` сохранена,
   в `chat_messages.parts` финальный part `(прервано)`. Проверяем
   что fetch на `/api/chat` действительно прервался
   (`read_network_requests`).
5. Запрос «расскажи про последний заказ» — агент делает SELECT и
   отвечает текстом. В чате одна ассистент-реплика без tool-плашек.
6. Закрытие вкладки во время ответа → ведёт себя как Stop.
7. PG-коннектов после нескольких полных циклов:
   `psql -c "SELECT count(*) FROM pg_stat_activity"` — не растёт.

---

## Источники (проверены живьём)

- AI SDK UIMessageStream v1 — заголовки и SSE-формат:
  `frontend/node_modules/ai/dist/index.mjs:5047`,
  `index.d.ts:2076-2200`, плюс
  https://github.com/vercel/ai/blob/main/packages/ai/src/ui-message-stream/json-to-sse-transform-stream.ts,
  https://github.com/vercel/ai/blob/main/packages/ai/src/ui-message-stream/ui-message-stream-headers.ts,
  https://github.com/vercel/ai/blob/main/packages/ai/src/ui-message-stream/ui-message-chunks.ts.
- AI SDK Persistence guide:
  https://ai-sdk.dev/docs/ai-sdk-ui/chatbot-message-persistence
- AI SDK useChat reference:
  https://ai-sdk.dev/docs/reference/ai-sdk-ui/use-chat
- OpenRouter streaming + tool deltas:
  https://openrouter.ai/docs/api-reference/streaming,
  плюс живой замер на `openai/gpt-5.6-terra` из этого репо (см. историю
  терминала перед стартом этой работы).
- OpenRouter reasoning_details:
  https://openrouter.ai/docs/guides/best-practices/reasoning-tokens
- `sse-starlette` про disconnect/CancelScope:
  https://github.com/sysid/sse-starlette
- Pydantic AI Vercel adapter (как референс самописного эмиттера):
  https://ai.pydantic.dev/ui/vercel-ai/
