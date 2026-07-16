"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { fetchNumbers, ignoreNumber, unignoreNumber, type NumberRow, type Numbers } from "@/lib/truth";
import { ToastProvider, useCopy, useToast } from "../toast";
import styles from "../truth.module.css";

type Tab = "candidates" | "conflicts" | "ignored";

function Inner() {
  const [d, setD] = useState<Numbers | null>(null);
  const [tab, setTab] = useState<Tab>("candidates");
  const copy = useCopy();
  const toast = useToast();

  const load = () => fetchNumbers().then(setD).catch(() => toast("API недоступен"));
  useEffect(() => { load(); }, []);  // eslint-disable-line react-hooks/exhaustive-deps

  if (!d) return <div className={styles.zeroState}><span className={styles.spin} /> считаю по прогонам…</div>;

  const rows: NumberRow[] = tab === "candidates" ? d.candidates
    : tab === "conflicts" ? d.catalog_conflicts : d.ignored;

  const doIgnore = async (n: string) => { await ignoreNumber(n); toast(`${n} — в игнор`); load(); };
  const doRestore = async (n: string) => { await unignoreNumber(n); toast(`${n} — возвращён`); load(); };

  return (
    <div className={styles.narrow}>
      <div className={styles.subTabs}>
        <button className={`${styles.tab} ${tab === "candidates" ? styles.tabActive : ""}`}
          onClick={() => setTab("candidates")}>Кандидаты ({d.candidates.length})</button>
        <button className={`${styles.tab} ${tab === "conflicts" ? styles.tabActive : ""}`}
          onClick={() => setTab("conflicts")}>Каталожные конфликты ({d.catalog_conflicts.length})</button>
        <button className={`${styles.tab} ${tab === "ignored" ? styles.tabActive : ""}`}
          onClick={() => setTab("ignored")}>Игнор ({d.ignored.length})</button>
        <span className={styles.groupCount} style={{ alignSelf: "center", marginLeft: 8 }}>
          скрыто известных кроссов: {d.hidden_known_crosses}
        </span>
      </div>

      {tab === "candidates" && (
        <div className={styles.comment} style={{ marginBottom: 12 }}>
          Номера, которые агент видел, но каталог не знает: настоящие кроссы — добавляй в articles детали в smart;
          мусор (штрихкоды, годы) — гаси крестиком. Новый формат артикула? Кнопка «правило».
        </div>
      )}
      {tab === "conflicts" && (
        <div className={styles.comment} style={{ marginBottom: 12 }}>
          Агент считает номер кроссом сматченной детали, а каталог держит его ДРУГОЙ деталью —
          чаще это компоненты китов (норма), но здесь же всплывают дубли каталога.
        </div>
      )}

      <table className={styles.table}>
        <thead>
          <tr>
            <th>номер</th><th>раз</th>{tab === "conflicts" && <th>в каталоге это</th>}
            <th>почему не взят / причина</th><th>листинги</th><th />
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.normalized}>
              <td><button className={styles.copy} onClick={() => copy(r.normalized)}>{r.normalized}</button></td>
              <td>{r.count}</td>
              {tab === "conflicts" && <td className={styles.mono}>{r.other_part_id}</td>}
              <td style={{ maxWidth: 380 }}>{tab === "ignored" ? (r.reason || "—") : r.why}</td>
              <td>
                {(r.listings || []).slice(0, 3).map((l) => (
                  <Link key={l} href={`/truth/listing/${l}`} className={styles.mono}
                        style={{ marginRight: 8, textDecoration: "underline" }}>{l}</Link>
                ))}
                {(r.listings || []).length > 3 && <span className={styles.groupCount}>+{r.listings.length - 3}</span>}
              </td>
              <td style={{ whiteSpace: "nowrap" }}>
                {tab !== "ignored" ? (
                  <>
                    <Link href={`/truth/rules?seed=${encodeURIComponent(r.normalized)}`}
                          className={styles.copy} style={{ marginRight: 6 }}>правило</Link>
                    <button className={styles.iconBtn} title="игнорировать навсегда"
                            onClick={() => doIgnore(r.normalized)}>✕</button>
                  </>
                ) : (
                  <button className={styles.copy} onClick={() => doRestore(r.normalized)}>вернуть</button>
                )}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr><td colSpan={6} style={{ textAlign: "center", color: "var(--on-dark-faint)", padding: 24 }}>пусто</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

export default function NumbersPage() {
  return <ToastProvider><Inner /></ToastProvider>;
}
