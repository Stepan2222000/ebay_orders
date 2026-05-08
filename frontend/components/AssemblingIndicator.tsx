"use client";
import { useState } from "react";
import { Sparkles } from "lucide-react";
import { API } from "@/lib/api";
import { useSSE } from "@/lib/sse";
import type { Stats } from "@/lib/types";
import styles from "./AssemblingIndicator.module.css";

// Pill в шапке чата. Источник чисел — общий /api/status/stream.
//
// Два режима:
//  - streaming === true и agent_total > 0 → «Обработано {done} из {total} · ошибок {failed}».
//  - assembling > 0 → «Новых снимков: {assembling}».
//  - иначе скрыт.
export default function AssemblingIndicator({ streaming = false }: { streaming?: boolean }) {
  const [s, setS] = useState<Stats | null>(null);

  useSSE(`${API}/status/stream`, (data) => {
    if (!data || typeof data !== "object") return;
    setS(data as Stats);
  });

  if (!s) return null;

  if (streaming && s.agent_total > 0) {
    return (
      <span className={styles.pill} data-testid="assembling-indicator" data-mode="streaming">
        <span className={styles.spark}>
          <Sparkles size={12} />
        </span>
        Обработано {s.agent_done} из {s.agent_total} · ошибок {s.agent_failed}
      </span>
    );
  }

  if (s.assembling > 0) {
    return (
      <span className={styles.pill} data-testid="assembling-indicator" data-mode="assembling">
        <span className={styles.spark}>
          <Sparkles size={12} />
        </span>
        Новых снимков: {s.assembling}
      </span>
    );
  }

  return null;
}
