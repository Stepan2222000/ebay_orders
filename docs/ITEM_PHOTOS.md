# Фото товаров (item photos)

Показ фотографий объявлений eBay (и ручных снимков) рядом с распознанными
товарами. Цель: в блоке «Товары» детали скриншота у каждого товара видна
галерея — как он выглядел в объявлении на eBay, плюс снимки, добавленные вручную.

Решения ниже согласованы и проверены в терминале на живом окружении
(MinIO `2.27.20.221:9000`, БД `2.27.20.221`, eBay `itm.ebaydesc.com`).

## Архитектура — лёгкий путь, владелец `ebay_orders`

Фото **не** идут через `ebay_data`. Полный парс PDP (`fetch_item_page`) хрупок —
на живых листингах падает `ParseError` ~2 из 7 (вёрстка eBay). Для фоток он не
нужен: берём только URL картинок лёгким `ebay_library.fetch_image_urls(item_id)`
(один GET, без браузера/прокси, на замере 222/223 успешно). Индекс ссылок
храним в `ebay_orders` — это данные о заказах, их из eBay не вытащить (ручные),
и другим логикам достаточно общих файлов в MinIO + чтения нашей таблицы.

```
OCR-done ──observed.item_number──▶ fetch_image_urls ──▶ скачать ──▶ MinIO ──▶ item_photos
                                   (ebay_library)        (fetch_images)  (S3Photos)   (ebay_orders)
ручная загрузка ──▶ конверт в JPEG ──▶ MinIO (свой бакет) ──▶ item_photos(source=manual)
фронт ◀── https-прокси бэкенда ◀── MinIO (http)
```

## Источники фото

- **eBay** (`source='ebay'`): галерея объявления, `s-l1600`. Качаем при появлении
  `item_number` (см. триггер). Файлы → бакет `ebay-data-photos`, ключ
  `{item_number}/{md5(ebay_url)}.jpg` (конвенция `ebay_data`/`ebay_library`).
- **Ручные** (`source='manual'`): снимки пользователя, грузятся через UI **после**
  транскрибации. Любой вход (PNG/JPEG/WebP/**HEIC**) → конверт в JPEG (`Pillow` +
  `pillow-heif`), кап **1600** по длинной стороне, q≈85, EXIF срезаем. Файлы →
  отдельный бакет `ebay-orders-my-photos`, ключ `{item_number}/{md5(bytes)}.jpg`.

Привязка обеих веток — к `item_number` (id объявления). В любой плашке с тем же
номером — те же фото. Порядок в галерее: сначала eBay (по `idx`), потом ручные.

## Хранилище (MinIO)

- endpoint (заливка/прокси): `EBAY_S3_ENDPOINT`, дефолт `http://2.27.20.221:9000`
  (внутри docker позже можно `http://minio:9000`); `public_base_url` — внешний.
- бакеты `public-read` (как соседние): `ebay-data-photos` (есть),
  `ebay-orders-my-photos` (создать). Заливка через `ebay_library.S3Photos`
  с переопределением `S3Config(bucket=..., public_base_url=...)`.

## Схема БД (`ebay_orders`, миграция 007)

Ключ — `item_number`, **без FK на `items`**: строки `items` создаёт `save_order`
(после сборки), а фото триггерятся раньше (OCR-done). FK плодил бы сирот и портил
счётчики матчинга.

```
listing_photos(            -- статус скачивания eBay-галереи по номеру
  item_number   text PK,
  ebay_status   text  CHECK in ('pending','done','failed'),
  attempts      int,
  last_error    text,
  fetched_at    timestamptz
)
item_photos(              -- сама галерея (eBay + ручные)
  id           bigserial PK,
  item_number  text NOT NULL,
  source       text CHECK in ('ebay','manual'),
  idx          int,                 -- порядок внутри источника
  s3_url       text NOT NULL,       -- полный публичный URL в MinIO
  url_hash     bytea NOT NULL,      -- md5(ebay_url) или md5(bytes); дедуп
  ebay_url     text,                -- только для source='ebay' (трассировка)
  created_at   timestamptz DEFAULT now(),
  UNIQUE (item_number, source, url_hash)
)
```

## Триггер скачивания eBay

- Инлайн в OCR-воркере (`app/worker.py`): после коммита `raw_ocr` (`ocr_status=done`),
  для каждого `observed.items[].item_number` — best-effort шаг. Не валит OCR.
- Идемпотентность по `listing_photos`: качаем только если номера там нет или он
  `pending` (транзиент). Один номер встречается на многих скриншотах — качаем раз.
- Конкуренция фото-операций (eBay+MinIO) — общий семафор **6** (замер: 223 шт. за
  111с, eBay не блокнул). Защита от «шторма» (ср. коммит 271a868).
- Семантика ошибок: `404`/`ItemEnded`/`ErrorPageError` → `failed` навсегда (не
  ретраим); транзиент (сеть/`503`/таймаут) → `pending` (повтор при след. встрече);
  живой с 0 картинок → `done` с нулём.

`item_number` выбивается OCR'ом в 99% (378/380), 223 уникальных — покрытие годное.

## Ручная загрузка

- Эндпоинт: `POST /api/listings/{item_number}/photos` (multipart, можно несколько).
  Конверт в JPEG (кап 1600) → MinIO `ebay-orders-my-photos` → строка `item_photos`.
- Удаление: `DELETE` своей строки (+ объект в MinIO). eBay-строки не трогаем.

## Показ на фронте

- Единственная плашка товара — блок «Товары» в `ScreenshotDetailModal`
  (`observed.items`). Отдельной страницы заказа нет; галерею вешаем сюда.
- Общий компонент-галерея по `item_number`: ряд превью (eBay, затем ручные с
  бейджем «моё»), клик → лайтбокс. Кнопка «+ добавить фото» (ручная загрузка).
- **Mixed content**: сайт по https, MinIO по http → браузер блокирует `<img http>`.
  Картинки отдаём **через бэкенд по https** (как уже сделано для скриншотов):
  `GET /api/listings/{item_number}/photos/{id}/image` — бэкенд тянет из MinIO
  (http, серверно) и стримит. Фронт MinIO напрямую не трогает.

## Упаковка / зависимости

- `ebay-library @ git+https://github.com/Stepan2222000/ebay-library.git@399c42d`
  в `pyproject` (паттерн `parser_ebay`); `git` в `backend/Dockerfile`. Проверено:
  ставится в чистый venv, импортит `fetch_image_urls`/`S3Photos`.
- Доп. зависимости `ebay_orders`: `Pillow`, `pillow-heif` (HEIC).

## Бэкофилл (после реализации)

Разовый скрипт по всем 223 номерам: у живых (на замере 151/223, 520 фоток)
записать в `item_photos`/`listing_photos`, мёртвые (71) — `failed`. Инлайн-триггер
существующие скриншоты не покрывает (OCR давно `done`), а 68% листингов ещё живы и
вымирают — поэтому бэкофилл нужен, но запускается уже после готовой реализации.

## Порядок реализации

1. `pyproject` (пин либы + Pillow/pillow-heif) + `Dockerfile` (`git`).
2. Миграция 007 (`item_photos`, `listing_photos`).
3. Инлайн-фетч eBay в OCR-пайплайн (семафор, статусы, идемпотентность).
4. Ручная загрузка: upload-эндпоинт + конвертация + бакет `ebay-orders-my-photos`.
5. API отдачи галереи + https-прокси картинок.
6. Фронт: компонент-галерея в блоке «Товары».
7. Бэкофилл-скрипт (запуск — после).
