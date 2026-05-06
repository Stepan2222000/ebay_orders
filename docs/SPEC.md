# Спецификация: ebay_orders

Единый источник правды по системе. Любое расхождение между этим документом и кодом — расхождение, которое надо устранить либо правкой кода, либо правкой документа. Решений, отражённых только в чате и нигде здесь, не существует. Раздел «Открытые вопросы» в конце фиксирует то, что осознанно отложено до момента имплементации.

---

## 1. Цель и принципы

Система принимает скриншоты страниц eBay (Order details, Tracking, Item view и подобные), извлекает из них структурированные данные о заказах и складывает их в реляционную базу Postgres. Поверх базы работает чат, в котором пользователь общается с агентом: задаёт вопросы про ранее сохранённые заказы и иногда корректирует данные словами. Система рассчитана на одного пользователя, без авторизации, запускается локально на машине пользователя и подключается к удалённой Postgres.

Принципы, которыми спека и код должны соответствовать:

- **Никаких глубоких фолбеков и перестраховок.** Если шаг не отработал — он завершается ошибкой, ошибка видна пользователю, разбираемся. Не наслаиваем «попробовать второй моделью», «подставить дефолт», «угадать поле». Это правило взято из `CLAUDE.md` и распространяется на всё.
- **Перед реализацией читаем документацию подсистемы, а не пишем по памяти.** Перед написанием кода, который зависит от поведения OpenRouter, Vercel AI SDK, Postgres триггеров или Docker — сначала проверяем поведение в терминале на живом окружении, потом пишем код.
- **Один источник правды на каждое решение.** Если поле описано в DDL, в дальнейших разделах оно упоминается ссылкой, а не повторяется заново. Если меняется — меняется в одном месте.
- **Без overengineering.** Один общий tool записи вместо десятка узких. Один общий tool чтения через сырой SQL вместо набора фильтров. Защита данных — на стороне Postgres через CHECK и триггеры, а не дублирующими валидаторами в Python.
- **Сырое отделено от чистового.** Результат OCR хранится в БД как иммутабельный слой `raw_ocr`. Из него агент собирает чистовые таблицы (`orders` и связанные). При расхождении приоритет у чистовых, но raw остаётся для аудита.
- **Тихая работа в чате.** Агент не комментирует каждый шаг и не подтверждает действия. Он сохраняет данные и пишет в чат только финальную короткую сводку по батчу или вопрос, если столкнулся с явным противоречием.

---

## 2. Архитектура

Система состоит из двух LLM-инстансов, бэкенда на Python FastAPI, фронта на Next.js и одной Postgres базы.

**Инстанс A (OCR).** Принимает на вход одну картинку и фиксированный промпт «куда смотреть». Возвращает один JSON-объект с распознанными полями. Не пишет в БД. Не общается с пользователем. Не использует историю чата. Один скриншот — один вызов A.

**Инстанс B (агент).** Принимает на вход историю чата и список свежих `raw_ocr.sha256`, которые ещё не обработал. Получает сами записи `raw_ocr` через SQL. Группирует их в логические заказы (по `order_number`, а где номер не виден — по совпадению title и других признаков, которые сам решит запросить из БД). Выполняет инструменты `save_order_data`, `sql`, `link_screenshot`, `mark_unlinked`, `delete_order`. Пишет короткие сообщения в чат. Спрашивает пользователя только если столкнулся с противоречием, которое не может разрешить по контексту.

**Развязка между A и B — таблицы Postgres.** A пишет результат в `raw_ocr`. B читает `raw_ocr` через SQL и идёт в чистовые таблицы. Прямой передачи данных от A к B в памяти процесса нет: всё через БД. Это даёт три свойства одновременно: идемпотентность (повторный вызов A с тем же sha256 даст ту же запись), аудитируемость (видно что распозналось и что из этого попало в чистовое) и устойчивость к падениям (бэкенд можно перезапустить — все недоработанные снимки видны по статусу в `screenshots`).

**Бэкенд.** Python FastAPI с одним основным endpoint'ом, реализующим Vercel AI SDK Data Stream Protocol для совместимости с хуком `useChat` на фронте. Внутри бэкенда работает два пула воркеров — для стадии A и для стадии B — с настраиваемой параллельностью через семафоры. Воркеры читают из таблицы `screenshots` записи в статусе pending по соответствующей колонке, обрабатывают, обновляют статус. Бэкенд деплоится локально на машине пользователя. Postgres подключается удалённо.

**Фронтенд.** Next.js приложение с одной страницей. На странице чат с drag&drop зоной для файлов и папок. Использует `useChat` из `ai` v5. Файлы прикрепляются к сообщению как multimodal parts (data URL или upload). Прогресс обработки отображается отдельным «живым» пузырьком, который обновляется по мере прихода data parts от бэкенда. Никаких кнопок approve/reject и никаких confirm-режимов: всё, что пользователь хочет сказать агенту, он пишет в чат словами.

**Поток данных при загрузке скриншотов:**

Пользователь перетаскивает в чат пачку файлов. Фронт прикрепляет их к следующему сообщению. Бэкенд при получении сообщения вычисляет sha256 каждого файла, сохраняет байты в `screenshots` с `ocr_status='pending'` и `agent_status='pending'`, после чего открывает SSE-стрим обратно фронту. Воркер стадии A разбирает pending-записи: вычитывает байты, дёргает Kimi K2.6 (vision), получает JSON, пишет его в `raw_ocr` и переводит снимок в `ocr_status='done'`. Воркер стадии B видит снимок, готовый к агентной обработке (`ocr_status='done'`, `agent_status='pending'`), вычитывает запись из `raw_ocr`, в одном вызове агента для текущей сессии чата передаёт ему историю и метаданные снимков. Агент через `sql` доисследует БД, через `save_order_data` пишет данные, через `link_screenshot` или `mark_unlinked` помечает снимок. По мере записей бэкенд эмитит data parts с прогрессом в открытый SSE-стрим, в конце эмитит финальную короткую сводку, которая попадает в чат как обычное сообщение ассистента.

**Поток данных при текстовом сообщении без файлов.** Бэкенд просто сохраняет сообщение пользователя в `chat_messages`, вызывает B с историей сессии и пустым списком новых снимков, B отвечает (через `sql` смотрит данные, через `save_order_data` корректирует если попросили), ответ стримится в чат.

---

## 3. Подключение к Postgres

База `ebay_orders` уже создана на удалённом сервере и пуста. Postgres-версия 18.3.

