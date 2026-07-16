"""Автодифф боевого кода агента с эталоном прототипа (article_truth, этап 3).

Гоняет app.truth.run_listing в СУХОМ режиме по когорте эталона
(article_truth/probes/dry_run_v2_results.json) и сравнивает:
- вердикт (эталонный conflict_uncovered считается conflict);
- состав {part_id: qty} для linked;
- набор канонических номеров вне каталога для not_in_catalog.
Тексты (note/comment) не сравниваются — LLM недетерминирован.

Расхождение хоть по одному листингу = ненулевой exit-код, включать запись нельзя.
Запуск из корня проекта: `python db/truth_diff.py [путь-к-эталону]`.
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import close, pool  # noqa: E402
from app.truth import run_listing  # noqa: E402

DEFAULT_BASELINE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "article_truth", "probes", "dry_run_v2_results.json")
CONCURRENCY = 4


def _norm_verdict(v: str) -> str:
    return "conflict" if v == "conflict_uncovered" else v


def _linked_parts(positions: list[dict]) -> dict[str, int]:
    agg: dict[str, int] = {}
    for p in positions:
        if p.get("part_id"):
            agg[p["part_id"]] = agg.get(p["part_id"], 0) + max(1, p.get("qty", 1))
    return agg


def _missing(positions: list[dict]) -> set[str]:
    return {(p.get("canonical") or p.get("article_read") or "").upper()
            for p in positions if not p.get("part_id")}


async def main() -> None:
    import logging
    logging.basicConfig(level=logging.WARNING)
    baseline_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BASELINE
    baseline = {r["item_number"]: r for r in json.load(open(baseline_path))
                if r.get("verdict") not in (None, "ERROR")}
    print(f"эталон: {len(baseline)} листингов из {baseline_path}")

    sem = asyncio.Semaphore(CONCURRENCY)
    results: dict[str, dict] = {}

    async def one(num: str) -> None:
        async with sem:
            res = await run_listing(num, write=False)
            results[num] = res or {"verdict": "MISSING"}
            print(f"  [{len(results)}/{len(baseline)}] {num}: {results[num]['verdict']}",
                  flush=True)

    await pool()
    await asyncio.gather(*(one(n) for n in baseline))

    diffs: list[str] = []
    for num, base in sorted(baseline.items()):
        new = results.get(num) or {}
        bv, nv = _norm_verdict(base["verdict"]), _norm_verdict(new.get("verdict", "MISSING"))
        if bv != nv:
            diffs.append(f"{num}: вердикт {bv} -> {nv}")
            continue
        if nv == "linked":
            bp, np_ = _linked_parts(base.get("positions", [])), _linked_parts(new.get("positions", []))
            if bp != np_:
                diffs.append(f"{num}: состав {bp} -> {np_}")
        elif nv == "not_in_catalog":
            bm, nm = _missing(base.get("positions", [])), _missing(new.get("positions", []))
            if bm != nm:
                diffs.append(f"{num}: не-в-каталоге {sorted(bm)} -> {sorted(nm)}")

    print(f"\n=== ИТОГ: расхождений {len(diffs)} из {len(baseline)} ===")
    for d in diffs:
        print("  DIFF:", d)
    await close()
    sys.exit(1 if diffs else 0)


if __name__ == "__main__":
    asyncio.run(main())
