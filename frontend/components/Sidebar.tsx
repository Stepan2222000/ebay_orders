"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { fetchScreenshots } from "@/lib/api";
import type { Screenshot } from "@/lib/types";
import StatsRow from "./StatsRow";
import DropZone from "./DropZone";
import ScreenshotCard from "./ScreenshotCard";
import ScreenshotDetailModal from "./ScreenshotDetailModal";
import styles from "./Sidebar.module.css";

// StatsRow зовёт onChange на КАЖДОЕ SSE-сообщение статуса (а их при обработке
// много). Перетягивать весь список скриншотов на каждый тик нельзя — тяжёлые
// GET'ы забивают лимит сокетов браузера и душат загрузку (POST не пролезает).
// Троттлим: список обновляется не чаще и не реже раза в этот интервал.
const SSE_RELOAD_MS = 1500;

export default function Sidebar() {
  const [items, setItems] = useState<Screenshot[]>([]);
  const [openSha, setOpenSha] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [activeQuery, setActiveQuery] = useState("");

  const activeQueryRef = useRef(activeQuery);
  activeQueryRef.current = activeQuery;

  const reload = useCallback(async () => {
    try {
      const list = await fetchScreenshots(activeQueryRef.current || undefined);
      setItems(list);
    } catch {
      // молча — ConnectionBanner покажет если сервер недоступен
    }
  }, []);

  // Троттл reload для частых SSE-тиков: leading + trailing, не чаще раза в
  // SSE_RELOAD_MS, но и не реже (чтобы финальное состояние всё равно доехало).
  const reloadAt = useRef(0);
  const reloadTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const throttledReload = useCallback(() => {
    const since = Date.now() - reloadAt.current;
    if (since >= SSE_RELOAD_MS) {
      reloadAt.current = Date.now();
      reload();
    } else if (reloadTimer.current === null) {
      reloadTimer.current = setTimeout(() => {
        reloadTimer.current = null;
        reloadAt.current = Date.now();
        reload();
      }, SSE_RELOAD_MS - since);
    }
  }, [reload]);

  useEffect(
    () => () => {
      if (reloadTimer.current) clearTimeout(reloadTimer.current);
    },
    [],
  );

  useEffect(() => {
    const t = setTimeout(() => setActiveQuery(query.trim()), 200);
    return () => clearTimeout(t);
  }, [query]);

  useEffect(() => {
    reload();
  }, [reload, activeQuery]);

  const isSearch = activeQuery.length > 0;

  return (
    <aside className={styles.sidebar} data-testid="sidebar">
      <header className={styles.header}>
        <h1 className={styles.title}>Скриншоты.</h1>
      </header>

      <StatsRow onChange={throttledReload} />

      <input
        type="search"
        className={styles.search}
        placeholder="Поиск по содержимому"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        data-testid="sidebar-search"
      />

      <DropZone onUploaded={reload} />

      <div className={styles.list} data-testid="screenshot-list">
        {items.length === 0 ? (
          <p className={styles.empty}>
            {isSearch
              ? `Ничего не нашлось по «${activeQuery}».`
              : "Здесь будут карточки загруженных скриншотов."}
          </p>
        ) : (
          items.map((s) => (
            <ScreenshotCard
              key={s.sha}
              screenshot={s}
              query={isSearch ? activeQuery : undefined}
              onClick={(x) => setOpenSha(x.sha)}
            />
          ))
        )}
      </div>

      {openSha ? (
        <ScreenshotDetailModal
          sha={openSha}
          onClose={() => setOpenSha(null)}
          onDeleted={() => {
            setOpenSha(null);
            reload();
          }}
          onRetried={reload}
        />
      ) : null}
    </aside>
  );
}
