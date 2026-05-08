const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export async function apiFetch<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...options?.headers },
    ...options,
  });
  if (!res.ok) throw new Error(`API ${res.status}: ${await res.text()}`);
  return res.json();
}

// Upload files and get classification.
//
// As of Apr-30 the response can also carry `match_candidates` — pre-existing
// projects that share carrier/account with the just-uploaded files. The
// upload page renders a banner when this list is non-empty, offering the
// analyst the choice to append to the existing project instead of creating
// a duplicate. Field is always returned (empty list when no match), so
// callers can read it unconditionally.
export async function apiClassify(
  files: File[],
  projectName: string,
  clientName: string,
  description: string,
): Promise<{
  upload_id: string;
  files: ClassifiedFileResponse[];
  match_candidates?: MatchCandidate[];
}> {
  const form = new FormData();
  files.forEach((f) => form.append("files", f));
  form.append("project_name", projectName);
  form.append("client_name", clientName);
  form.append("description", description);

  const res = await fetch(`${API_BASE}/api/uploads/classify`, {
    method: "POST",
    body: form,
  });
  if (!res.ok) throw new Error(`Classify failed: ${res.status}`);
  return res.json();
}

// Append new files to an existing upload — iterative inventory updates.
// The backend classifies them inline, returns the classification list, then
// runs extraction in the background and merges new rows by account/phone/
// circuit keys. Frontend should poll status on the target upload_id after
// this call (same poll loop as /extract).
export async function apiAppend(
  targetUploadId: string,
  files: File[],
): Promise<{
  upload_id: string;
  status: string;
  appended: ClassifiedFileResponse[];
}> {
  const form = new FormData();
  files.forEach((f) => form.append("files", f));
  const res = await fetch(`${API_BASE}/api/uploads/${targetUploadId}/append`, {
    method: "POST",
    body: form,
  });
  if (!res.ok) throw new Error(`Append failed: ${res.status} ${await res.text()}`);
  return res.json();
}

// Start extraction with user's carrier assignments
export async function apiExtract(
  uploadId: string,
  files: { filename: string; carrier: string; doc_type?: string }[],
): Promise<{ upload_id: string; status: string }> {
  return apiFetch("/api/uploads/extract", {
    method: "POST",
    body: JSON.stringify({ upload_id: uploadId, files }),
  });
}

// Poll extraction status
export async function apiGetStatus(uploadId: string): Promise<{
  upload_id: string;
  status: string;
  total_rows: number;
  files_processed: number;
  files_total: number;
}> {
  return apiFetch(`/api/uploads/${uploadId}/status`);
}

// Get extraction results
export async function apiGetResults(uploadId: string): Promise<{
  upload_id: string;
  project_name: string;
  status: string;
  total_rows: number;
  rows: ExtractedRowAPI[];
}> {
  return apiFetch(`/api/uploads/${uploadId}/results`);
}

// List all uploads (for restoring state after page refresh)
export interface UploadSummary {
  upload_id: string;
  project_name: string;
  client_name: string;
  status: string;
  total_rows: number;
  files_total: number;
  files_processed: number;
  created_at: string;
  deleted_at?: string | null;
  classified: ClassifiedFileResponse[];
  // Computed stats (present after extraction completes)
  rows_with_issues?: number;
  rows_error_level?: number;
  unique_accounts?: number;
  rows_needing_carrier_validation?: number;
  carriers?: string[]; // Canonical names when registered, else raw LLM-detected string
  // Per-file failures during extraction (oversized PDF, parse failure, etc).
  // When non-empty, the upload card renders an amber banner so silent
  // 0-row results stop being silent.
  extraction_errors?: { filename: string; carrier?: string; reason: string }[];
}

export async function apiListUploads(): Promise<{ uploads: UploadSummary[] }> {
  return apiFetch("/api/uploads");
}

// Soft-delete an upload (moves to bin, reversible)
export async function apiDeleteUpload(uploadId: string): Promise<{ upload_id: string; deleted: boolean }> {
  return apiFetch(`/api/uploads/${uploadId}`, { method: "DELETE" });
}

