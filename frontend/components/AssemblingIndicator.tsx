"use client";
import { useState } from "react";
import { Sparkles } from "lucide-react";
import { API } from "@/lib/api";
import { useSSE } from "@/lib/sse";
import type { Stats } from "@/lib/types";
import styles from "./AssemblingIndicator.module.css";

// Pill в шапке чата. Источник — общий /api/status/stream.
// Видим, только если есть распознанные снимки, ожидающие сборки в заказы.
// Источник числа — assembling (count where ocr_status='done'
// AND agent_status IN ('pending','running')).
export default function AssemblingIndicator() {
  const [s, setS] = useState<Stats | null>(null);

  useSSE(`${API}/status/stream`, (data) => {
    if (!data || typeof data !== "object") return;
    setS(data as Stats);
  });

  if (!s || s.assembling === 0) return null;

  return (
    <span className={styles.pill} data-testid="assembling-indicator">
      <span className={styles.spark}>
        <Sparkles size={12} />
      </span>
      Новых снимков: {s.assembling}
    </span>
  );
}
