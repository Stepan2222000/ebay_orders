"""Привязка eBay-листингов к деталям каталога `smart` по артикулу.

Матчинг — по regex-правилам из `brands_fdw.article_match_rules` (хранятся в
отдельной БД brands_mapping, видны через postgres_fdw). Каталог `smart` виден
через `smart_fdw`. Бренд НЕ определяется: прогоняем ВСЕ enabled-правила, кандидат
= склейка capture-групп; точный лукап по `smart_fdw.part_articles` (артикулы
уникальны → совпадение однозначно). Generic-фолбэка нет — правила правит человек
в БД.

Матчинг вызывается из save_order ОТДЕЛЬНЫМ шагом после коммита заказа
(best-effort): недоступность каталога/FDW не должна ронять сохранение заказа.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import asyncpg

_RULES_SQL = "SELECT name, find_regex FROM brands_fdw.article_match_rules WHERE enabled ORDER BY name"

_LOOKUP_SQL = """
SELECT upper(pa.article) AS art, pa.part_id, p.name, p.is_draft
  FROM smart_fdw.part_articles pa
  JOIN smart_fdw.parts p ON p.id = pa.part_id
 WHERE upper(pa.article) = ANY($1::text[])
"""

# Кеш скомпилированных правил на процесс. Сбрасывается refresh-флагом
# (бэкофилл после правки правил в БД).
_rules_cache: list[tuple[str, "re.Pattern[str]"]] | None = None


async def load_rules(conn: asyncpg.Connection) -> list[tuple[str, "re.Pattern[str]"]]:
    rows = await conn.fetch(_RULES_SQL)
    return [(r["name"], re.compile(r["find_regex"])) for r in rows]


async def get_rules(
    conn: asyncpg.Connection, *, refresh: bool = False
) -> list[tuple[str, "re.Pattern[str]"]]:
    global _rules_cache
    if _rules_cache is None or refresh:
        _rules_cache = await load_rules(conn)
    return _rules_cache


def extract_candidates(title: str, rules: list[tuple[str, "re.Pattern[str]"]]) -> set[str]:
    """Кандидаты-артикулы из титула: для каждого правила — склейка непустых
    capture-групп (нет групп → весь матч), пробелы убраны, uppercase."""
    text = title.upper()
    out: set[str] = set()
    for _name, rx in rules:
        for m in rx.finditer(text):
            groups = m.groups()
            cand = ("".join(g for g in groups if g) if groups else m.group(0))
            cand = cand.replace(" ", "").upper()
            if cand:
                out.add(cand)
    return out


@dataclass
class MatchResult:
    item_number: str
    status: str                 # 'linked' | 'needs_review' | 'not_in_catalog'
    parts: list[dict]           # [{part_id, matched_article, name, is_draft}]
    note: str | None

    def to_dict(self) -> dict:
        return {
            "item_number": self.item_number,
            "status": self.status,
            "parts": self.parts,
            "note": self.note,
        }


async def classify(
    conn: asyncpg.Connection,
    item_number: str,
    title: str,
    rules: list[tuple[str, "re.Pattern[str]"]],
) -> MatchResult:
    cands = extract_candidates(title, rules)
    if not cands:
        return MatchResult(
            item_number, "needs_review", [],
            "нет кандидата: нет артикула в титуле ИЛИ нужно правило",
        )
    rows = await conn.fetch(_LOOKUP_SQL, list(cands))
    # part_id -> (article, name, is_draft); один артикул на part, разные part = бандл
    by_part: dict[str, dict] = {}
    for r in rows:
        by_part.setdefault(r["part_id"], {
            "part_id": r["part_id"],
            "matched_article": r["art"],
            "name": r["name"],
            "is_draft": r["is_draft"],
        })
    if not by_part:
        return MatchResult(
            item_number, "not_in_catalog", [],
            f"кандидаты {sorted(cands)} не найдены в каталоге",
        )
    if len(by_part) == 1:
        return MatchResult(item_number, "linked", list(by_part.values()), None)
    parts = list(by_part.values())
    note = "bundle: " + ", ".join(f"{p['part_id']}({p['matched_article']})" for p in parts)
    return MatchResult(item_number, "needs_review", parts, note)


async def match_listing(
    conn: asyncpg.Connection,
    item_number: str,
    title: str,
    rules: list[tuple[str, "re.Pattern[str]"]],
) -> MatchResult:
    """Матчит листинг и записывает результат. Перетирает только свои
    regex_exact-строки; строки agent/human не трогает. Вызывать только для
    листингов в статусе 'pending'."""
    res = await classify(conn, item_number, title, rules)

    await conn.execute(
        "DELETE FROM item_parts WHERE item_number = $1 AND match_method = 'regex_exact'",
        item_number,
    )
    if res.status == "linked":
        for p in res.parts:
            await conn.execute(
                "INSERT INTO item_parts(item_number, part_id, matched_article, match_method) "
                "VALUES ($1, $2, $3, 'regex_exact') "
                "ON CONFLICT (item_number, part_id) DO NOTHING",
                item_number, p["part_id"], p["matched_article"],
            )
        await conn.execute(
            "UPDATE items SET match_status = 'linked', matched_at = now(), match_note = NULL "
            "WHERE item_number = $1",
            item_number,
        )
    else:
        # needs_review (в т.ч. бандл) и not_in_catalog: авто-строки НЕ пишем,
        # кандидаты бандла остаются в match_note — состав согласует агент с человеком.
        await conn.execute(
            "UPDATE items SET match_status = $2, matched_at = now(), match_note = $3 "
            "WHERE item_number = $1",
            item_number, res.status, res.note,
        )
    return res


async def _report_existing(conn: asyncpg.Connection, item_number: str, status: str) -> dict:
    """Раскладка уже разобранного листинга (дубль-листинг во втором заказе)."""
    rows = await conn.fetch(
        """
        SELECT ip.part_id, ip.matched_article, ip.match_method, p.name, p.is_draft
          FROM item_parts ip
          LEFT JOIN smart_fdw.parts p ON p.id = ip.part_id
         WHERE ip.item_number = $1
        """,
        item_number,
    )
    return {
        "item_number": item_number,
        "status": status,
        "parts": [dict(r) for r in rows],
        "note": "уже разобрано ранее",
    }


async def run_matching(
    pool: asyncpg.Pool,
    item_numbers: list[str],
    *,
    source: str = "screenshot",
) -> list[dict]:
    """Отдельная транзакция (после коммита заказа). Матчит только pending-листинги,
    уже разобранные — переиспользует (reuse). Возвращает matches[] для агента.
    Бросает исключение наверх при недоступности FDW — save_order ловит и считает
    матчинг отложенным."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f"SET LOCAL app.source = '{source}'")
            rules = await get_rules(conn)
            out: list[dict] = []
            for inum in item_numbers:
                row = await conn.fetchrow(
                    "SELECT item_title, match_status FROM items WHERE item_number = $1", inum
                )
                if row is None:
                    continue
                if row["match_status"] == "pending":
                    res = await match_listing(conn, inum, row["item_title"], rules)
                    out.append(res.to_dict())
                else:
                    out.append(await _report_existing(conn, inum, row["match_status"]))
            return out
