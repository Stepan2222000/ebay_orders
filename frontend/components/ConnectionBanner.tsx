"use client";
import { useEffect, useState } from "react";
import { API } from "@/lib/api";
import { useSSE } from "@/lib/sse";
import styles from "./ConnectionBanner.module.css";

// Подписан на тот же общий /api/status/stream через useSSE — НЕ открывает свой
// EventSource (browser pool 6/origin под HTTP/1.1, см. lib/sse.tsx).
// При полном отсутствии событий за 8с показываем баннер. Бэк гарантирует
// первое сообщение сразу после connect и data-heartbeat каждые ~15с.
const LOST_GRACE_MS = 8000;

export default function ConnectionBanner() {
  const [lost, setLost] = useState(false);
  const [tick, setTick] = useState(0);

  useSSE(`${API}/status/stream`, () => {
    setLost(false);
    setTick((t) => t + 1);
  });

  useEffect(() => {
    const t = setTimeout(() => setLost(true), LOST_GRACE_MS);
    return () => clearTimeout(t);
  }, [tick]);

  if (!lost) return null;

  return (
    <div className={styles.banner} data-testid="connection-banner">
      Связь с сервером потеряна. Жду восстановления…
    </div>
  );
}
