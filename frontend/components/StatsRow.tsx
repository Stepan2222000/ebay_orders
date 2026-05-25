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

type MatchKey = "match_needs_review" | "match_not_in_catalog";

const MATCH_LABELS: Record<MatchKey, string> = {
  match_needs_review: "на ревью",
  match_not_in_catalog: "нет в каталоге",
};
const MATCH_ORDER: MatchKey[] = ["match_needs_review", "match_not_in_catalog"];

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
    match_not_in_catalog: 0,
    match_needs_review: 0,
    match_pending: 0,
    match_no_article: 0,
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
    <div data-testid="stats" data-json={JSON.stringify(stats)}>
      <div className={styles.row}>
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
      <div className={styles.matchRow}>
        {MATCH_ORDER.map((k) => (
          <div
            key={k}
            className={`${styles.cell} ${styles.match}`}
            data-testid={`stats-${k}`}
            aria-label={MATCH_LABELS[k]}
          >
            <span className={styles.num}>{stats[k] ?? 0}</span>
            <span className={styles.lab}>{MATCH_LABELS[k]}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
