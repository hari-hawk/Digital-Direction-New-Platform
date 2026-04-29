"use client";

import { useState } from "react";
import { ResizableHandle, ResizablePanel, ResizablePanelGroup } from "@/components/ui/resizable";
import { ChevronLeft, ChevronRight, CheckCircle2, AlertCircle, HelpCircle, Eye, Loader2, ArrowLeft } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { toast } from "sonner";
import { useAppStore } from "@/lib/store";
import { apiSubmitCorrection, apiBulkApprove } from "@/lib/api";
import dynamic from "next/dynamic";

const PdfViewer = dynamic(() => import("@/components/pdf-viewer").then((m) => ({ default: m.PdfViewer })), { ssr: false });

const confConfig: Record<string, { icon: typeof CheckCircle2; color: string; bg: string; label: string }> = {
  high: { icon: CheckCircle2, color: "text-emerald-400", bg: "bg-emerald-500/10", label: "Verified" },
  medium: { icon: AlertCircle, color: "text-amber-400", bg: "bg-amber-500/10", label: "Review" },
  low: { icon: HelpCircle, color: "text-rose-400", bg: "bg-rose-500/10", label: "Low" },
};

const displayFields = [
  { group: "Row Info", fields: ["row_type", "status", "notes", "invoice_file_name", "files_used"] },
  { group: "Carrier", fields: ["carrier", "sourceFile", "carrier_name", "carrier_account_number", "master_account", "sub_account_number_1", "sub_account_number_2", "btn"] },
  { group: "Billing & Location", fields: ["billing_name", "service_address_1", "service_address_2", "city", "state", "zip", "country"] },
  { group: "Service", fields: ["phone_number", "carrier_circuit_number", "additional_circuit_ids", "service_type", "service_type_2"] },
  { group: "Component", fields: ["usoc", "service_or_component", "component_or_feature_name", "monthly_recurring_cost", "quantity", "cost_per_unit", "currency", "conversion_rate", "mrc_per_currency"] },
  { group: "LD & Usage", fields: ["charge_type", "num_calls", "ld_minutes", "ld_cost", "rate", "ld_flat_rate", "point_to_number"] },
  { group: "Circuit Speed", fields: ["port_speed", "access_speed", "upload_speed"] },
  { group: "Z Location", fields: ["z_location_name", "z_address_1", "z_address_2", "z_city", "z_state", "z_zip", "z_country"] },
  { group: "Contract", fields: ["contract_info_received", "contract_term_months", "contract_begin_date", "contract_expiration_date", "billing_per_contract", "currently_month_to_month", "mtm_or_less_than_year", "contract_file_name", "contract_number", "contract_number_2", "auto_renew", "auto_renewal_notes"] },
];

function formatField(name: string): string {
  return name.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()).replace("Mrc", "MRC");
}

interface ReviewPageProps {
  onBack?: () => void;
}

