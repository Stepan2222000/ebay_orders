export type OcrStatus = "pending" | "running" | "done" | "failed";
export type AgentStatus = "pending" | "running" | "done" | "failed";

export interface Stats {
  pending: number;
  running: number;
  done: number;
  failed: number;
  /** скриншоты с ocr_status=done и agent_status в работе/ожидании (есть, что обработать) */
  assembling: number;
  /** общее число снимков, попавших в стадию B хотя бы раз */
  agent_total: number;
  /** из них собрано в заказы */
  agent_done: number;
  /** из них помечено failed */
  agent_failed: number;
  /** листинги, ожидающие ревью привязки (бандлы / без кандидата) */
  match_needs_review?: number;
  /** артикул распознан, но детали нет в каталоге smart */
  match_not_in_catalog?: number;
  /** ещё не сматчено (обычно 0) */
  match_pending?: number;
  /** в титуле нет артикула (ручной статус) */
  match_no_article?: number;
}

export interface Screenshot {
  sha: string;
  ocr_status: OcrStatus;
  agent_status: AgentStatus;
  last_error: string | null;
  byte_size: number;
  mime_type: string;
  created_at: string;
  order_number: string | null;
  /** сниппет вокруг совпадения. Возвращается только когда передан ?q= */
  match?: string | null;
}

export interface ScreenshotDetail extends Screenshot {
  raw_json: any | null;
  ocr_model: string | null;
  ocr_at: string | null;
}

export interface UploadResult {
  screenshots: { sha256: string; status: "queued" | "duplicate" }[];
}
