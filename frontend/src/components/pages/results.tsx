"use client";

import { useState, useMemo, useEffect } from "react";
import {
  Download, FileSpreadsheet, FileText, Search, CheckCircle2, AlertCircle,
  HelpCircle, Minus, ArrowUpDown, Eye, ArrowLeft, Upload as UploadIcon, Loader2, Merge, GitMerge,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { toast } from "sonner";
import * as XLSX from "xlsx";
import { useAppStore, type ExtractedRow } from "@/lib/store";
import { apiExportExcel, apiImportCorrections, apiMerge, apiGetStatus, apiGetResults, apiGetResultsWithView } from "@/lib/api";
import { mapAPIRowToStore } from "@/components/pages/upload";

const confStyle = {
  high: { icon: CheckCircle2, color: "text-emerald-400", bg: "bg-emerald-500/10" },
  medium: { icon: AlertCircle, color: "text-amber-400", bg: "bg-amber-500/10" },
  low: { icon: HelpCircle, color: "text-rose-400", bg: "bg-rose-500/10" },
};

// Full 60-field column catalog for the "All columns" data grid view.
// Grouped logically for readability; shown in order.
const ALL_COLUMNS: { key: string; label: string }[] = [
  // Row / file
  { key: "row_type", label: "Row" },
  { key: "sourceFile", label: "Source File" },
  { key: "carrier", label: "Carrier" },
  { key: "carrier_name", label: "Carrier Name" },
  { key: "invoice_file_name", label: "Invoice File" },
  { key: "contract_file_name", label: "Contract File" },
  // Billing / location
  { key: "billing_name", label: "Billing Name" },
  { key: "service_address_1", label: "Service Address 1" },
  { key: "service_address_2", label: "Service Address 2" },
  { key: "city", label: "City" },
  { key: "state", label: "State" },
  { key: "zip", label: "Zip" },
  { key: "country", label: "Country" },
  // Billing address (from invoice) — only populated when it differs from the primary service address
  { key: "billing_name_from_invoice", label: "Billing Name (Invoice)" },
  { key: "billing_address_1", label: "Billing Address (Invoice)" },
  { key: "billing_city", label: "Billing City (Invoice)" },
  { key: "billing_state", label: "Billing State (Invoice)" },
  { key: "billing_zip", label: "Billing Zip (Invoice)" },
  // Accounts
  { key: "master_account", label: "Master Account" },
  { key: "carrier_account_number", label: "Account #" },
  { key: "sub_account_number_1", label: "Sub Account 1" },
  { key: "sub_account_number_2", label: "Sub Account 2" },
  { key: "btn", label: "BTN" },
  { key: "phone_number", label: "Phone" },
  { key: "carrier_circuit_number", label: "Circuit #" },
  { key: "additional_circuit_ids", label: "Other Circuit IDs" },
  // Service
  { key: "service_type", label: "Service Type" },
  { key: "service_type_2", label: "Service Type 2" },
  { key: "usoc", label: "USOC" },
  { key: "service_or_component", label: "S/C" },
  { key: "component_or_feature_name", label: "Component" },
  // Cost
  { key: "monthly_recurring_cost", label: "MRC" },
  { key: "quantity", label: "Qty" },
  { key: "cost_per_unit", label: "Cost/Unit" },
  { key: "currency", label: "Currency" },
  { key: "conversion_rate", label: "FX Rate" },
  { key: "mrc_per_currency", label: "MRC / Currency" },
  { key: "charge_type", label: "Charge Type" },
  { key: "num_calls", label: "# Calls" },
  { key: "ld_minutes", label: "LD Mins" },
  { key: "ld_cost", label: "LD Cost" },
  { key: "rate", label: "Rate" },
  { key: "ld_flat_rate", label: "LD Flat Rate" },
  { key: "point_to_number", label: "Point-to #" },
  // Speeds
  { key: "port_speed", label: "Port Speed" },
  { key: "access_speed", label: "Access Speed" },
  { key: "upload_speed", label: "Upload Speed" },
  // Z location
  { key: "z_location_name", label: "Z Location" },
  { key: "z_address_1", label: "Z Address 1" },
  { key: "z_address_2", label: "Z Address 2" },
  { key: "z_city", label: "Z City" },
  { key: "z_state", label: "Z State" },
  { key: "z_zip", label: "Z Zip" },
  { key: "z_country", label: "Z Country" },
  // Contract
  { key: "contract_term_months", label: "Term (mo)" },
  { key: "contract_begin_date", label: "Contract Start" },
  { key: "contract_expiration_date", label: "Contract End" },
  { key: "billing_per_contract", label: "Billing / Contract" },
  { key: "currently_month_to_month", label: "MTM" },
  { key: "mtm_or_less_than_year", label: "MTM / <1Y" },
  { key: "contract_number", label: "Contract #" },
  { key: "contract_number_2", label: "Contract #2" },
  { key: "auto_renew", label: "Auto Renew" },
  { key: "auto_renewal_notes", label: "Auto-Renew Notes" },
  // Status + audit
  { key: "status", label: "Status" },
  { key: "compliance_flags", label: "Compliance" },
  { key: "notes", label: "Notes" },
  { key: "contract_info_received", label: "Contract Info Received" },
  { key: "files_used", label: "Files Used" },
  { key: "confidence", label: "Confidence" },
];

// Severity → tailwind class for the compliance badge.
const _COMPL_SEVERITY_STYLE: Record<string, { fg: string; bg: string; bd: string }> = {
  error:   { fg: "text-rose-300",  bg: "bg-rose-500/15",  bd: "border-rose-500/30" },
  warning: { fg: "text-amber-300", bg: "bg-amber-500/15", bd: "border-amber-500/30" },
  info:    { fg: "text-sky-300",   bg: "bg-sky-500/15",   bd: "border-sky-500/30" },
};

// Human-readable label per check (matches backend/pipeline/compliance.py).
const _COMPL_CHECK_LABEL: Record<string, string> = {
  rate_mismatch:      "rate mismatch",
  expired_contract:   "expired contract",
  mtm_inconsistency:  "MTM inconsistency",
  term_date_mismatch: "term/date mismatch",
  no_contract:        "no contract",
};

interface ResultsPageProps {
  onReviewRow?: (rowId: string) => void;
  onBack?: () => void;
}

export function ResultsPage({ onReviewRow, onBack }: ResultsPageProps) {
  const { getActiveUpload, setSelectedRow, uploads, activeUploadId, setActiveUpload } = useAppStore();
  const upload = getActiveUpload();
  const [search, setSearch] = useState("");
  const [carrierFilter, setCarrierFilter] = useState("all");
  const [confFilter, setConfFilter] = useState("all");
  const [sortField, setSortField] = useState<string | null>(null);
  const [sortDir, setSortDir] = useState<"asc" | "desc">("asc");
  const [exporting, setExporting] = useState(false);
  const [importing, setImporting] = useState(false);
  const [merging, setMerging] = useState(false);
  const [viewMode, setViewMode] = useState<"default" | "raw" | "merged">("default");
  const [rawRows, setRawRows] = useState<ExtractedRow[] | null>(null);
  const [hasMerged, setHasMerged] = useState(false);
  const [columnView, setColumnView] = useState<"compact" | "all">("compact");
  const [validationFilter, setValidationFilter] = useState<"all" | "needs-review" | "clean" | "compliance">("all");

  // Check if merged results exist
  useEffect(() => {
    if (upload?.id) {
      apiGetResultsWithView(upload.id).then((resp) => {
        if (resp.has_merged) {
          setHasMerged(true);
          if (viewMode === "default") setViewMode("merged");
        }
      }).catch(() => {});
    }
  }, [upload?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const rows = (viewMode === "raw" && rawRows) ? rawRows : (upload?.results || []);
  const carriers = [...new Set(rows.map((r) => r.carrier))];

  const filtered = useMemo(() => {
    let data = [...rows];
    if (search) {
      const s = search.toLowerCase();
      data = data.filter((r) => Object.values(r).some((v) => String(v).toLowerCase().includes(s)));
    }
    if (carrierFilter !== "all") data = data.filter((r) => r.carrier === carrierFilter);
    if (confFilter !== "all") data = data.filter((r) => r.confidence === confFilter);
    if (validationFilter === "needs-review") {
      data = data.filter((r) => {
        const issues = (r as Record<string, unknown>).validation_issues as unknown[] | undefined;
        const status = (r as Record<string, unknown>).status as string | undefined;
        return (issues && issues.length > 0) || status === "Needs Review";
      });
    } else if (validationFilter === "clean") {
      data = data.filter((r) => {
        const issues = (r as Record<string, unknown>).validation_issues as unknown[] | undefined;
        const status = (r as Record<string, unknown>).status as string | undefined;
        return (!issues || issues.length === 0) && status !== "Needs Review";
      });
    } else if (validationFilter === "compliance") {
      // Surface only rows the post-merge audit flagged (rate mismatch,
      // expired contract, MTM inconsistency, term/date mismatch, no contract).
      data = data.filter((r) => {
        const flags = (r as Record<string, unknown>).compliance_flags as unknown[] | undefined;
        return Array.isArray(flags) && flags.length > 0;
      });
    }
    if (sortField) {
      data.sort((a, b) => {
        const av = (a as Record<string, unknown>)[sortField];
        const bv = (b as Record<string, unknown>)[sortField];
        const cmp = String(av ?? "").localeCompare(String(bv ?? ""), undefined, { numeric: true });
        return sortDir === "asc" ? cmp : -cmp;
      });
    }
    return data;
  }, [rows, search, carrierFilter, confFilter, sortField, sortDir]);

  // Render cap — large tables (>500 rows) are lazy-loaded to keep the page responsive.
  const ROWS_PER_PAGE = 500;
  const [displayLimit, setDisplayLimit] = useState(ROWS_PER_PAGE);
  useEffect(() => {
    // reset the cap whenever the filter set changes so pagination starts fresh
    setDisplayLimit(ROWS_PER_PAGE);
  }, [search, carrierFilter, confFilter, validationFilter, sortField, sortDir, viewMode, upload?.id]);
  const visible = useMemo(() => filtered.slice(0, displayLimit), [filtered, displayLimit]);

  // If no active upload, show list of all projects
  if (!upload || rows.length === 0) {
    const doneUploads = uploads.filter((u) => u.status === "done" && u.totalRows > 0);
    return (
      <div className="p-8 max-w-4xl mx-auto space-y-6">
        <h1 className="text-2xl font-bold tracking-tight">Results</h1>
        {doneUploads.length === 0 ? (
          <div className="text-center py-20 text-muted-foreground">
            <FileText className="w-12 h-12 mx-auto mb-4 opacity-30" />
            <p className="text-lg font-medium">No extractions yet</p>
            <p className="text-sm mt-1">Upload and extract documents to see results here</p>
          </div>
        ) : (
          <div className="space-y-3">
            <p className="text-sm text-muted-foreground">{doneUploads.length} completed extraction(s)</p>
            {doneUploads.map((u) => (
              <div
                key={u.id}
                className="flex items-center justify-between p-5 rounded-xl border border-border/50 bg-card/50 hover:bg-muted/30 cursor-pointer transition-colors"
                onClick={() => { setActiveUpload(u.id); }}
              >
                <div>
                  <p className="font-semibold">{u.projectName}</p>
                  {u.description && <p className="text-xs text-muted-foreground mt-0.5">{u.description}</p>}
                  <div className="flex items-center gap-3 mt-1.5">
                    {u.clientName && <Badge variant="secondary" className="text-[10px]">{u.clientName}</Badge>}
                    <span className="text-xs text-muted-foreground">
                      {u.files.filter((f) => f.status === "done").length} files · {u.carriers.join(", ")}
                    </span>
                    <span className="text-xs text-muted-foreground">
                      {u.createdAt.toLocaleDateString()}
                    </span>
                  </div>
                </div>
                <div className="flex items-center gap-3">
                  <div className="text-right">
                    <p className="text-lg font-bold">{u.totalRows.toLocaleString()}</p>
                    <p className="text-[10px] text-muted-foreground">rows</p>
                  </div>
                  <Eye className="w-5 h-5 text-muted-foreground" />
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    );
  }

  const handleToggleView = async (mode: "raw" | "merged") => {
    if (mode === "raw" && !rawRows) {
      // Fetch raw results from API
      try {
        const resp = await apiGetResultsWithView(upload.id, "raw");
        if (resp.rows) {
          setRawRows(resp.rows.map(mapAPIRowToStore));
          setHasMerged(resp.has_merged);
        }
      } catch {
        // No raw results available
        return;
      }
    }
    setViewMode(mode);
  };

  const handleExcelDownload = async () => {
    setExporting(true);
    try {
      // Server-side export with confidence color-coding
      const blob = await apiExportExcel(upload.id);
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${upload.projectName || "extraction"}_${new Date().toISOString().slice(0, 10)}.xlsx`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      window.URL.revokeObjectURL(url);
      toast.success(`Downloaded ${rows.length} rows with confidence color-coding`);
    } catch {
      // Fallback to client-side export
      const columns = [
        "row_type", "carrier", "sourceFile", "confidence",
        "billing_name", "service_address_1", "service_address_2", "city", "state", "zip", "country",
        "carrier_name", "carrier_account_number", "master_account", "sub_account_number_1", "sub_account_number_2", "btn",
        "phone_number", "carrier_circuit_number", "additional_circuit_ids", "service_type", "service_type_2",
        "usoc", "service_or_component", "component_or_feature_name",
        "monthly_recurring_cost", "quantity", "cost_per_unit", "currency", "conversion_rate", "mrc_per_currency",
        "charge_type", "num_calls", "ld_minutes", "ld_cost", "rate", "ld_flat_rate", "point_to_number",
        "port_speed", "access_speed", "upload_speed",
        "z_location_name", "z_address_1", "z_address_2", "z_city", "z_state", "z_zip", "z_country",
        "contract_term_months", "contract_begin_date", "contract_expiration_date",
        "billing_per_contract", "currently_month_to_month", "mtm_or_less_than_year",
        "contract_file_name", "contract_number", "contract_number_2", "auto_renew", "auto_renewal_notes",
        "status", "notes", "contract_info_received", "invoice_file_name", "files_used",
      ];
      const exportRows = filtered.map((r) => {
        const out: Record<string, unknown> = {};
        for (const col of columns) out[col] = (r as Record<string, unknown>)[col] ?? "";
        return out;
      });
      const ws = XLSX.utils.json_to_sheet(exportRows);
      const wb = XLSX.utils.book_new();
      XLSX.utils.book_append_sheet(wb, ws, "Extracted Data");
      XLSX.writeFile(wb, `${upload.projectName || "extraction"}_${new Date().toISOString().slice(0, 10)}.xlsx`);
      toast.success(`Downloaded ${filtered.length} rows (client-side fallback)`);
    } finally {
      setExporting(false);
    }
  };

  const handleMerge = async () => {
    setMerging(true);
    try {
      await apiMerge(upload.id);
      toast.info("Merging documents... This may take a moment.");
      // Poll until merge is complete
      const poll = async () => {
        for (let i = 0; i < 60; i++) {
          await new Promise((r) => setTimeout(r, 2000));
          const status = await apiGetStatus(upload.id);
          if (status.status === "done") {
            // Reload results
            const resultsResp = await apiGetResults(upload.id);
            const merged = resultsResp.rows.map(mapAPIRowToStore);
            useAppStore.getState().restoreUploadResults(upload.id, merged);
            setHasMerged(true);
            setViewMode("merged");
            setRawRows(null); // clear cached raw so it re-fetches
            toast.success(`Merge complete: ${merged.length} rows (was ${rows.length})`);
            return;
          }
          if (status.status !== "merging") {
            toast.error(`Merge ended with status: ${status.status}`);
            return;
          }
        }
        toast.error("Merge timed out");
      };
      await poll();
    } catch (e) {
      toast.error(`Merge failed: ${e instanceof Error ? e.message : "Unknown error"}`);
    } finally {
      setMerging(false);
    }
  };

  const handleImportCorrections = async (file: File) => {
    setImporting(true);
    try {
      const result = await apiImportCorrections(upload.id, file);
      toast.success(`Imported ${result.corrections_created} corrections from ${result.rows_compared} rows`);
    } catch (e) {
      toast.error(`Import failed: ${e instanceof Error ? e.message : "Unknown error"}`);
    } finally {
      setImporting(false);
    }
  };

  const handleSort = (field: string) => {
    if (sortField === field) setSortDir(sortDir === "asc" ? "desc" : "asc");
    else { setSortField(field); setSortDir("asc"); }
  };

  const totalMrc = filtered.reduce((sum, r) => sum + (Number(r.monthly_recurring_cost) || 0), 0);
  const uniqueAccounts = new Set(rows.map((r) => r.carrier_account_number).filter(Boolean));
  const highCount = rows.filter((r) => r.confidence === "high").length;
  const medCount = rows.filter((r) => r.confidence === "medium").length;
  const lowCount = rows.filter((r) => r.confidence === "low").length;

  return (
    <div className="h-full flex flex-col">
      {/* Header */}
      <div className="px-6 py-4 border-b border-border/50 shrink-0">
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-3">
            <Button variant="ghost" size="sm" className="h-8 px-2" onClick={() => { setActiveUpload(null); onBack?.(); }}>
              <ArrowLeft className="w-4 h-4" />
            </Button>
            <div>
              <h1 className="text-lg font-bold">{upload.projectName}</h1>
              <div className="flex items-center gap-3 text-xs text-muted-foreground mt-0.5">
                <span>{filtered.length} of {rows.length} rows</span>
                <span>·</span>
                <span>{uniqueAccounts.size} account{uniqueAccounts.size !== 1 ? "s" : ""}</span>
                <span>·</span>
                <span>MRC: ${totalMrc.toLocaleString(undefined, { minimumFractionDigits: 2 })}</span>
                <span>·</span>
                <span className="text-emerald-400">{highCount} high</span>
                <span className="text-amber-400">{medCount} medium</span>
                <span className="text-rose-400">{lowCount} low</span>
              </div>
            </div>
          </div>
          <div className="flex items-center gap-2">
            {carriers.length > 0 && (
              <Button
                size="sm"
                className="bg-indigo-600 hover:bg-indigo-500 text-white"
                onClick={handleMerge}
                disabled={merging}
              >
                {merging ? <Loader2 className="w-4 h-4 mr-1.5 animate-spin" /> : <GitMerge className="w-4 h-4 mr-1.5" />}
                {merging ? "Merging..." : "Merge & Enrich"}
              </Button>
            )}
            {hasMerged && (
              <div className="flex items-center rounded-lg border border-border/50 overflow-hidden">
                <button
                  className={`px-3 py-1.5 text-xs font-medium transition-colors ${viewMode === "raw" ? "bg-muted text-foreground" : "text-muted-foreground hover:text-foreground"}`}
                  onClick={() => handleToggleView("raw")}
                >
                  Raw ({viewMode === "raw" ? rows.length : "..."})
                </button>
                <button
                  className={`px-3 py-1.5 text-xs font-medium transition-colors ${viewMode !== "raw" ? "bg-muted text-foreground" : "text-muted-foreground hover:text-foreground"}`}
                  onClick={() => setViewMode("merged")}
                >
                  Merged
                </button>
              </div>
            )}
            <Button variant="outline" size="sm" onClick={handleExcelDownload} disabled={exporting}>
              {exporting ? <Loader2 className="w-4 h-4 mr-1.5 animate-spin" /> : <FileSpreadsheet className="w-4 h-4 mr-1.5" />}
              Excel ({filtered.length} rows)
            </Button>
            <Button variant="outline" size="sm" disabled={importing} onClick={() => document.getElementById("import-excel-input")?.click()}>
              {importing ? <Loader2 className="w-4 h-4 mr-1.5 animate-spin" /> : <UploadIcon className="w-4 h-4 mr-1.5" />}
              Import
            </Button>
            <input id="import-excel-input" type="file" accept=".xlsx,.xls" className="hidden" onChange={(e) => { const f = e.target.files?.[0]; if (f) handleImportCorrections(f); e.target.value = ""; }} />
          </div>
        </div>

        <div className="flex items-center gap-3">
          <div className="relative flex-1 max-w-sm">
            <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
            <Input placeholder="Search..." value={search} onChange={(e) => setSearch(e.target.value)} className="pl-9 h-9 bg-card/50" />
          </div>
          <Select value={carrierFilter} onValueChange={(v) => setCarrierFilter(v ?? "all")}>
            <SelectTrigger className="w-36 h-9"><SelectValue placeholder="Carrier" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All carriers</SelectItem>
              {carriers.map((c) => <SelectItem key={c} value={c}>{c}</SelectItem>)}
            </SelectContent>
          </Select>
          <Select value={confFilter} onValueChange={(v) => setConfFilter(v ?? "all")}>
            <SelectTrigger className="w-36 h-9"><SelectValue placeholder="Confidence" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All levels</SelectItem>
              <SelectItem value="high">High</SelectItem>
              <SelectItem value="medium">Medium</SelectItem>
              <SelectItem value="low">Low</SelectItem>
            </SelectContent>
          </Select>
          <Select value={validationFilter} onValueChange={(v) => setValidationFilter((v as "all" | "needs-review" | "clean" | "compliance") ?? "all")}>
            <SelectTrigger className="w-44 h-9"><SelectValue placeholder="Validation" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All rows</SelectItem>
              <SelectItem value="needs-review">Needs review</SelectItem>
              <SelectItem value="compliance">Compliance flagged</SelectItem>
              <SelectItem value="clean">Clean</SelectItem>
            </SelectContent>
          </Select>

          {/* Column-view toggle — Compact (8 cols) vs All (every field) */}
          <div className="flex items-center rounded-lg border border-border/50 overflow-hidden ml-auto">
            <button
              className={`px-3 py-1.5 text-xs font-medium transition-colors ${columnView === "compact" ? "bg-muted text-foreground" : "text-muted-foreground hover:text-foreground"}`}
              onClick={() => setColumnView("compact")}
            >
              Compact
            </button>
            <button
              className={`px-3 py-1.5 text-xs font-medium transition-colors ${columnView === "all" ? "bg-muted text-foreground" : "text-muted-foreground hover:text-foreground"}`}
              onClick={() => setColumnView("all")}
              title={`Show all ${ALL_COLUMNS.length} fields`}
            >
              All {ALL_COLUMNS.length}
            </button>
          </div>
        </div>
      </div>

      {/* Table — wrap in overflow-auto so the 60-col view scrolls horizontally. */}
      <div className="flex-1 overflow-auto">
        {columnView === "compact" ? (
          <Table className="table-compact">
            <TableHeader className="sticky top-0 bg-card/80 backdrop-blur z-10">
              <TableRow className="border-border/30">
                <TableHead className="w-10"></TableHead>
                <TableHead className="cursor-pointer" onClick={() => handleSort("carrier")}>
                  <div className="flex items-center gap-1">Carrier <ArrowUpDown className="w-3 h-3" /></div>
                </TableHead>
                <TableHead>Source File</TableHead>
                <TableHead>Account</TableHead>
                <TableHead>Service</TableHead>
                <TableHead>Component</TableHead>
                <TableHead className="text-right cursor-pointer" onClick={() => handleSort("monthly_recurring_cost")}>
                  <div className="flex items-center gap-1 justify-end">MRC <ArrowUpDown className="w-3 h-3" /></div>
                </TableHead>
                <TableHead className="w-10">Type</TableHead>
                <TableHead className="w-10"></TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {visible.map((row) => {
                const cs = confStyle[row.confidence] || confStyle.high;
                const Icon = cs.icon;
                const issues = (row as Record<string, unknown>).validation_issues as Array<{severity?: string; message?: string}> | undefined;
                const status = (row as Record<string, unknown>).status as string | undefined;
                const errorIssues = issues?.filter((i) => i?.severity === "error") ?? [];
                const needsValidation = status === "Validate carrier";
                const needsReview = status === "Needs Review" || errorIssues.length > 0 || needsValidation;
                const complianceFlags = (Array.isArray((row as Record<string, unknown>).compliance_flags)
                    ? ((row as Record<string, unknown>).compliance_flags as Array<{check?: string; severity?: string; message?: string}>)
                    : []);
                const worstSeverity = complianceFlags.some((f) => f?.severity === "error")
                    ? "error"
                    : complianceFlags.some((f) => f?.severity === "warning")
                        ? "warning"
                        : (complianceFlags.length ? "info" : null);
                return (
                  <TableRow
                    key={row.id}
                    className={`border-border/20 cursor-pointer hover:bg-muted/30 transition-colors group ${needsReview ? "bg-amber-500/5" : ""}`}
                    onClick={() => { setSelectedRow(row.id); onReviewRow?.(row.id); }}
                  >
                    <TableCell>
                      <div className={`w-6 h-6 rounded-full flex items-center justify-center ${cs.bg}`}>
                        <Icon className={`w-3.5 h-3.5 ${cs.color}`} />
                      </div>
                    </TableCell>
                    <TableCell className="font-medium">{row.carrier}</TableCell>
                    <TableCell className="text-xs text-muted-foreground max-w-[150px] truncate">{row.sourceFile}</TableCell>
                    <TableCell className="font-mono text-xs">{row.carrier_account_number || "—"}</TableCell>
                    <TableCell><Badge variant="secondary" className="text-[10px]">{row.service_type || "—"}</Badge></TableCell>
                    <TableCell className="max-w-[180px] truncate">{row.component_or_feature_name || "—"}</TableCell>
                    <TableCell className="text-right font-mono">{row.monthly_recurring_cost != null ? `$${Number(row.monthly_recurring_cost).toFixed(2)}` : "—"}</TableCell>
                    <TableCell>
                      <div className="flex items-center gap-1 flex-wrap">
                        <Badge className={row.row_type === "S" ? "bg-blue-500/20 text-blue-400 text-[10px]" : "bg-violet-500/20 text-violet-400 text-[10px]"}>
                          {row.row_type}
                        </Badge>
                        {needsReview && (
                          <Badge
                            className="bg-amber-500/15 text-amber-400 text-[10px] border border-amber-500/30"
                            title={errorIssues.length > 0
                              ? errorIssues.map((i) => i.message).filter(Boolean).join(" · ")
                              : (needsValidation
                                ? "Carrier name not in registry — confirm and register"
                                : "Status: Needs Review")}
                          >
                            {needsValidation ? "Validate" : "Review"}
                          </Badge>
                        )}
                        {worstSeverity && (
                          <Badge
                            className={`text-[10px] border ${
                              worstSeverity === "error"
                                ? "bg-rose-500/15 text-rose-300 border-rose-500/30"
                                : worstSeverity === "warning"
                                    ? "bg-amber-500/15 text-amber-300 border-amber-500/30"
                                    : "bg-sky-500/15 text-sky-300 border-sky-500/30"
                            }`}
                            title={complianceFlags
                              .map((f) => `[${f.severity}] ${(_COMPL_CHECK_LABEL[f.check ?? ""] || f.check)}: ${f.message ?? ""}`)
                              .join("\n")}
                          >
                            {complianceFlags.length === 1
                              ? (_COMPL_CHECK_LABEL[complianceFlags[0].check ?? ""] || complianceFlags[0].check || "compliance")
                              : `${complianceFlags.length} compliance`}
                          </Badge>
                        )}
                      </div>
                    </TableCell>
                    <TableCell>
                      <Eye className="w-4 h-4 text-muted-foreground opacity-0 group-hover:opacity-100" />
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        ) : (
          /* Full 60-column data grid — horizontal scroll via parent overflow-auto. */
          <Table className="table-compact" style={{ minWidth: `${ALL_COLUMNS.length * 140}px` }}>
            <TableHeader className="sticky top-0 bg-card/95 backdrop-blur z-10">
              <TableRow className="border-border/30">
                <TableHead className="w-10 sticky left-0 bg-card/95 z-20"></TableHead>
                {ALL_COLUMNS.map((col) => (
                  <TableHead
                    key={col.key}
                    className="whitespace-nowrap cursor-pointer"
                    onClick={() => handleSort(col.key)}
                  >
                    {col.label}
                  </TableHead>
                ))}
              </TableRow>
            </TableHeader>
            <TableBody>
              {visible.map((row) => {
                const cs = confStyle[row.confidence] || confStyle.high;
                const Icon = cs.icon;
                const rec = row as unknown as Record<string, unknown>;
                return (
                  <TableRow
                    key={row.id}
                    className="border-border/20 cursor-pointer hover:bg-muted/30 transition-colors group"
                    onClick={() => { setSelectedRow(row.id); onReviewRow?.(row.id); }}
                  >
                    <TableCell className="sticky left-0 bg-background/95 group-hover:bg-muted/50 z-10">
                      <div className={`w-6 h-6 rounded-full flex items-center justify-center ${cs.bg}`}>
                        <Icon className={`w-3.5 h-3.5 ${cs.color}`} />
                      </div>
                    </TableCell>
                    {ALL_COLUMNS.map((col) => {
                      const raw = rec[col.key];

                      // Custom render: compliance_flags is a list of {check, severity, message}.
                      // Surface as severity-colored badges so the audit results are
                      // visible at-a-glance rather than buried in the row JSON.
                      if (col.key === "compliance_flags") {
                        const flags = (Array.isArray(raw) ? raw : []) as Array<{ check?: string; severity?: string; message?: string }>;
                        if (!flags.length) {
                          return (
                            <TableCell key={col.key} className="whitespace-nowrap text-xs text-muted-foreground/50" title="No compliance flags">—</TableCell>
                          );
                        }
                        const tipLines = flags.map((f) => `[${f.severity ?? "info"}] ${f.check ?? ""}: ${f.message ?? ""}`);
                        return (
                          <TableCell key={col.key} className="whitespace-nowrap" title={tipLines.join("\n")}>
                            <div className="flex items-center gap-1">
                              {flags.slice(0, 3).map((f, i) => {
                                const sev = (f.severity || "info") as keyof typeof _COMPL_SEVERITY_STYLE;
                                const sty = _COMPL_SEVERITY_STYLE[sev] || _COMPL_SEVERITY_STYLE.info;
                                const lbl = _COMPL_CHECK_LABEL[f.check ?? ""] || (f.check ?? "flag");
                                return (
                                  <span key={i} className={`text-[10px] px-1.5 py-0.5 rounded border ${sty.fg} ${sty.bg} ${sty.bd}`}>
                                    {lbl}
                                  </span>
                                );
                              })}
                              {flags.length > 3 && (
                                <span className="text-[10px] text-muted-foreground">+{flags.length - 3}</span>
                              )}
                            </div>
                          </TableCell>
                        );
                      }

                      let display: string;
                      if (raw == null || raw === "") display = "—";
                      else if (col.key === "monthly_recurring_cost" || col.key === "ld_cost" || col.key === "cost_per_unit" || col.key === "mrc_per_currency" || col.key === "ld_flat_rate") {
                        display = `$${Number(raw).toFixed(2)}`;
                      } else if (typeof raw === "number") display = String(raw);
                      else if (typeof raw === "boolean") display = raw ? "Yes" : "No";
                      else display = String(raw);
                      return (
                        <TableCell
                          key={col.key}
                          className={`whitespace-nowrap max-w-[220px] truncate font-mono text-xs ${
                            raw == null || raw === "" ? "text-muted-foreground/50" : ""
                          }`}
                          title={display}
                        >
                          {display}
                        </TableCell>
                      );
                    })}
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        )}
        {filtered.length > visible.length && (
          <div className="px-6 py-4 flex items-center justify-center gap-4 border-t border-border/30">
            <span className="text-xs text-muted-foreground">
              Showing {visible.length.toLocaleString()} of {filtered.length.toLocaleString()} rows
            </span>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setDisplayLimit((n) => n + ROWS_PER_PAGE)}
            >
              Load {Math.min(ROWS_PER_PAGE, filtered.length - visible.length).toLocaleString()} more
            </Button>
            {filtered.length - visible.length > ROWS_PER_PAGE && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setDisplayLimit(filtered.length)}
              >
                Show all
              </Button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