// List soft-deleted uploads (the bin)
export async function apiListBin(): Promise<{ uploads: UploadSummary[] }> {
  return apiFetch("/api/uploads/bin");
}

// Restore a soft-deleted upload from the bin
export async function apiRestoreUpload(uploadId: string): Promise<{ upload_id: string; restored: boolean }> {
  return apiFetch(`/api/uploads/${uploadId}/restore`, { method: "POST" });
}

// Permanently purge an upload (irreversible)
export async function apiPurgeUpload(uploadId: string): Promise<{ upload_id: string; purged: boolean }> {
  return apiFetch(`/api/uploads/${uploadId}/purge`, { method: "POST" });
}

// LLM spend tracking (cumulative, against the configured cap)
export interface SpendStatus {
  total_usd: number;
  cap_usd: number;
  remaining_usd: number | null;
  pct_used: number;
  warn_at_pct: number;
  by_backend?: Record<string, number>;  // 'vertex' | 'aistudio' | 'anthropic' → $
  routing_mode?: string;                // 'auto' | 'vertex' | 'aistudio'
}

export async function apiGetSpend(): Promise<SpendStatus> {
  return apiFetch("/api/spend");
}

// Clean up orphaned temp folders
export async function apiCleanupOrphaned(): Promise<{ cleaned: number }> {
  return apiFetch("/api/uploads/cleanup", { method: "POST" });
}

// Cancel an in-progress extraction
export async function apiCancelExtraction(uploadId: string): Promise<{ upload_id: string; status: string }> {
  return apiFetch(`/api/uploads/${uploadId}/cancel`, { method: "POST" });
}

// Retry/re-run extraction. Works for failed/interrupted/cancelled AND completed projects
// (re-extracts with current prompts/config — useful after prompt updates).
export async function apiRetryExtraction(uploadId: string): Promise<{ upload_id: string; status: string }> {
  return apiFetch(`/api/uploads/${uploadId}/retry`, { method: "POST" });
}

// Download all uploaded source files for a project as a single ZIP.
export async function apiDownloadFiles(uploadId: string): Promise<Blob> {
  const res = await fetch(`${API_BASE}/api/uploads/${uploadId}/download`);
  if (!res.ok) throw new Error(`Download failed: ${res.status}`);
  return res.blob();
}

// Cross-doc merge
export async function apiMerge(uploadId: string): Promise<{ upload_id: string; status: string }> {
  return apiFetch(`/api/uploads/${uploadId}/merge`, { method: "POST" });
}

// Get results with optional view (raw = pre-merge) or version (frozen snapshot)
export async function apiGetResultsWithView(
  uploadId: string,
  view?: "raw" | "merged",
  version?: number,
): Promise<{
  upload_id: string;
  project_name: string;
  status: string;
  total_rows: number;
  rows: ExtractedRowAPI[];
  view: string;
  has_merged: boolean;
  version?: { number: number; source: string; rows_count: number; note?: string; created_at?: string; has_file?: boolean };
}> {
  const params = new URLSearchParams();
  if (view) params.set("view", view);
  if (version !== undefined) params.set("version", String(version));
  const query = params.toString() ? `?${params.toString()}` : "";
  return apiFetch(`/api/uploads/${uploadId}/results${query}`);
}

// List all saved versions (snapshots) of an upload's inventory
export interface InventoryVersion {
  id: string;
  upload_id: string;
  version_number: number;
  source: "extraction" | "import" | string;
  rows_count: number;
  file_hash: string | null;
  has_file: boolean;
  note: string | null;
  created_at: string | null;
  created_by: string | null;
}
export async function apiListVersions(uploadId: string): Promise<{ upload_id: string; versions: InventoryVersion[] }> {
  return apiFetch(`/api/uploads/${uploadId}/versions`);
}

// List configured carriers
export interface CarrierInfo {
  key: string;
  name: string;
  format_count: number;
}

