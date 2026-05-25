"""Полный тест матчинга против ЖИВЫХ БД, целиком в транзакции с ROLLBACK.

Ничего не коммитит: внутри одной транзакции на ebay_orders поднимает FDW (smart +
brands_mapping), применяет схему 006 (items/item_parts + бэкофилл из order_items),
затем гоняет реальные функции app.matching по всем листингам и проверяет:
  - per-rule извлечение кандидатов (чистая функция);
  - классификацию и регрессию 181 linked / 2 bundle / 35 no-hit;
  - запись item_parts и отчёт-JOIN «предметы в заказе».

Запуск из корня: `python db/test_matching.py`.
"""
import asyncio
import os
import sys

import asyncpg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings
from app.matching import extract_candidates, get_rules, match_listing

_FDW_DDL = f"""
CREATE EXTENSION IF NOT EXISTS postgres_fdw;
CREATE SERVER smart_srv  FOREIGN DATA WRAPPER postgres_fdw OPTIONS (host '{settings.pg_host}', port '5402', dbname 'smart');
CREATE SERVER brands_srv FOREIGN DATA WRAPPER postgres_fdw OPTIONS (host '{settings.pg_host}', port '5411', dbname 'brands_mapping');
CREATE USER MAPPING FOR CURRENT_USER SERVER smart_srv  OPTIONS (user '{settings.pg_user}', password '{settings.pg_password}');
CREATE USER MAPPING FOR CURRENT_USER SERVER brands_srv OPTIONS (user '{settings.pg_user}', password '{settings.pg_password}');
CREATE SCHEMA smart_fdw;
CREATE SCHEMA brands_fdw;
IMPORT FOREIGN SCHEMA public LIMIT TO (parts, part_articles) FROM SERVER smart_srv INTO smart_fdw;
IMPORT FOREIGN SCHEMA public LIMIT TO (article_match_rules, brands, brand_aliases) FROM SERVER brands_srv INTO brands_fdw;
"""

_SCHEMA_006 = """
CREATE TABLE items (
    item_number text PRIMARY KEY, item_title text NOT NULL,
    match_status text NOT NULL DEFAULT 'pending'
        CHECK (match_status IN ('pending','linked','needs_review','no_article','not_in_catalog')),
    matched_at timestamptz, match_note text, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE item_parts (
    item_number text NOT NULL REFERENCES items(item_number) ON DELETE CASCADE,
    part_id text NOT NULL, quantity int NOT NULL DEFAULT 1 CHECK (quantity > 0),
    matched_article text, match_method text NOT NULL CHECK (match_method IN ('regex_exact','agent','human')),
    created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (item_number, part_id));
INSERT INTO items(item_number, item_title)
SELECT DISTINCT ON (item_number) item_number, item_title FROM order_items ORDER BY item_number;
ALTER TABLE order_items ADD CONSTRAINT order_items_item_fk FOREIGN KEY (item_number) REFERENCES items(item_number);
ALTER TABLE order_items DROP COLUMN item_title;
"""

# (title, expected candidate среди извлечённых) — покрывает все 14 правил
PER_RULE = [
    ("NEW OEM Mercury Quicksilver 8M0077471 Ignition Coil", "8M0077471"),
    ("OEM Quicksilver 47-8M0142980 Impeller Water Pump Kit", "8M0142980"),
    ("Quicksilver Mercury Seal Kit 26-88397A 1 Genuine", "88397A1"),
    ("Volvo Penta Gasket Set 876266-8", "876266"),
    ("Volvo Penta Seal 09-812B OEM", "09-812B"),
    ("SEASTAR SSC13416 16' BACKMOUNT RACK", "SSC13416"),
    ("Yamaha Outboard Impeller 4X7-13440-90-00 OEM", "4X7-13440-90-00"),
    ("Honda BF Marine Impeller Kit 06192-ZW9-020 Genuine", "06192-ZW9-020"),
    ("Suzuki DF Outboard Impeller 16510-61A21 NEW", "16510-61A21"),
    ("Polaris Drive Belt 1019520-067 OEM", "1019520-067"),
    ("SEA-DOO LINQ BAG 003-498 OEM", "003-498"),
]