| Параметр   | Значение         |
| ---------- | ---------------- |
| Хост       | `2.26.53.128`    |
| Порт       | `5405`           |
| Пользователь | `admin`        |
| Пароль     | `Password123` (через переменную окружения `POSTGRES_PASSWORD`) |
| База       | `ebay_orders`    |

Используется одна роль `admin` с полными правами. Отдельных read-only ролей нет. Защита данных от записи мусора — на уровне CHECK-ограничений и триггеров, а не на уровне ролей. Логика бэкенда и логика агента работают через один и тот же коннект.

Когда сервис будет переезжать на удалённый сервер целиком, хост в конфиге меняется на `localhost` или внутреннее имя контейнера; всё остальное — без изменений.

---

## 4. Схема БД

Схема организована в три слоя. Слой бинарных данных и сырого OCR — `screenshots` и `raw_ocr`. Бизнес-слой — `orders` и связанные таблицы. Технический слой — `change_log`, `chat_sessions`, `chat_messages`. Все DDL-фрагменты ниже соответствуют ровно тому, что должно лежать в первой миграции.

Перед таблицами создаём расширение для нечёткого поиска по строкам — `pg_trgm`. Оно понадобится агенту для запросов по `title` через `ILIKE` и `similarity()`.

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;
```

### 4.1. screenshots — байты и общий статус снимка

Хранит сами файлы как `bytea` и текущий статус каждого снимка по двум стадиям обработки. Ключ — sha256 файла, чтобы при повторной загрузке того же файла не дублировать ни байты, ни OCR.

```sql
CREATE TABLE screenshots (
    sha256          char(64) PRIMARY KEY,
    bytes           bytea NOT NULL,
    mime            text NOT NULL,
    width_px        integer,
    height_px       integer,
    captured_at     timestamptz,        -- из EXIF, если есть; иначе NULL
    uploaded_at     timestamptz NOT NULL DEFAULT now(),
    ocr_status      text NOT NULL DEFAULT 'pending'
        CHECK (ocr_status IN ('pending', 'running', 'done', 'failed')),
    ocr_error       text,
    agent_status    text NOT NULL DEFAULT 'pending'
        CHECK (agent_status IN ('pending', 'running', 'done', 'failed')),
    agent_error     text
);

CREATE INDEX idx_screenshots_ocr_pending   ON screenshots (uploaded_at) WHERE ocr_status = 'pending';
CREATE INDEX idx_screenshots_agent_pending ON screenshots (uploaded_at) WHERE ocr_status = 'done' AND agent_status = 'pending';
```

Идемпотентность загрузки: при повторной заливке того же файла бэкенд делает `INSERT ... ON CONFLICT (sha256) DO NOTHING`. Если запись уже есть — стадии не сбрасываются, обработка не повторяется. Это и есть кэш OCR без отдельной логики.

Поля статуса работают как очередь без отдельной таблицы. Воркер стадии A читает `WHERE ocr_status = 'pending'` с `FOR UPDATE SKIP LOCKED` и переводит в `running`/`done`/`failed`. Воркер стадии B — аналогично по `agent_status` с предусловием `ocr_status = 'done'`.

### 4.2. raw_ocr — иммутабельный сырой результат A

Одна строка на снимок. Создаётся стадией A после успешного распознавания. После создания не меняется. Хранит весь JSON-выход модели как jsonb, плюс версии промпта и модели — чтобы видно было, какими настройками получен результат, если правила вдруг поменяются.

```sql
CREATE TABLE raw_ocr (
    sha256           char(64) PRIMARY KEY REFERENCES screenshots(sha256) ON DELETE CASCADE,
    ocr_prompt_ver   text NOT NULL,
    ocr_model        text NOT NULL,
    is_ebay          boolean NOT NULL,
    raw_json         jsonb NOT NULL,
    ocr_at           timestamptz NOT NULL DEFAULT now()
);
```

`is_ebay` дублируется наружу из `raw_json` для удобства фильтрации без распаковки JSON. Если модель посчитала, что это не eBay-страница, `is_ebay=false`, и стадия B такие снимки сразу игнорирует, помечая `agent_status='done'` без записи в чистовые таблицы.

### 4.3. orders — заказы

Главная бизнес-таблица. Один заказ — одна строка. Идентифицируется по eBay order number формата `XX-XXXXX-XXXXX` (две цифры, тире, пять цифр, тире, пять цифр).

```sql
CREATE TABLE orders (
    order_number       text PRIMARY KEY
        CHECK (order_number ~ '^[0-9]{2}-[0-9]{5}-[0-9]{5}$'),
    time_placed        timestamptz,
    total              numeric(10,2)
        CHECK (total IS NULL OR total >= 0),
    item_subtotal      numeric(10,2)
        CHECK (item_subtotal IS NULL OR item_subtotal >= 0),
    shipping_subtotal  numeric(10,2)
        CHECK (shipping_subtotal IS NULL OR shipping_subtotal >= 0),
    currency           char(3),                   -- "USD", "EUR", и т.п.
    sold_by            text,                       -- username продавца, опционально FK логически на sellers.username
    delivered_date     date,
    deleted_at         timestamptz,                -- soft delete
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);
```

Денежные поля типа `numeric(10,2)` — без потерь точности на копейках. `sold_by` — текст, не FK на `sellers`, чтобы не требовать обязательной вставки строки в `sellers` при каждом upsert заказа; данные о продавце в `sellers` живут отдельной жизнью и обновляются, когда есть что обновлять. Soft-delete через `deleted_at`: физически строки не удаляются никогда.

### 4.4. order_items — позиции в заказе

eBay-заказ может содержать несколько позиций. Для каждой — отдельная строка.

```sql
CREATE TABLE order_items (
    id              bigserial PRIMARY KEY,
    order_number    text NOT NULL REFERENCES orders(order_number) ON DELETE CASCADE,
    item_number     text,                         -- внутренний eBay item id, если виден
    title           text NOT NULL,
    price           numeric(10,2)
        CHECK (price IS NULL OR price >= 0),
    qty             integer NOT NULL DEFAULT 1
        CHECK (qty > 0),
    image_sha256    char(64) REFERENCES screenshots(sha256),  -- если у нас есть скрин с фоткой товара
    UNIQUE (order_number, item_number)            -- если item_number есть, дубль той же позиции не появляется
);

