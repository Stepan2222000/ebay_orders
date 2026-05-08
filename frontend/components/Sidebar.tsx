"use client";
import { useCallback, useEffect, useState } from "react";
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

  const reload = useCallback(async () => {
    try {
      const list = await fetchScreenshots();
      setItems(list);
    } catch {
      // молча — ConnectionBanner покажет если сервер недоступен
    }
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  return (
    <aside className={styles.sidebar} data-testid="sidebar">
      <header className={styles.header}>
        <h1 className={styles.title}>Скриншоты.</h1>
      </header>

      <StatsRow onChange={reload} />

      <DropZone onUploaded={reload} />

      <div className={styles.list} data-testid="screenshot-list">
        {items.length === 0 ? (
          <p className={styles.empty}>Здесь будут карточки загруженных скриншотов.</p>
        ) : (
          items.map((s) => (
            <ScreenshotCard
              key={s.sha}
              screenshot={s}
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
        />
      ) : null}
    </aside>
  );
}
