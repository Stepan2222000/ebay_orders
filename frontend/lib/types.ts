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
  /** листинги без артикула в каталоге, ждут пополнения каталога */
  match_not_in_catalog: number;
  /** листинги, требующие ручного разбора (бандл / нет кандидата) */
  match_needs_review: number;
  /** листинги, ещё не прогнанные матчером (матчинг был отложен) */
  match_pending: number;
  /** листинги, помеченные «нет артикула» вручную */
  match_no_article: number;
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
