"use client";
import { useState } from "react";
import { Sparkles } from "lucide-react";
import { API } from "@/lib/api";
import { useSSE } from "@/lib/sse";
import type { Stats } from "@/lib/types";
import styles from "./AssemblingIndicator.module.css";

// В шапке чата. Виден пока есть активная сессия и в ней что-то делается.
// Источник — общий /api/status/stream (useSSE). Цифры — agent_total / done / failed
// из _status_dict (count screenshots по agent_status).
export default function AssemblingIndicator() {
  const [s, setS] = useState<Stats | null>(null);

  useSSE(`${API}/status/stream`, (data) => {
    if (!data || typeof data !== "object") return;
    setS(data as Stats);
  });

  if (!s || !s.agent_active || s.agent_total === 0) return null;

  return (
    <span className={styles.pill} data-testid="assembling-indicator">
      <span className={styles.spark}>
        <Sparkles size={12} />
      </span>
      Обработано {s.agent_done}/{s.agent_total}
      {s.agent_failed > 0 ? ` · ошибок ${s.agent_failed}` : ""}
    </span>
  );
}
