export type OcrStatus = "pending" | "running" | "done" | "failed";
export type AgentStatus = "pending" | "running" | "done" | "failed";

export interface Stats {
  pending: number;
  running: number;
  done: number;
  failed: number;
  /** скриншоты с ocr_status=done и agent_status в работе/ожидании (агент сейчас собирает заказы) */
  assembling: number;
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
}

export interface ScreenshotDetail extends Screenshot {
  raw_json: any | null;
  ocr_model: string | null;
  ocr_at: string | null;
}

export interface UploadResult {
  screenshots: { sha256: string; status: "queued" | "duplicate" }[];
}
