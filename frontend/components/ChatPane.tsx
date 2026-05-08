"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { useChat } from "@ai-sdk/react";
import { DefaultChatTransport } from "ai";
import type { UIMessage } from "ai";
import { API, resetChat as resetChatApi } from "@/lib/api";
import { useSSE } from "@/lib/sse";
import type { Stats } from "@/lib/types";
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
  // Уже AI SDK-формат: tool-NAME с toolCallId/state/input/output — пропускаем как есть.
  if (typeof p.type === "string" && p.type.startsWith("tool-")) {
    return p;
  }
  // Старый формат БД: {type:"tool", name, arguments, result} — конвертируем.
  if (p.type === "tool" && p.name) {
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
        <ChatInner key={resetKey} initialMessages={history} />
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

function ChatInner({ initialMessages }: { initialMessages: UIMessage[] }) {
  // useChat — UI-обёртка для рендера messages и отправки sendMessage. Сам стрим
  // ответа агента не нужен: POST /chat/messages возвращает 200 OK, useChat
  // считает запрос завершённым; ответ агента приезжает воркером и догоняется
  // через /api/chat/stream → setMessages.
  const transport = useRef(
    new DefaultChatTransport({ api: `${API}/chat/messages` }),
  ).current;

  const { messages, sendMessage, setMessages } = useChat({
    transport,
    messages: initialMessages,
  });

  // agent_active — единственный источник истины для Stop/Composer.busy.
  const [agentActive, setAgentActive] = useState(false);

  useSSE(`${API}/status/stream`, (data) => {
    if (!data || typeof data !== "object") return;
    setAgentActive(!!(data as Stats).agent_active);
  });

  // /api/chat/stream — снапшот всех messages при каждом NOTIFY chat_changed.
  useSSE(`${API}/chat/stream`, (data) => {
    if (!data || typeof data !== "object") return;
    const list = (data as { messages?: PersistedMessage[] }).messages;
    if (!Array.isArray(list)) return;
    const ui = list.map((m) => persistedToUIMessage(m));
    setMessages(ui);
  });

  const threadRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const el = threadRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [messages, agentActive]);

  const onSend = ({ text, files }: { text: string; files: File[] }) => {
    // Send во время работы агента: API сам ставит agent_run.stop_requested=true,
    // воркер аккуратно прервёт текущую и тут же стартует новую с этим user-сообщением.
    let fileList: FileList | undefined;
    if (files.length) {
      const dt = new DataTransfer();
      for (const f of files) dt.items.add(f);
      fileList = dt.files;
    }
    sendMessage({ text, files: fileList });
  };

  const onStop = async () => {
    try {
      await fetch(`${API}/agent/stop`, { method: "POST" });
    } catch {
      /* банер «связь потеряна» сам покажется */
    }
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
                streaming={agentActive && i === messages.length - 1 && m.role === "assistant"}
              />
            ))}
          </div>
        )}
      </div>

      <footer className={styles.composerWrap}>
        <Composer onSend={onSend} onStop={onStop} busy={agentActive} />
      </footer>
    </>
  );
}
