"use client";
import { useEffect, useState } from "react";
import { X, Trash2, RotateCw } from "lucide-react";
import {
  deleteScreenshot,
  fetchScreenshotDetail,
  imageUrl,
  retryScreenshot,
} from "@/lib/api";
import type { ScreenshotDetail } from "@/lib/types";
import StatusPill from "./StatusPill";
import ConfirmDialog from "./ConfirmDialog";
import styles from "./ScreenshotDetailModal.module.css";

function fmtDuration(startIso: string, endIso: string | null): string | null {
  if (!endIso) return null;
  const ms = new Date(endIso).getTime() - new Date(startIso).getTime();
  if (ms < 0 || !Number.isFinite(ms)) return null;
  return `${(ms / 1000).toFixed(1)} с`;
}

function shortSha(sha: string): string {
  return `${sha.slice(0, 8)}…`;
}

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} КБ`;
  return `${(n / (1024 * 1024)).toFixed(1)} МБ`;
}

const FIELD_LABELS: Record<string, string> = {
  order_number: "номер заказа",
  ordered_at: "оформлен",
  sold_by: "продавец",
  order_total_text: "итог заказа",
  item_subtotal_text: "сумма товаров",
  shipping_text: "доставка",
  sales_tax_text: "налог",
  delivery_status: "статус доставки",
  delivered_date: "доставлено",
  arriving_by_date: "ожидается",
  shipping_service: "сервис доставки",
};

export default function ScreenshotDetailModal({
  sha,
  onClose,
  onDeleted,
  onRetried,
}: {
  sha: string;
  onClose: () => void;
  onDeleted: () => void;
  onRetried?: () => void;
}) {
  const [data, setData] = useState<ScreenshotDetail | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [confirm, setConfirm] = useState(false);
  const [retryConfirm, setRetryConfirm] = useState(false);
  const [busy, setBusy] = useState(false);

  const inProgress =
    data?.ocr_status === "pending" || data?.ocr_status === "running";

  useEffect(() => {
    let cancelled = false;
    fetchScreenshotDetail(sha)
      .then((d) => !cancelled && setData(d))
      .catch((e) => !cancelled && setErr((e as Error).message));
    return () => {
      cancelled = true;
    };
  }, [sha]);

  useEffect(() => {
    if (!inProgress) return;
    let cancelled = false;
    const timer = setTimeout(async () => {
      try {
        const d = await fetchScreenshotDetail(sha);
        if (!cancelled) setData(d);
      } catch {
        // молча: следующий тик попробует ещё раз
      }
    }, 1500);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [sha, inProgress]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const observed = data?.raw_json?.observed ?? null;
  const items: any[] = observed?.items ?? [];
  const refunds: any[] = observed?.refunds ?? [];
  const trackingNumbers: string[] = observed?.tracking_numbers ?? [];
  const unreadable: string[] = data?.raw_json?.unreadable ?? [];

  const doDelete = async () => {
    setBusy(true);
    try {
      await deleteScreenshot(sha);
      onDeleted();
    } catch (e) {
      setErr((e as Error).message);
      setBusy(false);
    }
  };

  const doRetry = async () => {
    setBusy(true);
    setRetryConfirm(false);
    setErr(null);
    try {
      await retryScreenshot(sha);
      const d = await fetchScreenshotDetail(sha);
      setData(d);
      onRetried?.();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const onRetryClick = () => {
    if (data?.order_number) {
      setRetryConfirm(true);
    } else {
      void doRetry();
    }
  };

  return (
    <div className={styles.backdrop} data-testid="detail-modal" onClick={onClose}>
      <div className={styles.card} onClick={(e) => e.stopPropagation()}>
        <button className={styles.closeBtn} onClick={onClose} aria-label="Закрыть">
          <X size={18} />
        </button>

        <div className={styles.body}>
          <div className={styles.preview}>
            <img src={imageUrl(sha)} alt="" draggable={false} />
          </div>

          <div className={styles.meta} data-testid="detail-raw-ocr">
            <div className={styles.heading}>
              <h2 className={styles.title}>
                {data?.order_number ? `Заказ #${data.order_number}` : "Скриншот"}
              </h2>
              <div className={styles.subline}>
                <span className={styles.sha}>{shortSha(sha)}</span>
                {data ? <StatusPill status={data.ocr_status} size="md" /> : null}
              </div>
            </div>

            {err ? <p className={styles.err}>Ошибка: {err}</p> : null}
            {!data && !err ? <p className={styles.muted}>Загружаю…</p> : null}

            {data ? (
              <>
                {data.last_error ? (
                  <p className={styles.err}>Ошибка стадии: {data.last_error}</p>
                ) : null}

                <dl className={styles.fields}>
                  <Field k="размер файла" v={fmtBytes(data.byte_size)} />
                  <Field k="формат" v={data.mime_type} />
                  <Field k="загружено" v={new Date(data.created_at).toLocaleString("ru")} />
                  <Field k="модель OCR" v={data.ocr_model ?? "—"} />
                  <Field
                    k="время OCR"
                    v={fmtDuration(data.created_at, data.ocr_at) ?? "—"}
                  />
                  <Field
                    k="заказ"
                    v={data.order_number ? `#${data.order_number}` : "пока не привязан"}
                  />
                </dl>

                {observed ? (
                  <>
                    <h3 className={styles.section}>Распознанные поля</h3>
                    <dl className={styles.fields}>
                      {Object.entries(FIELD_LABELS).map(([k, lab]) => (
                        <Field key={k} k={lab} v={observed[k] ?? "—"} />
                      ))}
                    </dl>

                    {items.length > 0 ? (
                      <>
                        <h3 className={styles.section}>Товары</h3>
                        <ul className={styles.list}>
                          {items.map((it, i) => (
                            <li key={i} className={styles.listItem}>
                              <span className={styles.listMain}>
                                {it.item_title ?? "—"}
                              </span>
                              <span className={styles.listSub}>
                                {it.item_quantity ? `×${it.item_quantity}` : ""}
                                {it.item_line_total_text ? ` · ${it.item_line_total_text}` : ""}
                                {it.item_number ? ` · #${it.item_number}` : ""}
                              </span>
                            </li>
                          ))}
                        </ul>
                      </>
                    ) : null}

                    {refunds.length > 0 ? (
                      <>
                        <h3 className={styles.section}>Возвраты</h3>
                        <ul className={styles.list}>
                          {refunds.map((r, i) => (
                            <li key={i} className={styles.listItem}>
                              <span className={styles.listMain}>
                                {r.refund_amount_text ?? "—"}
                              </span>
                              <span className={styles.listSub}>
                                {r.refund_date ?? ""}
                                {r.refund_note ? ` · ${r.refund_note}` : ""}
                              </span>
                            </li>
                          ))}
                        </ul>
                      </>
                    ) : null}

                    {trackingNumbers.length > 0 ? (
                      <>
                        <h3 className={styles.section}>Трек-номера</h3>
                        <ul className={styles.list}>
                          {trackingNumbers.map((t, i) => (
                            <li key={i} className={styles.listItem}>
                              <span className={styles.listMain}>{t}</span>
                            </li>
                          ))}
                        </ul>
                      </>
                    ) : null}

                    {unreadable.length > 0 ? (
                      <>
                        <h3 className={styles.section}>Не смогли разобрать</h3>
                        <ul className={styles.list}>
                          {unreadable.map((u, i) => (
                            <li key={i} className={styles.listItem}>
                              <span className={styles.listSub}>{u}</span>
                            </li>
                          ))}
                        </ul>
                      </>
                    ) : null}
                  </>
                ) : null}
              </>
            ) : null}
          </div>
        </div>

        <div className={styles.footer}>
          <button
            className={styles.deleteBtn}
            onClick={() => setConfirm(true)}
            disabled={busy}
            data-testid="detail-delete"
          >
            <Trash2 size={16} />
            Удалить
          </button>
          <div className={styles.footerRight}>
            <button
              className={styles.retryBtn}
              onClick={onRetryClick}
              disabled={busy || inProgress || !data}
              data-testid="detail-retry"
              title={
                inProgress
                  ? "OCR ещё идёт"
                  : "Удалить распознанные данные и прогнать стадию A заново"
              }
            >
              <RotateCw size={16} className={inProgress ? styles.spin : undefined} />
              {inProgress ? "Идёт OCR…" : "Повторить OCR"}
            </button>
            <button className={styles.closeText} onClick={onClose}>
              Закрыть
            </button>
          </div>
        </div>
      </div>

      {confirm ? (
        <ConfirmDialog
          title="Удалить скриншот?"
          body="Файл и его OCR будут удалены. Связанный заказ останется."
          confirmLabel="Удалить"
          destructive
          onConfirm={doDelete}
          onCancel={() => setConfirm(false)}
        />
      ) : null}

      {retryConfirm ? (
        <ConfirmDialog
          title="Повторить OCR?"
          body={
            data?.order_number
              ? `Скриншот связан с заказом #${data.order_number}. Связь будет снята, текущие распознанные поля удалены, снимок встанет в очередь на повторное распознавание. Сам заказ останется — удалите его через чат, если он ошибочный.`
              : "Текущие распознанные поля будут удалены, снимок встанет в очередь."
          }
          confirmLabel="Повторить"
          destructive
          onConfirm={doRetry}
          onCancel={() => setRetryConfirm(false)}
        />
      ) : null}
    </div>
  );
}

function Field({ k, v }: { k: string; v: any }) {
  const text = v == null || v === "" ? "—" : String(v);
  return (
    <div className={styles.field}>
      <dt>{k}</dt>
      <dd>{text}</dd>
    </div>
  );
}