export async function apiListCarriers(): Promise<{ carriers: CarrierInfo[] }> {
  return apiFetch("/api/carriers");
}

// ============================================
// Clients API (§2.1 per-client master-data)
// ============================================
export interface ClientSummary {
  id: string;
  name: string;
  notes: string | null;
  project_count: number;
  reference_data_count: number;
}

export interface ClientReferenceData {
  id: string;
  kind: string;
  carrier: string | null;
  account_number: string | null;
  key_fields: Record<string, unknown>;
  values: Record<string, unknown>;
  source: string | null;
  confirmed_by: string | null;
  confirmed_at: string | null;
}

export interface ClientDetail {
  id: string;
  name: string;
  notes: string | null;
  reference_data: ClientReferenceData[];
}

export async function apiListClients(): Promise<{ clients: ClientSummary[] }> {
  return apiFetch("/api/clients");
}

export async function apiGetClient(clientId: string): Promise<ClientDetail> {
  return apiFetch(`/api/clients/${clientId}`);
}

// Dashboard stats
export interface DashboardStats {
  extraction_runs: {
    total: number;
    total_documents: number;
    total_rows: number;
    total_cost_usd: number;
  };
  rows: {
    total: number;
    total_mrc: number;
    reviewed: number;
  };
  review_status: Record<string, number>;
  confidence: Record<string, number>;
  carriers: { carrier: string; row_count: number; mrc: number }[];
  corrections: number;
  recent_runs: {
    id: string;
    upload_id: string | null;
    status: string;
    documents_processed: number;
    rows_extracted: number;
    estimated_cost_usd: number;
    started_at: string | null;
    completed_at: string | null;
  }[];
}

export async function apiGetDashboardStats(): Promise<DashboardStats> {
  return apiFetch("/api/dashboard/stats");
}

// Live operational state (Redis + spend + carriers) — works even with zero persisted data
export interface DashboardLive {
  active: {
    count: number;
    files_in_flight: number;
    oldest_age_seconds: number;
  };
  completed_count: number;
  failed_count: number;
  bin_count: number;
  spend: {
    total_usd: number;
    cap_usd: number;
    pct_used: number;
    status: "ok" | "warn" | "danger";
  };
  carriers: { key: string; name: string; format_count: number }[];
}

export async function apiGetDashboardLive(): Promise<DashboardLive> {
  return apiFetch("/api/dashboard/live");
}

// Analytics stats
export interface AnalyticsStats {
  total_rows: number;
  field_fill_rates: {
    field: string;
    category: string;
    filled: number;
    total: number;
    fill_rate: number;
  }[];
  category_fill_rates: Record<string, { avg_fill_rate: number; field_count: number }>;
  top_corrected_fields: { field: string; corrections: number }[];
  corrections_by_carrier: { carrier: string; corrections: number }[];
}

export async function apiGetAnalyticsStats(): Promise<AnalyticsStats> {
  return apiFetch("/api/analytics/stats");
}

// ============================================
// Review API
// ============================================

export async function apiSubmitCorrection(
  rowId: string,
  fieldName: string,
  extractedValue: string | null,
  correctedValue: string,
): Promise<{ row_id: string; correction_id: string; field_name: string; status: string }> {
  return apiFetch(`/api/review/rows/${rowId}`, {
    method: "PATCH",
    body: JSON.stringify({
      field_name: fieldName,
      extracted_value: extractedValue,
      corrected_value: correctedValue,
    }),
  });
}

export async function apiBulkApprove(
  uploadId: string,
  rowIds: string[],
): Promise<{ upload_id: string; approved: number }> {
  return apiFetch(`/api/review/${uploadId}/bulk-approve`, {
    method: "POST",
    body: JSON.stringify({ row_ids: rowIds }),
  });
}

// ============================================
// Export / Import API
// ============================================

