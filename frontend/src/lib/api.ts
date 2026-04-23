const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export async function apiFetch<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...options?.headers },
    ...options,
  });
  if (!res.ok) throw new Error(`API ${res.status}: ${await res.text()}`);
  return res.json();
}

// Upload files and get classification
export async function apiClassify(
  files: File[],
  projectName: string,
  clientName: string,
  description: string,
): Promise<{ upload_id: string; files: ClassifiedFileResponse[] }> {
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
  carriers?: string[]; // LLM-detected carrier names; non-"Unknown" when known
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

// Get results with optional view (raw = pre-merge)
export async function apiGetResultsWithView(uploadId: string, view?: "raw" | "merged"): Promise<{
  upload_id: string;
  project_name: string;
  status: string;
  total_rows: number;
  rows: ExtractedRowAPI[];
  view: string;
  has_merged: boolean;
}> {
  const query = view ? `?view=${view}` : "";
  return apiFetch(`/api/uploads/${uploadId}/results${query}`);
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
// Types
// ============================================

export interface ClassifiedFileResponse {
  filename: string;
  carrier: string | null;
  doc_type: string | null;
  format_variant: string | null;
  file_size: number;
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
