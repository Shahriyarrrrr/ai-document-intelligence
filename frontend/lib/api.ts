/**
 * Typed API client.
 *
 * One module so the API surface is visible in a single place, and so a
 * backend field rename produces a TypeScript error rather than an
 * `undefined` rendered silently as blank in the review UI.
 */

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000";

export interface LineItem {
  description: string | null;
  quantity: number | null;
  unit_price: number | null;
  amount: number | null;
}

export interface Issue {
  id: string;
  code: string;
  severity: "info" | "warning" | "error";
  field: string | null;
  message: string;
  context: Record<string, unknown>;
}

export interface Correction {
  id: string;
  field: string;
  old_value: string | null;
  new_value: string | null;
  corrected_by: string;
  at: string;
}

export interface Invoice {
  id: string;
  document_id: string;
  invoice_number: string | null;
  invoice_date: string | null;
  supplier_name: string | null;
  customer_name: string | null;
  currency: string | null;
  subtotal: number | null;
  tax: number | null;
  total: number | null;
  line_items: LineItem[];
  field_confidence: Record<string, number>;
  extraction_method: string;
  needs_review: boolean;
  issues: Issue[];
  corrections: Correction[];
}

export interface DocumentSummary {
  id: string;
  filename: string;
  status: string;
  source_kind: string;
  page_count: number;
  ocr_page_count: number;
  byte_size: number;
  invoice_count: number;
  issue_count: number;
  needs_review: boolean;
  error_message: string | null;
  duration_s: number | null;
  created_at: string;
}

export interface ProcessingEvent {
  stage: string;
  status: string;
  duration_ms: number | null;
  detail: Record<string, unknown>;
  at: string;
}

export interface DocumentDetail extends DocumentSummary {
  metrics: Record<string, unknown>;
  invoices: Invoice[];
  events: ProcessingEvent[];
}

export interface StatusCounts {
  pending: number;
  processing: number;
  completed: number;
  needs_review: number;
  failed: number;
}

export interface SearchHit {
  document_id: string;
  filename: string;
  page_number: number | null;
  snippet: string | null;
  score: number;
  invoice_number: string | null;
  supplier_name: string | null;
  total: number | null;
}

export interface SearchResponse {
  mode: string;
  query: string;
  total: number;
  hits: SearchHit[];
  semantic_available: boolean;
}

export interface CrossCheckResponse {
  id: string;
  counts: Record<string, number>;
  rows: Array<{
    key: string;
    status: string;
    matched_by: string;
    left_source: string | null;
    right_source: string | null;
    differences: Array<{
      field: string;
      left: unknown;
      right: unknown;
      note: string | null;
    }>;
  }>;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      ...(init?.body && !(init.body instanceof FormData)
        ? { "Content-Type": "application/json" }
        : {}),
      ...init?.headers,
    },
    cache: "no-store",
  });
  if (!response.ok) {
    // Surface FastAPI's `detail` rather than a bare status code: "field
    // 'foo' is not correctable" is actionable, "400" is not.
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body?.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* response had no JSON body */
    }
    throw new Error(detail);
  }
  return response.json() as Promise<T>;
}

export const api = {
  upload(files: File[]) {
    const form = new FormData();
    files.forEach((file) => form.append("files", file));
    return request<{
      accepted: Array<{
        document_id: string;
        filename: string;
        status: string;
        duplicate_of: string | null;
      }>;
      rejected: Array<{ filename: string; reason: string }>;
    }>("/api/documents/upload", { method: "POST", body: form });
  },

  listDocuments(params: { status?: string; needs_review?: boolean } = {}) {
    const query = new URLSearchParams();
    if (params.status) query.set("status", params.status);
    if (params.needs_review !== undefined)
      query.set("needs_review", String(params.needs_review));
    const suffix = query.toString() ? `?${query}` : "";
    return request<DocumentSummary[]>(`/api/documents${suffix}`);
  },

  statusCounts() {
    return request<StatusCounts>("/api/documents/status");
  },

  getDocument(id: string) {
    return request<DocumentDetail>(`/api/documents/${id}`);
  },

  reprocess(id: string) {
    return request<DocumentSummary>(`/api/documents/${id}/reprocess`, {
      method: "POST",
    });
  },

  reprocessAllFailed() {
    return request<string[]>("/api/documents/reprocess-failed", {
      method: "POST",
    });
  },

  correct(
    invoiceId: string,
    changes: Array<{ field: string; value: unknown; corrected_by?: string }>,
  ) {
    return request<{ applied: string[]; invoice: Invoice }>(
      `/api/documents/invoices/${invoiceId}`,
      { method: "PATCH", body: JSON.stringify(changes) },
    );
  },

  search(q: string, mode: "keyword" | "semantic" | "hybrid" = "hybrid") {
    return request<SearchResponse>(
      `/api/search?q=${encodeURIComponent(q)}&mode=${mode}`,
    );
  },

  crossCheck(left: string[], right: string[]) {
    return request<CrossCheckResponse>("/api/search/crosscheck", {
      method: "POST",
      body: JSON.stringify({
        left_document_ids: left,
        right_document_ids: right,
      }),
    });
  },
};
