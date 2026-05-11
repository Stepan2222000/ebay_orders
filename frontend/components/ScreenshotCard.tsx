"use client";
import { imageUrl } from "@/lib/api";
import type { Screenshot } from "@/lib/types";
import StatusPill from "./StatusPill";
import styles from "./ScreenshotCard.module.css";

function Highlight({ text, query }: { text: string; query: string }) {
  const i = text.toLowerCase().indexOf(query.toLowerCase());
  if (i < 0) return <>{text}</>;
  return (
    <>
      {text.slice(0, i)}
      <mark className={styles.mark}>{text.slice(i, i + query.length)}</mark>
      {text.slice(i + query.length)}
    </>
  );
}

export default function ScreenshotCard({
  screenshot,
  query,
  onClick,
}: {
  screenshot: Screenshot;
  query?: string;
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
        {s.match && query ? (
          <span className={styles.snippet} data-testid="screenshot-match">
            <Highlight text={s.match} query={query} />
          </span>
        ) : null}
      </div>
    </button>
  );
}
