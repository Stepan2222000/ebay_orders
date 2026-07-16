"use client";
import { Suspense, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { dryRunRule, fetchRules, saveRule, type DryRun, type Rule } from "@/lib/truth";
import { ToastProvider, useToast } from "../toast";
import styles from "../truth.module.css";

const NEW: Rule = { name: "", canonical: "MERCRUISER", find_regex: "", note: "", enabled: true, example_from: null, example_to: null };

function Inner() {
  const seed = useSearchParams().get("seed") || "";
  const [rules, setRules] = useState<Rule[]>([]);
  const [brands, setBrands] = useState<string[]>([]);
  const [audit, setAudit] = useState<{ rule_name: string; action: string; note: string | null; created_at: string }[]>([]);
  const [sel, setSel] = useState<Rule | null>(null);
  const [isNew, setIsNew] = useState(false);
  const [form, setForm] = useState<Rule>(NEW);
  const [dry, setDry] = useState<DryRun | null>(null);
  const [dryFor, setDryFor] = useState<string>("");     // регекс, для которого считан dry-run
  const [busy, setBusy] = useState(false);
  const toast = useToast();

  const load = async () => {
    const d = await fetchRules();
    setRules(d.rules); setBrands(d.brands); setAudit(d.audit);
  };
  useEffect(() => {
    load().then(() => {
      if (seed) startNew(seed);
    }).catch(() => toast("API недоступен"));
  }, []);  // eslint-disable-line react-hooks/exhaustive-deps

  const pick = (r: Rule) => { setSel(r); setIsNew(false); setForm({ ...r }); setDry(null); setDryFor(""); };
  const startNew = (seedNum?: string) => {
    setSel(null); setIsNew(true); setDry(null); setDryFor("");
    setForm({ ...NEW, note: seedNum ? `под номер вида ${seedNum}` : "" });
  };

  const dirty = useMemo(() =>
    !sel || form.find_regex !== sel.find_regex || form.enabled !== sel.enabled || (form.note || "") !== (sel.note || ""),
    [form, sel]);
  const canSave = form.name.trim() && form.find_regex.trim() && dryFor === form.find_regex && dirty;

  const doDry = async () => {
    setBusy(true);
    try {
      setDry(await dryRunRule(form.name.trim() || "новое_правило", form.find_regex, form.enabled));
      setDryFor(form.find_regex);
    } catch (e) { toast(`${e}`); }
    setBusy(false);
  };

  const doSave = async () => {
    setBusy(true);
    try {
      const res = await saveRule(form.name.trim(), {
        find_regex: form.find_regex, enabled: form.enabled, note: form.note,
        canonical: form.canonical, audit_note: isNew ? "создано из UI" : "правка из UI",
      });
      toast(`сохранено (${res.action}) — нефинальные перепрогонятся сами`);
      await load();
      setIsNew(false);
      setSel({ ...form });
    } catch (e) { toast(`${e}`); }
    setBusy(false);
  };

  return (
    <div className={styles.rulesGrid}>
      <div>
        <button className={`${styles.btn} ${styles.btnPrimary}`} style={{ width: "100%", marginBottom: 12 }}
          onClick={() => startNew()}>+ новое правило</button>
        {rules.map((r) => (
          <div key={r.name}
            className={`${styles.ruleItem} ${sel?.name === r.name ? styles.ruleItemActive : ""}`}
            onClick={() => pick(r)}>
            <div className={styles.ruleName}>
              <span className={`${styles.dot} ${r.enabled ? styles.dotOn : styles.dotOff}`} />
              {r.name}
              <span className={styles.groupCount}>{r.canonical}</span>
            </div>
            <div className={styles.ruleRegex}>{r.find_regex}</div>
          </div>
        ))}
      </div>

      <div>
        {(sel || isNew) ? (
          <>
            <div className={styles.panel}>
              <h3 className={styles.panelTitle}>{isNew ? "Новое правило" : sel!.name}</h3>
              {isNew && (
                <div className={styles.formRow}>
                  <label className={styles.formLabel}>имя (kebab-case)</label>
                  <input className={styles.editInput} style={{ width: "100%" }}
                    value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
                </div>
              )}
              {isNew && (
                <div className={styles.formRow}>
                  <label className={styles.formLabel}>бренд (canonical)</label>
                  <select className={styles.select} value={form.canonical}
                    onChange={(e) => setForm({ ...form, canonical: e.target.value })}>
                    {brands.map((b) => <option key={b}>{b}</option>)}
                  </select>
                </div>
              )}
              <div className={styles.formRow}>
                <label className={styles.formLabel}>find_regex (кандидат = склейка capture-групп)</label>
                <textarea className={`${styles.textarea} ${styles.mono}`} style={{ minHeight: 64 }}
                  value={form.find_regex}
                  onChange={(e) => { setForm({ ...form, find_regex: e.target.value }); }} />
              </div>
              <div className={styles.formRow}>
                <label className={styles.formLabel}>заметка</label>
                <input className={styles.editInput} style={{ width: "100%", fontFamily: "var(--font-sans)" }}
                  value={form.note || ""} onChange={(e) => setForm({ ...form, note: e.target.value })} />
              </div>
              <label style={{ display: "flex", gap: 8, alignItems: "center", fontSize: 14 }}>
                <input type="checkbox" checked={form.enabled}
                  onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />
                enabled
              </label>
              <div className={styles.actions} style={{ marginTop: 14 }}>
                <button className={styles.btn} disabled={busy || !form.find_regex.trim()} onClick={doDry}>
                  {busy ? <span className={styles.spin} /> : "Dry-run"}
                </button>
                <button className={`${styles.btn} ${styles.btnPrimary}`} disabled={!canSave || busy} onClick={doSave}>
                  Сохранить
                </button>
                {!canSave && dirty && form.find_regex.trim() !== "" && dryFor !== form.find_regex && (
                  <span className={styles.comment} style={{ alignSelf: "center" }}>сначала dry-run — правило общее для проектов</span>
                )}
              </div>
            </div>

            {dry && (
              <div className={styles.panel}>
                <h3 className={styles.panelTitle}>Dry-run</h3>
                <div className={styles.diffCol}>
                  <div className={styles.diffPlus}>+ кандидаты по текстам: {dry.texts_gained.length}</div>
                  {dry.texts_gained.slice(0, 12).map((g, i) => (
                    <div key={i} style={{ paddingLeft: 14 }}>
                      <Link href={`/truth/listing/${g.item_number}`} className={styles.mono} style={{ textDecoration: "underline" }}>{g.item_number}</Link>
                      {" "}<span className={styles.mono}>{g.candidate}</span>
                      {" "}{g.part_id ? <span className={styles.ok}>→ {g.part_id}</span> : <span className={styles.groupCount}>не в каталоге</span>}
                    </div>
                  ))}
                  <div className={styles.diffMinus} style={{ marginTop: 8 }}>− потеряны по текстам: {dry.texts_lost.length}</div>
                  {dry.texts_lost.slice(0, 12).map((g, i) => (
                    <div key={i} style={{ paddingLeft: 14 }}>
                      <span className={styles.mono}>{g.item_number} {g.candidate}</span>
                    </div>
                  ))}
                  <div className={styles.diffPlus} style={{ marginTop: 8 }}>+ гейт: прочитанное агентом теперь проходит: {dry.gate_now_passing.length}</div>
                  {dry.gate_now_passing.slice(0, 12).map((g, i) => (
                    <div key={i} style={{ paddingLeft: 14 }}>
                      <Link href={`/truth/listing/${g.item_number}`} className={styles.mono} style={{ textDecoration: "underline" }}>{g.item_number}</Link>
                      {" "}<span className={styles.mono}>«{g.text}»</span>
                    </div>
                  ))}
                  <div className={styles.diffMinus} style={{ marginTop: 8 }}>− гейт: перестанет проходить: {dry.gate_now_failing.length}</div>
                  <div style={{ marginTop: 10 }}>
                    перепрогон затронет нефинальных: <b>{dry.affected_nonfinal.length}</b>
                    {dry.affected_nonfinal.slice(0, 8).map((n) => (
                      <Link key={n} href={`/truth/listing/${n}`} className={styles.mono}
                        style={{ marginLeft: 8, textDecoration: "underline" }}>{n}</Link>
                    ))}
                  </div>
                </div>
              </div>
            )}

            <div className={styles.panel}>
              <h3 className={styles.panelTitle}>Аудит правок</h3>
              {audit.filter((a) => !sel || a.rule_name === sel.name).slice(0, 12).map((a, i) => (
                <div key={i} className={styles.nearRow}>
                  <span className={styles.mono}>{a.rule_name}</span>
                  <span className={`${styles.pill} ${styles.pillGray}`}>{a.action}</span>
                  <span>{a.note || ""}</span>
                  <span className={styles.groupCount}>{new Date(a.created_at).toLocaleString("ru")}</span>
                </div>
              ))}
              {audit.length === 0 && <div className={styles.comment}>правок из UI ещё не было</div>}
            </div>
          </>
        ) : (
          <div className={styles.zeroState}>выбери правило слева или создай новое</div>
        )}
      </div>
    </div>
  );
}

export default function RulesPage() {
  return (
    <ToastProvider>
      <Suspense fallback={<div />}>
        <Inner />
      </Suspense>
    </ToastProvider>
  );
}