CREATE INDEX idx_order_items_title_trgm ON order_items USING gin (title gin_trgm_ops);
```

GIN-индекс по `title` позволяет агенту быстро искать заказы по нечёткому совпадению названия — это нужно для случая, когда A не увидел `order_number` и B пытается привязать снимок по title к существующему заказу.

### 4.5. tracking_numbers — все когда-либо встреченные трек-номера

Один заказ может иметь несколько трек-номеров (например, частичная отправка). Если в новом снимке трек-номер совпадает с уже сохранённым — обновляем `last_seen_at`. Если новый — INSERT. Если трек-номера не видно вообще — мы НЕ удаляем существующие. Это правило вшито в логику `save_order_data`, дополнительно защищено триггером `prevent_null_overwrite` для самих полей `orders` (трек-номера живут не в `orders`, но принцип тот же).

```sql
CREATE TABLE tracking_numbers (
    id              bigserial PRIMARY KEY,
    order_number    text NOT NULL REFERENCES orders(order_number) ON DELETE CASCADE,
    number          text NOT NULL
        CHECK (length(number) >= 8),
    carrier         text,                         -- "USPS", "UPS", "FedEx", если виден
    first_seen_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (order_number, number)
);
```

### 4.6. refunds — частичные и полные возвраты

Каждый возврат — отдельная строка. Идемпотентность по тройке `(order_number, amount, refund_date)`: если в новом скриншоте видна та же сумма за ту же дату — INSERT отвергается уникальным индексом, новой строки не появляется.

```sql
CREATE TABLE refunds (
    id              bigserial PRIMARY KEY,
    order_number    text NOT NULL REFERENCES orders(order_number) ON DELETE CASCADE,
    amount          numeric(10,2) NOT NULL
        CHECK (amount > 0),
    kind            text NOT NULL DEFAULT 'partial'
        CHECK (kind IN ('partial', 'full')),
    refund_date     date NOT NULL,
    note            text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (order_number, amount, refund_date)
);
```

### 4.7. shipping_addresses — адрес доставки

Одна строка на заказ. Если новый снимок содержит другой адрес — UPDATE поверх (адресов истории не ведём). Если не содержит — не трогаем (правило «не затирать пустым»).

```sql
CREATE TABLE shipping_addresses (
    order_number    text PRIMARY KEY REFERENCES orders(order_number) ON DELETE CASCADE,
    name            text,
    line1           text,
    line2           text,
    city            text,
    state           text,
    postal_code     text,
    country         text
);
```

### 4.8. sellers — продавцы

Опциональная таблица с дополнительной инфой о продавце (локация, отображаемое имя, процент позитивных). Заполняется когда видно на скриншоте. Связь с `orders.sold_by` — логическая по строковому совпадению, не физический FK. Это сделано чтобы upsert заказа не требовал обязательного INSERT'а строки `sellers`.

```sql
CREATE TABLE sellers (
    username        text PRIMARY KEY,
    display_name    text,
    location        text,
    positive_pct    numeric(5,2)
        CHECK (positive_pct IS NULL OR (positive_pct >= 0 AND positive_pct <= 100)),
    last_seen_at    timestamptz NOT NULL DEFAULT now()
);
```

### 4.9. screenshot_links — связь снимка и заказа

Один заказ может быть подкреплён несколькими снимками. Один снимок — максимум одним заказом. Эта таблица отвечает на вопрос «какие скрины относятся к заказу X».

```sql
CREATE TABLE screenshot_links (
    sha256          char(64) PRIMARY KEY REFERENCES screenshots(sha256) ON DELETE CASCADE,
    order_number    text NOT NULL REFERENCES orders(order_number) ON DELETE CASCADE,
    linked_at       timestamptz NOT NULL DEFAULT now(),
    linked_by       text NOT NULL DEFAULT 'agent'
        CHECK (linked_by IN ('agent', 'user'))
);

