"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ageText, fetchListing, markOrderCancelled, photoSrc, putSnapshotTexts,
  recheckCatalog, reduceTransit, refetchSnapshot, rerunListing, resolveCard,
  resolvePreview, saveComposition, saveOrderNote, unmarkOrderCancelled,
  STATUS_LABEL, type Listing, type ResolvedLine, type RunPosition,
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

const SRC_LABEL: Record<string, string> = {
  photo: "фото", title: "титул", description: "описание", specifics: "specifics",
};

const KIND_LABEL: Record<string, string> = {
  rule: "пример: дыра в правилах", not_seen: "пример: агент не увидел",
  semantic: "пример: ошибка смысла", unclear: "пример: причина неясна",
};

interface Row { article: string; qty: number; key: number; }

/** Текст с редактированием по двойному клику. */
function EditableText({ label, value, placeholder, onSave, mono }: {
  label: string; value: string; placeholder: string; mono?: boolean;
  onSave: (v: string) => Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (!editing) setDraft(value); }, [value, editing]);
  return (
    <div className={styles.textBlock}>
      <div className={styles.formLabel}>{label} <span className={styles.hint}>двойной клик — редактировать</span></div>
      {editing ? (
        <>
          <textarea className={styles.textarea} style={mono ? { fontFamily: "var(--font-mono)" } : undefined}
            value={draft} autoFocus onChange={(e) => setDraft(e.target.value)} />
          <div className={styles.actions} style={{ marginTop: 6 }}>
            <button className={`${styles.btn} ${styles.btnPrimary}`} disabled={busy || !draft.trim()}
              onClick={async () => { setBusy(true); await onSave(draft); setBusy(false); setEditing(false); }}>
              {busy ? <span className={styles.spin} /> : "Сохранить"}
            </button>
            <button className={`${styles.btn} ${styles.btnGhost}`} onClick={() => setEditing(false)}>отмена</button>
          </div>
        </>
      ) : (
        <div className={`${styles.textView} ${!value ? styles.textEmpty : ""}`}
          onDoubleClick={() => setEditing(true)} title="двойной клик — редактировать">
          {value || placeholder}
        </div>
      )}
    </div>
  );
}

/** Строка заказа: признак отмены (полный возврат / ручная пометка — SPEC §10)
    и личная заметка «для себя» (двойной клик). Разбор истины отмена не меняет —
    эффект только «не ждём приезда». */