export async function apiExportExcel(uploadId: string): Promise<Blob> {
  const res = await fetch(`${API_BASE}/api/exports/${uploadId}/excel`);
  if (!res.ok) throw new Error(`Export failed: ${res.status}`);
  return res.blob();
}

export async function apiImportCorrections(
  uploadId: string,
  file: File,
): Promise<{ upload_id: string; rows_compared: number; corrections_created: number }> {
  const form = new FormData();
  form.append("file", file);

  const res = await fetch(`${API_BASE}/api/exports/corrections/import?upload_id=${uploadId}`, {
    method: "POST",
    body: form,
  });
  if (!res.ok) throw new Error(`Import failed: ${res.status}`);
  return res.json();
}

// ============================================
// Chat + Patterns (Apr-30) — the "brain" surface
// ============================================
//
// Chat is SSE-streamed from POST /api/chat/stream. The endpoint emits three
// event types: `meta` (one-time, before first token, with row counts), `data`
// (token chunks, default event name), and `done` (final usage). We expose
// the stream as a callback API so callers can update React state incrementally
// without juggling raw EventSource semantics.

export type ChatRole = "user" | "assistant";
export interface ChatMessage {
  role: ChatRole;
  content: string;
}

export interface ChatMeta {
  row_count?: number;
  sampled?: number;
  project_count?: number;
  findings?: { total: number; by_kind: Record<string, number>; by_severity: Record<string, number> };
}

export interface ChatStreamCallbacks {
  onMeta?: (meta: ChatMeta) => void;
  onToken?: (text: string) => void;
  onDone?: (usage: { input_tokens: number; output_tokens: number }) => void;
  onError?: (err: string) => void;
}

/**
 * Stream a chat reply. Either project-scoped (pass projectId) or platform-wide.
 * Returns a Promise that resolves when the stream finishes (success or error).
 * The callbacks fire incrementally as events arrive — wire onToken to append
 * to the live message bubble, onDone for telemetry.
 */
