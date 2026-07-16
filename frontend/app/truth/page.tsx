"use client";
import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import {
  ageText, fetchAllListings, fetchQueue, photoSrc, STATUS_LABEL,
  type ListingRow, type Queue, type QueueRow,
} from "@/lib/truth";
import { ToastProvider, useCopy } from "./toast";
import styles from "./truth.module.css";

const STATUS_PILL: Record<string, string> = {
  linked: styles.pillGreen,
  not_in_catalog: styles.pillAmber,
  no_article: styles.pillAmber,
  conflict: styles.pillRed,
  needs_review: styles.pillGray,
  pending: styles.pillGray,
};
const ACCENT: Record<string, string> = {
  linked: "", conflict: "rowConflict", not_in_catalog: "rowAmber",
  no_article: "rowAmber", needs_review: "rowGray", pending: "rowGray",
};

function Row({ r, accent, sub, onOpen }: {
  r: QueueRow; accent: string; sub: React.ReactNode; onOpen: () => void;
}) {
  return (
    <div className={`${styles.row} ${accent}`} onClick={onOpen}
         role="button" tabIndex={0}
         onKeyDown={(e) => e.key === "Enter" && onOpen()}>
      {r.photo
        ? <img className={styles.thumb} src={photoSrc(r.photo)} alt="" loading="lazy" />
        : <div className={`${styles.thumb} ${styles.thumbEmpty}`}>нет фото</div>}
      <div className={styles.rowBody}>
        <div className={styles.rowTitle}>
          <span className={styles.mono}>{r.item_number}</span> · {r.item_title}
        </div>
        <div className={styles.rowSub}>{sub}</div>
      </div>
      <span className={styles.rowAge}>{ageText(r.age_s)}</span>
    </div>
  );
}

function AllListings({ filter, open }: { filter: string; open: (n: string) => void }) {
  const [rows, setRows] = useState<ListingRow[] | null>(null);
  useEffect(() => {
    fetchAllListings().then((d) => setRows(d.listings)).catch(() => setRows([]));
  }, []);
  if (!rows) return <div className={styles.zeroState}><span className={styles.spin} /> загрузка…</div>;
  const f = filter.trim().toLowerCase();
  const shown = rows.filter((r) => !f || r.item_number.includes(f)
    || (r.item_title || "").toLowerCase().includes(f)
    || (r.composition || "").toLowerCase().includes(f));
  return (
    <>
      <h2 className={styles.groupTitle}>Все листинги <span className={styles.groupCount}>{shown.length}</span></h2>
      {shown.map((r) => (
        <div key={r.item_number}
          className={`${styles.row} ${styles[ACCENT[r.match_status]] || ""}`}
          onClick={() => open(r.item_number)} role="button" tabIndex={0}
          onKeyDown={(e) => e.key === "Enter" && open(r.item_number)}>
          {r.photo
            ? <img className={styles.thumb} src={photoSrc(r.photo)} alt="" loading="lazy" />
            : <div className={`${styles.thumb} ${styles.thumbEmpty}`}>нет фото</div>}
          <div className={styles.rowBody}>
            <div className={styles.rowTitle}>
              <span className={styles.mono}>{r.item_number}</span> · {r.item_title}
            </div>
            <div className={styles.rowSub}>
              {r.composition
                ? <span className={styles.mono}>{r.composition}</span>
                : (r.match_note || "—")}
              {r.methods?.includes("human") && " · human"}
            </div>
          </div>
          <span className={`${styles.pill} ${STATUS_PILL[r.match_status] || styles.pillGray}`}>
            {STATUS_LABEL[r.match_status] || r.match_status}
          </span>
          <span className={styles.rowAge} style={{ marginLeft: 10 }}>{ageText(r.age_s)}</span>
        </div>
      ))}
    </>
  );
}

