import { create } from "zustand";
import { persist } from "zustand/middleware";

export interface UploadFile {
  file: File;
  name: string;
  size: number;
  carrier: string | null;
  docType: string | null;
  status: "classified" | "extracting" | "done" | "skipped" | "error" | "interrupted" | "cancelled";
  pdfUrl?: string; // blob URL for PDF viewing
}

export interface ExtractedRow {
  id: string;
  sourceFile: string;
  carrier: string;
  confidence: "high" | "medium" | "low";
  // All 60 fields stored as-is from API (snake_case)
  row_type?: string | null;
  status?: string | null;
  notes?: string | null;
  contract_info_received?: string | null;
  invoice_file_name?: string | null;
  files_used?: string | null;
  billing_name?: string | null;
  service_address_1?: string | null;
  service_address_2?: string | null;
  city?: string | null;
  state?: string | null;
  zip?: string | null;
  country?: string | null;
  carrier_name?: string | null;
  master_account?: string | null;
  carrier_account_number?: string | null;
  sub_account_number_1?: string | null;
  sub_account_number_2?: string | null;
  btn?: string | null;
  phone_number?: string | null;
  carrier_circuit_number?: string | null;
  additional_circuit_ids?: string | null;
  service_type?: string | null;
  service_type_2?: string | null;
  usoc?: string | null;
  service_or_component?: string | null;
  component_or_feature_name?: string | null;
  monthly_recurring_cost?: number | null;
  quantity?: number | null;
  cost_per_unit?: number | null;
  currency?: string | null;
  conversion_rate?: number | null;
  mrc_per_currency?: number | null;
  charge_type?: string | null;
  num_calls?: number | null;
  ld_minutes?: number | null;
  ld_cost?: number | null;
  rate?: number | null;
  ld_flat_rate?: number | null;
  point_to_number?: string | null;
  port_speed?: string | null;
  access_speed?: string | null;
  upload_speed?: string | null;
  z_location_name?: string | null;
  z_address_1?: string | null;
  z_address_2?: string | null;
  z_city?: string | null;
  z_state?: string | null;
  z_zip?: string | null;
  z_country?: string | null;
  contract_term_months?: number | null;
  contract_begin_date?: string | null;
  contract_expiration_date?: string | null;
  billing_per_contract?: string | null;
  currently_month_to_month?: string | null;
  mtm_or_less_than_year?: string | null;
  contract_file_name?: string | null;
  contract_number?: string | null;
  contract_number_2?: string | null;
  auto_renew?: string | null;
  auto_renewal_notes?: string | null;
  field_confidence?: Record<string, string>;
  [key: string]: unknown;
}

export interface Upload {
  id: string;
  projectName: string;
  description: string;
  clientName: string;
  createdAt: Date;
  status: "classifying" | "selecting" | "extracting" | "done" | "error" | "interrupted" | "cancelled";
  files: UploadFile[];
  results: ExtractedRow[];
  totalRows: number;
  totalCost: number;
  carriers: string[];
  // Summary stats surfaced on Previous Uploads cards (set by backend post-extraction)
  rowsWithIssues?: number;
  uniqueAccounts?: number;
  rowsNeedingCarrierValidation?: number;
}

interface ClassifiedFileFromAPI {
  filename: string;
  carrier: string | null;
  doc_type: string | null;
  format_variant: string | null;
  file_size: number;
}

interface AppStore {
  // Uploads
  uploads: Upload[];
  activeUploadId: string | null;
  selectedRowId: string | null;

  // Draft form fields — survive page navigation + browser refresh
  draftProjectName: string;
  draftClientName: string;
  draftDescription: string;
  setDraftField: (field: "draftProjectName" | "draftClientName" | "draftDescription", value: string) => void;
  clearDraftFields: () => void;

