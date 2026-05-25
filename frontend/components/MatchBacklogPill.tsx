"use client";
import { useState } from "react";
import { Tag } from "lucide-react";
import { API } from "@/lib/api";
import { useSSE } from "@/lib/sse";
import type { Stats } from "@/lib/types";
import styles from "./MatchBacklogPill.module.css";

// Пилюля в шапке рядом с «Новых снимков». Бэклог привязки items к каталогу
// деталей. Источник чисел — общий /api/status/stream. Скрыта, если разбирать
// нечего (needs_review + not_in_catalog === 0).
export default function MatchBacklogPill() {
  const [s, setS] = useState<Stats | null>(null);

  useSSE(`${API}/status/stream`, (data) => {
    if (!data || typeof data !== "object") return;
    setS(data as Stats);
  });

  if (!s) return null;
  const review = s.match_needs_review ?? 0;
  const noCat = s.match_not_in_catalog ?? 0;
  if (review + noCat === 0) return null;

  const parts: string[] = [];
  if (review > 0) parts.push(`${review} на ревью`);
  if (noCat > 0) parts.push(`${noCat} нет в каталоге`);

  return (
    <span
      className={styles.pill}
      data-testid="match-backlog"
      title="Привязка items к каталогу деталей"
    >
      <span className={styles.icon}>
        <Tag size={12} />
      </span>
      {parts.join(" · ")}
    </span>
  );
}
