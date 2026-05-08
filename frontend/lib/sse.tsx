"use client";
/**
 * Общий EventSource через Context + ref-count.
 *
 * Несколько компонентов (счётчики, баннер связи, прогресс агента) подписываются
 * на один и тот же `/api/status/stream`. Чтобы не открывать на каждый компонент
 * свой EventSource (браузер держит лимит ~6 HTTP/1.1 сокетов на origin),
 * экземпляр на (url) хранится в Map с ref-count и закрывается, когда последний
 * подписчик ушёл.
 *
 * Watchdog: бэк шлёт data-heartbeat каждые ~15с (см. app/listener.py).
 * Если за 60с ни одного события — считаем коннект мёртвым (Safari/iOS background
 * suspend без onerror), пересоздаём EventSource и переподписываем handlers.
 */
import {
  createContext,
  useContext,
  useEffect,
  useRef,
  type ReactNode,
} from "react";

type Handler = (data: unknown, raw: MessageEvent) => void;

interface Entry {
  count: number;
  source: EventSource;
  handlers: Set<Handler>;
  watchdog: ReturnType<typeof setTimeout> | null;
}

const SSEContext = createContext<Map<string, Entry> | null>(null);

const WATCHDOG_MS = 60_000;

function rearm(entry: Entry, key: string, map: Map<string, Entry>) {
  if (entry.watchdog) clearTimeout(entry.watchdog);
  entry.watchdog = setTimeout(() => {
    // зомби-коннект: тишина дольше watchdog'а. Закрываем и пересоздаём.
    try {
      entry.source.close();
    } catch {
      /* noop */
    }
    const next = new EventSource(key);
    entry.source = next;
    bind(next, entry, key, map);
    rearm(entry, key, map);
  }, WATCHDOG_MS);
}

function bind(source: EventSource, entry: Entry, key: string, map: Map<string, Entry>) {
  source.onopen = () => rearm(entry, key, map);
  source.onmessage = (e) => {
    rearm(entry, key, map);
    let data: unknown = e.data;
    try {
      data = JSON.parse(e.data);
    } catch {
      /* оставляем сырой текст */
    }
    // фильтр heartbeat — он только для оживления соединения
    if (
      data &&
      typeof data === "object" &&
      (data as { _heartbeat?: unknown })._heartbeat === true
    ) {
      return;
    }
    for (const h of entry.handlers) h(data, e);
  };
  source.onerror = () => {
    // встроенный авто-reconnect EventSource — ничего не делаем.
    // если браузер не сможет восстановить за WATCHDOG_MS — наш rearm пересоздаст.
  };
}

export function SSEProvider({ children }: { children: ReactNode }) {
  const ref = useRef<Map<string, Entry> | null>(null);
  if (ref.current === null) ref.current = new Map();
  return <SSEContext.Provider value={ref.current}>{children}</SSEContext.Provider>;
}

export interface UseSSEOptions {
  enabled?: boolean;
}

/** Подписаться на SSE-канал. JSON парсится автоматически; heartbeat'ы скрыты. */
export function useSSE(
  url: string,
  onMessage: Handler,
  { enabled = true }: UseSSEOptions = {},
) {
  const map = useContext(SSEContext);
  const handlerRef = useRef(onMessage);
  handlerRef.current = onMessage;

  useEffect(() => {
    if (!map || !enabled) return;
    const key = url;

    let entry = map.get(key);
    if (!entry) {
      const source = new EventSource(key);
      entry = {
        count: 0,
        source,
        handlers: new Set(),
        watchdog: null,
      };
      map.set(key, entry);
      bind(source, entry, key, map);
      rearm(entry, key, map);
    }
    entry.count += 1;

    const wrap: Handler = (data, raw) => handlerRef.current(data, raw);
    entry.handlers.add(wrap);

    return () => {
      const en = entry!;
      en.handlers.delete(wrap);
      en.count -= 1;
      if (en.count <= 0) {
        // defer close: при cleanup→remount в StrictMode счётчик успеет вернуться >0,
        // и мы НЕ закроем живой EventSource. В проде этот setTimeout(0) безвреден.
        setTimeout(() => {
          if (en.count <= 0 && map.get(key) === en) {
            if (en.watchdog) clearTimeout(en.watchdog);
            try {
              en.source.close();
            } catch {
              /* noop */
            }
            map.delete(key);
          }
        }, 0);
      }
    };
  }, [url, enabled, map]);
}
