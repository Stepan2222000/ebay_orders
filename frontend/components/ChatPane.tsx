"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { useChat } from "@ai-sdk/react";
import { DefaultChatTransport } from "ai";
import type { UIMessage } from "ai";
import { API, resetChat as resetChatApi } from "@/lib/api";
import AssemblingIndicator from "./AssemblingIndicator";
import ConfirmDialog from "./ConfirmDialog";
import Composer from "./Composer";
import Message from "./Message";
import ThemeToggle from "./ThemeToggle";
import styles from "./ChatPane.module.css";

interface PersistedMessage {
  id: number;
  role: "user" | "assistant" | "system";
  parts: any[];
  created_at: string;
}

function persistedPartToUIPart(p: any, idx: number): any {
  if (p.type === "text") return { type: "text", text: p.text };
  if (p.type === "file") {
    return { type: "file", mediaType: p.mime, url: p.data_url };
  }
  if (p.type === "tool") {
    const errored = p.result && typeof p.result === "object" && "error" in p.result;
    return {
      type: `tool-${p.name}`,
      toolCallId: `persisted-${idx}-${p.name}`,
      state: errored ? "output-error" : "output-available",
      input: p.arguments,
      output: p.result,
    };
  }
  return null;
}

function persistedToUIMessage(m: PersistedMessage): UIMessage {
  const parts = (m.parts || [])
    .map((p, i) => persistedPartToUIPart(p, i))
    .filter(Boolean);
  return { id: String(m.id), role: m.role, parts } as UIMessage;
}

export default function ChatPane() {
  const [history, setHistory] = useState<UIMessage[] | null>(null);
  const [resetKey, setResetKey] = useState(0);
  const [confirmReset, setConfirmReset] = useState(false);

  const loadHistory = useCallback(async () => {
    try {
      const r = await fetch(`${API}/chat/messages`, { cache: "no-store" });
      const data = await r.json();
      const msgs: UIMessage[] = (data.messages || []).map(persistedToUIMessage);
      setHistory(msgs);
    } catch {
      setHistory([]);
    }
  }, []);

  useEffect(() => {
    loadHistory();
  }, [loadHistory, resetKey]);

  const onReset = async () => {
    await resetChatApi();
    setConfirmReset(false);
    setHistory(null);
    setResetKey((k) => k + 1);
  };

  return (
    <section className={styles.pane} data-testid="chat-pane">
      <header className={styles.header}>
        <div className={styles.headerLeft}>
          <span className={styles.brand}>eBay orders.</span>
          <AssemblingIndicator />
        </div>
        <div className={styles.headerActions}>
          <ThemeToggle />
          <button
            type="button"
            className={styles.resetBtn}
            data-testid="reset-chat"
            onClick={() => setConfirmReset(true)}
          >
            Сбросить чат
          </button>
        </div>
      </header>

      {history === null ? (
        <div className={styles.loading}>Загружаю историю…</div>
      ) : (
        <ChatInner key={resetKey} initialMessages={history} onLoadMessages={loadHistory} />
      )}

      {confirmReset ? (
        <ConfirmDialog
          title="Сбросить чат?"
          body="История переписки будет удалена. Скриншоты и заказы останутся."
          confirmLabel="Сбросить"
          destructive
          onConfirm={onReset}
          onCancel={() => setConfirmReset(false)}
        />
      ) : null}
    </section>
  );
}

function ChatInner({
  initialMessages,
  onLoadMessages,
}: {
  initialMessages: UIMessage[];
  onLoadMessages: () => void;
}) {
  const transport = useRef(
    new DefaultChatTransport({ api: `${API}/chat/messages` }),
  ).current;

  const { messages, sendMessage, status, stop, setMessages } = useChat({
    transport,
    messages: initialMessages,
    onFinish: () => {
      // ассистент закончил — обновим из БД, чтобы взять
      // канонический набор parts, привязки к скриншотам и т.п.
      onLoadMessages();
    },
    onError: () => {
      onLoadMessages();
    },
  });

  const busy = status === "submitted" || status === "streaming";

  // Live-подписка на /api/chat/stream: бэк шлёт обновления при каждом INSERT
  // в chat_messages (например, когда auto-trigger пишет стартовое сообщение
  // или итоговую сводку). В активный стрим useChat не вмешиваемся.
  const statusRef = useRef(status);
  statusRef.current = status;
  useEffect(() => {
    let stopped = false;
    let backoff = 1000;
    let es: EventSource | null = null;
    const open = () => {
      if (stopped) return;
      es = new EventSource(`${API}/chat/stream`);
      es.onmessage = (e) => {
        // Debug-trace: видно через window.__chatLogs из dev-консоли/тестов.
        const w = window as any;
        if (!w.__chatLogs) w.__chatLogs = [];
        try {
          const data = JSON.parse(e.data);
          const ui = (data.messages || []).map((m: PersistedMessage) =>
            persistedToUIMessage(m),
          );
          w.__chatLogs.push(`evt msgs=${ui.length} status=${statusRef.current}`);
          if (statusRef.current !== "ready" && statusRef.current !== "error") {
            w.__chatLogs.push(`  skipped (busy)`);
            return;
          }
          setMessages(ui);
          w.__chatLogs.push(`  setMessages called`);
          backoff = 1000;
        } catch (err) {
          w.__chatLogs.push(`parse error: ${String(err)}`);
        }
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
  }, [setMessages]);

  const threadRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const el = threadRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [messages, busy]);

  const onSend = ({ text, files }: { text: string; files: File[] }) => {
    let fileList: FileList | undefined;
    if (files.length) {
      const dt = new DataTransfer();
      for (const f of files) dt.items.add(f);
      fileList = dt.files;
    }
    sendMessage({ text, files: fileList });
  };

  return (
    <>
      <div className={styles.thread} ref={threadRef} data-testid="thread">
        {messages.length === 0 ? (
          <p className={styles.empty}>Начните разговор или дроп скриншоты в сайдбар.</p>
        ) : (
          <div className={styles.threadInner}>
            {messages.map((m, i) => (
              <Message
                key={m.id}
                role={m.role as any}
                parts={(m as any).parts ?? []}
                streaming={busy && i === messages.length - 1 && m.role === "assistant"}
              />
            ))}
          </div>
        )}
      </div>

      <footer className={styles.composerWrap}>
        <Composer onSend={onSend} onStop={stop} busy={busy} />
      </footer>
    </>
  );
}
