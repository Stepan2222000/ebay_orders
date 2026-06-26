"use client";
import { useEffect, useRef, useState } from "react";
import { Paperclip, Send, Square, X } from "lucide-react";
import styles from "./Composer.module.css";

export default function Composer({
  onSend,
  onStop,
  busy,
  disabled,
}: {
  onSend: (args: { text: string; files: File[] }) => void;
  onStop?: () => void;
  busy?: boolean;
  disabled?: boolean;
}) {
  const [text, setText] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const taRef = useRef<HTMLTextAreaElement | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = `${Math.min(ta.scrollHeight, 220)}px`;
  }, [text]);

  const submit = () => {
    if (busy) return;
    const trimmed = text.trim();
    if (!trimmed && files.length === 0) return;
    onSend({ text: trimmed, files });
    setText("");
    setFiles([]);
  };

  return (
    <div className={`${styles.wrap} ${busy ? styles.busy : ""}`} data-testid="composer">
      {files.length > 0 ? (
        <ul className={styles.attachments} data-testid="attachments">
          {files.map((f, i) => (
            <li key={i} className={styles.chip}>
              <span>{f.name}</span>
              <button
                aria-label={`Убрать ${f.name}`}
                onClick={() => setFiles((prev) => prev.filter((_, j) => j !== i))}
              >
                <X size={12} />
              </button>
            </li>
          ))}
        </ul>
      ) : null}

      <div className={styles.inputRow}>
        <textarea
          ref={taRef}
          rows={1}
          className={styles.input}
          placeholder="Сообщение…"
          value={text}
          disabled={disabled}
          data-testid="composer-input"
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
        />
      </div>

      <div className={styles.actions}>
        <div className={styles.left}>
          <button
            type="button"
            className={styles.iconBtn}
            onClick={() => fileRef.current?.click()}
            aria-label="Прикрепить фото"
            data-testid="attach"
            disabled={disabled || busy}
          >
            <Paperclip size={16} />
          </button>
          <input
            ref={fileRef}
            type="file"
            accept="image/png,image/jpeg,image/webp,image/gif"
            multiple
            hidden
            data-testid="attach-input"
            onChange={(e) => {
              // захватываем файлы СИНХРОННО: в Safari value="" обнуляет FileList,
              // а отложенный Array.from внутри setFiles увидел бы уже пусто
              const picked = Array.from(e.currentTarget.files ?? []);
              e.currentTarget.value = "";
              if (picked.length) setFiles((prev) => [...prev, ...picked]);
            }}
          />
        </div>
        {busy ? (
          <button
            type="button"
            onClick={onStop}
            className={styles.stop}
            data-testid="stop-btn"
            aria-label="Остановить"
          >
            <Square size={14} />
            Стоп
          </button>
        ) : (
          <button
            type="button"
            onClick={submit}
            className={styles.send}
            data-testid="send-btn"
            aria-label="Отправить"
            disabled={disabled || (!text.trim() && files.length === 0)}
          >
            <Send size={14} />
          </button>
        )}
      </div>
    </div>
  );
}
