"use client";

import { useCallback, useEffect, useState } from "react";
import { motion } from "framer-motion";
import { Trash2, RotateCcw, Download, AlertTriangle, Inbox } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import {
  apiListBin,
  apiRestoreUpload,
  apiPurgeUpload,
  apiExportExcel,
  type UploadSummary,
} from "@/lib/api";

function formatWhen(iso?: string | null): string {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    return d.toLocaleString();
  } catch {
    return iso;
  }
}

export function BinPage() {
  const [items, setItems] = useState<UploadSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const { uploads } = await apiListBin();
      setItems(uploads);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const onRestore = async (id: string) => {
    setBusyId(id);
    try {
      await apiRestoreUpload(id);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyId(null);
    }
  };

  const onPurge = async (id: string) => {
    const ok = window.confirm(
      "Permanently delete this project and all its extracted data? This cannot be undone.",
    );
    if (!ok) return;
    setBusyId(id);
    try {
      await apiPurgeUpload(id);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyId(null);
    }
  };

  const onEmptyBin = async () => {
    if (items.length === 0) return;
    const ok = window.confirm(
      `Permanently delete all ${items.length} project${items.length === 1 ? "" : "s"} in the bin? This cannot be undone.`,
    );
    if (!ok) return;
    setBusyId("__all__");
    setError(null);
    try {
      // Purge sequentially to avoid hammering the backend; slight UX wait acceptable for a destructive action.
      for (const u of items) {
        await apiPurgeUpload(u.upload_id);
      }
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyId(null);
    }
  };

  const onDownload = async (id: string, name: string) => {
    setBusyId(id);
    try {
      const blob = await apiExportExcel(id);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${name || id}.xlsx`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="h-full overflow-auto">
      <div className="mx-auto max-w-5xl px-6 py-8 space-y-6">
        <header className="flex items-start justify-between gap-4">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight flex items-center gap-2">
              <Trash2 className="w-6 h-6 text-rose-400" />
              Bin
            </h1>
            <p className="text-sm text-muted-foreground mt-1">
              Deleted projects stay here so you can download their data or restore them.
              Purging a project removes everything permanently.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={refresh} disabled={loading}>
              Refresh
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={onEmptyBin}
              disabled={loading || items.length === 0 || busyId !== null}
              className="text-rose-400 border-rose-500/30 hover:bg-rose-500/10 hover:text-rose-300"
              title="Permanently delete every project in the bin"
            >
              <Trash2 className="w-4 h-4 mr-1.5" />
              Empty bin {items.length > 0 && `(${items.length})`}
            </Button>
          </div>
        </header>

        <Separator className="opacity-50" />

        {error && (
          <div className="rounded-lg border border-rose-500/40 bg-rose-500/10 px-4 py-3 text-sm text-rose-300 flex items-start gap-2">
            <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />
            <span>{error}</span>
          </div>
        )}

        {loading ? (
          <div className="py-20 text-center text-sm text-muted-foreground">Loading…</div>
        ) : items.length === 0 ? (
          <div className="py-20 text-center space-y-2">
            <Inbox className="w-10 h-10 mx-auto text-muted-foreground/60" />
            <p className="text-sm text-muted-foreground">The bin is empty.</p>
          </div>
        ) : (
          <ul className="space-y-3">
            {items.map((u, i) => (
              <motion.li
                key={u.upload_id}
                initial={{ opacity: 0, y: 4 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: i * 0.02 }}
                className="neu rounded-xl px-5 py-4 flex items-center justify-between gap-4"
              >
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 min-w-0">
                    <span className="font-medium truncate">
                      {u.project_name || u.upload_id}
                    </span>
                    {u.client_name && (
                      <span className="text-xs text-muted-foreground">· {u.client_name}</span>
                    )}
                  </div>
                  <div className="text-xs text-muted-foreground mt-1 space-x-3">
                    <span>{u.files_total} file{u.files_total === 1 ? "" : "s"}</span>
                    <span>{u.total_rows} row{u.total_rows === 1 ? "" : "s"}</span>
                    <span>deleted {formatWhen(u.deleted_at)}</span>
                  </div>
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  {u.total_rows > 0 && (
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => onDownload(u.upload_id, u.project_name)}
                      disabled={busyId === u.upload_id}
                    >
                      <Download className="w-4 h-4 mr-1.5" />
                      Download
                    </Button>
                  )}
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => onRestore(u.upload_id)}
                    disabled={busyId === u.upload_id}
                  >
                    <RotateCcw className="w-4 h-4 mr-1.5" />
                    Restore
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    className="text-rose-400 hover:text-rose-300 hover:bg-rose-500/10"
                    onClick={() => onPurge(u.upload_id)}
                    disabled={busyId === u.upload_id}
                  >
                    <Trash2 className="w-4 h-4 mr-1.5" />
                    Purge
                  </Button>
                </div>
              </motion.li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
