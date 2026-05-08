"use client";
import { useEffect, useState } from "react";
import { Sparkles } from "lucide-react";
import { API } from "@/lib/api";
import type { Stats } from "@/lib/types";
import styles from "./AssemblingIndicator.module.css";

// Pill в шапке чата. Источник — /api/status/stream payload (поля
// agent_active + agent_thinking). Виден пока крутится агентская сессия;
// текст меняется в зависимости от того, идёт ли сейчас LLM-вызов или
// выполняется tool. Один компонент, один источник правды.

export default function AssemblingIndicator() {
  const [active, setActive] = useState(false);
  const [thinking, setThinking] = useState(false);

  useEffect(() => {
    let stopped = false;
    let backoff = 1000;
    let es: EventSource | null = null;

    const open = () => {
      if (stopped) return;
      es = new EventSource(`${API}/status/stream`);
      es.onmessage = (e) => {
        try {
          const s = JSON.parse(e.data) as Stats;
          setActive(!!s.agent_active);
          setThinking(!!s.agent_thinking);
          backoff = 1000;
        } catch {}
      };
      es.onerror = () => {
        es?.close();
        es = null;
        if (!stopped) setTimeout(open, backoff);
        backoff = Math.min(backoff * 2, 8000);
      };
    };
    open();
    return () => {
      stopped = true;
      es?.close();
    };
  }, []);

  if (!active) return null;

  return (
    <span className={styles.pill} data-testid="assembling-indicator" data-thinking={thinking}>
      <span className={styles.spark}>
        <Sparkles size={12} />
      </span>
      {thinking ? "агент думает…" : "агент работает…"}
    </span>
  );
}
