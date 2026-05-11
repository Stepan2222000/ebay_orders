"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { fetchScreenshots } from "@/lib/api";
import type { Screenshot } from "@/lib/types";
import StatsRow from "./StatsRow";
import DropZone from "./DropZone";
import ScreenshotCard from "./ScreenshotCard";
import ScreenshotDetailModal from "./ScreenshotDetailModal";
import styles from "./Sidebar.module.css";

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

      <StatsRow onChange={reload} />

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
