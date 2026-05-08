"use client";
import { useEffect, useRef, useState } from "react";
import { ChevronRight, Sparkles } from "lucide-react";
import styles from "./ToolBlock.module.css";

// Время от перехода state → 'output-available'/'output-error' до автоматического
// сворачивания блока. По SPEC ^действия: блок раскрыт пока действие идёт, после —
// сворачивается, но раскрываемо. Если пользователь сам открыл/закрыл —
// уважаем его решение.
const AUTO_COLLAPSE_MS = 5000;

type ToolState =
  | "input-streaming"
  | "input-available"
  | "output-available"
  | "output-error";

const STATE_LABEL: Record<string, string> = {
  "input-streaming": "формирует запрос…",
  "input-available": "выполняется…",
  "output-available": "выполнено",
  "output-error": "ошибка",
};

const TOOL_LABEL: Record<string, string> = {
  sql: "запрос к базе",
  save_order: "сохранение заказа",
};

function pretty(value: any): string {
  if (value == null) return "—";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

export default function ToolBlock({
  toolName,
  state,
  input,
  output,
  defaultOpen = false,
}: {
  toolName: string;
  state: ToolState | string;
  input: any;
  output: any;
  defaultOpen?: boolean;
}) {
  const finished = state === "output-available" || state === "output-error";
  const errored =
    state === "output-error" ||
    (output && typeof output === "object" && "error" in output);
  const [open, setOpen] = useState(defaultOpen || !finished);
  const userToggled = useRef(false);

  // Авто-сворачивание после завершения. Если юзер уже трогал — не трогаем.
  useEffect(() => {
    if (!finished || userToggled.current) return;
    const t = setTimeout(() => {
      if (!userToggled.current) setOpen(false);
    }, AUTO_COLLAPSE_MS);
    return () => clearTimeout(t);
  }, [finished]);

  const handleToggle = () => {
    userToggled.current = true;
    setOpen((v) => !v);
  };

  const label = TOOL_LABEL[toolName] ?? toolName;
  const stateLabel = errored
    ? STATE_LABEL["output-error"]
    : STATE_LABEL[state] ?? state;

  return (
    <div
      className={`${styles.block} ${errored ? styles.error : ""}`}
      data-testid="tool-block"
      data-tool={toolName}
      data-state={state}
    >
      <button
        type="button"
        className={styles.head}
        onClick={handleToggle}
        aria-expanded={open}
      >
        <span className={`${styles.icon} ${!finished ? styles.iconLive : ""}`}>
          <Sparkles size={14} />
        </span>
        <span className={styles.name}>{label}</span>
        <span className={styles.dot}>·</span>
        <span className={styles.state}>{stateLabel}</span>
        <ChevronRight
          size={14}
          className={`${styles.chev} ${open ? styles.chevOpen : ""}`}
        />
      </button>
      {open ? (
        <div className={styles.body}>
          {input != null ? (
            <>
              <div className={styles.label}>запрос</div>
              <pre className={styles.code}>{pretty(input)}</pre>
            </>
          ) : null}
          {finished ? (
            <>
              <div className={styles.label}>{errored ? "ошибка" : "ответ"}</div>
              <pre className={`${styles.code} ${errored ? styles.codeErr : ""}`}>
                {pretty(output)}
              </pre>
            </>
          ) : (
            <div className={styles.live}>
              <span className={styles.spinner} />
              ждём ответа
            </div>
          )}
        </div>
      ) : null}
    </div>
  );
}
