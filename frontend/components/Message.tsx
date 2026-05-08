"use client";
import { Sparkles } from "lucide-react";
import ToolBlock from "./ToolBlock";
import styles from "./Message.module.css";

type AnyPart = any;

export default function Message({
  role,
  parts,
  streaming,
}: {
  role: "user" | "assistant" | "system";
  parts: AnyPart[];
  streaming?: boolean;
}) {
  if (role === "user") {
    return <UserMessage parts={parts} />;
  }
  return <AssistantMessage parts={parts} streaming={streaming} />;
}

function UserMessage({ parts }: { parts: AnyPart[] }) {
  return (
    <div className={styles.userWrap} data-role="user">
      <div className={styles.userBubble}>
        {parts.map((p, i) => {
          if (p.type === "text") return <p key={i} className={styles.userText}>{p.text}</p>;
          if (p.type === "file" && (p.url || p.data_url)) {
            const src = p.url ?? p.data_url;
            const mt = p.mediaType ?? p.mime ?? "image/png";
            if (mt.startsWith("image/")) {
              return <img key={i} src={src} alt="" className={styles.userImg} />;
            }
            return <p key={i} className={styles.userText}>[файл: {mt}]</p>;
          }
          return null;
        })}
      </div>
    </div>
  );
}

function AssistantMessage({ parts, streaming }: { parts: AnyPart[]; streaming?: boolean }) {
  const visibleText = parts.some((p) => p.type === "text" && p.text);
  return (
    <div
      className={styles.assistant}
      data-role="assistant"
      data-streaming={streaming ? "true" : "false"}
    >
      <div className={styles.byline}>
        <span className={styles.spark}>
          <Sparkles size={14} />
        </span>
        <span>агент</span>
      </div>

      {parts.map((p, i) => {
        if (p.type === "text") {
          return <p key={i} className={styles.text}>{p.text}</p>;
        }
        if (typeof p.type === "string" && p.type.startsWith("tool-")) {
          const toolName = p.type.slice("tool-".length);
          return (
            <ToolBlock
              key={p.toolCallId ?? i}
              toolName={toolName}
              state={p.state ?? "input-available"}
              input={p.input ?? p.arguments}
              output={p.output ?? p.result}
            />
          );
        }
        if (p.type === "tool" && p.name) {
          // на случай чтения старых сообщений из БД, где формат был {type:"tool", name, arguments, result}
          return (
            <ToolBlock
              key={i}
              toolName={p.name}
              state="output-available"
              input={p.arguments}
              output={p.result}
            />
          );
        }
        return null;
      })}

      {streaming && !visibleText ? (
        <div className={styles.typing} data-testid="typing">
          <span /><span /><span />
        </div>
      ) : null}
    </div>
  );
}
