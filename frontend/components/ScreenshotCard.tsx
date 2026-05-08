"use client";
import { imageUrl } from "@/lib/api";
import type { Screenshot } from "@/lib/types";
import StatusPill from "./StatusPill";
import styles from "./ScreenshotCard.module.css";

export default function ScreenshotCard({
  screenshot,
  onClick,
}: {
  screenshot: Screenshot;
  onClick: (s: Screenshot) => void;
}) {
  const s = screenshot;
  return (
    <button
      type="button"
      className={styles.card}
      data-testid="screenshot-card"
      data-sha={s.sha}
      data-ocr-status={s.ocr_status}
      data-agent-status={s.agent_status}
      onClick={() => onClick(s)}
    >
      <img
        src={imageUrl(s.sha)}
        alt=""
        className={styles.thumb}
        loading="lazy"
        draggable={false}
      />
      <div className={styles.meta}>
        <StatusPill status={s.ocr_status} size="sm" />
        <span className={styles.order}>
          {s.order_number ? `#${s.order_number}` : "пока не привязан"}
        </span>
      </div>
    </button>
  );
}