  // Actions
  createUploadFromAPI: (
    uploadId: string,
    projectName: string,
    description: string,
    clientName: string,
    classifiedFiles: ClassifiedFileFromAPI[],
    rawFiles: File[],
  ) => void;
  updateUploadStatus: (id: string, status: Upload["status"]) => void;
  updateFileStatus: (uploadId: string, fileName: string, status: UploadFile["status"]) => void;
  addResults: (uploadId: string, rows: ExtractedRow[]) => void;
  reassignFileCarrier: (uploadId: string, fileName: string, newCarrier: string) => void;
  copyFileToCarrier: (uploadId: string, fileName: string, additionalCarrier: string) => void;
  deleteUpload: (uploadId: string) => void;
  setActiveUpload: (id: string | null) => void;
  setSelectedRow: (id: string | null) => void;
  getActiveUpload: () => Upload | undefined;
  getSelectedRow: () => ExtractedRow | undefined;
  updateRowField: (rowId: string, fieldName: string, value: string) => void;
  loadUploadsFromAPI: () => Promise<void>;
  restoreUploadResults: (uploadId: string, rows: ExtractedRow[]) => void;
}

export const useAppStore = create<AppStore>()(
  persist(
    (set, get) => ({
  uploads: [],
  activeUploadId: null,
  selectedRowId: null,

  draftProjectName: "",
  draftClientName: "",
  draftDescription: "",
  setDraftField: (field, value) => set({ [field]: value }),
  clearDraftFields: () => set({ draftProjectName: "", draftClientName: "", draftDescription: "" }),

  createUploadFromAPI: (uploadId, projectName, description, clientName, classifiedFiles, rawFiles) => {
    const rawFileMap = new Map(rawFiles.map((f) => [f.name, f]));
    const files: UploadFile[] = classifiedFiles.map((cf) => {
      const raw = rawFileMap.get(cf.filename);
      return {
        file: raw || new File([], cf.filename),
        name: cf.filename,
        size: cf.file_size,
        carrier: cf.carrier,
        docType: cf.doc_type,
        status: "classified" as const,
        pdfUrl: raw?.type === "application/pdf" ? URL.createObjectURL(raw) : undefined,
      };
    });

    const carriers = [...new Set(files.map((f) => f.carrier).filter(Boolean))] as string[];

    const upload: Upload = {
      id: uploadId,
      projectName: projectName || `Upload ${new Date().toLocaleDateString()}`,
      description,
      clientName,
      createdAt: new Date(),
      status: "selecting",
      files,
      results: [],
      totalRows: 0,
      totalCost: 0,
      carriers,
    };

    set((state) => ({ uploads: [upload, ...state.uploads], activeUploadId: uploadId }));
  },

  updateUploadStatus: (id, status) => {
    set((state) => ({
      uploads: state.uploads.map((u) => (u.id === id ? { ...u, status } : u)),
    }));
  },

  updateFileStatus: (uploadId, fileName, status) => {
    set((state) => ({
      uploads: state.uploads.map((u) =>
        u.id === uploadId
          ? { ...u, files: u.files.map((f) => (f.name === fileName ? { ...f, status } : f)) }
          : u
      ),
    }));
  },

  addResults: (uploadId, rows) => {
    set((state) => ({
      uploads: state.uploads.map((u) =>
        u.id === uploadId
          ? { ...u, results: [...u.results, ...rows], totalRows: u.totalRows + rows.length, status: "done" as const }
          : u
      ),
    }));
  },

  reassignFileCarrier: (uploadId, fileName, newCarrier) => {
    set((state) => ({
      uploads: state.uploads.map((u) => {
        if (u.id !== uploadId) return u;
        const updatedFiles = u.files.map((f) =>
          f.name === fileName ? { ...f, carrier: newCarrier } : f
        );
        const carriers = [...new Set(updatedFiles.map((f) => f.carrier).filter(Boolean))] as string[];
        return { ...u, files: updatedFiles, carriers };
      }),
    }));
  },

  copyFileToCarrier: (uploadId, fileName, additionalCarrier) => {
    set((state) => ({
      uploads: state.uploads.map((u) => {
        if (u.id !== uploadId) return u;
        const original = u.files.find((f) => f.name === fileName);
        if (!original) return u;
        // Create a copy with the new carrier assignment
        const copy: UploadFile = {
          ...original,
          carrier: additionalCarrier,
          name: `${original.name}`, // Same name, different carrier
        };
        const updatedFiles = [...u.files, copy];
        const carriers = [...new Set(updatedFiles.map((f) => f.carrier).filter(Boolean))] as string[];
        return { ...u, files: updatedFiles, carriers };
      }),
    }));
  },

  updateRowField: (rowId, fieldName, value) => {
    set((state) => ({
      uploads: state.uploads.map((u) => ({
        ...u,
        results: u.results.map((r) =>
          r.id === rowId ? { ...r, [fieldName]: value } : r
        ),
      })),
    }));
  },

  deleteUpload: (uploadId) => {
    set((state) => ({
      uploads: state.uploads.filter((u) => u.id !== uploadId),
      activeUploadId: state.activeUploadId === uploadId ? null : state.activeUploadId,
    }));
  },

  setActiveUpload: (id) => set({ activeUploadId: id }),
  setSelectedRow: (id) => set({ selectedRowId: id }),

  getActiveUpload: () => {
    const state = get();
    return state.uploads.find((u) => u.id === state.activeUploadId);
  },

  getSelectedRow: () => {
    const state = get();
    const upload = state.uploads.find((u) => u.id === state.activeUploadId);
    return upload?.results.find((r) => r.id === state.selectedRowId);
  },

  loadUploadsFromAPI: async () => {
    try {
      const { apiListUploads, apiGetResults } = await import("@/lib/api");
      const { uploads: summaries } = await apiListUploads();

      const restored: Upload[] = [];
      for (const s of summaries) {
        // Skip if already in store
        if (get().uploads.find((u) => u.id === s.upload_id)) continue;

        const statusMap: Record<string, Upload["status"]> = {
          classified: "selecting",
          extracting: "extracting",
          done: "done",
          error: "error",
          interrupted: "interrupted",
          cancelled: "cancelled",
          cancel_requested: "extracting",
        };

        // Restore files from classified data
        const files: UploadFile[] = (s.classified || []).map((cf) => ({
          file: new File([], cf.filename),
          name: cf.filename,
          size: cf.file_size,
          carrier: cf.carrier,
          docType: cf.doc_type,
          status: "classified" as const,
        }));
        // Use LLM-computed carriers from the backend (cleans out "Unknown")
        // when available; otherwise fall back to the classify-stage values.
        const fallbackCarriers = [...new Set(files.map((f) => f.carrier).filter(Boolean))] as string[];
        const carriers = (s.carriers && s.carriers.length > 0)
          ? s.carriers
          : fallbackCarriers.filter((c) => c && c.toLowerCase() !== "unknown");

        const upload: Upload = {
          id: s.upload_id,
          projectName: s.project_name || `Upload ${s.upload_id}`,
          description: "",
          clientName: s.client_name || "",
          createdAt: s.created_at ? new Date(s.created_at) : new Date(),
          status: statusMap[s.status] || "selecting",
          files,
          results: [],
          totalRows: s.total_rows,
          totalCost: 0,
          carriers,
          rowsWithIssues: s.rows_with_issues,
          uniqueAccounts: s.unique_accounts,
          rowsNeedingCarrierValidation: s.rows_needing_carrier_validation,
        };

        // If done, load results
        if (s.status === "done" && s.total_rows > 0) {
          try {
            const { rows } = await apiGetResults(s.upload_id);
            const { mapAPIRowToStore } = await import("@/components/pages/upload");
            upload.results = rows.map(mapAPIRowToStore);
            upload.totalRows = upload.results.length;
            upload.carriers = [...new Set(upload.results.map((r) => r.carrier))];
          } catch (err) {
            console.error(`Failed to restore results for ${s.upload_id}:`, err);
          }
        }

        restored.push(upload);
      }

      if (restored.length > 0) {
        set((state) => ({
          uploads: [...state.uploads, ...restored],
        }));
      }
    } catch (err) {
      console.error("Failed to load uploads from API:", err);
    }
  },

  restoreUploadResults: (uploadId, rows) => {
    set((state) => ({
      uploads: state.uploads.map((u) =>
        u.id === uploadId
          ? { ...u, results: rows, totalRows: rows.length, status: "done" as const }
          : u
      ),
    }));
  },
    }),
    {
      name: "dd-upload-store",
      // Only persist lightweight fields — uploads are restored from Redis via loadUploadsFromAPI
      partialize: (state) => ({
        draftProjectName: state.draftProjectName,
        draftClientName: state.draftClientName,
        draftDescription: state.draftDescription,
        activeUploadId: state.activeUploadId,
      }),
    },
  ),
);
