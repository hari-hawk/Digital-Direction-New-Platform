"use client";

import { useState, useCallback, useEffect, useRef } from "react";
import { motion } from "framer-motion";
import {
  FileText, CheckCircle2, Loader2, X, FolderOpen, Play, Eye, AlertTriangle, Upload as UploadIcon, Trash2, RotateCcw, ArrowLeft, Download, RefreshCw, LayoutGrid, List as ListIcon,
} from "lucide-react";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Progress } from "@/components/ui/progress";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { toast } from "sonner";
import { useAppStore, type ExtractedRow, type Upload } from "@/lib/store";
import {
  apiClassify, apiExtract, apiGetStatus, apiGetResults,
  apiCancelExtraction, apiRetryExtraction, apiListCarriers, apiDownloadFiles,
  apiDeleteUpload, apiCleanupOrphaned,
  type ExtractedRowAPI,
} from "@/lib/api";
import { MoveRight } from "lucide-react";

export function mapAPIRowToStore(row: ExtractedRowAPI): ExtractedRow {
  // Pass through all 60 fields as-is, just rename source_file → sourceFile.
  // compliance_flags + validation_issues are JSONB arrays from Postgres —
  // the spread carries them through unchanged for the table to render.
  const { source_file, ...rest } = row;
  return {
    ...rest,
    sourceFile: source_file,
    confidence: (row.confidence as "high" | "medium" | "low") || "medium",
    field_confidence: (row as unknown as Record<string, unknown>).field_confidence as Record<string, string> | undefined,
  };
}

const fileColors: Record<string, string> = {
  pdf: "text-rose-400", xlsx: "text-emerald-400", xls: "text-emerald-400",
  csv: "text-blue-400", docx: "text-blue-400", msg: "text-amber-400", eml: "text-amber-400",
};

interface UploadPageProps {
  onViewResults?: () => void;
}

