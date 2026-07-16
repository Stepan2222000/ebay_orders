"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  ageText, fetchListing, photoSrc, putSnapshotTexts, refetchSnapshot,
  rerunListing, resolveCard, resolvePreview, saveComposition,
  STATUS_LABEL, type Listing, type ResolvedLine,
} from "@/lib/truth";
import { ToastProvider, useCopy, useToast } from "../../toast";
import styles from "../../truth.module.css";

const STATUS_PILL: Record<string, string> = {
  linked: styles.pillGreen,
  not_in_catalog: styles.pillAmber,
  no_article: styles.pillAmber,
  conflict: styles.pillRed,
  needs_review: styles.pillGray,
  pending: styles.pillGray,
};

const METHOD_PILL: Record<string, [string, string]> = {
  agent: [styles.pillGreen, "агент"],
  human: [styles.pillBlue, "human"],
  regex_exact: [styles.pillGray, "regex ⚠ предварительно"],
};

function Inner({ item }: { item: string }) {
  const [d, setD] = useState<Listing | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [photoIdx, setPhotoIdx] = useState(0);
  const [editing, setEditing] = useState(false);
  const [lines, setLines] = useState<{ article: string; qty: number }[]>([]);
  const [resolved, setResolved] = useState<ResolvedLine[]>([]);
  const [texting, setTexting] = useState(false);
  const [specRaw, setSpecRaw] = useState("");
  const [descRaw, setDescRaw] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const copy = useCopy();
  const toast = useToast();
  const debounce = useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = useCallback(async () => {
    try { setD(await fetchListing(item)); setErr(null); }
    catch (e) { setErr(String(e)); }
  }, [item]);

  useEffect(() => { load(); }, [load]);

  // пока агент думает — обновляемся чаще
  useEffect(() => {
    const t = setInterval(load, d?.agent_running ? 3000 : 15000);
    return () => clearInterval(t);
  }, [load, d?.agent_running]);

  // живая валидация редактора состава
  useEffect(() => {
    if (!editing) return;
    if (debounce.current) clearTimeout(debounce.current);
    debounce.current = setTimeout(async () => {
      const filled = lines.filter((l) => l.article.trim());
      if (!filled.length) { setResolved([]); return; }
      try { setResolved((await resolvePreview(filled)).lines); } catch { /* ignore */ }
    }, 350);
  }, [lines, editing]);

  if (err) return <div className={`${styles.alert} ${styles.alertRed}`}>{err}</div>;
  if (!d) return <div className={styles.zeroState}><span className={styles.spin} /> загрузка…</div>;

  const run = d.runs.find((r) => r.status === "done" && !r.dry_run) || d.runs[0];
  const openCards = d.cards.filter((c) => c.status === "open");
  const photo = d.photos[photoIdx];
  const snap = d.snapshot as Record<string, string> | null;

  const startEdit = () => {
    setLines(d.composition.length
      ? d.composition.map((c) => ({ article: c.matched_article, qty: c.quantity }))
      : [{ article: "", qty: 1 }]);
    setResolved([]);
    setEditing(true);
  };

  const allOk = resolved.length > 0 && resolved.every((r) => r.part_id)
    && resolved.length === lines.filter((l) => l.article.trim()).length;

  const doSave = async () => {
    setBusy("save");
    try {
      await saveComposition(item, lines.filter((l) => l.article.trim()));
      toast("состав сохранён как human");
      setEditing(false);
      await load();
    } catch (e) { toast(`ошибка: ${e}`); }
    setBusy(null);
  };

  const doRerun = async () => {
    setBusy("rerun");
    try { await rerunListing(item); toast("прогон запущен"); await load(); }
    catch (e) { toast(`${e}`); }
    setBusy(null);
  };

  const doRefetch = async () => {
    setBusy("refetch");
    try { await refetchSnapshot(item); toast("снапшот перекачан"); await load(); }
    catch (e) { toast(`${e}`); }
    setBusy(null);
  };

  const doTexts = async () => {
    setBusy("texts");
    try {
      await putSnapshotTexts(item, specRaw, descRaw);
      toast("тексты сохранены — агент перепрогонит сам");
      setTexting(false);
      await load();
    } catch (e) { toast(`${e}`); }
    setBusy(null);
  };

  const confirmHuman = async () => {
    setBusy("confirm");
    try {
      await saveComposition(item, d.composition.map((c) => ({ article: c.matched_article, qty: c.quantity })));
      toast("подтверждено как human");
      await load();
    } catch (e) { toast(`ошибка: ${e}`); }
    setBusy(null);
  };

  return (
    <div className={styles.cardGrid}>
      {/* ── Фото ── */}
      <div className={styles.gallery}>
        {photo ? (
          <a href={photoSrc(photo.url)} target="_blank" rel="noreferrer">
            <img className={styles.mainPhoto} src={photoSrc(photo.url)} alt="" />
          </a>
        ) : <div className={styles.mainPhotoEmpty}>фото нет — агент работал по текстам</div>}
        {d.photos.length > 1 && (
          <div className={styles.thumbs}>
            {d.photos.map((p, i) => (
              <button key={p.id}
                className={`${styles.thumbBtn} ${i === photoIdx ? styles.thumbCurrent : ""} ${p.source === "manual" ? styles.thumbManual : ""}`}
                onClick={() => setPhotoIdx(i)}>
                <img src={photoSrc(p.url)} alt="" loading="lazy" />
              </button>
            ))}
          </div>
        )}
      </div>

      {/* ── Истина ── */}
      <div>
        <div className={styles.panel}>
          <span className={`${styles.pill} ${STATUS_PILL[d.item.match_status] || styles.pillGray}`}>
            {STATUS_LABEL[d.item.match_status] || d.item.match_status}
          </span>
          {d.agent_running && <span style={{ marginLeft: 10 }}><span className={styles.spin} /> агент думает…</span>}
          <div className={styles.listingTitle}>{d.item.item_title}</div>
          <div className={styles.metaLine}>
            <button className={styles.copy} onClick={() => copy(item)}>{item}</button>
            {d.orders.map((o) => (
              <span key={o.order_id}>заказ {o.order_number} ×{o.item_quantity}</span>
            ))}
            {run?.lot_kind && <span>{run.lot_kind}</span>}
            <span>{ageText(d.item.matched_at ? (Date.now() - Date.parse(d.item.matched_at)) / 1000 : null)}</span>
          </div>
        </div>

        {/* карточки разбора */}
        {openCards.map((c) => (
          <div key={c.id} className={`${styles.alert} ${styles.alertRed}`}>
            <b>{c.kind === "title_mismatch" ? "Титулы разошлись" :
                c.kind === "human_disagreement" ? "Агент не согласен с human" : "Противоречие"}</b>
            {c.kind === "title_mismatch" ? (
              <>
                <div style={{ margin: "8px 0" }}>
                  скриншот: «{String(c.payload.ocr_title)}»<br />
                  страница: «{String(c.payload.pdp_title)}»
                </div>
                <div className={styles.actions}>
                  <button className={styles.btn} disabled={!!busy}
                    onClick={async () => { await resolveCard(c.id, undefined, "pdp"); toast("канон = страница"); load(); }}>
                    канон — со страницы
                  </button>
                  <button className={styles.btn} disabled={!!busy}
                    onClick={async () => { await resolveCard(c.id, undefined, "ocr"); toast("канон = скриншот"); load(); }}>
                    канон — со скриншота
                  </button>
                </div>
              </>
            ) : (
              <>
                <div style={{ margin: "8px 0" }}>
                  {((c.payload.contradictions as string[]) || []).map((s, i) => <div key={i}>• {s}</div>)}
                  {Boolean(c.payload.qty_note) && <div>• {String(c.payload.qty_note)}</div>}
                  {((c.payload.uncovered as string[]) || []).length > 0 &&
                    <div>• формат не покрыт правилами: {(c.payload.uncovered as string[]).join(", ")}</div>}
                </div>
                <div className={styles.actions}>
                  <button className={styles.btn} disabled={!!busy}
                    onClick={async () => { await resolveCard(c.id, "разобрано, оставить как есть"); toast("карточка закрыта"); load(); }}>
                    закрыть без правки
                  </button>
                </div>
              </>
            )}
          </div>
        ))}

        {/* состав */}
        <div className={styles.panel}>
          <h3 className={styles.panelTitle}>Состав лота</h3>
          {!editing && d.composition.length === 0 && (
            <div className={styles.comment}>строк нет — истина не готова ({STATUS_LABEL[d.item.match_status] || d.item.match_status})</div>
          )}
          {!editing && d.composition.map((c) => {
            const [pill, label] = METHOD_PILL[c.match_method] || [styles.pillGray, c.match_method];
            return (
              <div key={c.part_id} className={styles.compRow}>
                <span className={styles.compArticle} onClick={() => copy(c.matched_article)}>{c.matched_article}</span>
                <span className={styles.compName}>{c.part_name || c.part_id}</span>
                <span className={`${styles.pill} ${pill}`}>{label}</span>
                <span className={styles.compQty}>×{c.quantity}</span>
              </div>
            );
          })}

          {editing && (
            <>
              {lines.map((l, i) => {
                const r = resolved[lines.filter((x, j) => j < i && x.article.trim()).length];
                const has = l.article.trim();
                return (
                  <div key={i} className={styles.editRow}>
                    <input className={styles.editInput} placeholder="артикул, напр. 26-88397A 1"
                      value={l.article} autoFocus={i === lines.length - 1}
                      onChange={(e) => setLines(lines.map((x, j) => j === i ? { ...x, article: e.target.value } : x))} />
                    <input className={`${styles.editInput} ${styles.editQty}`} type="number" min={1}
                      value={l.qty}
                      onChange={(e) => setLines(lines.map((x, j) => j === i ? { ...x, qty: Math.max(1, +e.target.value || 1) } : x))} />
                    <span className={styles.editStatus}>
                      {has && r ? (r.part_id
                        ? <span className={styles.ok}>✓ {r.canonical} → {r.part_name}</span>
                        : <span className={styles.bad}>✗ {r.gate === "uncovered" ? "не покрыт правилами" : "нет в каталоге"} {r.canonical && <button className={styles.copy} onClick={() => copy(r.canonical!)}>{r.canonical}</button>}</span>)
                        : null}
                    </span>
                    <button className={styles.iconBtn} title="убрать строку"
                      onClick={() => setLines(lines.filter((_, j) => j !== i))}>✕</button>
                  </div>
                );
              })}
              <div className={styles.actions}>
                <button className={styles.btn} onClick={() => setLines([...lines, { article: "", qty: 1 }])}>+ строка</button>
                <button className={`${styles.btn} ${styles.btnPrimary}`} disabled={!allOk || busy === "save"} onClick={doSave}>
                  {busy === "save" ? <span className={styles.spin} /> : "Сохранить как human"}
                </button>
                <button className={`${styles.btn} ${styles.btnGhost}`} onClick={() => setEditing(false)}>отмена</button>
              </div>
            </>
          )}

          {!editing && (
            <div className={styles.actions}>
              {d.composition.length > 0 && d.composition.some((c) => c.match_method !== "human") && (
                <button className={styles.btn} disabled={!!busy} onClick={confirmHuman}>
                  {busy === "confirm" ? <span className={styles.spin} /> : "Подтвердить как human"}
                </button>
              )}
              <button className={styles.btn} onClick={startEdit}>Править состав</button>
              <button className={styles.btn} disabled={!!busy || d.agent_running} onClick={doRerun}>
                {busy === "rerun" || d.agent_running ? <span className={styles.spin} /> : "Перепрогнать агентом"}
              </button>
              <button className={styles.btn} disabled={!!busy} onClick={doRefetch}>
                {busy === "refetch" ? <span className={styles.spin} /> : "Перекачать снапшот"}
              </button>
              <button className={styles.btn} onClick={() => setTexting(!texting)}>Догрузить тексты</button>
            </div>
          )}

          {texting && (
            <div style={{ marginTop: 12 }}>
              <textarea className={styles.textarea} placeholder="item specifics — вставь как скопировал со страницы"
                value={specRaw} onChange={(e) => setSpecRaw(e.target.value)} />
              <textarea className={styles.textarea} placeholder="description — как есть" style={{ marginTop: 8 }}
                value={descRaw} onChange={(e) => setDescRaw(e.target.value)} />
              <div className={styles.actions} style={{ marginTop: 8 }}>
                <button className={`${styles.btn} ${styles.btnPrimary}`}
                  disabled={busy === "texts" || (!specRaw.trim() && !descRaw.trim())} onClick={doTexts}>
                  {busy === "texts" ? <span className={styles.spin} /> : "Сохранить тексты"}
                </button>
              </div>
            </div>
          )}
        </div>

        {/* агент: вывод и наблюдения */}
        {run && (
          <div className={styles.panel}>
            <h3 className={styles.panelTitle}>
              Агент
              {run.verdict && <span className={`${styles.pill} ${STATUS_PILL[run.verdict] || styles.pillGray}`}>{STATUS_LABEL[run.verdict] || run.verdict}</span>}
              {run.dry_run && <span className={`${styles.pill} ${styles.pillGray}`}>сухой</span>}
            </h3>
            {run.error && <div className={`${styles.alert} ${styles.alertRed}`}>{run.error}</div>}
            {run.comment && <div className={styles.comment}>{run.comment}</div>}
            {(run.positions || []).filter((p) => !p.part_id).length > 0 && (
              <div className={`${styles.alert} ${styles.alertAmber}`}>
                прочитано, но не в каталоге:{" "}
                {(run.positions || []).filter((p) => !p.part_id).map((p) => (
                  <button key={p.article_read} className={styles.copy}
                    onClick={() => copy(p.canonical || p.article_read)}>{p.canonical || p.article_read}</button>
                ))}
                {" "}— заведи детали в smart, воркер закроет листинг сам (≤ часа) или жми «Перепрогнать».
              </div>
            )}
            {(run.near_articles || []).length > 0 && (
              <details className={styles.details}>
                <summary>ещё видел ({(run.near_articles || []).length}) — и почему не взял</summary>
                {(run.near_articles || []).map((n, i) => (
                  <div key={i} className={styles.nearRow}>
                    <span className={styles.mono}>{n.text}</span>
                    <span>{n.why}</span>
                  </div>
                ))}
              </details>
            )}
            <div className={styles.metaLine} style={{ marginTop: 10 }}>
              <span>{run.model}</span>
              {run.prompt_tokens != null && <span>{run.prompt_tokens} tok in / {run.completion_tokens} out</span>}
              <span>прогонов всего: {d.runs.length}</span>
            </div>
          </div>
        )}

        {/* тексты снапшота */}
        <div className={styles.panel}>
          <h3 className={styles.panelTitle}>Тексты листинга
            {snap && <span className={`${styles.pill} ${snap.status === "done" ? styles.pillGreen : styles.pillGray}`}>{String(snap.status)}{snap.source ? ` · ${snap.source}` : ""}</span>}
          </h3>
          {!snap && <div className={styles.comment}>снапшота нет</div>}
          {snap?.catalog_url && (
            <div className={styles.comment}>
              делистнут — подсказка: <a href={String(snap.catalog_url)} target="_blank" rel="noreferrer" style={{ textDecoration: "underline" }}>каталожная страница eBay ↗</a>
            </div>
          )}
          {snap?.specifics && (
            <details className={styles.details}>
              <summary>item specifics</summary>
              <div className={styles.detailsBody}>{JSON.stringify(snap.specifics, null, 2)}</div>
            </details>
          )}
          {Boolean(snap?.specifics_raw) && (
            <details className={styles.details}>
              <summary>specifics (ручная догрузка)</summary>
              <div className={styles.detailsBody}>{String(snap!.specifics_raw)}</div>
            </details>
          )}
          {Boolean(snap?.description) && (
            <details className={styles.details}>
              <summary>description</summary>
              <div className={styles.detailsBody}>{String(snap!.description)}</div>
            </details>
          )}
        </div>
      </div>
    </div>
  );
}

export default function ListingView({ item }: { item: string }) {
  return <ToastProvider><Inner item={item} /></ToastProvider>;
}
