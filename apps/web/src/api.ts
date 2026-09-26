/** Typed client for the Bag API. Cookies carry the session; every request adds the CSRF header. */

export interface ItemSummary {
  id: string;
  kind: string;
  source: string;
  title: string | null;
  user_note: string | null;
  mime_type: string | null;
  original_filename: string | null;
  source_url: string | null;
  language: string | null;
  tags: string[];
  collections: string[];
  processing_status: string;
  created_at: string;
  captured_at: string;
  updated_at: string;
  deleted_at: string | null;
}

export interface ItemDetail {
  id: string;
  kind: string;
  source: string;
  source_url: string | null;
  title: string | null;
  content: string | null;
  user_note: string | null;
  mime_type: string | null;
  original_filename: string | null;
  content_hash: string | null;
  extracted_text: string | null;
  language: string | null;
  tags: string[];
  collections: string[];
  processing_status: string;
  created_at: string;
  captured_at: string;
  updated_at: string;
}

export interface SearchResult extends ItemSummary {
  rank: number;
  snippet: string;
}

export interface ProcessingRun {
  processor: string;
  status: string;
  attempts: number;
  last_error: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface Session {
  owner_id: string;
  username: string | null;
  display_name: string;
  via: "token" | "session";
  expires_at: string | null;
}

export interface CaptureResult {
  id: string;
  status: "stored";
  processing_status: string;
  duplicate_of: string | null;
}

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("X-Bag-Csrf", "1");
  if (init.body !== undefined && !(init.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(path, { ...init, headers, credentials: "same-origin" });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      // Non-JSON error bodies keep the status text.
    }
    throw new ApiError(response.status, detail);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  session: () => request<Session>("/api/v1/session"),
  login: (username: string, password: string) =>
    request<Session>("/api/v1/session", { method: "POST", body: JSON.stringify({ username, password }) }),
  logout: () => request<void>("/api/v1/session", { method: "DELETE" }),
  items: (params: Record<string, string>) =>
    request<{ items: ItemSummary[]; next_cursor: string | null }>(`/api/v1/items?${new URLSearchParams(params)}`),
  search: (params: Record<string, string>) =>
    request<{ results: SearchResult[]; next_offset: number | null }>(`/api/v1/search?${new URLSearchParams(params)}`),
  item: (id: string) => request<ItemDetail>(`/api/v1/items/${encodeURIComponent(id)}`),
  processing: (id: string) => request<ProcessingRun[]>(`/api/v1/items/${encodeURIComponent(id)}/processing`),
  reprocess: (id: string) =>
    request<ProcessingRun[]>(`/api/v1/items/${encodeURIComponent(id)}/reprocess`, { method: "POST" }),
  captureText: (content: string, userNote: string | null) =>
    request<CaptureResult>("/api/v1/capture/text", {
      method: "POST",
      body: JSON.stringify({ content, user_note: userNote, source: "web", client_capture_id: crypto.randomUUID() }),
    }),
  captureUrl: (url: string, userNote: string | null) =>
    request<CaptureResult>("/api/v1/capture/url", {
      method: "POST",
      body: JSON.stringify({ url, user_note: userNote, source: "web", client_capture_id: crypto.randomUUID() }),
    }),
  captureFile: (file: File, userNote: string | null) => {
    const form = new FormData();
    form.append("file", file, file.name);
    form.append("metadata", JSON.stringify({ user_note: userNote, source: "web", client_capture_id: crypto.randomUUID() }));
    return request<CaptureResult>("/api/v1/capture/file", { method: "POST", body: form });
  },
  update: (id: string, patch: Partial<Pick<ItemDetail, "title" | "user_note" | "language" | "tags" | "collections">>) =>
    request<ItemDetail>(`/api/v1/items/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify(patch) }),
  trash: (id: string) => request<void>(`/api/v1/items/${encodeURIComponent(id)}`, { method: "DELETE" }),
  restore: (id: string) => request<ItemDetail>(`/api/v1/items/${encodeURIComponent(id)}/restore`, { method: "POST" }),
};

export const contentUrl = (id: string): string => `/api/v1/items/${encodeURIComponent(id)}/content`;

/** Absolute http(s) URL on a single line: capture it as a link instead of text. */
export const looksLikeUrl = (text: string): boolean =>
  /^https?:\/\/\S+$/i.test(text.trim()) && !text.trim().includes("\n");
