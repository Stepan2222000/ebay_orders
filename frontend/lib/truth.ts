/** API раздела «Истина» (article_truth/SPEC.md §9). */

const BASE = process.env.NEXT_PUBLIC_API_BASE || "";
export const API = `${BASE}/api`;

async function j<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API}${path}`, {
    ...init,
    headers: init?.body ? { "Content-Type": "application/json" } : undefined,
  });
  if (!res.ok) {
    let detail = "";
    try { detail = JSON.stringify((await res.json()).detail); } catch { /* noop */ }
    throw new Error(detail || `${res.status} ${res.statusText}`);
  }
  return (await res.json()) as T;
}

export interface QueueRow {
  item_number: string;
  item_title: string;
  match_status?: string;
  match_note?: string | null;
  photo?: string | null;
  age_s?: number | null;
  card_id?: number | null;
  card_payload?: Record<string, unknown> | null;
  contradictions?: string[] | null;
  qty_note?: string | null;
  missing?: string[];
  last_error?: string | null;
  catalog_url?: string | null;
  payload?: Record<string, unknown> | null;
}

export interface Queue {
  counts: Record<string, number>;
  linked: number;
  total_items: number;
  conflicts: QueueRow[];
  not_in_catalog: QueueRow[];
  need_texts: QueueRow[];
  title_cards: QueueRow[];
  refunds: { card_id: number; order_id: number; order_number: string; payload: Record<string, unknown>; age_s: number }[];
}

export interface CompositionRow {
  part_id: string;
  matched_article: string;
  quantity: number;
  match_method: string;
  part_name: string | null;
}

export interface RunPosition {
  article_read: string;
  canonical: string | null;
  part_id: string | null;
  part_name?: string | null;
  qty: number;
  sources: string[];
  note: string;
  gate: string;
}

export interface AgentRun {
  id: number;
  status: string;
  dry_run: boolean;
  verdict: string | null;
  positions: RunPosition[] | null;
  near_articles: { text: string; where: string; why: string }[] | null;
  contradictions: string[] | null;
  qty_note: string | null;
  comment: string | null;
  lot_kind: string | null;
  model: string | null;
  error: string | null;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  created_at: string;
  finished_at: string | null;
  no_llm?: boolean;
}

export interface Card {
  id: number;
  kind: string;
  payload: Record<string, unknown>;
  status: string;
  resolution: string | null;
  created_at: string;
  resolved_at: string | null;
}

export interface Listing {
  item: { item_number: string; item_title: string; match_status: string; match_note: string | null; matched_at: string | null };
  snapshot: Record<string, unknown> | null;
  photos: { id: number; source: string; url: string }[];
  composition: CompositionRow[];
  orders: {
    order_id: number; order_number: string; item_quantity: number;
    delivery_status: string | null; delivered_date: string | null;
    cancelled_at: string | null; cancel_note: string | null; user_note: string | null;
    order_total_usd: number; refunded_usd: number; full_refund: boolean;
    last_refund_date: string | null;
  }[];
  runs: AgentRun[];
  cards: Card[];
  agent_running: boolean;
}

export interface ResolvedLine {
  article: string;
  qty: number;
  canonical: string | null;
  part_id: string | null;
  part_name: string | null;
  gate: string;
}

export interface NumberRow {
  normalized: string;
  count: number;
  listings: string[];
  why: string;
  other_part_id?: string;
  reason?: string | null;
}

export interface Numbers {
  candidates: NumberRow[];
  catalog_conflicts: NumberRow[];
  ignored: NumberRow[];
  hidden_known_crosses: number;
  hidden_trash: number;
}

export interface ExampleRow {
  id: number;
  item_number: string;
  item_title: string;
  kind: string;
  human_lines: ResolvedLine[];
  agent_snapshot: { verdict?: string; positions?: RunPosition[] } | null;
  note: string | null;
  created_at: string;
}

export interface Rule {
  name: string;
  canonical: string;
  find_regex: string;
  note: string | null;
  enabled: boolean;
  example_from: string | null;
  example_to: string | null;
}

export interface DryRun {
  texts_gained: { item_number: string; candidate: string; part_id: string | null }[];
  texts_lost: { item_number: string; candidate: string; part_id: string | null }[];
  gate_now_passing: { item_number: string; text: string }[];
  gate_now_failing: { item_number: string; text: string }[];
  affected_nonfinal: string[];
}

export interface ListingRow {
  item_number: string;
  item_title: string;
  match_status: string;
  match_note: string | null;
  composition: string | null;
  methods: string[] | null;
  photo?: string | null;
  age_s?: number | null;
}

export const fetchQueue = () => j<Queue>("/truth/queue");
export const fetchAllListings = () => j<{ listings: ListingRow[] }>("/truth/listings");
export const fetchBadge = () => j<{ open: number }>("/truth/badge");
export const fetchListing = (n: string) => j<Listing>(`/truth/listing/${n}`);
export const rerunListing = (n: string) =>
  j<{ started: boolean }>(`/truth/listing/${n}/rerun`, { method: "POST", body: "{}" });
export const recheckCatalog = (n: string) =>
  j<{ applicable: boolean; reason?: string; verdict?: string; was?: string; missing?: string[] }>(
    `/truth/listing/${n}/recheck-catalog`, { method: "POST", body: "{}" });
export const resolvePreview = (lines: { article: string; qty: number }[]) =>
  j<{ lines: ResolvedLine[] }>("/truth/resolve", { method: "POST", body: JSON.stringify({ lines }) });
export const saveComposition = (n: string, lines: { article: string; qty: number }[], note?: string) =>
  j<{ composition: ResolvedLine[]; example_kind: string | null }>(`/truth/listing/${n}/composition`, {
    method: "PUT", body: JSON.stringify({ lines, note }),
  });
export const fetchExamples = () => j<{ examples: ExampleRow[] }>("/truth/examples");
export const deleteExample = (id: number) =>
  j<{ deleted: number }>(`/truth/examples/${id}`, { method: "DELETE" });
export const resolveCard = (id: number, resolution?: string, titleCanon?: "ocr" | "pdp") =>
  j<{ resolved: number }>(`/truth/cards/${id}/resolve`, {
    method: "POST", body: JSON.stringify({ resolution, title_canon: titleCanon }),
  });
export const fetchNumbers = () => j<Numbers>("/truth/numbers");
export const ignoreNumber = (normalized: string, reason?: string) =>
  j<{ ignored: string }>("/truth/numbers/ignore", {
    method: "POST", body: JSON.stringify({ normalized, reason }),
  });
export const unignoreNumber = (normalized: string) =>
  j<{ unignored: string }>(`/truth/numbers/ignore/${encodeURIComponent(normalized)}`, { method: "DELETE" });
export const fetchRules = () => j<{ rules: Rule[]; audit: { rule_name: string; action: string; note: string | null; created_at: string }[]; brands: string[] }>("/truth/rules");
export const dryRunRule = (name: string, find_regex: string, enabled: boolean) =>
  j<DryRun>("/truth/rules/dry-run", { method: "POST", body: JSON.stringify({ name, find_regex, enabled }) });
export const saveRule = (name: string, body: Partial<Rule> & { audit_note?: string }) =>
  j<{ saved: string; action: string }>(`/truth/rules/${encodeURIComponent(name)}`, {
    method: "PUT", body: JSON.stringify(body),
  });
export const markOrderCancelled = (id: number, note?: string) =>
  j<{ cancelled: string }>(`/truth/orders/${id}/cancel`, {
    method: "POST", body: JSON.stringify({ note }),
  });
export const unmarkOrderCancelled = (id: number) =>
  j<{ uncancelled: string }>(`/truth/orders/${id}/cancel`, { method: "DELETE" });
export const saveOrderNote = (id: number, note: string) =>
  j<{ saved: string }>(`/truth/orders/${id}/note`, {
    method: "PUT", body: JSON.stringify({ note }),
  });
export const refetchSnapshot = (n: string) =>
  j<Record<string, unknown>>(`/listings/${n}/snapshot/refetch`, { method: "POST", body: "{}" });
export const putSnapshotTexts = (n: string, specifics_raw: string, description: string) =>
  j<Record<string, unknown>>(`/listings/${n}/snapshot`, {
    method: "PUT", body: JSON.stringify({ specifics_raw, description }),
  });

export const photoSrc = (url: string) => `${BASE}${url}`;
export const ageText = (s?: number | null) => {
  if (s == null) return "";
  const d = Math.floor(s / 86400);
  if (d > 0) return `${d} дн`;
  const h = Math.floor(s / 3600);
  return h > 0 ? `${h} ч` : "только что";
};

export const STATUS_LABEL: Record<string, string> = {
  linked: "истина полная",
  not_in_catalog: "нет в каталоге",
  no_article: "артикул не найден",
  conflict: "конфликт",
  needs_review: "ждёт агента",
  pending: "ждёт обработки",
};
