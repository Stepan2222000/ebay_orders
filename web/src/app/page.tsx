"use client";

import { useChat } from "@ai-sdk/react";
import { DefaultChatTransport, type FileUIPart } from "ai";
import { useCallback, useEffect, useRef, useState } from "react";

import { walkEntries } from "@/lib/walk-entries";

type FilePart = FileUIPart;
type TextPart = { type: "text"; text: string };
type DataProgressPart = {
  type: "data-progress";
  id: string;
  data: {
    branch?: string;
    pending_screenshots?: number;
    tool_calls?: number;
    last_tool?: string;
  };
};
type AnyPart = TextPart | FilePart | DataProgressPart | { type: string };
type UploadResultItem = {
  sha256: string;
  is_new: boolean;
  byte_size: number;
};

function makeSessionId() {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : Math.random().toString(36).slice(2);
}

export default function Page() {
  // session_id minted once per page open, never persisted.
  const [sessionId, setSessionId] = useState(makeSessionId);

  const [input, setInput] = useState("");
  const [pendingFiles, setPendingFiles] = useState<File[]>([]);
  const [dragOver, setDragOver] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const scrollerRef = useRef<HTMLDivElement>(null);
  const sendInFlightRef = useRef(false);
  const [submitting, setSubmitting] = useState(false);

  const apiBase = process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8001";

  const { messages, sendMessage, setMessages, clearError, status } = useChat({
    id: sessionId,
    transport: new DefaultChatTransport({
      api: `${apiBase}/api/chat`,
      prepareSendMessagesRequest: ({ messages, body }) => ({
        body: {
          ...body,
          messages,
          session_id: sessionId,
        },
      }),
    }),
  });

  useEffect(() => {
    if (scrollerRef.current) {
      scrollerRef.current.scrollTop = scrollerRef.current.scrollHeight;
    }
  }, [messages]);

  const onDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(true);
  }, []);
  const onDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
  }, []);
  const onDrop = useCallback(async (e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    const files = await walkEntries(e.dataTransfer.items);
    if (files.length > 0) setPendingFiles((prev) => [...prev, ...files]);
  }, []);

  const onPickFiles = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || []).filter((f) =>
      f.type.startsWith("image/"),
    );
    if (files.length > 0) setPendingFiles((prev) => [...prev, ...files]);
    if (fileInputRef.current) fileInputRef.current.value = "";
  }, []);

  const syncInput = useCallback((value: string) => {
    setInput(value);
  }, []);

  const onSend = useCallback(async () => {
    const draft = textareaRef.current?.value ?? input;
    const text = draft.trim();
    if (!text && pendingFiles.length === 0) return;
    if (sendInFlightRef.current) return;
    if (status === "streaming" || status === "submitted") return;

    sendInFlightRef.current = true;
    setSubmitting(true);
    try {
      const fileParts: FileUIPart[] = pendingFiles.map((f) => ({
        type: "file" as const,
        mediaType: f.type || "image/png",
        url: URL.createObjectURL(f),
        filename: f.name,
      }));

      if (pendingFiles.length > 0) {
        const form = new FormData();
        pendingFiles.forEach((file) => form.append("files", file, file.name));
        const response = await fetch(`${apiBase}/api/upload`, {
          method: "POST",
          body: form,
        });
        if (!response.ok) {
          throw new Error(`upload failed: ${response.status}`);
        }
        const uploaded = (await response.json()) as UploadResultItem[];
        const uploadedSha256s = uploaded.map((item) => item.sha256);
        await sendMessage(
          text
            ? { text, files: fileParts }
            : { files: fileParts },
          { body: { uploaded_sha256s: uploadedSha256s } },
        );
      } else {
        await sendMessage(
          text
            ? { text, files: fileParts }
            : { files: fileParts },
        );
      }
      syncInput("");
      if (textareaRef.current) textareaRef.current.value = "";
      setPendingFiles([]);
    } catch (error) {
      console.error("failed to send chat message", error);
    } finally {
      sendInFlightRef.current = false;
      setSubmitting(false);
    }
  }, [apiBase, input, pendingFiles, sendMessage, status, syncInput]);

  const sending = submitting || status === "streaming" || status === "submitted";
  const hasDraft = input.trim().length > 0 || pendingFiles.length > 0;
  const canResetChat = !sending && (messages.length > 0 || hasDraft);

  const onResetChat = useCallback(() => {
    if (sending) return;
    const resetSessionId = sessionId;
    clearError();
    setMessages([]);
    setSessionId(makeSessionId());
    syncInput("");
    if (textareaRef.current) textareaRef.current.value = "";
    if (fileInputRef.current) fileInputRef.current.value = "";
    setPendingFiles([]);

    void fetch(`${apiBase}/api/chat/reset`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: resetSessionId }),
    }).catch((error) => {
      console.error("failed to reset chat session", error);
    });
  }, [apiBase, clearError, sending, sessionId, setMessages, syncInput]);

  useEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;

    const handleInput = () => syncInput(textarea.value);
    textarea.addEventListener("input", handleInput);
    textarea.addEventListener("change", handleInput);
    return () => {
      textarea.removeEventListener("input", handleInput);
      textarea.removeEventListener("change", handleInput);
    };
  }, [syncInput]);

  return (
    <main
      onDragOver={onDragOver}
      onDragLeave={onDragLeave}
      onDrop={onDrop}
      style={{
        height: "100vh",
        display: "grid",
        gridTemplateRows: "auto 1fr auto",
        background: "var(--product-bg)",
        position: "relative",
      }}
    >
      <header
        style={{
          padding: "var(--sp-lg) var(--sp-2xl)",
          borderBottom: "1px solid var(--product-stroke)",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: "var(--sp-md)",
        }}
      >
        <div>
          <h1
            className="display-md"
            style={{ color: "var(--on-dark-strong)", margin: 0 }}
          >
            Order details inbox.
          </h1>
          <p
            className="caption-up"
            style={{ color: "var(--on-dark-soft)", marginTop: "var(--sp-xs)" }}
          >
            Drop screenshots or write a question.
          </p>
        </div>
        <button
          type="button"
          onClick={onResetChat}
          disabled={!canResetChat}
          title="Reset chat"
          aria-label="Reset chat"
          className="body-sm"
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: "var(--sp-xs)",
            flexShrink: 0,
            padding: "8px 12px",
            borderRadius: "var(--radius-md)",
            border: "1px solid var(--product-stroke)",
            color: canResetChat ? "var(--on-dark-strong)" : "var(--on-dark-muted)",
            background: "var(--product-overlay)",
            opacity: canResetChat ? 1 : 0.45,
            cursor: canResetChat ? "pointer" : "default",
          }}
        >
          <svg
            width="17"
            height="17"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
          >
            <path d="M3 12a9 9 0 0 1 15.1-6.6L21 8" />
            <path d="M21 3v5h-5" />
            <path d="M21 12a9 9 0 0 1-15.1 6.6L3 16" />
            <path d="M3 21v-5h5" />
          </svg>
          Reset
        </button>
      </header>

      <div
        ref={scrollerRef}
        style={{
          padding: "var(--sp-lg) var(--sp-2xl)",
          overflowY: "auto",
          display: "flex",
          flexDirection: "column",
          gap: "var(--sp-md)",
        }}
      >
        {messages.length === 0 && (
          <div
            style={{
              alignSelf: "center",
              maxWidth: 560,
              textAlign: "center",
              color: "var(--on-dark-soft)",
              padding: "var(--sp-3xl) 0",
            }}
          >
            <p className="display-sm" style={{ color: "var(--on-dark-strong)" }}>
              Drop your eBay Order details screenshots here.
            </p>
            <p className="body-sm" style={{ color: "var(--on-dark-soft)", marginTop: "var(--sp-md)" }}>
              You can also ask questions about saved orders, edit a field by
              writing it out, or ask to delete an order — all in plain text.
            </p>
          </div>
        )}

        {messages.map((m, index) => (
          <MessageRow key={`${m.id}-${index}`} message={m} />
        ))}
      </div>

      <div
        style={{
          padding: "var(--sp-md) var(--sp-2xl) var(--sp-lg)",
          borderTop: "1px solid var(--product-stroke)",
        }}
      >
        {pendingFiles.length > 0 && (
          <div
            style={{
              display: "flex",
              flexWrap: "wrap",
              gap: "var(--sp-xs)",
              marginBottom: "var(--sp-sm)",
            }}
          >
            {pendingFiles.map((f, i) => (
              <span
                key={`${f.name}-${i}`}
                className="body-xs"
                style={{
                  padding: "4px 10px",
                  borderRadius: "var(--radius-pill)",
                  background: "var(--product-overlay)",
                  color: "var(--on-dark-strong)",
                }}
              >
                {f.name}
              </span>
            ))}
            <button
              className="body-xs"
              onClick={() => setPendingFiles([])}
              style={{
                color: "var(--on-dark-soft)",
                padding: "4px 10px",
              }}
            >
              clear
            </button>
          </div>
        )}
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "auto 1fr auto",
            alignItems: "end",
            gap: "var(--sp-sm)",
            background: "var(--product-input)",
            border: "1px solid var(--product-input-border)",
            borderRadius: "var(--radius-lg)",
            padding: "var(--sp-sm)",
          }}
        >
          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            title="Attach screenshots"
            aria-label="Attach screenshots"
            className="body-sm"
            style={{
              padding: "8px 10px",
              color: "var(--on-dark-soft)",
            }}
          >
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="m21.44 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l8.57-8.57A4 4 0 1 1 17.93 8.83l-8.59 8.57a2 2 0 1 1-2.83-2.83l8.49-8.48" />
            </svg>
          </button>
          <input
            type="file"
            multiple
            accept="image/*"
            onChange={onPickFiles}
            ref={fileInputRef}
            style={{ display: "none" }}
          />
          <textarea
            ref={textareaRef}
            defaultValue=""
            onInput={(e) => syncInput(e.currentTarget.value)}
            onChange={(e) => syncInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void onSend();
              }
            }}
            placeholder="Type a message or drop screenshots…"
            rows={1}
            disabled={sending}
            className="body-md"
            style={{
              minHeight: 28,
              maxHeight: 200,
              resize: "none",
              padding: "6px 4px",
              color: "var(--on-dark)",
              background: "transparent",
            }}
          />
          <button
            type="button"
            onClick={() => void onSend()}
            disabled={sending}
            className="body-sm"
            style={{
              background:
                sending || (!input.trim() && pendingFiles.length === 0)
                  ? "var(--brand-coral-disabled)"
                  : "var(--brand-coral)",
              color: "var(--on-primary)",
              padding: "8px 14px",
              borderRadius: "var(--radius-md)",
              fontWeight: 500,
              cursor: sending ? "default" : "pointer",
              opacity: sending ? 0.6 : 1,
            }}
          >
            Send
          </button>
        </div>
      </div>

      {dragOver && (
        <div
          style={{
            position: "absolute",
            inset: 0,
            background: "rgba(204,120,92,0.10)",
            border: "2px dashed var(--brand-coral)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            pointerEvents: "none",
            zIndex: 10,
          }}
        >
          <p className="display-sm" style={{ color: "var(--on-dark-strong)" }}>
            Drop to attach.
          </p>
        </div>
      )}
    </main>
  );
}

