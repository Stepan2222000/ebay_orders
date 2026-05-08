"use client";
import { useEffect, useState } from "react";
import { API } from "@/lib/api";
import styles from "./ConnectionBanner.module.css";

export default function ConnectionBanner() {
  const [lost, setLost] = useState(false);

  useEffect(() => {
    let stop = false;
    const ping = async () => {
      const ac = new AbortController();
      const t = setTimeout(() => ac.abort(), 2500);
      try {
        const r = await fetch(`${API}/status`, { cache: "no-store", signal: ac.signal });
        if (!stop) setLost(!r.ok);
      } catch {
        if (!stop) setLost(true);
      } finally {
        clearTimeout(t);
      }
    };
    ping();
    const id = setInterval(ping, 3000);
    return () => {
      stop = true;
      clearInterval(id);
    };
  }, []);

  if (!lost) return null;

  return (
    <div className={styles.banner} data-testid="connection-banner">
      Связь с сервером потеряна. Жду восстановления…
    </div>
  );
}
