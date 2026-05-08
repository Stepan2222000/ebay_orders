"use client";
import styles from "./ConfirmDialog.module.css";

export default function ConfirmDialog({
  title,
  body,
  confirmLabel = "Подтвердить",
  cancelLabel = "Отмена",
  onConfirm,
  onCancel,
  destructive = false,
}: {
  title: string;
  body?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  onConfirm: () => void;
  onCancel: () => void;
  destructive?: boolean;
}) {
  return (
    <div className={styles.backdrop} data-testid="confirm-dialog" onClick={onCancel}>
      <div className={styles.card} onClick={(e) => e.stopPropagation()}>
        <h2 className={styles.title}>{title}</h2>
        {body ? <p className={styles.body}>{body}</p> : null}
        <div className={styles.actions}>
          <button className={styles.cancel} onClick={onCancel}>
            {cancelLabel}
          </button>
          <button
            className={destructive ? styles.confirmDestructive : styles.confirm}
            onClick={onConfirm}
            data-testid="confirm-yes"
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
