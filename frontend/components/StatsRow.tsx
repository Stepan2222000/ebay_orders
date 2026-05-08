"use client";
import { useRef, useState } from "react";
import { API } from "@/lib/api";
import { useSSE } from "@/lib/sse";
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
    agent_total: 0,
    agent_done: 0,
    agent_failed: 0,
  });
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;

  useSSE(`${API}/status/stream`, (data) => {
    if (!data || typeof data !== "object") return;
    const next = data as Stats;
    setStats(next);
    onChangeRef.current?.(next);
  });

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