export function UploadPage({ onViewResults }: UploadPageProps) {
  const store = useAppStore();
  const upload = store.getActiveUpload();
  const projectName = store.draftProjectName;
  const clientName = store.draftClientName;
  const description = store.draftDescription;
  const setProjectName = (v: string) => store.setDraftField("draftProjectName", v);
  const setClientName = (v: string) => store.setDraftField("draftClientName", v);
  const setDescription = (v: string) => store.setDraftField("draftDescription", v);
  const [configuredCarriers, setConfiguredCarriers] = useState<string[]>([]);
  const [previousView, setPreviousView] = useState<"list" | "grid">("list");
  const [selectedCarriers, setSelectedCarriers] = useState<Set<string>>(new Set());
  const [isDragging, setIsDragging] = useState(false);
  const [classifying, setClassifying] = useState(false);
  const [extracting, setExtracting] = useState(false);
  const [progress, setProgress] = useState(0);
  const pollingRef = useRef(false);

  // Fetch configured carriers from backend on mount
  useEffect(() => {
    apiListCarriers()
      .then(({ carriers }) => {
        const names = carriers.map((c) => c.name);
        setConfiguredCarriers(names);
        setSelectedCarriers(new Set(names));
      })
      .catch(() => {});
  }, []);

  // Resume polling when component mounts with an extracting upload
  useEffect(() => {
    if (!upload || (upload.status !== "extracting" && upload.status !== "classifying")) return;
    if (pollingRef.current) return; // Already polling

    setExtracting(true);
    pollingRef.current = true;
    let cancelled = false;

    const poll = async () => {
      while (!cancelled) {
        await new Promise((r) => setTimeout(r, 2000));
        if (cancelled) break;
        try {
          const statusResp = await apiGetStatus(upload.id);
          if (statusResp.files_total > 0) {
            setProgress(Math.round((statusResp.files_processed / statusResp.files_total) * 100));
          }
          if (statusResp.status !== "extracting" && statusResp.status !== "cancel_requested") {
            if (statusResp.status === "done") {
              const resultsResp = await apiGetResults(upload.id);
              const rows = resultsResp.rows.map(mapAPIRowToStore);
              store.addResults(upload.id, rows);
              toast.success(`Extracted ${rows.length} rows`);
            } else {
              store.updateUploadStatus(upload.id, statusResp.status as Upload["status"]);
              if (statusResp.status === "cancelled") toast.info("Extraction cancelled");
              if (statusResp.status === "interrupted") toast.warning("Extraction was interrupted");
              if (statusResp.status === "error") toast.error("Extraction failed");
            }
            setExtracting(false);
            pollingRef.current = false;
            break;
          }
        } catch {
          // API error, keep polling
        }
      }
    };

    poll();
    return () => {
      cancelled = true;
      pollingRef.current = false;
    };
  }, [upload?.id, upload?.status]);

  const canUpload = projectName.trim().length > 0;

  const handleFiles = useCallback(async (rawFiles: FileList | File[]) => {
    if (!projectName.trim()) {
      toast.error("Please enter a Project Name before uploading files");
      return;
    }
    const files = Array.from(rawFiles).filter((f) => !f.name.startsWith(".") && f.size > 0);
    if (!files.length) return;

    setClassifying(true);
    try {
      const result = await apiClassify(files, projectName, clientName, description);
      store.createUploadFromAPI(
        result.upload_id,
        projectName,
        description,
        clientName,
        result.files,
        files,
      );
      // Group by effective carrier — treat null/blank as "Unknown" so unknown-carrier
      // files still go through the generic pipeline instead of being silently skipped.
      const carriers = [...new Set(result.files.map((f) => f.carrier || "Unknown"))] as string[];
      setSelectedCarriers(new Set(carriers));
      store.clearDraftFields();
      toast.success(`${files.length} files classified by backend`);
    } catch (err) {
      toast.error(`Classification failed: ${err instanceof Error ? err.message : "Unknown error"}`);
    } finally {
      setClassifying(false);
    }
  }, [clientName, projectName, description, store, configuredCarriers, canUpload]);

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(false);
    const items = e.dataTransfer.items;
    if (items?.length) {
      const allFiles: File[] = [];
      let pending = 0;
      const done = () => { if (pending === 0 && allFiles.length) handleFiles(allFiles); };
      const readEntry = (entry: FileSystemEntry) => {
        if (entry.isFile) {
          pending++;
          (entry as FileSystemFileEntry).file((f) => { allFiles.push(f); pending--; done(); });
        } else if (entry.isDirectory) {
          pending++;
          (entry as FileSystemDirectoryEntry).createReader().readEntries((entries) => {
            entries.forEach(readEntry);
            pending--;
            done();
          });
        }
      };
      for (let i = 0; i < items.length; i++) {
        const entry = items[i].webkitGetAsEntry();
        if (entry) readEntry(entry);
      }
      if (!items[0]?.webkitGetAsEntry?.() && e.dataTransfer.files.length) handleFiles(e.dataTransfer.files);
    } else if (e.dataTransfer.files.length) {
      handleFiles(e.dataTransfer.files);
    }
  }, [handleFiles]);

  const toggleCarrier = (c: string) => {
    setSelectedCarriers((prev) => {
      const next = new Set(prev);
      next.has(c) ? next.delete(c) : next.add(c);
      return next;
    });
  };

  const handleExtract = async () => {
    if (!upload) return;
    // Effective carrier: null/empty → "Unknown" so files classified as unknown
    // still flow through the generic extraction path.
    const effCarrier = (f: typeof upload.files[number]) => f.carrier || "Unknown";
    const filesToProcess = upload.files.filter((f) => selectedCarriers.has(effCarrier(f)));

    // Mark selected files as extracting, unselected as skipped
    filesToProcess.forEach((f) => store.updateFileStatus(upload.id, f.name, "extracting"));
    upload.files.filter((f) => !selectedCarriers.has(effCarrier(f))).forEach((f) => {
      store.updateFileStatus(upload.id, f.name, "skipped");
    });

    try {
      await apiExtract(
        upload.id,
        filesToProcess.map((f) => ({
          filename: f.name,
          carrier: effCarrier(f),
          doc_type: f.docType || undefined,
        })),
      );
      // Status update triggers the polling useEffect
      store.updateUploadStatus(upload.id, "extracting");
      setProgress(0);
    } catch (err) {
      store.updateUploadStatus(upload.id, "error");
      toast.error(`Extraction failed: ${err instanceof Error ? err.message : "Unknown error"}`);
    }
  };

  const handleCancel = async () => {
    if (!upload) return;
    try {
      await apiCancelExtraction(upload.id);
      toast.info("Cancellation requested — will stop after current file");
    } catch (err) {
      toast.error(`Failed to cancel: ${err instanceof Error ? err.message : "Unknown error"}`);
    }
  };

  const handleRetry = async () => {
    if (!upload) return;
    try {
      await apiRetryExtraction(upload.id);
      store.updateUploadStatus(upload.id, "extracting");
      setProgress(0);
      toast.info("Retrying extraction...");
    } catch (err) {
      toast.error(`Retry failed: ${err instanceof Error ? err.message : "Unknown error"}`);
    }
  };

  const handleReset = () => {
    store.setActiveUpload(null);
    setProgress(0);
    setExtracting(false);
  };

  // Group files by carrier
  const carrierGroups = upload
    ? [...new Set(upload.files.map((f) => f.carrier || "Unknown"))].map((carrier) => ({
        carrier,
        files: upload.files.filter((f) => (f.carrier || "Unknown") === carrier),
        configured: configuredCarriers.includes(carrier),
        selected: selectedCarriers.has(carrier),
      })).sort((a, b) => (a.configured === b.configured ? 0 : a.configured ? -1 : 1))
    : [];

  const selectedFileCount = upload?.files.filter((f) => selectedCarriers.has(f.carrier || "Unknown")).length || 0;
  const ext = (name: string) => name.split(".").pop()?.toLowerCase() || "";
  const isInterruptedOrFailed = upload && ["interrupted", "cancelled", "error"].includes(upload.status);
  const allUploads = store.uploads;

  const handleReExtract = async (uploadId: string, projectName: string) => {
    try {
      await apiRetryExtraction(uploadId);
      store.updateUploadStatus(uploadId, "extracting");
      toast.success(`Re-extracting "${projectName}" with the latest pipeline...`);
    } catch (err) {
      toast.error(`Re-extract failed: ${err instanceof Error ? err.message : "Unknown error"}`);
    }
  };

  const handleDownloadFiles = async (uploadId: string, projectName: string) => {
    try {
      const blob = await apiDownloadFiles(uploadId);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      const safe = (projectName || uploadId).replace(/[^a-zA-Z0-9-_]/g, "_");
      a.download = `${safe}_files.zip`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      toast.error(`Download failed: ${err instanceof Error ? err.message : "Unknown error"}`);
    }
  };

  const handleDelete = async (uploadId: string) => {
    try {
      await apiDeleteUpload(uploadId);
      store.deleteUpload(uploadId);
      toast.success("Moved to Bin");
    } catch (err) {
      // If the backend refused because the extraction is still running, cancel it and retry.
      const msg = err instanceof Error ? err.message : String(err);
      if (/cancel first/i.test(msg)) {
        try {
          await apiCancelExtraction(uploadId);
          // Give the worker a moment to acknowledge the cancel, then retry the delete.
          await new Promise((r) => setTimeout(r, 500));
          await apiDeleteUpload(uploadId);
          store.deleteUpload(uploadId);
          toast.success("Cancelled and moved to Bin");
          return;
        } catch (retryErr) {
          toast.error(
            `Delete failed: ${
              retryErr instanceof Error ? retryErr.message : "Unknown error"
            }`,
          );
          return;
        }
      }
      toast.error(`Delete failed: ${msg}`);
    }
  };

  const handleCleanup = async () => {
    try {
      const { cleaned } = await apiCleanupOrphaned();
      toast.success(`Cleaned ${cleaned} orphaned temp folder${cleaned !== 1 ? "s" : ""}`);
    } catch (err) {
      toast.error(`Cleanup failed: ${err instanceof Error ? err.message : "Unknown error"}`);
    }
  };

  const statusLabel = (s: string) => {
    const map: Record<string, { text: string; color: string }> = {
      selecting: { text: "Ready", color: "text-blue-400" },
      classifying: { text: "Classifying", color: "text-blue-400" },
      extracting: { text: "Extracting", color: "text-amber-400" },
      done: { text: "Done", color: "text-emerald-400" },
      error: { text: "Error", color: "text-rose-400" },
      interrupted: { text: "Interrupted", color: "text-amber-400" },
      cancelled: { text: "Cancelled", color: "text-zinc-400" },
    };
    return map[s] || { text: s, color: "text-muted-foreground" };
  };

  // No active upload — show drop zone + uploads list
  if (!upload || upload.status === "done") {
    return (
      <div className="px-6 py-6 space-y-5 max-w-6xl mx-auto">
        <header>
          <h1 className="text-2xl font-semibold tracking-tight">Upload & Extract</h1>
          <p className="text-muted-foreground text-sm mt-1">Drop files or a folder to start a new extraction</p>
        </header>

        {/* Show completed upload summary if exists */}
        {upload?.status === "done" && (
          <Card className="p-5 bg-emerald-500/5 border-emerald-500/20">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-3">
                <CheckCircle2 className="w-5 h-5 text-emerald-400" />
                <div>
                  <p className="font-medium">Extraction complete: {upload.projectName}</p>
                  <p className="text-sm text-muted-foreground">
                    {upload.totalRows} rows from {upload.files.filter((f) => f.status === "done").length} files
                  </p>
                </div>
              </div>
              <div className="flex items-center gap-2">
                <Button variant="outline" size="sm" onClick={handleReset}>New Upload</Button>
                <Button size="sm" className="bg-emerald-600 hover:bg-emerald-500 text-white" onClick={onViewResults}>
                  <Eye className="w-4 h-4 mr-1" /> View Results
                </Button>
              </div>
            </div>
          </Card>
        )}

        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
          <div>
            <label htmlFor="project-name" className="text-xs font-medium text-muted-foreground mb-1.5 block uppercase tracking-wider">Project name *</label>
            <Input
              id="project-name"
              placeholder="City of Dublin – Aug 2025"
              value={projectName}
              onChange={(e) => setProjectName(e.target.value)}
              className="bg-card/50 h-9"
              aria-required="true"
            />
          </div>
          <div>
            <label htmlFor="client-name" className="text-xs font-medium text-muted-foreground mb-1.5 block uppercase tracking-wider">Client</label>
            <Input
              id="client-name"
              placeholder="City of Dublin"
              value={clientName}
              onChange={(e) => setClientName(e.target.value)}
              className="bg-card/50 h-9"
            />
          </div>
          <div>
            <label htmlFor="description" className="text-xs font-medium text-muted-foreground mb-1.5 block uppercase tracking-wider">Description</label>
            <Input
              id="description"
              placeholder="Monthly invoice batch"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              className="bg-card/50 h-9"
            />
          </div>
        </div>

        {classifying ? (
          <div className="neu rounded-2xl p-10 text-center">
            <Loader2 className="w-8 h-8 mx-auto mb-3 text-emerald-400 animate-spin" />
            <p className="font-medium">Classifying files…</p>
            <p className="text-sm text-muted-foreground mt-1">Detecting carriers and document types</p>
          </div>
        ) : (
          <motion.div
            role="button"
            tabIndex={canUpload ? 0 : -1}
            aria-label="Drop files or folder here, or click to select"
            onDragOver={(e) => { if (canUpload) { e.preventDefault(); setIsDragging(true); } }}
            onDragLeave={() => setIsDragging(false)}
            onDrop={(e) => { if (canUpload) handleDrop(e); else { e.preventDefault(); toast.error("Please enter a Project Name first"); } }}
            onKeyDown={(e) => {
              if (!canUpload) return;
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                const input = document.createElement("input");
                input.type = "file"; input.multiple = true;
                input.accept = ".pdf,.xlsx,.xls,.csv,.docx,.msg,.eml";
                input.onchange = (ev) => { const t = ev.target as HTMLInputElement; if (t.files) handleFiles(t.files); };
                input.click();
              }
            }}
            animate={isDragging ? { scale: 1.005 } : { scale: 1 }}
            className={`
              rounded-2xl border-2 border-dashed p-10 text-center transition-colors
              focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/60
              ${!canUpload ? "opacity-40 cursor-not-allowed" : "cursor-pointer"}
              ${isDragging ? "border-emerald-500 bg-emerald-500/5" : "border-border/60 hover:border-border bg-card/30"}
            `}
            onClick={() => {
              if (!canUpload) { toast.error("Please enter a Project Name first"); return; }
              // Single entry: default to file picker (covers most flows).
              // Folder picker is available via the explicit button below, so both
              // modes are reachable with one unified affordance.
              const input = document.createElement("input");
              input.type = "file"; input.multiple = true;
              input.accept = ".pdf,.xlsx,.xls,.csv,.docx,.msg,.eml";
              input.onchange = (e) => { const t = e.target as HTMLInputElement; if (t.files) handleFiles(t.files); };
              input.click();
            }}
          >
            <div className="flex items-center justify-center gap-3 mb-2">
              <UploadIcon className={`w-7 h-7 ${isDragging ? "text-emerald-400" : "text-muted-foreground/60"}`} />
              <FolderOpen className={`w-7 h-7 ${isDragging ? "text-emerald-400" : "text-muted-foreground/60"}`} />
            </div>
            <p className="font-medium">
              {isDragging ? "Drop here" : "Drop files or folder to upload"}
            </p>
            <p className="text-xs text-muted-foreground mt-1">
              {canUpload ? "PDF · XLSX · CSV · DOCX · MSG · EML" : "Enter a Project Name above to enable upload"}
            </p>
            {canUpload && (
              <div className="mt-4 flex items-center justify-center gap-2 text-xs text-muted-foreground">
                <span>or pick:</span>
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    const input = document.createElement("input");
                    input.type = "file"; input.multiple = true;
                    input.accept = ".pdf,.xlsx,.xls,.csv,.docx,.msg,.eml";
                    input.onchange = (ev) => { const t = ev.target as HTMLInputElement; if (t.files) handleFiles(t.files); };
                    input.click();
                  }}
                  className="underline hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/60 rounded-sm"
                >
                  individual files
                </button>
                <span>·</span>
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    const input = document.createElement("input");
                    input.type = "file"; input.multiple = true;
                    input.setAttribute("webkitdirectory", ""); input.setAttribute("directory", "");
                    input.onchange = (ev) => { const t = ev.target as HTMLInputElement; if (t.files) handleFiles(t.files); };
                    input.click();
                  }}
                  className="underline hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/60 rounded-sm"
                >
                  a folder
                </button>
              </div>
            )}
          </motion.div>
        )}

        {/* Existing uploads list */}
        {allUploads.length > 0 && (
          <section aria-labelledby="previous-uploads-heading" className="space-y-3">
            <div className="flex items-center justify-between">
              <h2 id="previous-uploads-heading" className="text-sm font-semibold text-muted-foreground uppercase tracking-wider">
                Previous uploads
                <span className="ml-2 text-xs normal-case font-normal tracking-normal text-muted-foreground/70">
                  {allUploads.filter((u) => u.id !== upload?.id).length}
                </span>
              </h2>
              <div className="flex items-center gap-2">
                <div role="group" aria-label="Layout" className="flex items-center rounded-lg border border-border/60 overflow-hidden">
                  <button
                    type="button"
                    aria-pressed={previousView === "list"}
                    aria-label="List view"
                    onClick={() => setPreviousView("list")}
                    className={`px-2 py-1 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/60 ${previousView === "list" ? "bg-muted text-foreground" : "text-muted-foreground hover:text-foreground"}`}
                  >
                    <ListIcon className="w-3.5 h-3.5" />
                  </button>
                  <button
                    type="button"
                    aria-pressed={previousView === "grid"}
                    aria-label="Grid view"
                    onClick={() => setPreviousView("grid")}
                    className={`px-2 py-1 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/60 ${previousView === "grid" ? "bg-muted text-foreground" : "text-muted-foreground hover:text-foreground"}`}
                  >
                    <LayoutGrid className="w-3.5 h-3.5" />
                  </button>
                </div>
                <Button variant="ghost" size="sm" className="text-xs text-muted-foreground" onClick={handleCleanup}>
                  Clean up temp
                </Button>
              </div>
            </div>
            <div className={previousView === "grid" ? "grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3" : "space-y-3"}>
            {allUploads
              .filter((u) => u.id !== upload?.id)
              .map((u) => {
                const sl = statusLabel(u.status);
                const isGrid = previousView === "grid";
                const warnings = u.rowsWithIssues ?? 0;
                const accounts = u.uniqueAccounts ?? 0;
                const needsValidation = u.rowsNeedingCarrierValidation ?? 0;
                // Carriers: LLM-detected post-extraction; fallback to classify-stage
                // carrier labels, excluding "Unknown" which gets replaced once rows exist.
                const carrierList = (u.carriers || []).filter((c) => c && c.toLowerCase() !== "unknown");
                // Per-file extraction failures (silent-failure surfacing).
                // We render a compact banner so 0-row results stop being mysterious.
                const extErrors = u.extractionErrors ?? [];
                return (
                  <Card key={u.id} className={`neu rounded-xl ${isGrid ? "p-4" : "px-5 py-3"}`}>
                    <div className={isGrid ? "flex flex-col gap-3" : "flex items-center justify-between gap-3"}>
                      <div className="min-w-0 flex-1">
                        <p className="font-medium text-sm truncate" title={u.projectName}>
                          {u.projectName || `Upload ${u.id}`}
                        </p>
                        <p className="text-xs text-muted-foreground mt-0.5">
                          {u.files.length} file{u.files.length === 1 ? "" : "s"} · {u.totalRows.toLocaleString()} row{u.totalRows === 1 ? "" : "s"}
                          {accounts > 0 && <> · {accounts} account{accounts === 1 ? "" : "s"}</>}
                          {warnings > 0 && (
                            <>
                              {" · "}
                              <span className="text-amber-400">{warnings} warning{warnings === 1 ? "" : "s"}</span>
                            </>
                          )}
                        </p>
                        <div className="flex items-center flex-wrap gap-1.5 mt-2">
                          <span className={`text-[10px] px-1.5 py-0.5 rounded ${sl.color} bg-muted/60`}>
                            {sl.text}
                          </span>
                          {carrierList.length > 0 ? (
                            carrierList.slice(0, 4).map((c) => (
                              <span key={c} className="text-[10px] px-1.5 py-0.5 rounded bg-sky-500/10 text-sky-300 border border-sky-500/20">
                                {c}
                              </span>
                            ))
                          ) : u.status === "done" ? (
                            <span className="text-[10px] px-1.5 py-0.5 rounded bg-muted/40 text-muted-foreground italic">
                              unknown carrier
                            </span>
                          ) : (
                            <span className="text-[10px] px-1.5 py-0.5 rounded bg-muted/40 text-muted-foreground">
                              detecting…
                            </span>
                          )}
                          {carrierList.length > 4 && (
                            <span className="text-[10px] text-muted-foreground">+{carrierList.length - 4}</span>
                          )}
                          {needsValidation > 0 && (
                            <span
                              className="text-[10px] px-1.5 py-0.5 rounded bg-amber-500/15 text-amber-400 border border-amber-500/30"
                              title={`${needsValidation} row${needsValidation === 1 ? "" : "s"} reference a carrier not in the registry — click to review and confirm`}
                            >
                              Validate carrier ({needsValidation})
                            </span>
                          )}
                        </div>
                      </div>
                      <div className={`flex items-center ${isGrid ? "justify-end flex-wrap" : "shrink-0"} gap-1.5`}>
                        {u.status === "done" && (
                          <>
                            <Button variant="ghost" size="sm" className="h-7 text-xs" onClick={() => { store.setActiveUpload(u.id); onViewResults?.(); }}>
                              <Eye className="w-3.5 h-3.5 mr-1" /> View
                            </Button>
                            <Button
                              variant="ghost"
                              size="sm"
                              className="h-7 text-xs text-sky-400 hover:text-sky-300"
                              onClick={() => handleReExtract(u.id, u.projectName)}
                              title="Re-run extraction with the latest prompts/config"
                            >
                              <RefreshCw className="w-3.5 h-3.5 mr-1" /> Re-extract
                            </Button>
                          </>
                        )}
                        {["interrupted", "cancelled", "error"].includes(u.status) && (
                          <Button variant="ghost" size="sm" className="h-7 text-xs" onClick={() => handleReExtract(u.id, u.projectName)}>
                            <RotateCcw className="w-3.5 h-3.5 mr-1" /> Retry
                          </Button>
                        )}
                        {u.status === "selecting" && (
                          <Button variant="ghost" size="sm" className="h-7 text-xs" onClick={() => store.setActiveUpload(u.id)}>
                            <Play className="w-3.5 h-3.5 mr-1" /> Resume
                          </Button>
                        )}
                        {(u.status === "extracting" || u.status === "classifying") && (
                          <Button variant="ghost" size="sm" className="h-7 text-xs" onClick={() => store.setActiveUpload(u.id)}>
                            <Loader2 className="w-3.5 h-3.5 mr-1 animate-spin" /> Open
                          </Button>
                        )}
                        <Button
                          variant="ghost"
                          size="sm"
                          className="h-7 text-xs text-muted-foreground hover:text-foreground"
                          onClick={() => handleDownloadFiles(u.id, u.projectName)}
                          title="Download original uploaded files (ZIP)"
                        >
                          <Download className="w-3.5 h-3.5" />
                        </Button>
                        <Button variant="ghost" size="sm" className="h-7 text-xs text-rose-400 hover:text-rose-300" onClick={() => handleDelete(u.id)}>
                          <Trash2 className="w-3.5 h-3.5" />
                        </Button>
                      </div>
                    </div>
                    {extErrors.length > 0 && (
                      <div className="mt-3 rounded-md border border-amber-500/30 bg-amber-500/5 px-3 py-2">
                        <div className="flex items-start gap-2">
                          <AlertTriangle className="w-3.5 h-3.5 text-amber-400 shrink-0 mt-0.5" />
                          <div className="min-w-0 flex-1">
                            <p className="text-[11px] font-medium text-amber-300">
                              {extErrors.length} file{extErrors.length === 1 ? "" : "s"} produced no rows
                            </p>
                            <ul className="mt-1 space-y-0.5">
                              {extErrors.slice(0, 3).map((e, i) => (
                                <li key={i} className="text-[10px] text-amber-200/80 leading-tight" title={e.reason}>
                                  <span className="font-mono">{e.filename}</span>
                                  {e.carrier && <span className="text-amber-300/60"> · {e.carrier}</span>}
                                  <span className="text-amber-200/60"> — {e.reason.length > 80 ? e.reason.slice(0, 80) + "…" : e.reason}</span>
                                </li>
                              ))}
                              {extErrors.length > 3 && (
                                <li className="text-[10px] text-amber-300/60 italic">+{extErrors.length - 3} more</li>
                              )}
                            </ul>
                          </div>
                        </div>
                      </div>
                    )}
                  </Card>
                );
              })}
            </div>
          </section>
        )}
      </div>
    );
  }

  // Active upload — show carrier selection + extraction
  return (
    <div className="p-8 space-y-6 max-w-5xl mx-auto">
      <div className="flex items-center justify-between">
        <div className="flex items-start gap-3">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => store.setActiveUpload(null)}
            className="h-8 px-2 -ml-2 mt-0.5"
            aria-label="Back to Upload"
          >
            <ArrowLeft className="w-4 h-4" />
          </Button>
          <div>
            <h1 className="text-2xl font-bold tracking-tight">{upload.projectName}</h1>
            <p className="text-muted-foreground text-sm">
              {upload.clientName && <>{upload.clientName} · </>}{upload.files.length} files · {carrierGroups.length} carriers
            </p>
            {upload.description && <p className="text-xs text-muted-foreground/70 mt-0.5">{upload.description}</p>}
          </div>
        </div>
        <div className="flex items-center gap-2">
          {upload.status === "selecting" && (
            <>
              <Button variant="outline" size="sm" onClick={handleReset}><X className="w-4 h-4 mr-1" />Cancel</Button>
              <Button size="sm" className="bg-emerald-600 hover:bg-emerald-500 text-white" onClick={handleExtract} disabled={selectedFileCount === 0}>
                <Play className="w-4 h-4 mr-1" />Extract ({selectedFileCount} files)
              </Button>
            </>
          )}
          <Button
            variant="outline"
            size="sm"
            onClick={() => handleDownloadFiles(upload.id, upload.projectName)}
            title="Download all uploaded source files as a ZIP"
          >
            <Download className="w-4 h-4 mr-1" />Download files
          </Button>
        </div>
      </div>

      {/* Extraction progress with Stop button */}
      {extracting && (
        <Card className="p-4 bg-card/50 border-emerald-500/20">
          <div className="flex items-center justify-between mb-2">
            <div className="flex items-center gap-3">
              <Loader2 className="w-4 h-4 text-emerald-400 animate-spin" />
              <span className="text-sm font-medium">Extracting... {progress}%</span>
            </div>
            <Button variant="destructive" size="sm" onClick={handleCancel}>
              <X className="w-4 h-4 mr-1" /> Stop
            </Button>
          </div>
          <Progress value={progress} className="h-2" />
        </Card>
      )}

      {/* Interrupted / Cancelled / Error banner with Retry */}
      {isInterruptedOrFailed && (
        <Card className="p-5 bg-amber-500/5 border-amber-500/20">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <AlertTriangle className="w-5 h-5 text-amber-400" />
              <div>
                <p className="font-medium">
                  {upload.status === "interrupted" && "Extraction was interrupted (server restart)"}
                  {upload.status === "cancelled" && "Extraction was cancelled"}
                  {upload.status === "error" && "Extraction failed with an error"}
                </p>
              </div>
            </div>
            <div className="flex items-center gap-2">
              <Button variant="outline" size="sm" onClick={handleReset}>Dismiss</Button>
              <Button size="sm" className="bg-amber-600 hover:bg-amber-500 text-white" onClick={handleRetry}>
                <Play className="w-4 h-4 mr-1" /> Retry Extraction
              </Button>
            </div>
          </div>
        </Card>
      )}

      <div className="space-y-3">
        {carrierGroups.map((group) => (
          <Card key={group.carrier} className={`overflow-hidden border-border/50 ${group.selected ? "bg-card/50" : "bg-card/20 opacity-60"}`}>
            <div
              className="flex items-center gap-4 px-5 py-4 cursor-pointer hover:bg-muted/20 transition-colors"
              onClick={() => !extracting && !isInterruptedOrFailed && toggleCarrier(group.carrier)}
            >
              {!extracting && !isInterruptedOrFailed && (
                <div className={`w-5 h-5 rounded border-2 flex items-center justify-center ${group.selected ? "bg-emerald-500 border-emerald-500" : "border-muted-foreground/30"}`}>
                  {group.selected && <CheckCircle2 className="w-3.5 h-3.5 text-white" />}
                </div>
              )}
              <div className="flex-1">
                <div className="flex items-center gap-2">
                  <span className="font-semibold">{group.carrier}</span>
                  <Badge variant={group.configured ? "secondary" : "destructive"} className="text-[10px]">
                    {group.configured ? "Configured" : "Unknown"}
                  </Badge>
                  <span className="text-xs text-muted-foreground">{group.files.length} files</span>
                </div>
              </div>
              {!group.configured && <AlertTriangle className="w-4 h-4 text-amber-400" />}
            </div>
            {group.selected && (
              <div className="px-5 pb-4 border-t border-border/30 mt-0">
                {/* Move all files in this group to another carrier */}
                {!extracting && !isInterruptedOrFailed && (
                  <div className="mt-3 mb-2 flex items-center gap-2 p-2 rounded-lg bg-muted/30 border border-border/30">
                    <MoveRight className="w-4 h-4 text-muted-foreground shrink-0" />
                    <span className="text-xs text-muted-foreground">Move all to:</span>
                    <Select onValueChange={(v: string | null) => {
                      if (v && upload) {
                        group.files.forEach((f) => store.reassignFileCarrier(upload.id, f.name, v));
                        toast.success(`Moved ${group.files.length} files to ${v}`);
                      }
                    }}>
                      <SelectTrigger className="h-7 w-32 text-xs"><SelectValue placeholder="Select carrier" /></SelectTrigger>
                      <SelectContent>
                        {configuredCarriers.filter((c) => c !== group.carrier).map((c) => <SelectItem key={c} value={c}>{c}</SelectItem>)}
                        {group.carrier !== "Unknown" && <SelectItem value="Unknown">Unknown</SelectItem>}
                      </SelectContent>
                    </Select>
                  </div>
                )}
                <div className="mt-3 space-y-1">
                  {group.files.map((f, i) => (
                    <div key={i} className="flex items-center gap-3 text-sm py-1.5">
                      <FileText className={`w-4 h-4 shrink-0 ${fileColors[ext(f.name)] || "text-muted-foreground"}`} />
                      <span className="flex-1 truncate text-muted-foreground">{f.name}</span>
                      <span className="text-xs text-muted-foreground">{(f.size / 1024).toFixed(0)} KB</span>

                      {/* Doc-type override — shown pre-extraction so the user can correct
                          the auto-detected type before the wrong prompt fires. Pre-fills
                          with what the classifier guessed; defaults to "invoice" when blank. */}
                      {!extracting && !isInterruptedOrFailed && f.status === "classified" ? (
                        <Select
                          value={f.docType || "invoice"}
                          onValueChange={(v: string | null) => {
                            if (v && upload) {
                              store.setFileDocType(upload.id, f.name, v);
                              toast.success(`"${f.name}" → ${v}`);
                            }
                          }}
                        >
                          <SelectTrigger className="h-6 w-28 text-[10px]">
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            <SelectItem value="invoice">invoice</SelectItem>
                            <SelectItem value="csr">csr</SelectItem>
                            <SelectItem value="contract">contract</SelectItem>
                            <SelectItem value="report">report</SelectItem>
                            <SelectItem value="did_list">did_list</SelectItem>
                            <SelectItem value="subscription">subscription</SelectItem>
                            <SelectItem value="email">email</SelectItem>
                            <SelectItem value="service_guide">service_guide</SelectItem>
                            <SelectItem value="other">other</SelectItem>
                          </SelectContent>
                        </Select>
                      ) : (
                        f.docType && <Badge variant="secondary" className="text-[10px]">{f.docType}</Badge>
                      )}

                      {/* Per-file carrier reassign */}
                      {!extracting && !isInterruptedOrFailed && f.status === "classified" && (
                        <Select onValueChange={(v: string | null) => {
                          if (v && upload) {
                            store.reassignFileCarrier(upload.id, f.name, v);
                            toast.success(`Moved "${f.name}" to ${v}`);
                          }
                        }}>
                          <SelectTrigger className="h-6 w-28 text-[10px]">
                            <SelectValue placeholder="Move to..." />
                          </SelectTrigger>
                          <SelectContent>
                            {configuredCarriers.filter((c) => c !== group.carrier).map((c) => <SelectItem key={c} value={c}>{c}</SelectItem>)}
                            {group.carrier !== "Unknown" && <SelectItem value="Unknown">Unknown</SelectItem>}
                          </SelectContent>
                        </Select>
                      )}

                      {/* Copy to another carrier */}
                      {!extracting && !isInterruptedOrFailed && f.status === "classified" && (
                        <Select onValueChange={(v: string | null) => {
                          if (v && upload) {
                            store.copyFileToCarrier(upload.id, f.name, v);
                            toast.success(`Copied "${f.name}" to ${v} (will extract under both carriers)`);
                          }
                        }}>
                          <SelectTrigger className="h-6 w-24 text-[10px]">
                            <SelectValue placeholder="Copy to..." />
                          </SelectTrigger>
                          <SelectContent>
                            {configuredCarriers.filter((c) => c !== group.carrier).map((c) => <SelectItem key={c} value={c}>{c}</SelectItem>)}
                          </SelectContent>
                        </Select>
                      )}

                      {f.status === "extracting" && <Loader2 className="w-3.5 h-3.5 text-amber-400 animate-spin" />}
                      {f.status === "done" && <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400" />}
                      {f.status === "skipped" && <X className="w-3.5 h-3.5 text-zinc-500" />}
                    </div>
                  ))}
                </div>
              </div>
            )}
          </Card>
        ))}
      </div>
    </div>
  );
}
