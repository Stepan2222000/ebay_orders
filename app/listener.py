"""Один LISTEN-коннект + asyncio.Queue fan-out для всех SSE-подписчиков.

Зачем: раньше каждый /api/*/stream открывал свой `asyncpg.connect()` мимо пула.
При silent client disconnect (background-таб, потеря wifi) ASGI http.disconnect
не приходит до TCP keepalive (на macOS = 2ч), и PG-коннект остаётся висеть idle.
Замер показывал 32+ висячих коннекта.

Решение: один dedicated `asyncpg.Connection` с LISTEN на все нужные каналы,
SSE-handler'ы получают payload через `asyncio.Queue`. Pool sql-запросов
(`_status_dict`, `_chat_history`) идёт через общий пул.

asyncpg не делает auto-rebind после reconnect — поэтому держим watchdog с
`SELECT 1` каждые 30с и сами re-add_listener при падении.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Iterable

import asyncpg

log = logging.getLogger(__name__)


class PgFanout:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        user: str,
        password: str,
        database: str,
        channels: Iterable[str],
    ) -> None:
        self._cfg = dict(
            host=host, port=port, user=user, password=password, database=database
        )
        self._channels: list[str] = list(channels)
        self._subscribers: dict[str, set[asyncio.Queue]] = {
            ch: set() for ch in self._channels
        }
        self._conn: asyncpg.Connection | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    # ─── lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        await self._connect()
        self._watchdog_task = asyncio.create_task(self._watchdog())

    async def close(self) -> None:
        self._stop.set()
        t = self._watchdog_task
        if t:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        if self._conn is not None:
            try:
                await self._conn.close()
            except Exception:
                pass
            self._conn = None

    async def _connect(self) -> None:
        self._conn = await asyncpg.connect(**self._cfg)
        for ch in self._channels:
            await self._conn.add_listener(ch, self._on_notify)
        log.info("fanout connected, listening on: %s", self._channels)

    async def _reconnect(self) -> None:
        if self._conn is not None:
            try:
                await self._conn.close()
            except Exception:
                pass
            self._conn = None
        while not self._stop.is_set():
            try:
                await self._connect()
                return
            except Exception as e:
                log.warning("fanout reconnect failed: %s — retry in 2s", e)
                await asyncio.sleep(2)

    # ─── pubsub ───────────────────────────────────────────────────────────

    def _on_notify(
        self,
        _conn: asyncpg.Connection,
        _pid: int,
        channel: str,
        payload: str,
    ) -> None:
        # asyncpg вызывает sync-callback из протокола, нельзя await q.put().
        # Bounded queue + drop on overflow: медленный потребитель отвалится сам
        # по 15s-таймауту в SSE-генераторе.
        for q in list(self._subscribers.get(channel, ())):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    def subscribe(self, *channels: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        for ch in channels:
            self._subscribers.setdefault(ch, set()).add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        for s in self._subscribers.values():
            s.discard(q)

    # ─── watchdog ─────────────────────────────────────────────────────────

    async def _watchdog(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=30.0)
                    return  # stop set
                except asyncio.TimeoutError:
                    pass
                if self._conn is None:
                    await self._reconnect()
                    continue
                try:
                    await asyncio.wait_for(
                        self._conn.execute("SELECT 1"), timeout=5.0
                    )
                except Exception as e:
                    log.warning("fanout watchdog: %s — reconnecting", e)
                    await self._reconnect()
        except asyncio.CancelledError:
            return
