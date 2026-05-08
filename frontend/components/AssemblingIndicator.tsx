"use client";
import { useEffect, useState } from "react";
import { Sparkles } from "lucide-react";
import { API } from "@/lib/api";
import type { Stats } from "@/lib/types";
import styles from "./AssemblingIndicator.module.css";

export default function AssemblingIndicator() {
  const [n, setN] = useState(0);

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
          setN(s.assembling || 0);
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

  if (n <= 0) return null;

  return (
    <span className={styles.pill} data-testid="assembling-indicator" data-count={n}>
      <span className={styles.spark}>
        <Sparkles size={12} />
      </span>
      агент собирает заказы…
    </span>
  );
}
