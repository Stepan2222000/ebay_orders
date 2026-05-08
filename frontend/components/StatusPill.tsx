import type { OcrStatus } from "@/lib/types";
import styles from "./StatusPill.module.css";

const LABELS: Record<OcrStatus, string> = {
  pending: "ожидает",
  running: "идёт",
  done: "готово",
  failed: "ошибка",
};

export default function StatusPill({ status, size = "sm" }: { status: OcrStatus; size?: "sm" | "md" }) {
  return (
    <span className={`${styles.pill} ${styles[status]} ${styles[`size_${size}`]}`} data-status={status}>
      <span className={styles.dot} />
      {LABELS[status]}
    </span>
  );
}