export async function apiChatStream(
  args: {
    messages: ChatMessage[];
    mode: "project" | "platform";
    projectId?: string;
    clientFilter?: string;
  },
  cb: ChatStreamCallbacks = {},
): Promise<void> {
  const res = await fetch(`${API_BASE}/api/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      messages: args.messages,
      mode: args.mode,
      project_id: args.projectId,
      client_filter: args.clientFilter,
    }),
  });
  if (!res.ok || !res.body) {
    const text = await res.text();
    cb.onError?.(`HTTP ${res.status}: ${text}`);
    return;
  }

  // Parse SSE manually — `EventSource` doesn't support POST and doesn't expose
  // headers, so we own the line-buffering ourselves. Each event is separated
  // by \n\n; lines within an event are key:value pairs.
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let sep: number;
      while ((sep = buffer.indexOf("\n\n")) !== -1) {
        const rawEvent = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);
        if (!rawEvent.trim()) continue;

        let eventName = "message"; // SSE default when no `event:` line
        const dataLines: string[] = [];
        for (const line of rawEvent.split("\n")) {
          if (line.startsWith("event:")) eventName = line.slice(6).trim();
          else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
        }
        const data = dataLines.join("\n");
        if (!data) continue;

        try {
          const parsed = JSON.parse(data);
          if (eventName === "meta") cb.onMeta?.(parsed);
          else if (eventName === "done") cb.onDone?.(parsed);
          else if (eventName === "error") cb.onError?.(parsed.error || "stream error");
          else if (parsed.type === "token" && typeof parsed.text === "string") cb.onToken?.(parsed.text);
        } catch {
          // Non-JSON data — pass through as a token if we're in the default channel.
          if (eventName === "message") cb.onToken?.(data);
        }
      }
    }
  } catch (e) {
    cb.onError?.(e instanceof Error ? e.message : String(e));
  }
}

// Pattern findings — same shape backend returns. Used by the Results-page
// insights card and the dedicated /patterns dashboard.
export interface PatternFinding {
  kind:
    | "recurring_vendor"
    | "pricing_anomaly"
    | "contract_cluster"
    | "multi_carrier_account"
    | "m2m_no_contract";
  severity: "info" | "warning" | "error";
  title: string;
  detail: string;
  evidence_row_ids: string[];
  metric: Record<string, unknown>;
}

export interface PatternsResponse {
  project_id?: string;
  project_name?: string;
  project_count?: number;
  row_count: number;
  findings: PatternFinding[];
  summary: { total: number; by_kind: Record<string, number>; by_severity: Record<string, number> };
  client_filter?: string | null;
}

export async function apiPatternsForProject(projectId: string): Promise<PatternsResponse> {
  return apiFetch(`/api/chat/patterns/${projectId}`);
}

export async function apiPatternsPlatform(client?: string): Promise<PatternsResponse> {
  const q = client ? `?client=${encodeURIComponent(client)}` : "";
  return apiFetch(`/api/chat/patterns${q}`);
}


// ============================================
// Types
// ============================================

export interface ClassifiedFileResponse {
  filename: string;
  carrier: string | null;
  doc_type: string | null;
  format_variant: string | null;
  file_size: number;
  // Apr-30 — surfaced from backend so we can show the detected account
  // number alongside the carrier in the review screen, and so the auto-
  // match banner can explain *why* a candidate matched.
  account_number?: string | null;
}

// A pre-existing project that looks like the same one being uploaded.
// Surfaced by the backend on the /classify response when (carrier,account)
// overlap is high enough; the upload page renders a banner if any candidate
// is present, offering "Append to existing" or "Create new project".
export interface MatchCandidate {
  upload_id: string;
  project_name: string;
  client_name: string | null;
  files_total: number;
  total_rows: number;
  created_at: string | null;
  age_days: number | null;
  score: number;
  match_reason: string;
}

export interface ExtractedRowAPI {
  id: string;
  source_file: string;
  carrier: string;
  confidence: string;
  // All 60 fields
  row_type: string | null;
  status: string | null;
  notes: string | null;
  contract_info_received: string | null;
  invoice_file_name: string | null;
  files_used: string | null;
  billing_name: string | null;
  service_address_1: string | null;
  service_address_2: string | null;
  city: string | null;
  state: string | null;
  zip: string | null;
  country: string | null;
  // Sidecar: billing address from invoice (present when it diverges from the CSR service address)
  billing_address_1?: string | null;
  billing_city?: string | null;
  billing_state?: string | null;
  billing_zip?: string | null;
  billing_name_from_invoice?: string | null;
  carrier_name: string | null;
  master_account: string | null;
  carrier_account_number: string | null;
  sub_account_number_1: string | null;
  sub_account_number_2: string | null;
  btn: string | null;
  phone_number: string | null;
  carrier_circuit_number: string | null;
  additional_circuit_ids: string | null;
  service_type: string | null;
  service_type_2: string | null;
  usoc: string | null;
  service_or_component: string | null;
  component_or_feature_name: string | null;
  monthly_recurring_cost: number | null;
  quantity: number | null;
  cost_per_unit: number | null;
  currency: string | null;
  conversion_rate: number | null;
  mrc_per_currency: number | null;
  charge_type: string | null;
  num_calls: number | null;
  ld_minutes: number | null;
  ld_cost: number | null;
  rate: number | null;
  ld_flat_rate: number | null;
  point_to_number: string | null;
  port_speed: string | null;
  access_speed: string | null;
  upload_speed: string | null;
  z_location_name: string | null;
  z_address_1: string | null;
  z_address_2: string | null;
  z_city: string | null;
  z_state: string | null;
  z_zip: string | null;
  z_country: string | null;
  contract_term_months: number | null;
  contract_begin_date: string | null;
  contract_expiration_date: string | null;
  billing_per_contract: string | null;
  currently_month_to_month: string | null;
  mtm_or_less_than_year: string | null;
  contract_file_name: string | null;
  contract_number: string | null;
  contract_number_2: string | null;
  auto_renew: string | null;
  auto_renewal_notes: string | null;
}
