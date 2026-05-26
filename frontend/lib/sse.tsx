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
  useState,
  type ReactNode,
} from "react";

type Handler = (data: unknown, raw: MessageEvent) => void;

/** Состояние реального SSE-коннекта (EventSource), а не частоты сообщений. */
export type SSEStatus = "connecting" | "open" | "lost";
type StatusSub = (status: SSEStatus) => void;

interface Entry {
  count: number;
  source: EventSource;
  handlers: Set<Handler>;
  watchdog: ReturnType<typeof setTimeout> | null;
  status: SSEStatus;
  statusSubs: Set<StatusSub>;
}

const SSEContext = createContext<Map<string, Entry> | null>(null);

const WATCHDOG_MS = 60_000;

function setStatus(entry: Entry, status: SSEStatus) {
  if (entry.status === status) return;
  entry.status = status;
  for (const sub of entry.statusSubs) sub(status);
}

function rearm(entry: Entry, key: string, map: Map<string, Entry>) {
  if (entry.watchdog) clearTimeout(entry.watchdog);
  entry.watchdog = setTimeout(() => {
    // зомби-коннект: тишина дольше watchdog'а (Safari/iOS background suspend
    // без onerror). Помечаем потерю, закрываем и пересоздаём.
    setStatus(entry, "lost");
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
  source.onopen = () => {
    setStatus(entry, "open");
    rearm(entry, key, map);
  };
  source.onmessage = (e) => {
    setStatus(entry, "open");
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
    // EventSource сам уходит в авто-reconnect (readyState→CONNECTING). Для нас
    // любой разрыв — «связь потеряна»; вернётся onopen/onmessage → снова open.
    // Если браузер не восстановит за WATCHDOG_MS — rearm пересоздаст коннект.
    setStatus(entry, "lost");
  };
}

/** Создать или переиспользовать ref-counted entry на (url). */
function acquire(map: Map<string, Entry>, key: string): Entry {
  let entry = map.get(key);
  if (!entry) {
    const source = new EventSource(key);
    entry = {
      count: 0,
      source,
      handlers: new Set(),
      watchdog: null,
      status: "connecting",
      statusSubs: new Set(),
    };
    map.set(key, entry);
    bind(source, entry, key, map);
    rearm(entry, key, map);
  }
  entry.count += 1;
  return entry;
}

function release(map: Map<string, Entry>, key: string, entry: Entry) {
  entry.count -= 1;
  if (entry.count <= 0) {
    // defer close: при cleanup→remount в StrictMode счётчик успеет вернуться >0,
    // и мы НЕ закроем живой EventSource. В проде этот setTimeout(0) безвреден.
    setTimeout(() => {
      if (entry.count <= 0 && map.get(key) === entry) {
        if (entry.watchdog) clearTimeout(entry.watchdog);
        try {
          entry.source.close();
        } catch {
          /* noop */
        }
        map.delete(key);
      }
    }, 0);
  }
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
    const entry = acquire(map, key);

    const wrap: Handler = (data, raw) => handlerRef.current(data, raw);
    entry.handlers.add(wrap);

    return () => {
      entry.handlers.delete(wrap);
      release(map, key, entry);
    };
  }, [url, enabled, map]);
}

/**
 * Подписаться на состояние SSE-коннекта (а не на сообщения). Держит коннект
 * по ref-count так же, как useSSE, поэтому работает и в одиночку.
 */
export function useSSEStatus(
  url: string,
  { enabled = true }: UseSSEOptions = {},
): SSEStatus {
  const map = useContext(SSEContext);
  const [status, setLocal] = useState<SSEStatus>("connecting");

  useEffect(() => {
    if (!map || !enabled) return;
    const key = url;
    const entry = acquire(map, key);

    setLocal(entry.status);
    const sub: StatusSub = (s) => setLocal(s);
    entry.statusSubs.add(sub);

    return () => {
      entry.statusSubs.delete(sub);
      release(map, key, entry);
    };
  }, [url, enabled, map]);

  return status;
}
