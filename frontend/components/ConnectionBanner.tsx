"use client";
import { useEffect, useState } from "react";
import { API } from "@/lib/api";
import styles from "./ConnectionBanner.module.css";

// Мы и так держим долгоживущий SSE-канал на /api/status/stream — переиспользуем
// его для определения «связь жива». Раньше был отдельный fetch-ping каждые 3с,
// который в Safari вставал в очередь за thumbnails (http/1.1 connection limit)
// и выдавал ложные «связь потеряна». По SPEC ^связь — банер появляется без
// авто-повторов и снимается, когда стрим снова идёт; никаких retry-крутилок.
const LOST_GRACE_MS = 8000;

export default function ConnectionBanner() {
  const [lost, setLost] = useState(false);

  useEffect(() => {
    let stopped = false;
    let lostTimer: ReturnType<typeof setTimeout> | null = null;
    let es: EventSource | null = null;

    const markAlive = () => {
      if (lostTimer) {
        clearTimeout(lostTimer);
        lostTimer = null;
      }
      setLost(false);
    };

    const open = () => {
      if (stopped) return;
      es = new EventSource(`${API}/status/stream`);
      es.onopen = markAlive;
      es.onmessage = markAlive;
      es.onerror = () => {
        if (stopped || lostTimer) return;
        lostTimer = setTimeout(() => setLost(true), LOST_GRACE_MS);
      };
    };

    open();
    return () => {
      stopped = true;
      if (lostTimer) clearTimeout(lostTimer);
      es?.close();
    };
  }, []);

  if (!lost) return null;

  return (
    <div className={styles.banner} data-testid="connection-banner">
      Связь с сервером потеряна. Жду восстановления…
    </div>
  );
}