function OrderLine({ o, item, onChanged }: {
  o: Listing["orders"][number]; item: string; onChanged: () => Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(o.user_note || "");
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (!editing) setDraft(o.user_note || ""); }, [o.user_note, editing]);
  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try { await fn(); await onChanged(); } finally { setBusy(false); }
  };
  const transitTotal = o.transit.parts.reduce((s, p) => s + p.draft + p.accepted, 0);
  return (
    <>
    <div className={styles.metaLine}>
      <span>заказ <span className={styles.mono}>{o.order_number}</span> ×{o.item_quantity}</span>
      {o.full_refund && (
        <span className={`${styles.pill} ${styles.pillRed}`}>
          отменён · полный возврат ${o.refunded_usd}{o.last_refund_date ? ` от ${o.last_refund_date}` : ""}
        </span>
      )}
      {!!o.cancelled_at && !o.full_refund && (
        <span className={`${styles.pill} ${styles.pillRed}`} title={o.cancel_note || ""}>отменён вручную</span>
      )}
      {!o.full_refund && (o.cancelled_at
        ? <button className={styles.copy} disabled={busy}
            onClick={() => run(() => unmarkOrderCancelled(o.order_id))}>снять отмену</button>
        : <button className={styles.copy} disabled={busy}
            onClick={() => run(() => markOrderCancelled(o.order_id))}>пометить отменённым</button>)}
      {editing ? (
        <input className={styles.inlineInput} style={{ minWidth: 260 }} value={draft} autoFocus
          placeholder="заметка для себя (может не приехать, приехать неполным…)"
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") run(() => saveOrderNote(o.order_id, draft)).then(() => setEditing(false));
            if (e.key === "Escape") setEditing(false);
          }} />
      ) : (
        <span className={o.user_note ? undefined : styles.textEmpty}
          onDoubleClick={() => setEditing(true)} title="двойной клик — заметка">
          {o.user_note || "заметка…"}
        </span>
      )}
      {o.transit.journal && (
        <span className={`${styles.pill} ${transitTotal ? styles.pillGreen : styles.pillGray}`}>
          едущие: {transitTotal}
        </span>
      )}
    </div>
    {/* едущие экземпляры (SPEC §10): состав запечатлён, правка — удалить/уменьшить draft */}
    {o.transit.parts.length > 0 && (
      <div className={styles.metaLine} style={{ paddingLeft: 18 }}>
        {o.transit.parts.map((p) => (
          <span key={p.part_id}>
            {p.name || p.part_id} ×{p.draft + p.accepted}
            {p.accepted > 0 && ` (принято ${p.accepted})`}
            {p.draft > 0 && (
              <>
                {" "}
                <button className={styles.copy} disabled={busy} title="приедет на 1 меньше"
                  onClick={() => run(() => reduceTransit(o.order_id, item, p.part_id, 1))}>−1</button>
                <button className={styles.copy} disabled={busy} title="не приедет — убрать все черновики"
                  onClick={() => run(() => reduceTransit(o.order_id, item, p.part_id, p.draft))}>убрать</button>
              </>
            )}
          </span>
        ))}
      </div>
    )}
    {o.transit.journal && o.transit.parts.length === 0 && (
      <div className={`${styles.metaLine} ${styles.textEmpty}`} style={{ paddingLeft: 18 }}>
        едущие удалены вручную — пересоздания не будет
      </div>
    )}
    </>
  );
}

