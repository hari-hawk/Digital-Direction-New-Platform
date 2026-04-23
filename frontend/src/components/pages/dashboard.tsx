"use client";

import { useState, useEffect } from "react";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import {
  LayoutDashboard,
  FileText,
  DollarSign,
  CheckCircle2,
  AlertCircle,
  HelpCircle,
  Loader2,
  Rows3,
  Activity,
  Wallet,
  Zap,
  Satellite,
  Trash2,
} from "lucide-react";
import {
  apiGetDashboardStats,
  apiGetDashboardLive,
  type DashboardStats,
  type DashboardLive,
} from "@/lib/api";

interface DashboardPageProps {
  onViewUpload?: (id: string) => void;
}

export function DashboardPage({ onViewUpload }: DashboardPageProps) {
  const [stats, setStats] = useState<DashboardStats | null>(null);
  const [live, setLive] = useState<DashboardLive | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([apiGetDashboardStats(), apiGetDashboardLive()])
      .then(([s, l]) => {
        setStats(s);
        setLive(l);
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  // Refresh live panel every 10s so in-flight projects update without a full page reload.
  useEffect(() => {
    const t = setInterval(() => {
      apiGetDashboardLive().then(setLive).catch(() => {});
    }, 10_000);
    return () => clearInterval(t);
  }, []);

  if (loading) {
    return (
      <div className="p-8 space-y-8 max-w-7xl mx-auto">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Dashboard</h1>
          <p className="text-muted-foreground text-sm mt-1">Extraction pipeline overview</p>
        </div>
        <div className="flex items-center gap-2 text-muted-foreground py-12 justify-center">
          <Loader2 className="w-4 h-4 animate-spin" />
          <span className="text-sm">Loading metrics...</span>
        </div>
      </div>
    );
  }

  if (error || !stats || stats.rows.total === 0) {
    return (
      <div className="p-8 space-y-8 max-w-7xl mx-auto">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Dashboard</h1>
          <p className="text-muted-foreground text-sm mt-1">Extraction pipeline overview</p>
        </div>
        <Card className="p-12 bg-card/50 border-border/50 text-center">
          <LayoutDashboard className="w-10 h-10 mx-auto mb-4 text-muted-foreground/30" />
          <p className="text-lg font-medium text-muted-foreground">
            {error ? "Couldn't load metrics" : "No data yet"}
          </p>
          <p className="text-sm text-muted-foreground/70 mt-1">
            {error ? error : "Run an extraction to see dashboard metrics."}
          </p>
        </Card>
      </div>
    );
  }

  const confidenceHigh = stats.confidence["high"] || 0;
  const confidenceMedium = stats.confidence["medium"] || 0;
  const confidenceLow = stats.confidence["low"] || 0;
  const confidenceTotal = confidenceHigh + confidenceMedium + confidenceLow || 1;

  const reviewPending = stats.review_status["pending"] || 0;
  const reviewApproved = stats.review_status["approved"] || 0;
  const reviewCorrected = stats.review_status["corrected"] || 0;

  const totalDocs = stats.extraction_runs.total_documents;
  const totalRows = stats.rows.total;
  const totalCost = stats.extraction_runs.total_cost_usd || live?.spend.total_usd || 0;
  const avgRowsPerDoc = totalDocs > 0 ? totalRows / totalDocs : 0;
  const costPerRow = totalRows > 0 ? totalCost / totalRows : 0;

  const fmtDuration = (secs: number) => {
    if (secs < 60) return `${secs}s`;
    if (secs < 3600) return `${Math.floor(secs / 60)}m ${secs % 60}s`;
    return `${Math.floor(secs / 3600)}h ${Math.floor((secs % 3600) / 60)}m`;
  };

  return (
    <div className="p-8 space-y-6 max-w-7xl mx-auto">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Dashboard</h1>
        <p className="text-muted-foreground text-sm mt-1">Extraction pipeline overview</p>
      </div>

      {/* Live Operations strip — real-time Redis/spend/config state, refreshes every 10s */}
      {live && (
        <div className="grid grid-cols-3 gap-4">
          <Card className="neu rounded-xl p-5">
            <div className="flex items-center gap-3 mb-3">
              <div className="flex items-center justify-center w-9 h-9 rounded-lg bg-amber-500/10">
                {live.active.count > 0 ? (
                  <Loader2 className="w-4 h-4 text-amber-400 animate-spin" />
                ) : (
                  <Zap className="w-4 h-4 text-amber-400" />
                )}
              </div>
              <div className="flex-1">
                <span className="text-sm text-muted-foreground">Currently processing</span>
                <div className="flex items-baseline gap-1.5">
                  <span className="text-xl font-bold">{live.active.count}</span>
                  <span className="text-xs text-muted-foreground">
                    project{live.active.count === 1 ? "" : "s"}
                    {live.active.files_in_flight > 0 && ` · ${live.active.files_in_flight} files`}
                  </span>
                </div>
              </div>
            </div>
            {live.active.count > 0 ? (
              <p className="text-xs text-muted-foreground">
                Oldest running for {fmtDuration(live.active.oldest_age_seconds)}
              </p>
            ) : (
              <p className="text-xs text-muted-foreground">
                {live.completed_count} completed · {live.failed_count} failed · {live.bin_count} in bin
              </p>
            )}
          </Card>

          <Card className="neu rounded-xl p-5">
            <div className="flex items-center gap-3 mb-3">
              <div
                className={`flex items-center justify-center w-9 h-9 rounded-lg ${
                  live.spend.status === "danger"
                    ? "bg-rose-500/10"
                    : live.spend.status === "warn"
                      ? "bg-amber-500/10"
                      : "bg-emerald-500/10"
                }`}
              >
                <Wallet
                  className={`w-4 h-4 ${
                    live.spend.status === "danger"
                      ? "text-rose-400"
                      : live.spend.status === "warn"
                        ? "text-amber-400"
                        : "text-emerald-400"
                  }`}
                />
              </div>
              <div className="flex-1">
                <span className="text-sm text-muted-foreground">LLM spend</span>
                <div className="flex items-baseline gap-1.5">
                  <span className="text-xl font-bold tabular-nums">
                    ${live.spend.total_usd.toFixed(2)}
                  </span>
                  <span className="text-xs text-muted-foreground">
                    of ${live.spend.cap_usd.toFixed(0)} cap
                  </span>
                </div>
              </div>
            </div>
            <div className="h-1.5 rounded-full bg-muted overflow-hidden">
              <div
                className={`h-full transition-all ${
                  live.spend.status === "danger"
                    ? "bg-rose-400"
                    : live.spend.status === "warn"
                      ? "bg-amber-400"
                      : "bg-emerald-400"
                }`}
                style={{ width: `${Math.min(live.spend.pct_used, 100)}%` }}
              />
            </div>
          </Card>

          <Card className="neu rounded-xl p-5">
            <div className="flex items-center gap-3 mb-3">
              <div className="flex items-center justify-center w-9 h-9 rounded-lg bg-sky-500/10">
                <Satellite className="w-4 h-4 text-sky-400" />
              </div>
              <div className="flex-1">
                <span className="text-sm text-muted-foreground">Configured carriers</span>
                <div className="flex items-baseline gap-1.5">
                  <span className="text-xl font-bold">{live.carriers.length}</span>
                  <span className="text-xs text-muted-foreground">
                    · {live.carriers.reduce((s, c) => s + c.format_count, 0)} formats
                  </span>
                </div>
              </div>
            </div>
            <div className="flex flex-wrap gap-1.5">
              {live.carriers.map((c) => (
                <Badge key={c.key} variant="secondary" className="text-xs text-muted-foreground">
                  {c.name}
                </Badge>
              ))}
            </div>
          </Card>
        </div>
      )}

      {/* KPI Cards */}
      <div className="grid grid-cols-6 gap-4">
        <Card className="neu rounded-xl p-5">
          <div className="flex items-center gap-3 mb-3">
            <div className="flex items-center justify-center w-9 h-9 rounded-lg bg-blue-500/10">
              <Rows3 className="w-4 h-4 text-blue-400" />
            </div>
            <span className="text-sm text-muted-foreground">Total Rows</span>
          </div>
          <p className="text-2xl font-bold">{stats.rows.total.toLocaleString()}</p>
        </Card>

        <Card className="neu rounded-xl p-5">
          <div className="flex items-center gap-3 mb-3">
            <div className="flex items-center justify-center w-9 h-9 rounded-lg bg-emerald-500/10">
              <DollarSign className="w-4 h-4 text-emerald-400" />
            </div>
            <span className="text-sm text-muted-foreground">Total MRC</span>
          </div>
          <p className="text-2xl font-bold">
            ${stats.rows.total_mrc.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
          </p>
        </Card>

        <Card className="neu rounded-xl p-5">
          <div className="flex items-center gap-3 mb-3">
            <div className="flex items-center justify-center w-9 h-9 rounded-lg bg-violet-500/10">
              <FileText className="w-4 h-4 text-violet-400" />
            </div>
            <span className="text-sm text-muted-foreground">Documents</span>
          </div>
          <p className="text-2xl font-bold">{stats.extraction_runs.total_documents}</p>
        </Card>

        <Card className="neu rounded-xl p-5">
          <div className="flex items-center gap-3 mb-3">
            <div className="flex items-center justify-center w-9 h-9 rounded-lg bg-amber-500/10">
              <Activity className="w-4 h-4 text-amber-400" />
            </div>
            <span className="text-sm text-muted-foreground">Extraction Runs</span>
          </div>
          <p className="text-2xl font-bold">{stats.extraction_runs.total}</p>
        </Card>

        <Card className="neu rounded-xl p-5">
          <div className="flex items-center gap-3 mb-3">
            <div className="flex items-center justify-center w-9 h-9 rounded-lg bg-indigo-500/10">
              <Rows3 className="w-4 h-4 text-indigo-400" />
            </div>
            <span className="text-sm text-muted-foreground">Rows per doc</span>
          </div>
          <p className="text-2xl font-bold">
            {totalDocs > 0 ? avgRowsPerDoc.toFixed(1) : "—"}
          </p>
        </Card>

        <Card className="neu rounded-xl p-5">
          <div className="flex items-center gap-3 mb-3">
            <div className="flex items-center justify-center w-9 h-9 rounded-lg bg-teal-500/10">
              <DollarSign className="w-4 h-4 text-teal-400" />
            </div>
            <span className="text-sm text-muted-foreground">Cost per row</span>
          </div>
          <p className="text-2xl font-bold">
            {totalRows > 0 && totalCost > 0 ? `$${costPerRow.toFixed(5)}` : "—"}
          </p>
        </Card>
      </div>

      <div className="grid grid-cols-3 gap-4">
        {/* Confidence Breakdown */}
        <Card className="neu rounded-xl p-5">
          <h2 className="font-semibold mb-4">Confidence</h2>
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <CheckCircle2 className="w-4 h-4 text-emerald-400" />
                <span className="text-sm">High</span>
              </div>
              <div className="flex items-center gap-2">
                <span className="text-sm font-medium">{confidenceHigh}</span>
                <span className="text-xs text-muted-foreground w-10 text-right">
                  {Math.round((confidenceHigh / confidenceTotal) * 100)}%
                </span>
              </div>
            </div>
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <AlertCircle className="w-4 h-4 text-amber-400" />
                <span className="text-sm">Medium</span>
              </div>
              <div className="flex items-center gap-2">
                <span className="text-sm font-medium">{confidenceMedium}</span>
                <span className="text-xs text-muted-foreground w-10 text-right">
                  {Math.round((confidenceMedium / confidenceTotal) * 100)}%
                </span>
              </div>
            </div>
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <HelpCircle className="w-4 h-4 text-red-400" />
                <span className="text-sm">Low</span>
              </div>
              <div className="flex items-center gap-2">
                <span className="text-sm font-medium">{confidenceLow}</span>
                <span className="text-xs text-muted-foreground w-10 text-right">
                  {Math.round((confidenceLow / confidenceTotal) * 100)}%
                </span>
              </div>
            </div>
          </div>
          {/* Stacked bar */}
          <div className="flex h-2 rounded-full overflow-hidden mt-4 bg-muted/30">
            {confidenceHigh > 0 && (
              <div className="bg-emerald-500" style={{ width: `${(confidenceHigh / confidenceTotal) * 100}%` }} />
            )}
            {confidenceMedium > 0 && (
              <div className="bg-amber-500" style={{ width: `${(confidenceMedium / confidenceTotal) * 100}%` }} />
            )}
            {confidenceLow > 0 && (
              <div className="bg-red-500" style={{ width: `${(confidenceLow / confidenceTotal) * 100}%` }} />
            )}
          </div>
        </Card>

        {/* Review Status */}
        <Card className="neu rounded-xl p-5">
          <h2 className="font-semibold mb-4">Review Status</h2>
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <span className="text-sm text-muted-foreground">Pending</span>
              <span className="text-sm font-medium">{reviewPending}</span>
            </div>
            <Separator className="opacity-30" />
            <div className="flex items-center justify-between">
              <span className="text-sm text-muted-foreground">Approved</span>
              <span className="text-sm font-medium text-emerald-400">{reviewApproved}</span>
            </div>
            <Separator className="opacity-30" />
            <div className="flex items-center justify-between">
              <span className="text-sm text-muted-foreground">Corrected</span>
              <span className="text-sm font-medium text-amber-400">{reviewCorrected}</span>
            </div>
            <Separator className="opacity-30" />
            <div className="flex items-center justify-between">
              <span className="text-sm text-muted-foreground">Total corrections</span>
              <span className="text-sm font-medium">{stats.corrections}</span>
            </div>
          </div>
        </Card>

        {/* Carrier Breakdown */}
        <Card className="neu rounded-xl p-5">
          <h2 className="font-semibold mb-4">By Carrier</h2>
          {stats.carriers.length === 0 ? (
            <p className="text-sm text-muted-foreground py-2">No carrier data</p>
          ) : (
            <div className="space-y-3">
              {stats.carriers.slice(0, 6).map((c) => (
                <div key={c.carrier} className="flex items-center justify-between">
                  <span className="text-sm truncate max-w-[140px]">{c.carrier}</span>
                  <div className="flex items-center gap-3">
                    <Badge variant="secondary" className="text-xs">
                      {c.row_count} rows
                    </Badge>
                    <span className="text-xs text-muted-foreground w-20 text-right font-mono">
                      ${c.mrc.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                    </span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </Card>
      </div>

      {/* Recent Runs */}
      {stats.recent_runs.length > 0 && (
        <Card className="neu rounded-xl p-5">
          <h2 className="font-semibold mb-4">Recent Extraction Runs</h2>
          <div className="space-y-2">
            {stats.recent_runs.map((run) => (
              <div
                key={run.id}
                className="flex items-center justify-between py-2 px-3 rounded-lg hover:bg-muted/30 transition-colors cursor-pointer"
                onClick={() => run.upload_id && onViewUpload?.(run.upload_id)}
              >
                <div className="flex items-center gap-3">
                  <Badge
                    variant="secondary"
                    className={
                      run.status === "completed"
                        ? "text-emerald-400 bg-emerald-500/10"
                        : run.status === "failed"
                          ? "text-red-400 bg-red-500/10"
                          : "text-amber-400 bg-amber-500/10"
                    }
                  >
                    {run.status}
                  </Badge>
                  <span className="text-sm font-mono text-muted-foreground">
                    {run.upload_id || run.id.slice(0, 8)}
                  </span>
                </div>
                <div className="flex items-center gap-6 text-sm">
                  <span className="text-muted-foreground">
                    {run.documents_processed} docs
                  </span>
                  <span className="font-medium">
                    {run.rows_extracted} rows
                  </span>
                  {run.completed_at && (
                    <span className="text-xs text-muted-foreground">
                      {new Date(run.completed_at).toLocaleDateString()}
                    </span>
                  )}
                </div>
              </div>
            ))}
          </div>
        </Card>
      )}
    </div>
  );
}