function QueueInner() {
  const [q, setQ] = useState<Queue | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const [mode, setMode] = useState<"attention" | "all">("attention");
  const router = useRouter();
  const copy = useCopy();

  useEffect(() => {
    let alive = true;
    const load = () => fetchQueue().then((d) => alive && (setQ(d), setErr(null)))
      .catch((e) => alive && setErr(String(e)));
    load();
    const t = setInterval(load, 15000);
    return () => { alive = false; clearInterval(t); };
  }, []);

  const match = useMemo(() => {
    const f = filter.trim().toLowerCase();
    return (r: QueueRow) => !f
      || r.item_number.includes(f)
      || (r.item_title || "").toLowerCase().includes(f);
  }, [filter]);

  if (err) return <div className={styles.alert + " " + styles.alertRed}>API недоступен: {err}</div>;
  if (!q) return <div className={styles.zeroState}><span className={styles.spin} /> загрузка…</div>;

  const open = (n: string) => router.push(`/truth/listing/${n}`);
  const conflicts = q.conflicts.filter(match);
  const nic = q.not_in_catalog.filter(match);
  const needTexts = q.need_texts.filter(match);
  const titles = q.title_cards.filter(match);
  const empty = q.counts.total === 0;

  return (
    <div className={styles.narrow}>
      <div className={styles.sectionHead}>
        <div className={styles.segmented}>
          <button className={`${styles.segBtn} ${mode === "attention" ? styles.segActive : ""}`}
            onClick={() => setMode("attention")}>требуют внимания ({q.counts.total})</button>
          <button className={`${styles.segBtn} ${mode === "all" ? styles.segActive : ""}`}
            onClick={() => setMode("all")}>все листинги ({q.total_items})</button>
        </div>
        <input className={styles.search} placeholder="поиск: номер, титул или артикул…"
               value={filter} onChange={(e) => setFilter(e.target.value)} />
      </div>

      {mode === "all" && <AllListings filter={filter} open={open} />}

      {mode === "attention" && empty && (
        <div className={styles.zeroState}>
          <div className={styles.zeroTitle}>Истина полная ✓</div>
          <div>{q.linked} из {q.total_items} листингов — linked, разбирать нечего.</div>
        </div>
      )}

      {mode === "attention" && conflicts.length > 0 && (
        <>
          <h2 className={styles.groupTitle}>Конфликты <span className={styles.groupCount}>{conflicts.length}</span></h2>
          {conflicts.map((r) => (
            <Row key={r.item_number} r={r} accent={styles.rowConflict} onOpen={() => open(r.item_number)}
              sub={(r.contradictions && r.contradictions[0]) || r.qty_note || r.match_note || "конфликт"} />
          ))}
        </>
      )}

      {mode === "attention" && nic.length > 0 && (
        <>
          <h2 className={styles.groupTitle}>Нет в каталоге <span className={styles.groupCount}>{nic.length}</span></h2>
          {nic.map((r) => (
            <Row key={r.item_number} r={r} accent={styles.rowAmber} onOpen={() => open(r.item_number)}
              sub={<>
                завести в smart:{" "}
                {(r.missing || []).map((m) => (
                  <button key={m} className={styles.copy}
                          onClick={(e) => { e.stopPropagation(); copy(m); }}>{m}</button>
                ))}
              </>} />
          ))}
        </>
      )}

      {mode === "attention" && titles.length > 0 && (
        <>
          <h2 className={styles.groupTitle}>Титулы разошлись <span className={styles.groupCount}>{titles.length}</span></h2>
          {titles.map((r) => (
            <Row key={r.item_number} r={r} accent={styles.rowConflict} onOpen={() => open(r.item_number)}
              sub="скриншот и страница дают разные титулы — выбери канон в карточке" />
          ))}
        </>
      )}

      {mode === "attention" && needTexts.length > 0 && (
        <>
          <h2 className={styles.groupTitle}>Можно догрузить <span className={styles.groupCount}>{needTexts.length}</span></h2>
          {needTexts.map((r) => (
            <Row key={r.item_number} r={r} accent={styles.rowGray} onOpen={() => open(r.item_number)}
              sub={r.catalog_url ? "листинг делистнут — есть каталожная страница-подсказка" : "страница мертва — тексты только руками"} />
          ))}
        </>
      )}

      {mode === "attention" && q.refunds.length > 0 && (
        <>
          <h2 className={styles.groupTitle}>Возвраты <span className={styles.groupCount}>{q.refunds.length}</span></h2>
          {q.refunds.map((r) => (
            <div key={r.card_id} className={`${styles.row} ${styles.rowConflict}`}>
              <div className={styles.rowBody}>
                <div className={styles.rowTitle}>заказ {r.order_number}</div>
                <div className={styles.rowSub}>refund — действия появятся с этапом едущих экземпляров</div>
              </div>
              <span className={styles.rowAge}>{ageText(r.age_s)}</span>
            </div>
          ))}
        </>
      )}
    </div>
  );
}

export default function QueuePage() {
  return <ToastProvider><QueueInner /></ToastProvider>;
}
