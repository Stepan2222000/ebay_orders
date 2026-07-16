"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import {
  deleteExample, fetchExamples, fetchNumbers, ignoreNumber, unignoreNumber,
  type ExampleRow, type NumberRow, type Numbers,
} from "@/lib/truth";
import { ToastProvider, useCopy, useToast } from "../toast";
import styles from "../truth.module.css";

const KIND: Record<string, [string, string]> = {
  rule: [styles.pillRed, "правило"],
  not_seen: [styles.pillAmber, "не увидел"],
  semantic: [styles.pillBlue, "смысл"],
  unclear: [styles.pillGray, "неясно"],
};

function exportCsv(rows: ExampleRow[]) {
  const esc = (s: unknown) => `"${String(s ?? "").replace(/"/g, '""')}"`;
  const lines = [["дата", "листинг", "тип", "агент_прочитал", "агент_вердикт", "должно_быть", "заметка"].join(";")];
  for (const e of rows) {
    const agent = (e.agent_snapshot?.positions || [])
      .map((p) => `${p.article_read}${p.part_id ? `→${p.part_id}` : ""}×${p.qty}`).join(" | ");
    const human = e.human_lines.map((l) => `${l.canonical || l.article}→${l.part_id}×${l.qty}`).join(" | ");
    lines.push([e.created_at.slice(0, 10), e.item_number, KIND[e.kind]?.[1] || e.kind,
      agent, e.agent_snapshot?.verdict || "", human, e.note || ""].map(esc).join(";"));
  }
  const blob = new Blob(["﻿" + lines.join("\n")], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `match_examples_${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
}

function Corrections() {
  const [rows, setRows] = useState<ExampleRow[] | null>(null);
  const [filter, setFilter] = useState<string>("all");
  const toast = useToast();
  const load = () => fetchExamples().then((d) => setRows(d.examples)).catch(() => toast("API недоступен"));
  useEffect(() => { load(); }, []);  // eslint-disable-line react-hooks/exhaustive-deps

  if (!rows) return <div className={styles.comment}><span className={styles.spin} /> загрузка…</div>;
  const shown = rows.filter((r) => filter === "all" || r.kind === filter);

  return (
    <>
      <div className={styles.sectionHead}>
        <div className={styles.segmented}>
          {[["all", `все (${rows.length})`],
            ["rule", `правило (${rows.filter((r) => r.kind === "rule").length})`],
            ["not_seen", `не увидел (${rows.filter((r) => r.kind === "not_seen").length})`],
            ["semantic", `смысл (${rows.filter((r) => r.kind === "semantic").length})`],
          ].map(([k, label]) => (
            <button key={k} className={`${styles.segBtn} ${filter === k ? styles.segActive : ""}`}
              onClick={() => setFilter(k)}>{label}</button>
          ))}
        </div>
        <button className={styles.btn} disabled={!rows.length} onClick={() => exportCsv(shown)}>экспорт CSV</button>
        <button className={styles.btn} disabled={!rows.length}
          onClick={() => {
            const blob = new Blob([JSON.stringify(shown, null, 1)], { type: "application/json" });
            const a = document.createElement("a");
            a.href = URL.createObjectURL(blob);
            a.download = "match_examples.json"; a.click();
          }}>JSON</button>
      </div>
      {shown.length === 0 && (
        <div className={styles.comment} style={{ padding: "18px 0" }}>
          примеров пока нет — они появляются сами, когда ты правишь состав в карточке листинга
        </div>
      )}
      {shown.map((e) => {
        const [pill, label] = KIND[e.kind] || [styles.pillGray, e.kind];
        return (
          <div key={e.id} className={styles.exampleRow}>
            <div className={styles.exampleHead}>
              <span className={`${styles.pill} ${pill}`}>{label}</span>
              <Link href={`/truth/listing/${e.item_number}`} className={styles.mono}
                style={{ textDecoration: "underline" }}>{e.item_number}</Link>
              <span className={styles.rowSub} style={{ flex: 1 }}>{e.item_title}</span>
              <span className={styles.groupCount}>{new Date(e.created_at).toLocaleString("ru")}</span>
              <button className={styles.iconBtn} title="удалить пример"
                onClick={async () => { await deleteExample(e.id); load(); }}>✕</button>
            </div>
            <div className={styles.examplePair}>
              <div>
                <div className={styles.formLabel}>агент увидел / решил ({e.agent_snapshot?.verdict || "—"})</div>
                {(e.agent_snapshot?.positions || []).map((p, i) => (
                  <div key={i} className={styles.mono}>
                    {p.article_read} {p.part_id ? `→ ${p.part_id}` : "→ не в каталоге"} ×{p.qty}
                  </div>
                ))}
                {!(e.agent_snapshot?.positions || []).length && <span className={styles.groupCount}>ничего</span>}
              </div>
              <div className={styles.exampleArrow}>→</div>
              <div>
                <div className={styles.formLabel}>должно быть (человек)</div>
                {e.human_lines.map((l, i) => (
                  <div key={i} className={styles.mono}>{l.canonical || l.article} → {l.part_id} ×{l.qty}</div>
                ))}
              </div>
            </div>
            {e.note && <div className={styles.comment} style={{ marginTop: 6 }}>«{e.note}»</div>}
          </div>
        );
      })}
    </>
  );
}

function Noticed() {
  const [d, setD] = useState<Numbers | null>(null);
  const [tab, setTab] = useState<"candidates" | "conflicts" | "ignored">("candidates");
  const copy = useCopy();
  const toast = useToast();
  const load = () => fetchNumbers().then(setD).catch(() => toast("API недоступен"));
  useEffect(() => { load(); }, []);  // eslint-disable-line react-hooks/exhaustive-deps

  if (!d) return <div className={styles.comment}><span className={styles.spin} /> считаю по прогонам…</div>;
  const rows: NumberRow[] = tab === "candidates" ? d.candidates
    : tab === "conflicts" ? d.catalog_conflicts : d.ignored;

  return (
    <>
      <div className={styles.sectionHead}>
        <div className={styles.segmented}>
          <button className={`${styles.segBtn} ${tab === "candidates" ? styles.segActive : ""}`}
            onClick={() => setTab("candidates")}>кроссы-кандидаты ({d.candidates.length})</button>
          <button className={`${styles.segBtn} ${tab === "conflicts" ? styles.segActive : ""}`}
            onClick={() => setTab("conflicts")}>каталожные конфликты ({d.catalog_conflicts.length})</button>
          <button className={`${styles.segBtn} ${tab === "ignored" ? styles.segActive : ""}`}
            onClick={() => setTab("ignored")}>игнор ({d.ignored.length})</button>
        </div>
        <span className={styles.groupCount}>скрыто: кроссы {d.hidden_known_crosses} · мусор {d.hidden_trash}</span>
      </div>
      <table className={styles.table}>
        <thead>
          <tr>
            <th>номер</th><th>раз</th>{tab === "conflicts" && <th>в каталоге это</th>}
            <th>почему не взят</th><th>листинги</th><th />
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.normalized}>
              <td><button className={styles.copy} onClick={() => copy(r.normalized)}>{r.normalized}</button></td>
              <td>{r.count}</td>
              {tab === "conflicts" && <td className={styles.mono}>{r.other_part_id}</td>}
              <td style={{ maxWidth: 420 }}>{tab === "ignored" ? (r.reason || "—") : r.why}</td>
              <td>
                {(r.listings || []).slice(0, 3).map((l) => (
                  <Link key={l} href={`/truth/listing/${l}`} className={styles.mono}
                    style={{ marginRight: 8, textDecoration: "underline" }}>{l}</Link>
                ))}
                {(r.listings || []).length > 3 && <span className={styles.groupCount}>+{r.listings.length - 3}</span>}
              </td>
              <td style={{ whiteSpace: "nowrap" }}>
                {tab !== "ignored"
                  ? <button className={styles.iconBtn} title="игнорировать навсегда"
                      onClick={async () => { await ignoreNumber(r.normalized); toast(`${r.normalized} — в игнор`); load(); }}>✕</button>
                  : <button className={styles.copy}
                      onClick={async () => { await unignoreNumber(r.normalized); toast("возвращён"); load(); }}>вернуть</button>}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr><td colSpan={6} style={{ textAlign: "center", color: "var(--on-dark-faint)", padding: 24 }}>пусто</td></tr>
          )}
        </tbody>
      </table>
    </>
  );
}

export default function ExamplesPage() {
  return (
    <ToastProvider>
      <div className={styles.narrow}>
        <h2 className={styles.groupTitle}>Твои поправки → материал для правил и промпта</h2>
        <Corrections />
        <h2 className={styles.groupTitle} style={{ marginTop: 40 }}>Замеченные номера</h2>
        <div className={styles.comment} style={{ marginBottom: 12 }}>
          Номера, которые агент видел мимоходом: кроссы-кандидаты — добавляй в articles детали в smart;
          каталожные конфликты — возможные дубли каталога; мусор гасится крестиком.
        </div>
        <Noticed />
      </div>
    </ToastProvider>
  );
}
