"use client";
import { useRef, useState } from "react";
import { Upload } from "lucide-react";
import { collectImageFiles, collectInputFiles } from "@/lib/fileTree";
import { uploadFiles } from "@/lib/api";
import styles from "./DropZone.module.css";

export default function DropZone({ onUploaded }: { onUploaded?: () => void }) {
  const [over, setOver] = useState(false);
  const [busy, setBusy] = useState(false);
  const [hint, setHint] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  const send = async (files: File[]) => {
    if (!files.length) {
      setHint("Файлов изображений не найдено.");
      return;
    }
    setBusy(true);
    setHint(`Загружаю: ${files.length}`);
    try {
      const r = await uploadFiles(files);
      const dup = r.screenshots.filter((s) => s.status === "duplicate").length;
      const queued = r.screenshots.length - dup;
      setHint(`Принято: ${queued}${dup ? `, дубликатов: ${dup}` : ""}.`);
      onUploaded?.();
    } catch (e) {
      setHint(`Не удалось загрузить: ${(e as Error).message}`);
    } finally {
      setBusy(false);
      setTimeout(() => setHint(null), 4000);
    }
  };

  return (
    <div
      className={`${styles.zone} ${over ? styles.over : ""} ${busy ? styles.busy : ""}`}
      data-testid="dropzone"
      onDragEnter={(e) => {
        e.preventDefault();
        setOver(true);
      }}
      onDragOver={(e) => {
        e.preventDefault();
        e.dataTransfer.dropEffect = "copy";
      }}
      onDragLeave={() => setOver(false)}
      onDrop={async (e) => {
        e.preventDefault();
        setOver(false);
        const files = await collectImageFiles(e.dataTransfer.items);
        send(files);
      }}
      onClick={() => inputRef.current?.click()}
      role="button"
      tabIndex={0}
    >
      <Upload size={20} aria-hidden />
      <div className={styles.title}>Перетащите скриншоты или папку.</div>
      <div className={styles.sub}>{hint ?? "PNG, JPEG, WebP, GIF. До 10 МБ каждый."}</div>
      <input
        ref={inputRef}
        type="file"
        multiple
        accept="image/png,image/jpeg,image/webp,image/gif"
        className={styles.input}
        data-testid="dropzone-fallback-input"
        onChange={(e) => {
          const files = collectInputFiles(e.currentTarget);
          e.currentTarget.value = "";
          send(files);
        }}
      />
    </div>
  );
}
