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

function persistedPartToUIPart(p: any): any {
  if (p.type === "text") return { type: "text", text: p.text };
  if (p.type === "file") {
    return {
      type: "file",
      mediaType: p.mediaType ?? p.mime,
      url: p.url ?? p.data_url,
    };
  }
  if (typeof p.type === "string" && p.type.startsWith("tool-")) {
    return p;
  }
  return null;
}

function persistedToUIMessage(m: PersistedMessage): UIMessage {
  const parts = (m.parts || [])
    .map((p) => persistedPartToUIPart(p))
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
      {history === null ? (
        <div className={styles.loading}>Загружаю историю…</div>
      ) : (
        <ChatInner
          key={resetKey}
          initialMessages={history}
          onResetClick={() => setConfirmReset(true)}
        />
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
  onResetClick,
}: {
  initialMessages: UIMessage[];
  onResetClick: () => void;
}) {
  const transport = useRef(
    new DefaultChatTransport({
      api: `${API}/chat`,
      // только последнее сообщение отдаём бэку — историю он перечитывает из БД сам
      prepareSendMessagesRequest: ({ messages }) => ({
        body: { message: messages.at(-1) },
      }),
    }),
  ).current;

  const { messages, sendMessage, status, stop } = useChat({
    transport,
    messages: initialMessages,
  });

  const busy = status === "streaming" || status === "submitted";

  const threadRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const el = threadRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [messages]);

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
      <header className={styles.header}>
        <div className={styles.headerLeft}>
          <span className={styles.brand}>eBay orders.</span>
          <AssemblingIndicator streaming={busy} />
        </div>
        <div className={styles.headerActions}>
          <ThemeToggle />
          <button
            type="button"
            className={styles.resetBtn}
            data-testid="reset-chat"
            onClick={onResetClick}
            disabled={busy}
          >
            Сбросить чат
          </button>
        </div>
      </header>

      <div className={styles.thread} ref={threadRef} data-testid="thread">
        {messages.length === 0 ? (
          <p className={styles.empty}>Начните разговор или дроп скриншоты в сайдбар.</p>
        ) : (
          <div className={styles.threadInner}>
            {messages.map((m) => (
              <Message
                key={m.id}
                role={m.role as any}
                parts={(m as any).parts ?? []}
              />
            ))}
          </div>
        )}
      </div>

      <footer className={styles.composerWrap}>
        <Composer onSend={onSend} busy={busy} onStop={stop} />
      </footer>
    </>
  );
}