function Inner({ item }: { item: string }) {
  const [d, setD] = useState<Listing | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [photoIdx, setPhotoIdx] = useState(0);
  const [rows, setRows] = useState<Row[]>([]);
  const [baseline, setBaseline] = useState("");        // для dirty-сравнения
  const [resolved, setResolved] = useState<ResolvedLine[]>([]);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const keyRef = useRef(1);
  const copy = useCopy();
  const toast = useToast();
  const debounce = useRef<ReturnType<typeof setTimeout> | null>(null);

  const initRows = useCallback((data: Listing) => {
    const run = data.runs.find((r) => r.status === "done" && !r.dry_run);
    const src: Row[] = data.composition.length
      ? data.composition.map((c) => ({ article: c.matched_article, qty: c.quantity, key: keyRef.current++ }))
      : (run?.positions || []).map((p) => ({
          article: p.canonical || p.article_read, qty: Math.max(1, p.qty), key: keyRef.current++ }));
    setRows(src);
    setBaseline(JSON.stringify(src.map((r) => [r.article, r.qty])));
    setResolved([]);
    setNote("");
  }, []);

  const load = useCallback(async (reinit = false) => {
    try {
      const data = await fetchListing(item);
      setD(data); setErr(null);
      if (reinit) initRows(data);
      return data;
    } catch (e) { setErr(String(e)); return null; }
  }, [item, initRows]);

  useEffect(() => { load(true); }, [load]);

  const dirty = useMemo(
    () => JSON.stringify(rows.map((r) => [r.article, r.qty])) !== baseline,
    [rows, baseline]);

  // фоновое обновление — только пока не редактируешь
  useEffect(() => {
    const t = setInterval(async () => {
      if (dirty) return;
      const data = await load(false);
      if (data && !dirty) initRows(data);
    }, d?.agent_running ? 3000 : 20000);
    return () => clearInterval(t);
  }, [load, initRows, dirty, d?.agent_running]);

  // живая валидация строк
  useEffect(() => {
    if (debounce.current) clearTimeout(debounce.current);
    debounce.current = setTimeout(async () => {
      const filled = rows.filter((r) => r.article.trim());
      if (!filled.length) { setResolved([]); return; }
      try {
        setResolved((await resolvePreview(filled.map((r) => ({ article: r.article, qty: r.qty })))).lines);
      } catch { /* ignore */ }
    }, 300);
  }, [rows]);

  if (err) return <div className={`${styles.alert} ${styles.alertRed}`}>{err}</div>;
  if (!d) return <div className={styles.zeroState}><span className={styles.spin} /> загрузка…</div>;

  const run = d.runs.find((r) => r.status === "done" && !r.dry_run) || d.runs[0];
  const posInfo = new Map<string, RunPosition>();
  for (const p of run?.positions || []) {
    for (const k of [p.canonical, p.article_read]) if (k) posInfo.set(k.toUpperCase(), p);
  }
  const openCards = d.cards.filter((c) => c.status === "open");
  const photo = d.photos[photoIdx];
  const snap = d.snapshot as Record<string, string> | null;
  const isHuman = d.composition.length > 0 && d.composition.every((c) => c.match_method === "human");

  const filled = rows.filter((r) => r.article.trim());
  const allOk = filled.length > 0 && resolved.length === filled.length && resolved.every((r) => r.part_id);

  const doSave = async () => {
    setBusy("save");
    try {
      const res = await saveComposition(item, filled.map((r) => ({ article: r.article, qty: r.qty })), note);
      toast(res.example_kind ? `сохранено · ${KIND_LABEL[res.example_kind]}` : "сохранено как human");
      const data = await load(false);
      if (data) initRows(data);
    } catch (e) { toast(`ошибка: ${e}`); }
    setBusy(null);
  };

  const act = (name: string, fn: () => Promise<unknown>, done: string) => async () => {
    setBusy(name);
    try { await fn(); toast(done); const data = await load(false); if (data && !dirty) initRows(data); }
    catch (e) { toast(`${e}`); }
    setBusy(null);
  };

  // перерешив без LLM (SPEC §6): «завёл деталь в smart → проверил» — мгновенно
  const doRecheck = async () => {
    setBusy("recheck");
    try {
      const r = await recheckCatalog(item);
      if (!r.applicable) {
        toast(r.reason === "unchanged" ? "вход не менялся — вердикт прежний"
          : r.reason === "reading_changed" ? "менялся не только каталог — нужен полный перепрогон агентом"
          : "нет прочитанного состава — только полный прогон агентом");
      } else if (r.verdict === "linked") {
        toast("✓ всё нашлось в каталоге — linked");
      } else if (r.verdict === "not_in_catalog") {
        toast(`каталог всё ещё не знает: ${(r.missing || []).join(", ")}`);
      } else {
        toast(`вердикт: ${STATUS_LABEL[r.verdict || ""] || r.verdict}`);
      }
      const data = await load(false);
      if (data && !dirty) initRows(data);
    } catch (e) { toast(`${e}`); }
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

      {/* ── Правая колонка ── */}
      <div>
        <div className={styles.panel}>
          <span className={`${styles.pill} ${STATUS_PILL[d.item.match_status] || styles.pillGray}`}>
            {STATUS_LABEL[d.item.match_status] || d.item.match_status}
          </span>
          {isHuman && <span className={`${styles.pill} ${styles.pillBlue}`} style={{ marginLeft: 6 }}>human</span>}
          {d.agent_running && <span style={{ marginLeft: 10 }}><span className={styles.spin} /> агент думает…</span>}
          <div className={styles.listingTitle}>{d.item.item_title}</div>
          <div className={styles.metaLine}>
            <button className={styles.copy} onClick={() => copy(item)}>{item}</button>
            {run?.lot_kind && <span>{run.lot_kind}</span>}
            <span>{ageText(d.item.matched_at ? (Date.now() - Date.parse(d.item.matched_at)) / 1000 : null)}</span>
          </div>
          {d.orders.map((o) => (
            <OrderLine key={o.order_id} o={o} item={item}
              onChanged={async () => { await load(false); }} />
          ))}
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
                    onClick={act("card", () => resolveCard(c.id, undefined, "pdp"), "канон = страница")}>
                    канон — со страницы
                  </button>
                  <button className={styles.btn} disabled={!!busy}
                    onClick={act("card", () => resolveCard(c.id, undefined, "ocr"), "канон = скриншот")}>
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
                    onClick={act("card", () => resolveCard(c.id, "разобрано, оставить как есть"), "карточка закрыта")}>
                    закрыть без правки
                  </button>
                </div>
              </>
            )}
          </div>
        ))}

        {/* ── Состав: всегда виден, редактируется на месте ── */}
        <div className={styles.panel}>
          <h3 className={styles.panelTitle}>
            Состав лота
            {d.composition.length === 0 && rows.length > 0 &&
              <span className={`${styles.pill} ${styles.pillAmber}`}>прочитано агентом, не записано</span>}
          </h3>
          {rows.length === 0 && <div className={styles.comment}>агент не нашёл ни одного артикула — добавь строки сам</div>}

          {rows.map((r, i) => {
            const res = resolved[filled.findIndex((f) => f.key === r.key)];
            const info = posInfo.get((res?.canonical || r.article).toUpperCase())
              || posInfo.get(r.article.toUpperCase());
            return (
              <div key={r.key} className={styles.compRowEdit}>
                <input className={`${styles.inlineInput} ${styles.mono}`} value={r.article}
                  placeholder="артикул"
                  onChange={(e) => setRows(rows.map((x) => x.key === r.key ? { ...x, article: e.target.value } : x))} />
                <span className={styles.compName}>
                  {r.article.trim() === "" ? "" :
                    res == null ? <span className={styles.spin} /> :
                    res.part_id ? <span className={styles.ok}>✓ {res.part_name}</span> :
                    <span className={styles.bad}>{res.gate === "uncovered" ? "не покрыт правилами" : "нет в каталоге"}</span>}
                </span>
                {info && (
                  <span className={styles.srcBadges} title={info.note || ""}>
                    {(info.sources || []).map((s) => (
                      <span key={s} className={`${styles.pill} ${styles.pillGray}`}>{SRC_LABEL[s] || s}</span>
                    ))}
                  </span>
                )}
                <span className={styles.qtyWrap}>×<input type="number" min={1}
                  className={`${styles.inlineInput} ${styles.inlineQty}`} value={r.qty}
                  onChange={(e) => setRows(rows.map((x) => x.key === r.key ? { ...x, qty: Math.max(1, +e.target.value || 1) } : x))} /></span>
                <button className={styles.iconBtn} title="убрать строку"
                  onClick={() => setRows(rows.filter((x) => x.key !== r.key))}>✕</button>
              </div>
            );
          })}

          <div className={styles.actions} style={{ marginTop: 10 }}>
            <button className={`${styles.btn} ${styles.btnGhost}`}
              onClick={() => setRows([...rows, { article: "", qty: 1, key: keyRef.current++ }])}>+ строка</button>
            {dirty && (
              <>
                <input className={styles.noteInput} placeholder="заметка к правке (необязательно)"
                  value={note} onChange={(e) => setNote(e.target.value)} />
                <button className={`${styles.btn} ${styles.btnPrimary}`} disabled={!allOk || busy === "save"} onClick={doSave}>
                  {busy === "save" ? <span className={styles.spin} /> : "Сохранить как human"}
                </button>
                <button className={`${styles.btn} ${styles.btnGhost}`} onClick={() => { if (d) initRows(d); }}>отменить</button>
              </>
            )}
            {/* видна и при незаписанной истине (conflict и т.п.): «согласен с
                прочитанным — записать как есть», активна когда все строки зелёные */}
            {!dirty && filled.length > 0 && !isHuman && (
              <button className={styles.btn} disabled={!!busy || !allOk} onClick={doSave}>
                {busy === "save" ? <span className={styles.spin} /> : "Подтвердить как human"}
              </button>
            )}
            {!dirty && (
              <>
                {run && run.status === "done" && !run.dry_run
                  && !d.composition.some((c) => c.match_method !== "regex_exact") && (
                  <button className={styles.btn} disabled={!!busy || d.agent_running}
                    onClick={doRecheck} title="перерешив по свежему каталогу — мгновенно, без LLM">
                    {busy === "recheck" ? <span className={styles.spin} /> : "Проверить каталог"}
                  </button>
                )}
                <button className={styles.btn} disabled={!!busy || d.agent_running}
                  onClick={act("rerun", () => rerunListing(item), "прогон запущен")}>
                  {busy === "rerun" || d.agent_running ? <span className={styles.spin} /> : "Перепрогнать агентом"}
                </button>
                <button className={styles.btn} disabled={!!busy}
                  onClick={act("refetch", () => refetchSnapshot(item), "снапшот перекачан")}>
                  {busy === "refetch" ? <span className={styles.spin} /> : "Перекачать снапшот"}
                </button>
              </>
            )}
          </div>
        </div>

        {/* агент: комментарий и наблюдения */}
        {run && (
          <div className={styles.panel}>
            <h3 className={styles.panelTitle}>
              Агент
              {run.verdict && <span className={`${styles.pill} ${STATUS_PILL[run.verdict] || styles.pillGray}`}>{STATUS_LABEL[run.verdict] || run.verdict}</span>}
            </h3>
            {run.error && <div className={`${styles.alert} ${styles.alertRed}`}>{run.error}</div>}
            {run.comment && <div className={styles.comment}>{run.comment}</div>}
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
              {run.no_llm && <span>без LLM · перерешив по чтению прежнего прогона</span>}
              {run.prompt_tokens != null && <span>{run.prompt_tokens} tok in / {run.completion_tokens} out</span>}
              <span>прогонов: {d.runs.length}</span>
            </div>
          </div>
        )}

        {/* ── Тексты: всегда видны, dblclick — правка ── */}
        <div className={styles.panel}>
          <h3 className={styles.panelTitle}>Тексты листинга
            {snap && <span className={`${styles.pill} ${snap.status === "done" ? styles.pillGreen : styles.pillGray}`}>{String(snap.status)}{snap.source ? ` · ${snap.source}` : ""}</span>}
          </h3>
          {snap?.catalog_url && (
            <div className={styles.comment} style={{ marginBottom: 8 }}>
              делистнут — подсказка: <a href={String(snap.catalog_url)} target="_blank" rel="noreferrer" style={{ textDecoration: "underline" }}>каталожная страница eBay ↗</a>
            </div>
          )}
          {snap?.specifics ? (
            <div className={styles.textBlock}>
              <div className={styles.formLabel}>item specifics (со страницы)</div>
              <div className={styles.textView}>{Object.entries(snap.specifics as unknown as Record<string, string>).map(([k, v]) => `${k}: ${v}`).join("\n")}</div>
            </div>
          ) : null}
          <EditableText label={snap?.specifics ? "specifics — ручная догрузка" : "item specifics"}
            value={String(snap?.specifics_raw || "")}
            placeholder="пусто — вставь как скопировал со страницы (двойной клик)"
            onSave={async (v) => { await putSnapshotTexts(item, v, ""); toast("сохранено — агент перепрогонит сам"); await load(false); }} />
          <EditableText label="description" value={String(snap?.description || "")}
            placeholder="пусто — вставь описание со страницы (двойной клик)"
            onSave={async (v) => { await putSnapshotTexts(item, "", v); toast("сохранено — агент перепрогонит сам"); await load(false); }} />
        </div>
      </div>
    </div>
  );
}

export default function ListingView({ item }: { item: string }) {
  return <ToastProvider><Inner item={item} /></ToastProvider>;
}