CREATE INDEX idx_screenshot_links_order ON screenshot_links (order_number);
```

Снимки, которые `is_ebay=false` или которые B не смог привязать (нет `order_number` и не нашёл совпадения по title), сюда не попадают вообще. Они помечаются как «unlinked» через `agent_status='done'` плюс отсутствие записи в `screenshot_links`. То, что снимок «висит без привязки», восстанавливается выборкой `screenshots LEFT JOIN screenshot_links` с `WHERE link IS NULL`.

### 4.10. change_log — история изменений бизнес-полей

Заполняется триггером, не вручную. Каждый раз, когда какое-то поле в `orders` (или в связанных таблицах, для которых триггер настроен) изменилось, появляется строка с тем что было, что стало, и источником.

```sql
CREATE TABLE change_log (
    id              bigserial PRIMARY KEY,
    table_name      text NOT NULL,
    pk_value        text NOT NULL,                -- order_number или составной ключ как строка
    field           text NOT NULL,
    old_value       text,
    new_value       text,
    source          text NOT NULL                  -- 'screenshot:<sha256>' | 'user_chat' | 'agent_decision'
        CHECK (source ~ '^(screenshot:[a-f0-9]{64}|user_chat|agent_decision|tool:[a-z_]+)$'),
    at              timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_change_log_pk ON change_log (table_name, pk_value, at DESC);
```

Источник проставляется тем кодом, который дёрнул UPDATE (внутри `save_order_data` и `update_order_field` на питоне). Триггер берёт значение `source` из переменной сессии Postgres, которую Python устанавливает командой `SET LOCAL my.source = '...'` перед запросом.

### 4.11. chat_sessions — сессии чата

Одна сессия на каждое открытие страницы.

```sql
CREATE TABLE chat_sessions (
    session_id      uuid PRIMARY KEY,
    created_at      timestamptz NOT NULL DEFAULT now(),
    last_used_at    timestamptz NOT NULL DEFAULT now()
);
```

Сессии живут 24 часа после последнего использования. Старые удаляются простым SQL `DELETE FROM chat_sessions WHERE last_used_at < now() - interval '24 hours'`, который запускается раз в час из бэкграунд-таска внутри FastAPI. Никаких внешних cron'ов.

### 4.12. chat_messages — сообщения чата

```sql
CREATE TABLE chat_messages (
    id              bigserial PRIMARY KEY,
    session_id      uuid NOT NULL REFERENCES chat_sessions(session_id) ON DELETE CASCADE,
    role            text NOT NULL
        CHECK (role IN ('user', 'assistant', 'system')),
    parts           jsonb NOT NULL,                -- структура UIMessage.parts из Vercel AI SDK
    attached_sha256 char(64)[],                    -- какие скрины пришли вместе с этим сообщением, для трассировки
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_chat_messages_session ON chat_messages (session_id, created_at);
```

При удалении сессии каскадно удаляются и сообщения. Скриншоты при этом не удаляются: они отвязаны от сессии и хранятся самостоятельно.

### 4.13. Триггеры

**Триггер `prevent_null_overwrite` на таблице `orders`.** При UPDATE проходим по бизнес-колонкам (`time_placed`, `total`, `item_subtotal`, `shipping_subtotal`, `currency`, `sold_by`, `delivered_date`). Если в `NEW` значение `NULL`, а в `OLD` оно не `NULL` — подменяем `NEW` старым значением. Этот триггер реализует правило «новое пустое значение не затирает заполненное», которое относится не только к трек-номерам, но и ко всем полям заказа. Для `tracking_numbers` это правило выполняется по-другому — там вообще нет UPDATE поля `number`, есть только INSERT новых и UPDATE `last_seen_at`.

**Триггер `set_updated_at` на `orders`.** BEFORE UPDATE обновляет `NEW.updated_at = now()`.

**Триггер `log_changes_orders` на `orders`.** AFTER UPDATE сравнивает старые и новые значения бизнес-полей; для каждого изменившегося пишет строку в `change_log` с `source = current_setting('my.source', true)`. Если переменная `my.source` не установлена — пишет `agent_decision`.

DDL триггеров:

```sql
CREATE OR REPLACE FUNCTION fn_prevent_null_overwrite_orders() RETURNS trigger AS $$
BEGIN
    IF NEW.time_placed       IS NULL AND OLD.time_placed       IS NOT NULL THEN NEW.time_placed       := OLD.time_placed;       END IF;
    IF NEW.total             IS NULL AND OLD.total             IS NOT NULL THEN NEW.total             := OLD.total;             END IF;
    IF NEW.item_subtotal     IS NULL AND OLD.item_subtotal     IS NOT NULL THEN NEW.item_subtotal     := OLD.item_subtotal;     END IF;
    IF NEW.shipping_subtotal IS NULL AND OLD.shipping_subtotal IS NOT NULL THEN NEW.shipping_subtotal := OLD.shipping_subtotal; END IF;
    IF NEW.currency          IS NULL AND OLD.currency          IS NOT NULL THEN NEW.currency          := OLD.currency;          END IF;
    IF NEW.sold_by           IS NULL AND OLD.sold_by           IS NOT NULL THEN NEW.sold_by           := OLD.sold_by;           END IF;
    IF NEW.delivered_date    IS NULL AND OLD.delivered_date    IS NOT NULL THEN NEW.delivered_date    := OLD.delivered_date;    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_prevent_null_overwrite_orders
    BEFORE UPDATE ON orders
    FOR EACH ROW EXECUTE FUNCTION fn_prevent_null_overwrite_orders();

CREATE OR REPLACE FUNCTION fn_set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_set_updated_at_orders
    BEFORE UPDATE ON orders
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_at();

CREATE OR REPLACE FUNCTION fn_log_changes_orders() RETURNS trigger AS $$
DECLARE
    src text := current_setting('my.source', true);
BEGIN
    IF src IS NULL OR src = '' THEN src := 'agent_decision'; END IF;

    IF NEW.time_placed       IS DISTINCT FROM OLD.time_placed       THEN INSERT INTO change_log (table_name, pk_value, field, old_value, new_value, source) VALUES ('orders', NEW.order_number, 'time_placed',       OLD.time_placed::text,       NEW.time_placed::text,       src); END IF;
    IF NEW.total             IS DISTINCT FROM OLD.total             THEN INSERT INTO change_log (table_name, pk_value, field, old_value, new_value, source) VALUES ('orders', NEW.order_number, 'total',             OLD.total::text,             NEW.total::text,             src); END IF;
    IF NEW.item_subtotal     IS DISTINCT FROM OLD.item_subtotal     THEN INSERT INTO change_log (table_name, pk_value, field, old_value, new_value, source) VALUES ('orders', NEW.order_number, 'item_subtotal',     OLD.item_subtotal::text,     NEW.item_subtotal::text,     src); END IF;
    IF NEW.shipping_subtotal IS DISTINCT FROM OLD.shipping_subtotal THEN INSERT INTO change_log (table_name, pk_value, field, old_value, new_value, source) VALUES ('orders', NEW.order_number, 'shipping_subtotal', OLD.shipping_subtotal::text, NEW.shipping_subtotal::text, src); END IF;
    IF NEW.currency          IS DISTINCT FROM OLD.currency          THEN INSERT INTO change_log (table_name, pk_value, field, old_value, new_value, source) VALUES ('orders', NEW.order_number, 'currency',          OLD.currency,                NEW.currency,                src); END IF;
    IF NEW.sold_by           IS DISTINCT FROM OLD.sold_by           THEN INSERT INTO change_log (table_name, pk_value, field, old_value, new_value, source) VALUES ('orders', NEW.order_number, 'sold_by',           OLD.sold_by,                 NEW.sold_by,                 src); END IF;
    IF NEW.delivered_date    IS DISTINCT FROM OLD.delivered_date    THEN INSERT INTO change_log (table_name, pk_value, field, old_value, new_value, source) VALUES ('orders', NEW.order_number, 'delivered_date',    OLD.delivered_date::text,    NEW.delivered_date::text,    src); END IF;
    IF NEW.deleted_at        IS DISTINCT FROM OLD.deleted_at        THEN INSERT INTO change_log (table_name, pk_value, field, old_value, new_value, source) VALUES ('orders', NEW.order_number, 'deleted_at',        OLD.deleted_at::text,        NEW.deleted_at::text,        src); END IF;

    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_log_changes_orders
    AFTER UPDATE ON orders
    FOR EACH ROW EXECUTE FUNCTION fn_log_changes_orders();
```

Дополнительные CHECK-ограничения, которые не влезли в DDL таблиц выше, не вводятся: ровно тех, что прописаны в `CREATE TABLE`, достаточно. Если в процессе работы выяснится, что нужны ещё — добавим миграцией с явной мотивацией.

### 4.14. Представление orders_view

Удобное представление для чтения. Джойнит `orders` с `shipping_addresses`, агрегатом `tracking_numbers` и агрегатом `refunds`. Возвращает один заказ — одной строкой. Используется агентом по умолчанию, когда он отвечает на вопросы пользователя.

```sql
CREATE VIEW orders_view AS
SELECT
    o.order_number,
    o.time_placed,
    o.total,
    o.item_subtotal,
    o.shipping_subtotal,
    o.currency,
    o.sold_by,
    o.delivered_date,
    o.deleted_at,
    o.created_at,
    o.updated_at,
    sa.name           AS ship_name,
    sa.line1          AS ship_line1,
    sa.line2          AS ship_line2,
    sa.city           AS ship_city,
    sa.state          AS ship_state,
    sa.postal_code    AS ship_postal_code,
    sa.country        AS ship_country,
    COALESCE((SELECT array_agg(t.number ORDER BY t.first_seen_at) FROM tracking_numbers t WHERE t.order_number = o.order_number), '{}') AS tracking_numbers,
    COALESCE((SELECT sum(r.amount) FROM refunds r WHERE r.order_number = o.order_number), 0) AS refunds_total,
    o.total - COALESCE((SELECT sum(r.amount) FROM refunds r WHERE r.order_number = o.order_number), 0) AS effective_total
FROM orders o
LEFT JOIN shipping_addresses sa ON sa.order_number = o.order_number
WHERE o.deleted_at IS NULL;
```

`effective_total` — это «сколько реально в итоге заплатили»: total минус сумма всех возвратов. Удалённые заказы из view исключены.

---

## 5. Инстанс A — OCR

### 5.1. Назначение и контракт

Один вызов A — ровно один скриншот. На вход: PNG/JPEG/WEBP/GIF в base64 data URL. На выход: один JSON-объект фиксированной схемы. Стриминг не используется — A возвращает результат целиком, потому что внизу по конвейеру всё равно стоит INSERT в `raw_ocr` одной транзакцией.

Модель — `moonshotai/kimi-k2.6` через OpenRouter. Thinking отключён (см. конфиг). История чата на A не подаётся. Никаких tools у A нет. A не пишет в БД сам — это делает бэкенд после получения результата.

Если модель вернула невалидный JSON, или JSON не соответствует ожидаемой схеме (отсутствует `is_ebay`, например), бэкенд переводит снимок в `ocr_status='failed'` и пишет текст ошибки в `ocr_error`. Никаких ретраев и второй модели — следуем правилу «без глубоких фолбеков».

### 5.2. Промпт

Промпт A не зависит от типа eBay-страницы. Один универсальный текст, который просит модель распознать всё, что увидит, и вернуть JSON. Это и есть «куда смотреть» в формулировке пользователя. Финальный текст промпта — согласовываем при имплементации (см. «Открытые вопросы»). Принципы, которым он должен соответствовать:

- Просим модель внимательно смотреть на фотографию и описать, что она видит из набора полей eBay-заказа.
- Поля, которые надо найти: `order_number`, `time_placed`, `total`, `item_subtotal`, `shipping_subtotal`, `currency`, `sold_by`, `delivered_date`, `tracking_numbers[]`, `items[]` (с `item_number`, `title`, `price`, `qty`), `refunds[]` (с `amount`, `refund_date`, `kind`, `note`), `shipping_address` (с `name`, `line1`, `line2`, `city`, `state`, `postal_code`, `country`), `seller` (с `username`, `display_name`, `location`, `positive_pct`).
- Чего нет на картинке — поле = `null` (или пустой массив для списков).
- Дополнительно поле `is_ebay: true|false` — если на картинке нет ни одного eBay-маркера, ставим `false`, остальные поля — `null`.
- Никакого текста вокруг JSON: ровно один JSON-объект на выход.
- Числа — числами, даты — ISO-форматом (`YYYY-MM-DD` для дат, `YYYY-MM-DDTHH:MM:SSZ` для timestamps), деньги — числами без символа валюты (символ идёт в отдельное поле `currency`).

### 5.3. JSON-схема выхода A

Один к одному совпадает с тем, что ждёт `save_order_data`. Это не случайно: B берёт `raw_ocr.raw_json` и в простом случае передаёт его прямо в `save_order_data`. Из-за этого минимизируется маппинг и меньше точек, где можно расходиться.

```json
{
  "is_ebay": true,
  "order_number": "12-12345-67890",
  "time_placed": "2026-04-15T14:30:00Z",
  "total": 49.00,
  "item_subtotal": 40.00,
  "shipping_subtotal": 9.00,
  "currency": "USD",
  "sold_by": "sarah_middleton_2024",
  "delivered_date": "2026-04-22",
  "tracking_numbers": [
    { "number": "9400110200881234567890", "carrier": "USPS" }
  ],
  "items": [
    { "item_number": "335123456789", "title": "Vintage typewriter", "price": 40.00, "qty": 1 }
  ],
  "refunds": [
    { "amount": 5.00, "refund_date": "2026-04-25", "kind": "partial", "note": null }
  ],
  "shipping_address": {
    "name": "John Smith",
    "line1": "742 Evergreen Terrace",
    "line2": null,
    "city": "Springfield",
    "state": "OR",
    "postal_code": "97477",
    "country": "USA"
  },
  "seller": {
    "username": "sarah_middleton_2024",
    "display_name": "Sarah's Vintage",
    "location": "Springfield, OR",
    "positive_pct": 99.6
  }
}
```

Все поля кроме `is_ebay` опциональны и могут быть `null`. Когда `is_ebay=false`, бэкенд не передаёт результат в B вообще, а сразу помечает `agent_status='done'` и пишет в чат уведомление, что снимок не похож на eBay.

### 5.4. Кэширование

Полностью реализовано через первичный ключ `screenshots.sha256` и `INSERT ... ON CONFLICT DO NOTHING`. Если приходит снимок с тем же sha256 — повторного OCR не будет, в `raw_ocr` уже лежит готовый результат. Никаких отдельных кэш-структур не существует.

Версии промпта и модели хранятся в `raw_ocr.ocr_prompt_ver` и `raw_ocr.ocr_model`. Команды переобработки (reprocess) в системе нет: это сознательное решение. Если когда-нибудь понадобится — прочистим `raw_ocr` руками и сбросим `ocr_status`. До тех пор нет.

---

## 6. Инстанс B — агент

### 6.1. Назначение и контракт

B видит две вещи: историю текущей сессии чата (массив сообщений из `chat_messages`) и список sha256, готовых к обработке (свежие записи в `screenshots` со статусом `ocr_status='done'` и `agent_status='pending'`). Через `sql` он сам читает `raw_ocr.raw_json` для каждого sha256 и сам решает, что с этим делать.

B не получает картинки. Он работает только с текстом и JSON из `raw_ocr`. Это снижает стоимость в разы и делает B «дешёвым» во всём кроме особых случаев. Если ему понадобится посмотреть на картинку (теоретически — например, агент сомневается и хочет показать пользователю превью), он может попросить пользователя — отдельной картинки в его контекст не идёт.

Модель — та же `moonshotai/kimi-k2.6`. Thinking включён (см. конфиг). Используются tool calls. Стриминг включён.

### 6.2. System prompt

Полный текст пишется при имплементации (см. «Открытые вопросы»). Принципы, которым system prompt должен соответствовать:

- B — **тихий** агент. Он сохраняет данные, но **почти ничего не пишет в чат**. Стандартный режим — обработать новые снимки и в конце выдать одну короткую строку-сводку: «Принято N снимков → X новых заказов, Y обновлено, Z без изменений, W не привязано». Никаких подробностей по каждому заказу.
- В чат B обращается только в трёх случаях: (1) финальная сводка батча, (2) явное противоречие, которое сам разрешить не может (например, total не сходится с суммой items), (3) когда пользователь напрямую задал вопрос.
- При **корректировке от пользователя** в чате (например, «у XX-12345-67890 цена была $30, а не $35») B делает то, что пользователь сказал, не интерпретируя и не уточняя. Запись идёт через `save_order_data` с `source='user_chat'`.
- Когда у снимка **нет `order_number`**, B пытается привязать его к существующему заказу. Алгоритм — на усмотрение B, но через инструменты: типичный путь — `sql` с поиском по title через `ILIKE` или `similarity()`, плюс сравнение seller, total, items. Если уверенно нашёл — `link_screenshot`. Если не уверенно — `mark_unlinked` и одной строкой в чат.
- B не комментирует **каждое поле**: при обновлении не пишет «было A → стало B» в чат — для этого есть `change_log`. Только если пользователь явно спросит «а что менялось у XX?» — B читает `change_log` через `sql` и отвечает.
- Правило «не затирать пустым» B понимать в явном виде не должен: оно реализовано триггером `prevent_null_overwrite`. B просто кладёт в `save_order_data` всё, что есть, включая null'ы, — БД сама не даст затереть.
- Соответственно: `tracking_numbers` без явного `number` в новом снимке — B не упоминает. Если в `raw_ocr` массив `tracking_numbers` пустой — `save_order_data` не трогает уже сохранённые номера.
- `refunds` идемпотентны: B может всегда передавать всё, что увидел, — БД отвергнет дубль.
- B **не** делает `DELETE FROM` ни через `sql`, ни через `save_order_data`. Удаление возможно только через явный tool `delete_order` (soft-delete), и только если пользователь явно попросил.

### 6.3. Tools

У B пять инструментов. Никаких узких помощников вроде `get_order_by_id` или `find_orders_by_seller` — всё это делается через `sql`.

#### 6.3.1. save_order_data

Один универсальный write-tool. Принимает все поля заказа целиком. Внутри Python-обёртки разбивается на серию INSERT/UPDATE по таблицам `orders`, `order_items`, `tracking_numbers`, `refunds`, `shipping_addresses`, `sellers`, всё в одной транзакции с `SET LOCAL my.source = 'screenshot:<sha256>'` или `'user_chat'` (определяется параметром `source`).

Pydantic-схема (псевдокод; точные имена при имплементации):

```python
class TrackingIn(BaseModel):
    number: str
    carrier: str | None = None

class ItemIn(BaseModel):
    item_number: str | None = None
    title: str
    price: Decimal | None = None
    qty: int = 1

class RefundIn(BaseModel):
    amount: Decimal
    refund_date: date
    kind: Literal['partial', 'full'] = 'partial'
    note: str | None = None

class AddressIn(BaseModel):
    name: str | None = None
    line1: str | None = None
    line2: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    country: str | None = None

class SellerIn(BaseModel):
    username: str
    display_name: str | None = None
    location: str | None = None
    positive_pct: Decimal | None = None

class SaveOrderData(BaseModel):
    order_number: str
    time_placed: datetime | None = None
    total: Decimal | None = None
    item_subtotal: Decimal | None = None
    shipping_subtotal: Decimal | None = None
    currency: str | None = None
    sold_by: str | None = None
    delivered_date: date | None = None
    tracking_numbers: list[TrackingIn] = Field(default_factory=list)
    items: list[ItemIn] = Field(default_factory=list)
    refunds: list[RefundIn] = Field(default_factory=list)
    shipping_address: AddressIn | None = None
    seller: SellerIn | None = None
    source: Literal['screenshot', 'user_chat'] = 'screenshot'
    source_sha256: str | None = None      # обязателен при source='screenshot'
```

Поведение:

- `orders` — `INSERT ... ON CONFLICT (order_number) DO UPDATE`. Триггер `prevent_null_overwrite` гарантирует, что null'ы не затрут заполненные поля. Триггер `log_changes_orders` пишет diff в `change_log`.
- `order_items` — `INSERT ... ON CONFLICT (order_number, item_number) DO UPDATE` если `item_number` есть; иначе — INSERT без UPDATE (новый item-без-номера = новая строка). Удаление позиций из `save_order_data` не происходит: если в новом снимке item не виден, мы не удаляем существующий.
- `tracking_numbers` — `INSERT ... ON CONFLICT (order_number, number) DO UPDATE SET last_seen_at = now()`.
- `refunds` — `INSERT ... ON CONFLICT (order_number, amount, refund_date) DO NOTHING`.
- `shipping_addresses` — `INSERT ... ON CONFLICT (order_number) DO UPDATE` для непустых полей; пустые поля `NEW` не затирают, реализовано на уровне Python-обёртки (там нет триггера, потому что таблица в нынешнем виде однострочная-на-заказ).
- `sellers` — `INSERT ... ON CONFLICT (username) DO UPDATE SET last_seen_at = now(), display_name = COALESCE(EXCLUDED.display_name, sellers.display_name), ...` — пустые тоже не затирают.

Возвращает агенту короткое summary: `{order_number, action: 'created'|'updated'|'no_changes', fields_changed: [list]}`. Агент по этому summary решает, надо ли что-то говорить пользователю.

#### 6.3.2. sql

Универсальный read-инструмент. Принимает строку с SQL и возвращает результат как массив объектов (rows) плюс массив имён колонок. Полный SQL-доступ к базе, без отдельной read-only роли. Сильное соглашение system prompt: «используй для SELECT и явных SET LOCAL my.source если нужно; для записи бизнес-данных используй `save_order_data` или `delete_order`, но не пиши в `orders`/`order_items`/etc. напрямую».

Pydantic-схема:

```python
class Sql(BaseModel):
    query: str
    parameters: list[Any] = Field(default_factory=list)
```

Параметры — позиционные, в Postgres-стиле `$1`, `$2`. Так агент не может случайно сделать SQL-инъекцию из текста пользователя (и не привыкнет к этому). Возврат — массив объектов; на больших ответах — обрезаем до настраиваемого лимита (например 200 строк) и докладываем агенту количество.

#### 6.3.3. link_screenshot

Привязка снимка к существующему заказу. Используется когда B после анализа `raw_ocr` и поиска в БД уверен, что сниимок относится к конкретному заказу.

```python
class LinkScreenshot(BaseModel):
    sha256: str
    order_number: str
    reason: str   # короткий текст: "matched by title and total"
```

INSERT в `screenshot_links` с `linked_by='agent'`. Если уже связан — DO NOTHING.

#### 6.3.4. mark_unlinked

Когда B **не смог** привязать снимок ни к одному заказу. После вызова бэкенд переводит снимок в `agent_status='done'` (он уже обработан; просто не нашёл заказа). В `screenshot_links` записи нет. Снимок будет виден в выборке «висящих» снимков для пользователя.

```python
class MarkUnlinked(BaseModel):
    sha256: str
    reason: str   # "no order_number visible, no title match in DB"
```

Никаких записей в дополнительные таблицы это не создаёт; просто терминирует обработку для данного снимка.

#### 6.3.5. delete_order

Soft-delete заказа. Вызывается только по явной просьбе пользователя в чате.

```python
class DeleteOrder(BaseModel):
    order_number: str
    reason: str
```

UPDATE `orders SET deleted_at = now()`. Триггер `log_changes_orders` зафиксирует изменение `deleted_at`. Заказ исчезает из `orders_view` (там фильтр `deleted_at IS NULL`). Hard-delete не предусмотрен.

### 6.4. Поведение агента — тихие сценарии

**Чистый батч.** Все снимки распознались, все привязались, противоречий нет. B пишет одну строку: «Принято 70 снимков → 12 новых заказов, 3 обновлено, 55 без изменений». И всё.

**Снимок не-eBay.** Бэкенд не вызывает B вообще — снимок отсекается на стадии после A (`is_ebay=false`). В сводке батча такие снимки попадают в счётчик «не похоже на eBay: N».

**Снимок без `order_number`.** B вызывает `sql` с поиском по title. Если уверенный матч (например, единственный заказ с тем же title и close к total) — `link_screenshot`. Если не уверенный — `mark_unlinked`. В сводке батча — счётчик «не привязано: M».

**Противоречие в данных.** Например, новый снимок показывает `tracking_number=X`, но в `tracking_numbers` для этого заказа уже есть `X` с другим carrier. B пишет одну строку в чат: «Заказ XX-…: видно tracking X с carrier USPS, в БД — UPS. Какой правильный?». Дальше ждёт ответа в следующем сообщении.

**Корректировка от пользователя.** Пользователь пишет «у XX-12345-67890 цена была $30, а не $35». B вызывает `save_order_data(order_number='XX-12345-67890', total=Decimal('30.00'), source='user_chat')`. Триггер запишет в `change_log` `source='user_chat'`. B пишет «обновил».

**Запрос данных.** Пользователь пишет «сколько я потратил у sarah_middleton_2024». B пишет `sql("SELECT sum(effective_total) FROM orders_view WHERE sold_by = $1", ['sarah_middleton_2024'])`, читает результат, отвечает «$340.50 за всё время».

---

## 7. Конфиг

Один YAML-файл, читается при старте бэкенда. Лежит рядом с кодом. Никакой UI-настройки моделей, никакого селектора в чате. Поменять — отредактировать файл, перезапустить.

```yaml
openrouter:
  api_key_env: OPENROUTER_API_KEY
  base_url: "https://openrouter.ai/api/v1"

models:
  ocr:
    name: "moonshotai/kimi-k2.6"
    thinking: false
    max_tokens: 16000
  agent:
    name: "moonshotai/kimi-k2.6"
    thinking: true
    max_tokens: 16000

concurrency:
  ocr: 10        # одновременных вызовов A
  agent: 5       # одновременных вызовов B (один agent-call обрабатывает группу снимков одной сессии)

postgres:
  host: "2.26.53.128"
  port: 5405
  user: "admin"
  password_env: "POSTGRES_PASSWORD"
  database: "ebay_orders"

chat:
  session_ttl_hours: 24

ocr_prompt_version: "v1"
agent_prompt_version: "v1"
```

`thinking` управляет параметром `extra_body.thinking.type` в запросах: `false` → `disabled`, `true` → `enabled`. У K2.6 `temperature` фиксирован в 1.0 моделью; параметр в конфиг не выносим. `max_tokens` обязан быть ≥ 16000 для thinking-режима — это требование Moonshot. OPENROUTER_API_KEY и POSTGRES_PASSWORD читаются из переменных окружения, в YAML не хранятся.

---

## 8. Backend

Python 3.11+, FastAPI, asyncio. Один процесс. Внутри — один HTTP endpoint для чата, два пула воркеров (для стадии A и стадии B), один бэкграунд-таск для очистки сессий.

### 8.1. Endpoints

**`POST /api/chat`** — единственный endpoint, через который ходит фронт. Тело запроса — массив `messages` в формате Vercel AI SDK UIMessage v5 (роли user/assistant/system, parts с типами text и file). К запросу через query-параметр или header передаётся `session_id` (uuid). Ответ — поток данных в формате Vercel AI SDK Data Stream Protocol (SSE с заголовком `x-vercel-ai-ui-message-stream: v1`).

Логика endpoint:

1. Проверить, что `session_id` существует в `chat_sessions`. Если нет — INSERT.
2. UPDATE `chat_sessions.last_used_at = now()`.
3. INSERT всех новых сообщений из тела (берём по `id`, которых ещё нет в `chat_messages` для этой сессии).
4. Если в последнем user-сообщении есть file-parts — для каждого: вычислить sha256, INSERT в `screenshots` через `ON CONFLICT DO NOTHING`. Сохранить связь `chat_messages.attached_sha256 = ARRAY[...]`.
5. Открыть SSE-стрим в ответ.
6. Запустить async-задачу «обработай эту сессию»: дождаться, пока стадии A и B закончат для всех `attached_sha256` этой сессии (либо появятся в `screenshots` и переедут в `agent_status='done'`).
7. По мере обработки эмитить data parts: `{type: 'data-progress', data: {ocr_done: 12, ocr_total: 70, agent_done: 8, agent_total: 70}}`.
8. Когда все снимки сессии прошли стадию B, вызвать B ещё раз с целью **сформировать финальную сводку**: передать историю чата + краткую статистику (сколько создано, обновлено, не привязано) → B пишет одну строку → стримим её как `text`-part и сохраняем в `chat_messages`.
9. Закрыть стрим.

Если пользователь закрыл вкладку (стрим разорван) — обработка снимков **продолжается** в воркерах (она независима от стрима). При следующем открытии страницы пользователь получит новый `session_id` и не увидит хвостов; зато данные уже в БД, можно через чат их запросить.

### 8.2. Воркеры

**Воркер стадии A.** Бесконечный цикл: `SELECT sha256, bytes, mime FROM screenshots WHERE ocr_status='pending' ORDER BY uploaded_at LIMIT N FOR UPDATE SKIP LOCKED`. Параллельность контролируется `asyncio.Semaphore(N)` с `N = concurrency.ocr`. Для каждого снимка: переводим в `running`, дёргаем Kimi K2.6 через OpenRouter с промптом A и data URL картинки, получаем JSON, валидируем по Pydantic-схеме, INSERT в `raw_ocr`, переводим `ocr_status='done'` (или `failed`).

**Воркер стадии B.** Запускается при наличии в БД хотя бы одной записи с `ocr_status='done' AND agent_status='pending'`, **сгруппировано по сессиям чата**. Для каждой сессии один вызов B, в котором передаём ему историю + список pending-sha256 этой сессии. Результат обработки: B вызывает tools, после `link_screenshot`/`mark_unlinked`/`save_order_data` бэкенд сам обновляет `agent_status='done'`. Параллельность по сессиям — `concurrency.agent`.

Если снимок не привязан ни к какой сессии (например, сессия была удалена за TTL) — он всё равно обрабатывается, но через специальную «фоновую» сессию без чата; B всё равно может его привязать или mark_unlinked, просто без сводки в чат.

### 8.3. Очистка чат-сессий

Один бэкграунд-таск в FastAPI запускается при старте, спит час, выполняет:

```sql
DELETE FROM chat_sessions WHERE last_used_at < now() - interval '24 hours';
```

CASCADE удаляет связанные `chat_messages`. Скриншоты и заказы это не трогает.

### 8.4. Vercel AI SDK Data Stream Protocol

Бэкенд формирует поток в формате, который ждёт `useChat` v5 на фронте. Это означает: каждое event — отдельная строка SSE с типом и payload. Текст ассистента отдаётся через события `text-start`/`text-delta`/`text-end`. Прогресс-обновления — через `data-progress` (это data-часть со своим typed payload, фронт умеет рендерить как кастомный компонент). Завершение — событие `finish`. Заголовок `x-vercel-ai-ui-message-stream: v1` обязателен. Подробности и точные имена событий — по официальной документации Vercel AI SDK Stream Protocol; реализуем «как написано в доках, без своих фантазий».

---

## 9. Frontend

Next.js 14+ App Router, одна страница `/`. Никакой авторизации, никакого роутинга кроме корня.

Главный компонент — чат-интерфейс на хуке `useChat` из пакета `ai` v5. Подключён к `/api/chat`. Транспорт — стандартный (Data Stream Protocol). Сообщения рендерятся как пузырьки с поддержкой text-частей и кастомных data-частей.

**Drag & drop файлов и папок.** На странице — большая drop-зона. Поддерживается перетаскивание как отдельных файлов, так и папки целиком (через `webkitGetAsEntry()` API). Все файлы превращаются в data URL'ы через FileReader и прикладываются к следующему сообщению как file-parts. Фильтр по mime: только PNG, JPEG, WEBP, GIF.

**Прогресс-пузырёк.** Когда в стриме приходят `data-progress` события, отображается отдельным пузырьком ассистента с двумя строками: «OCR: 23/70», «Запись: 8/70». Пузырёк обновляется in-place. Когда обработка завершена, пузырёк сменяется финальной строкой-сводкой (тоже отдельное assistant-сообщение).

**Сессия чата на каждое открытие страницы.** При первом рендере страница генерирует `session_id = crypto.randomUUID()` и держит его в React state. При перезагрузке страницы — новый id. В localStorage не сохраняется.

**Приём drag для папки.** Если пользователь перетащил папку — рекурсивно собираем все файлы внутри, фильтруем по mime, прикрепляем все. UI показывает один общий счётчик «прикреплено: 73 файла» и кнопку «отправить». Превью миниатюр не показываем, чтобы не нагружать DOM на 70+ картинках.

---

## 10. Чат-сессии

Одна сессия на одно открытие страницы. Идентификатор — UUID, генерируется на фронте при первом рендере. На каждое сообщение фронт отсылает `session_id` бэкенду. Бэкенд при первом сообщении создаёт строку в `chat_sessions`, при каждом следующем — обновляет `last_used_at`. Сессии без активности 24+ часа удаляются периодической задачей бэкенда. Каскадно удаляются их сообщения.

Скриншоты не привязаны к сессиям жёстко — есть только косвенная связь через `chat_messages.attached_sha256`. После удаления сессии скриншоты остаются в БД и продолжают участвовать в `orders_view`. Это сознательно: данные о заказах долгоживущие, чат — нет.

---

## 11. Открытые вопросы

Это пункты, которые осознанно отложены до момента имплементации. Решаются ровно тогда, когда нужно начинать соответствующий код, и фиксируются в этом документе изменением соответствующего раздела.

1. **Точный текст промпта A.** Принципы зафиксированы в §5.2. Финальная формулировка пишется в момент кодирования стадии A с прогоном на 5–10 реальных скриншотов из `last_photos/` и проверкой, что JSON валидный и корректный. После согласования текст промпта попадает в код и в этот раздел спеки заменяет принципы.
2. **Точный текст system prompt B.** Принципы зафиксированы в §6.2. Финальный текст пишется параллельно с кодированием B, тестируется на сценариях из §6.4, и попадает в код. Тогда же в этот раздел приходит финальный текст.
3. **Формат ошибок от Postgres-триггеров и CHECK-ограничений для B.** При нарушении CHECK или конфликте уникальности Postgres возвращает текст ошибки. B должен уметь интерпретировать его и либо переформулировать вопрос пользователю, либо сообщить о проблеме в чат. Конкретные форматы пишутся при имплементации `save_order_data`, тогда же фиксируется в `change_log` через `source` и в system prompt B как примеры.
4. **Граница «уверенно нашёл» при привязке снимка по title.** B сам решает, привязывать ли снимок без `order_number` по title-совпадению. Точный порог уверенности (один кандидат vs несколько, точное совпадение vs `similarity > 0.X`) фиксируется при написании system prompt B.
5. **Точные тексты сообщений B в чат.** Финальные формулировки сводок и вопросов фиксируются при имплементации B.
6. **Когда стартует воркер стадии B относительно стрима.** Альтернативы: (а) дожидаемся всех A для сессии, потом один вызов B; (б) пускаем B по мере готовности групп по `order_number`. Решается при написании воркеров с замером латенси на реальных батчах.
7. **Деталь UI «прогресс-пузырёк»: как именно его рендерить через `data-*` парты.** Зависит от точной формы Data Stream Protocol — реализуется по официальной документации Vercel AI SDK на момент кодинга.
