"use client";
import { useEffect, useState } from "react";
import { API } from "@/lib/api";
import { useSSEStatus } from "@/lib/sse";
import styles from "./ConnectionBanner.module.css";

// Баннер отражает РЕАЛЬНОЕ состояние SSE-коннекта (EventSource), а не частоту
// сообщений: при разрыве EventSource шлёт onerror и сам уходит в авто-reconnect
// — это и есть «связь потеряна». На простаивающем здоровом бэке коннект остаётся
// open (heartbeat'ы держат его живым), поэтому баннер не появляется.
// SHOW_AFTER_MS — дебаунс: гасит мигание на коротких переподключениях.
const SHOW_AFTER_MS = 4000;

export default function ConnectionBanner() {
  const status = useSSEStatus(`${API}/status/stream`);
  const [show, setShow] = useState(false);

  useEffect(() => {
    if (status === "open") {
      setShow(false);
      return;
    }
    // connecting / lost: показываем с задержкой, чтобы не мигало на блипах
    const t = setTimeout(() => setShow(true), SHOW_AFTER_MS);
    return () => clearTimeout(t);
  }, [status]);

  if (!show) return null;

  return (
    <div
      className={styles.banner}
      role="status"
      aria-live="polite"
      data-testid="connection-banner"
    >
      Связь с сервером потеряна. Жду восстановления…
    </div>
  );
}