function MessageRow({ message }: { message: { id: string; role: string; parts: AnyPart[] } }) {
  const isUser = message.role === "user";
  return (
    <div
      style={{
        alignSelf: isUser ? "flex-end" : "flex-start",
        maxWidth: "70%",
        display: "flex",
        flexDirection: "column",
        gap: "var(--sp-xs)",
      }}
    >
      <span
        className="caption-up"
        style={{
          color: "var(--on-dark-soft)",
          alignSelf: isUser ? "flex-end" : "flex-start",
        }}
      >
        {isUser ? "You" : "Inbox"}
      </span>
      <div
        style={{
          padding: "var(--sp-sm) var(--sp-md)",
          borderRadius: "var(--radius-lg)",
          background: isUser ? "var(--product-overlay)" : "var(--product-input)",
          color: "var(--on-dark)",
          border: isUser ? "0" : "1px solid var(--product-stroke)",
          display: "flex",
          flexDirection: "column",
          gap: "var(--sp-xs)",
        }}
      >
        {message.parts.map((p, i) => {
          if (p.type === "text") {
            return (
              <p key={i} className="body-md" style={{ color: "inherit", whiteSpace: "pre-wrap" }}>
                {(p as TextPart).text}
              </p>
            );
          }
          if (p.type === "file" && (p as FilePart).mediaType?.startsWith("image/")) {
            const fp = p as FilePart;
            return (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                key={i}
                src={fp.url}
                alt={fp.filename || "attachment"}
                style={{ maxWidth: 240, borderRadius: "var(--radius-sm)" }}
              />
            );
          }
          if (p.type === "data-progress") {
            const d = (p as DataProgressPart).data || {};
            return (
              <div
                key={i}
                className="body-xs"
                style={{ color: "var(--on-dark-soft)" }}
              >
                {d.branch === "screenshot"
                  ? `Working — ${d.pending_screenshots ?? 0} screenshots, ${d.tool_calls ?? 0} actions`
                  : `Working — ${d.tool_calls ?? 0} actions${d.last_tool ? ` · ${d.last_tool}` : ""}`}
              </div>
            );
          }
          return null;
        })}
      </div>
    </div>
  );
}
