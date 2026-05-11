import type { Screenshot, ScreenshotDetail, Stats, UploadResult } from "./types";

const BASE = process.env.NEXT_PUBLIC_API_BASE || "";
export const API = `${BASE}/api`;

async function jsonFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API}${path}`, init);
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText}: ${text || path}`);
  }
  return (await res.json()) as T;
}

export async function fetchStats(): Promise<Stats> {
  return jsonFetch<Stats>("/status");
}

export async function fetchScreenshots(q?: string): Promise<Screenshot[]> {
  const path = q && q.trim()
    ? `/screenshots?q=${encodeURIComponent(q.trim())}`
    : "/screenshots";
  const data = await jsonFetch<{ screenshots: Screenshot[] }>(path);
  return data.screenshots;
}

export async function fetchScreenshotDetail(sha: string): Promise<ScreenshotDetail> {
  return jsonFetch<ScreenshotDetail>(`/screenshots/${sha}`);
}

export async function uploadFiles(files: File[]): Promise<UploadResult> {
  const fd = new FormData();
  for (const f of files) fd.append("files", f);
  return jsonFetch<UploadResult>("/screenshots", { method: "POST", body: fd });
}

export async function deleteScreenshot(sha: string): Promise<void> {
  await jsonFetch(`/screenshots/${sha}`, { method: "DELETE" });
}

export async function resetChat(): Promise<{ deleted: number }> {
  return jsonFetch<{ deleted: number }>("/chat/reset", { method: "POST" });
}

export function imageUrl(sha: string): string {
  return `${API}/screenshots/${sha}/image`;
}
