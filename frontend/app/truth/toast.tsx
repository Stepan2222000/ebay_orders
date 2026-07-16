"use client";
import { createContext, useCallback, useContext, useRef, useState } from "react";
import styles from "./truth.module.css";

const ToastCtx = createContext<(msg: string) => void>(() => {});

export function useToast() { return useContext(ToastCtx); }

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [msg, setMsg] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const show = useCallback((m: string) => {
    setMsg(m);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setMsg(null), 2200);
  }, []);
  return (
    <ToastCtx.Provider value={show}>
      {children}
      {msg && <div className={styles.toast}>{msg}</div>}
    </ToastCtx.Provider>
  );
}

/** Копирование с тостом: единый жест для любых артикулов. */
export function useCopy() {
  const toast = useToast();
  return useCallback(async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      toast(`скопировано: ${text}`);
    } catch {
      toast("не удалось скопировать");
    }
  }, [toast]);
}