class _Rollback(Exception):
    pass


async def main() -> None:
    conn = await asyncpg.connect(
        host=settings.pg_host, port=settings.pg_port, user=settings.pg_user,
        password=settings.pg_password, database=settings.pg_database,
    )
    failures: list[str] = []
    try:
        try:
            async with conn.transaction():
                await conn.execute("SET LOCAL app.source = 'screenshot'")
                await conn.execute(_FDW_DDL)
                await conn.execute(_SCHEMA_006)
                rules = await get_rules(conn, refresh=True)
                print(f"rules loaded from brands_fdw: {len(rules)}")

                # T1a: per-rule извлечение
                for title, expected in PER_RULE:
                    cands = extract_candidates(title, rules)
                    if expected.upper() not in cands:
                        failures.append(f"per-rule: {expected!r} не извлечён из {title!r} (got {sorted(cands)})")
                print(f"per-rule extraction: {len(PER_RULE) - len([f for f in failures if f.startswith('per-rule')])}/{len(PER_RULE)} ok")

                # T1b: классификация всех листингов + регрессия
                items = await conn.fetch("SELECT item_number, item_title FROM items")
                from collections import Counter
                tally: Counter[str] = Counter()
                for it in items:
                    res = await match_listing(conn, it["item_number"], it["item_title"], rules)
                    key = res.status if res.status != "needs_review" else (
                        "needs_review(bundle)" if res.parts else "needs_review(no-cand)")
                    tally[key] += 1
                print("classification:", dict(tally))

                linked = tally.get("linked", 0)
                bundle = tally.get("needs_review(bundle)", 0)
                nohit = tally.get("not_in_catalog", 0) + tally.get("needs_review(no-cand)", 0)
                if linked != 181: failures.append(f"regression: linked={linked}, ожидалось 181")
                if bundle != 2:   failures.append(f"regression: bundle={bundle}, ожидалось 2")
                if nohit != 35:   failures.append(f"regression: no-hit={nohit}, ожидалось 35")

                # item_parts: только linked дают строки (бандл — нет, кандидаты в note)
                n_ip = await conn.fetchval("SELECT count(*) FROM item_parts")
                if n_ip != 181: failures.append(f"item_parts={n_ip}, ожидалось 181 (бандлы без авто-строк)")
                bundle_rows = await conn.fetch(
                    "SELECT item_number, match_note FROM items WHERE match_status='needs_review' AND match_note LIKE 'bundle:%'")
                if len(bundle_rows) != 2: failures.append(f"bundle-notes={len(bundle_rows)}, ожидалось 2")

                # отчёт-JOIN
                rep = await conn.fetch("""
                    SELECT o.order_number, it.item_title, oi.item_quantity, p.name AS part_name, it.match_status
                    FROM order_items oi JOIN orders o USING (order_id) JOIN items it USING (item_number)
                    LEFT JOIN item_parts ip USING (item_number) LEFT JOIN smart_fdw.parts p ON p.id = ip.part_id
                    WHERE it.match_status='linked' LIMIT 3""")
                print("\nотчёт-JOIN (sample):")
                for r in rep:
                    print(f"  {r['order_number']}: {r['item_title'][:34]!r} -> {(r['part_name'] or '')[:30]!r}")
                raise _Rollback()
        except _Rollback:
            print("\nROLLBACK — ничего не закоммичено")
    finally:
        clean = await conn.fetchval("SELECT count(*) FROM information_schema.tables WHERE table_name='items'")
        await conn.close()
    print(f"после rollback items есть?: {clean}")
    if failures:
        print("\n❌ FAILURES:")
        for f in failures:
            print("  -", f)
        raise SystemExit(1)
    print("\n✅ ВСЕ ПРОВЕРКИ ЗЕЛЁНЫЕ")


if __name__ == "__main__":
    asyncio.run(main())
