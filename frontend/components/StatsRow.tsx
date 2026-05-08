"use client";
import { useEffect, useRef, useState } from "react";
import { API } from "@/lib/api";
import type { Stats } from "@/lib/types";
import styles from "./StatsRow.module.css";

type CounterKey = "pending" | "running" | "done" | "failed";

const LABELS: Record<CounterKey, string> = {
  pending: "ожидает",
  running: "идёт",
  done: "готово",
  failed: "ошибка",
};
const ORDER: CounterKey[] = ["pending", "running", "done", "failed"];

export default function StatsRow({ onChange }: { onChange?: (s: Stats) => void }) {
  const [stats, setStats] = useState<Stats>({
    pending: 0,
    running: 0,
    done: 0,
    failed: 0,
    assembling: 0,
    agent_active: false,
    agent_thinking: false,
  });
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;

  useEffect(() => {
    let es: EventSource | null = null;
    let stopped = false;
    let backoff = 1000;

    const open = () => {
      if (stopped) return;
      es = new EventSource(`${API}/status/stream`);
      es.onmessage = (e) => {
        try {
          const next = JSON.parse(e.data) as Stats;
          setStats(next);
          onChangeRef.current?.(next);
          backoff = 1000;
        } catch {
          // игнорируем мусор
        }
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

  return (
    <div className={styles.row} data-testid="stats" data-json={JSON.stringify(stats)}>
      {ORDER.map((k) => (
        <div
          key={k}
          className={`${styles.cell} ${styles[k]}`}
          data-testid={`stats-${k}`}
          aria-label={LABELS[k]}
        >
          <span className={styles.num}>{stats[k]}</span>
          <span className={styles.lab}>{LABELS[k]}</span>
        </div>
      ))}
    </div>
  );
}