export function ReviewPage({ onBack }: ReviewPageProps = {}) {
  const { getActiveUpload, selectedRowId, setSelectedRow, updateRowField } = useAppStore();
  const upload = getActiveUpload();
  const rows = upload?.results || [];
  const currentIndex = rows.findIndex((r) => r.id === selectedRowId);
  const row = currentIndex >= 0 ? rows[currentIndex] : rows[0];
  const [editingField, setEditingField] = useState<string | null>(null);
  const [editValue, setEditValue] = useState("");
  const [oldValue, setOldValue] = useState<string | null>(null);
  const [filter, setFilter] = useState("all");
  const [saving, setSaving] = useState(false);
  const [approving, setApproving] = useState(false);

  // Find the source file's PDF URL — use blob URL if available, otherwise serve from backend.
  // Note: extension check is case-INSENSITIVE so files like "COXBUS~1.PDF"
  // (Windows 8.3 short filenames are upper-case) still resolve. The previous
  // case-sensitive `.endsWith(".pdf")` check returned false for any uppercase
  // .PDF and showed the "No document selected" placeholder even when a row
  // was clearly selected.
  const sourceFile = upload?.files.find((f) => f.name === row?.sourceFile);
  const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
  const pdfUrl = sourceFile?.pdfUrl
    || (upload && row?.sourceFile && row.sourceFile.toLowerCase().endsWith(".pdf")
      ? `${API_BASE}/api/uploads/${upload.id}/files/${encodeURIComponent(row.sourceFile)}`
      : undefined);

  if (!row) {
    return (
      <div className="h-full flex flex-col">
        {onBack && (
          <div className="px-6 py-3 border-b border-border/50">
            <Button variant="ghost" size="sm" onClick={onBack} className="h-8 gap-2 -ml-2">
              <ArrowLeft className="w-4 h-4" />
              Back to Results
            </Button>
          </div>
        )}
        <div className="flex-1 flex items-center justify-center text-muted-foreground">
          <div className="text-center">
            <Eye className="w-12 h-12 mx-auto mb-4 opacity-30" />
            <p className="font-medium">No row selected</p>
            <p className="text-sm mt-1">Click a row in Results to review it here</p>
          </div>
        </div>
      </div>
    );
  }

  const navigate = (dir: -1 | 1) => {
    const next = currentIndex + dir;
    if (next >= 0 && next < rows.length) setSelectedRow(rows[next].id);
  };

  const startEditing = (fieldName: string) => {
    const currentValue = (row as Record<string, unknown>)[fieldName];
    setEditingField(fieldName);
    setEditValue(String(currentValue ?? ""));
    setOldValue(currentValue != null ? String(currentValue) : null);
  };

  const handleSave = async (field: string) => {
    setSaving(true);
    try {
      await apiSubmitCorrection(row.id, field, oldValue, editValue);
      updateRowField(row.id, field, editValue);
      toast.success(`Corrected: ${formatField(field)}`);
      setEditingField(null);
    } catch (e) {
      toast.error(`Failed to save: ${e instanceof Error ? e.message : "Unknown error"}`);
    } finally {
      setSaving(false);
    }
  };

  const handleApprove = async () => {
    if (!upload) return;
    setApproving(true);
    try {
      await apiBulkApprove(upload.id, [row.id]);
      toast.success("Row approved");
      navigate(1);
    } catch (e) {
      toast.error(`Approve failed: ${e instanceof Error ? e.message : "Unknown error"}`);
    } finally {
      setApproving(false);
    }
  };

  return (
    <div className="h-full flex flex-col">
      {/* Top bar */}
      <div className="flex items-center justify-between px-6 py-3 border-b border-border/50 bg-card/30 shrink-0">
        <div className="flex items-center gap-3">
          {onBack && (
            <Button variant="ghost" size="sm" onClick={onBack} className="h-8 px-2 -ml-2" aria-label="Back to Results">
              <ArrowLeft className="w-4 h-4" />
            </Button>
          )}
          <h1 className="text-lg font-semibold">Review</h1>
          <Badge variant="secondary" className="font-mono text-xs">
            {currentIndex + 1} / {rows.length}
          </Badge>
          <Badge className={row.row_type === "S" ? "bg-blue-500/20 text-blue-400 text-xs" : "bg-violet-500/20 text-violet-400 text-xs"}>
            {row.row_type === "S" ? "Service" : "Component"}
          </Badge>
          <span className="text-xs text-muted-foreground">{row.carrier} · {row.sourceFile}</span>
        </div>
        <div className="flex items-center gap-2">
          <Select value={filter} onValueChange={(v) => setFilter(v ?? "all")}>
            <SelectTrigger className="w-32 h-8 text-xs"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All fields</SelectItem>
              <SelectItem value="review">Needs review</SelectItem>
            </SelectContent>
          </Select>
          <Button
            size="sm"
            className="h-8 bg-emerald-600 hover:bg-emerald-500 text-white"
            onClick={handleApprove}
            disabled={approving}
          >
            {approving ? <Loader2 className="w-3.5 h-3.5 mr-1.5 animate-spin" /> : <CheckCircle2 className="w-3.5 h-3.5 mr-1.5" />}
            Approve
          </Button>
        </div>
      </div>

      {/* Split pane */}
      <ResizablePanelGroup className="flex-1">
        <ResizablePanel defaultSize={45} minSize={30}>
          <div className="h-full flex flex-col">
            <div className="px-4 py-3 border-b border-border/50 flex items-center gap-2">
              <Eye className="w-4 h-4 text-muted-foreground" />
              <span className="text-sm font-medium">Source: {row.sourceFile}</span>
            </div>
            <div className="flex-1 overflow-hidden">
              <PdfViewer url={pdfUrl} />
            </div>
          </div>
        </ResizablePanel>

        <ResizableHandle className="w-1 bg-border/30 hover:bg-emerald-500/50 transition-colors" />

        <ResizablePanel defaultSize={55} minSize={35}>
          {/* Wrap in a flex column so the ScrollArea takes the height left over
              after the nav bar — without this the `h-full` ScrollArea claims the
              entire panel height including the nav, pushing its scrollable area
              below the visible viewport so users can't scroll to the last fields
              (Matt's image011 report). The `min-h-0` on the scroller's flex parent
              is what actually lets the inner content overflow correctly. */}
          <div className="h-full flex flex-col">
            {/* Navigation — pinned at top */}
            <div className="px-4 py-3 border-b border-border/50 flex items-center justify-between shrink-0">
              <Button variant="outline" size="sm" disabled={currentIndex <= 0} onClick={() => navigate(-1)}>
                <ChevronLeft className="w-4 h-4 mr-1" />Previous
              </Button>
              <span className="text-xs text-muted-foreground">{currentIndex + 1} of {rows.length}</span>
              <Button variant="outline" size="sm" disabled={currentIndex >= rows.length - 1} onClick={() => navigate(1)}>
                Next<ChevronRight className="w-4 h-4 ml-1" />
              </Button>
            </div>
            <div className="flex-1 min-h-0">
              <ScrollArea className="h-full">
                <div className="p-4 space-y-6 pb-12">
              {displayFields.map((group) => (
                <div key={group.group}>
                  <h3 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-3">{group.group}</h3>
                  <div className="space-y-1">
                    {group.fields.map((fieldName) => {
                      const value = (row as Record<string, unknown>)[fieldName];
                      // Per-field confidence: prefer field-level, fall back to row-level
                      const conf = row.field_confidence?.[fieldName] || row.confidence || "high";
                      const style = confConfig[conf] || confConfig.high;
                      const Icon = style.icon;
                      const isEditing = editingField === fieldName;

                      if (filter === "review" && conf !== "medium" && conf !== "low") return null;

                      return (
                        <div
                          key={fieldName}
                          className={`flex items-center gap-3 px-3 py-2 rounded-lg cursor-pointer transition-colors ${isEditing ? `${style.bg} border border-current/20` : "hover:bg-muted/30"}`}
                          onClick={() => { if (!isEditing) startEditing(fieldName); }}
                        >
                          <Icon className={`w-4 h-4 shrink-0 ${style.color}`} />
                          <div className="flex-1 min-w-0">
                            <p className="text-[11px] text-muted-foreground">{formatField(fieldName)}</p>
                            {isEditing ? (
                              <div className="flex items-center gap-2 mt-1">
                                <Input
                                  value={editValue} onChange={(e) => setEditValue(e.target.value)}
                                  className="h-7 text-sm bg-background/50" autoFocus
                                  onKeyDown={(e) => { if (e.key === "Enter") handleSave(fieldName); if (e.key === "Escape") setEditingField(null); }}
                                />
                                <Button size="sm" className="h-7 px-2 text-xs bg-emerald-600" onClick={() => handleSave(fieldName)} disabled={saving}>
                                  {saving ? <Loader2 className="w-3 h-3 animate-spin" /> : "Save"}
                                </Button>
                              </div>
                            ) : (
                              <p className={`text-sm font-medium truncate ${!value ? "text-zinc-600 italic" : ""}`}>
                                {["monthly_recurring_cost", "cost_per_unit", "ld_cost", "rate", "ld_flat_rate", "mrc_per_currency"].includes(fieldName) && value != null ? `$${Number(value).toFixed(2)}` : value != null ? String(value) : "—"}
                              </p>
                            )}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                  <Separator className="mt-4 opacity-30" />
                </div>
              ))}
                </div>
              </ScrollArea>
            </div>
          </div>
        </ResizablePanel>
      </ResizablePanelGroup>
    </div>
  );
}
