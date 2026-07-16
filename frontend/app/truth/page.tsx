"use client";
import { Suspense, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
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
  conflict: styles.rowConflict, not_in_catalog: styles.rowAmber,
  no_article: styles.rowAmber, needs_review: styles.rowGray, pending: styles.rowGray,
};

/** Прокрутка живёт в #truth-scroll (layout) — запоминаем/восстанавливаем по URL. */
function useScrollMemory(ready: boolean) {
  const sp = useSearchParams();
  const pathname = usePathname();
  const restored = useRef<string | null>(null);
  useEffect(() => {
    const el = document.getElementById("truth-scroll");
    if (!el || !ready) return;
    const key = `truth-scroll:${pathname}?${sp.toString()}`;
    if (restored.current !== key) {
      restored.current = key;
      const saved = sessionStorage.getItem(key);
      if (saved) el.scrollTop = +saved;
    }
    const onScroll = () => sessionStorage.setItem(key, String(el.scrollTop));
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, [ready, pathname, sp]);
}

function Row({ r, accent, sub, right }: {
  r: { item_number: string; item_title: string; photo?: string | null; age_s?: number | null };
  accent?: string; sub: React.ReactNode; right?: React.ReactNode;
}) {
  return (
    <Link href={`/truth/listing/${r.item_number}`} className={`${styles.row} ${accent || ""}`}>
      {r.photo
        ? <img className={styles.thumb} src={photoSrc(r.photo)} alt="" loading="lazy" />
        : <div className={`${styles.thumb} ${styles.thumbEmpty}`}>нет фото</div>}
      <div className={styles.rowBody}>
        <div className={styles.rowTitle}>
          <span className={styles.mono}>{r.item_number}</span> · {r.item_title}
        </div>
        <div className={styles.rowSub}>{sub}</div>
      </div>
      {right}
      <span className={styles.rowAge} style={{ marginLeft: 10 }}>{ageText(r.age_s)}</span>
    </Link>
  );
}

function AllListings({ filter, onReady }: { filter: string; onReady: (ok: boolean) => void }) {
  const [rows, setRows] = useState<ListingRow[] | null>(null);
  useEffect(() => {
    fetchAllListings().then((d) => { setRows(d.listings); onReady(true); })
      .catch(() => { setRows([]); onReady(true); });
  }, [onReady]);
  if (!rows) return <div className={styles.zeroState}><span className={styles.spin} /> загрузка…</div>;
  const f = filter.trim().toLowerCase();
  const shown = rows.filter((r) => !f || r.item_number.includes(f)
    || (r.item_title || "").toLowerCase().includes(f)
    || (r.composition || "").toLowerCase().includes(f));
  return (
    <>
      <h2 className={styles.groupTitle}>Все листинги <span className={styles.groupCount}>{shown.length}</span></h2>
      {shown.map((r) => (
        <Row key={r.item_number} r={r} accent={ACCENT[r.match_status]}
          sub={<>
            {r.composition
              ? <span className={styles.mono}>{r.composition}</span>
              : (r.match_note || "—")}
            {r.methods?.includes("human") && " · human"}
          </>}
          right={
            <span className={`${styles.pill} ${STATUS_PILL[r.match_status] || styles.pillGray}`}>
              {STATUS_LABEL[r.match_status] || r.match_status}
            </span>
          } />
      ))}
    </>
  );
}

function QueueInner() {
  const router = useRouter();
  const pathname = usePathname();
  const sp = useSearchParams();
  const mode = sp.get("view") === "all" ? "all" : "attention";
  const [filter, setFilter] = useState(sp.get("q") || "");
  const [q, setQ] = useState<Queue | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [allReady, setAllReady] = useState(false);
  const copy = useCopy();
  const debounce = useRef<ReturnType<typeof setTimeout> | null>(null);

  const setUrl = (view: string, query: string) => {
    const p = new URLSearchParams();
    if (view === "all") p.set("view", "all");
    if (query.trim()) p.set("q", query.trim());
    router.replace(`${pathname}${p.size ? `?${p}` : ""}`, { scroll: false });
  };

  const onFilter = (v: string) => {
    setFilter(v);
    if (debounce.current) clearTimeout(debounce.current);
    debounce.current = setTimeout(() => setUrl(mode, v), 350);
  };

  useEffect(() => {
    let alive = true;
    const load = () => fetchQueue().then((d) => alive && (setQ(d), setErr(null)))
      .catch((e) => alive && setErr(String(e)));
    load();
    const t = setInterval(load, 15000);
    return () => { alive = false; clearInterval(t); };
  }, []);

  useScrollMemory(mode === "all" ? allReady : q !== null);

  const match = useMemo(() => {
    const f = filter.trim().toLowerCase();
    return (r: QueueRow) => !f
      || r.item_number.includes(f)
      || (r.item_title || "").toLowerCase().includes(f);
  }, [filter]);

  if (err) return <div className={styles.alert + " " + styles.alertRed}>API недоступен: {err}</div>;
  if (!q) return <div className={styles.zeroState}><span className={styles.spin} /> загрузка…</div>;

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
            onClick={() => setUrl("attention", filter)}>требуют внимания ({q.counts.total})</button>
          <button className={`${styles.segBtn} ${mode === "all" ? styles.segActive : ""}`}
            onClick={() => setUrl("all", filter)}>все листинги ({q.total_items})</button>
        </div>
        <input className={styles.search} placeholder="поиск: номер, титул или артикул…"
               value={filter} onChange={(e) => onFilter(e.target.value)} />
      </div>

      {mode === "all" && <AllListings filter={filter} onReady={setAllReady} />}

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
            <Row key={r.item_number} r={r} accent={styles.rowConflict}
              sub={(r.contradictions && r.contradictions[0]) || r.qty_note || r.match_note || "конфликт"} />
          ))}
        </>
      )}

      {mode === "attention" && nic.length > 0 && (
        <>
          <h2 className={styles.groupTitle}>Нет в каталоге <span className={styles.groupCount}>{nic.length}</span></h2>
          {nic.map((r) => (
            <Row key={r.item_number} r={r} accent={styles.rowAmber}
              sub={<>
                завести в smart:{" "}
                {(r.missing || []).map((m) => (
                  <button key={m} className={styles.copy}
                    onClick={(e) => { e.preventDefault(); e.stopPropagation(); copy(m); }}>{m}</button>
                ))}
              </>} />
          ))}
        </>
      )}

      {mode === "attention" && titles.length > 0 && (
        <>
          <h2 className={styles.groupTitle}>Титулы разошлись <span className={styles.groupCount}>{titles.length}</span></h2>
          {titles.map((r) => (
            <Row key={r.item_number} r={r} accent={styles.rowConflict}
              sub="скриншот и страница дают разные титулы — выбери канон в карточке" />
          ))}
        </>
      )}

      {mode === "attention" && needTexts.length > 0 && (
        <>
          <h2 className={styles.groupTitle}>Можно догрузить <span className={styles.groupCount}>{needTexts.length}</span></h2>
          {needTexts.map((r) => (
            <Row key={r.item_number} r={r} accent={styles.rowGray}
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
  return (
    <ToastProvider>
      <Suspense fallback={<div />}>
        <QueueInner />
      </Suspense>
    </ToastProvider>
  );
}
