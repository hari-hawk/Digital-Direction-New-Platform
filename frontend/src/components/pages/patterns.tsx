"use client";

// PatternsPage — deterministic insights dashboard. Same 5 detectors that
// power chat, surfaced visually so analysts can scan/filter/click into
// findings without typing a question.
//
// Layout (Stitch-inspired):
//   Top — summary header with severity counts (chips) + client filter input.
//   Middle — finding cards, grouped by severity (errors first, then warnings,
//   then info). Each card shows kind, title, detail, key metric, evidence
//   count, and a "Discuss in chat" action that pre-fills the chat input.

import { useEffect, useState, useMemo } from "react";
import {
  Sparkles, AlertCircle, AlertTriangle, Info, RefreshCw, Loader2,
  TrendingUp, Phone, FileText, Repeat, Calendar, ArrowRight,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  apiPatternsPlatform, apiPatternsForProject,
  type PatternFinding, type PatternsResponse,
} from "@/lib/api";
import { useAppStore } from "@/lib/store";
import { toast } from "sonner";

// Icon + color for each detector kind. Kept here (not in api.ts) so render
// concerns stay in the view layer.
const KIND_VISUAL: Record<
  PatternFinding["kind"],
  { icon: React.ElementType; label: string; tint: string }
> = {
  recurring_vendor:      { icon: Repeat,     label: "Recurring vendor",       tint: "blue"   },
  pricing_anomaly:       { icon: TrendingUp, label: "Pricing anomaly",        tint: "amber"  },
  contract_cluster:      { icon: Calendar,   label: "Contract cluster",       tint: "violet" },
  multi_carrier_account: { icon: Phone,      label: "Multi-carrier account",  tint: "rose"   },
  m2m_no_contract:       { icon: FileText,   label: "M2M without contract",   tint: "emerald" },
};

const SEVERITY_ORDER: PatternFinding["severity"][] = ["error", "warning", "info"];
const SEVERITY_VISUAL: Record<
  PatternFinding["severity"],
  { icon: React.ElementType; label: string; ring: string; bg: string; text: string }
> = {
  error:   { icon: AlertCircle,    label: "Critical", ring: "ring-red-500/20",   bg: "bg-red-500/5",    text: "text-red-500"   },
  warning: { icon: AlertTriangle,  label: "Warning",  ring: "ring-amber-500/20", bg: "bg-amber-500/5",  text: "text-amber-500" },
  info:    { icon: Info,           label: "Info",     ring: "ring-blue-500/20",  bg: "bg-blue-500/5",   text: "text-blue-500"  },
};

interface PatternsPageProps {
  /** When set, runs the per-project detector instead of the platform one. */
  projectId?: string;
  /** Click handler for "Discuss in chat" — parent navigates + pre-loads. */
  onDiscussInChat?: (finding: PatternFinding) => void;
}

export function PatternsPage({ projectId, onDiscussInChat }: PatternsPageProps) {
  const store = useAppStore();
  const [data, setData] = useState<PatternsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [clientFilter, setClientFilter] = useState("");
  const [activeSeverities, setActiveSeverities] = useState<Set<PatternFinding["severity"]>>(
    new Set(["error", "warning", "info"]),
  );

  const load = async (mode: "initial" | "refresh" = "initial") => {
    if (mode === "refresh") setRefreshing(true); else setLoading(true);
    try {
      const r = projectId
        ? await apiPatternsForProject(projectId)
        : await apiPatternsPlatform(clientFilter || undefined);
      setData(r);
    } catch (e) {
      toast.error(`Failed to load patterns: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  };

  useEffect(() => { load("initial"); }, [projectId]); // eslint-disable-line react-hooks/exhaustive-deps

  const grouped = useMemo(() => {
    const out: Record<PatternFinding["severity"], PatternFinding[]> = { error: [], warning: [], info: [] };
    for (const f of data?.findings || []) {
      if (activeSeverities.has(f.severity)) out[f.severity].push(f);
    }
    return out;
  }, [data, activeSeverities]);

  const toggleSev = (s: PatternFinding["severity"]) => {
    setActiveSeverities((prev) => {
      const next = new Set(prev);
      if (next.has(s)) next.delete(s); else next.add(s);
      return next;
    });
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64 text-muted-foreground">
        <Loader2 className="w-5 h-5 animate-spin mr-2" />
        Loading insights…
      </div>
    );
  }

  const totalShown = grouped.error.length + grouped.warning.length + grouped.info.length;

  return (
    <div className="p-8 max-w-6xl mx-auto space-y-6">
      {/* Header */}
      <div className="flex items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2">
            <Sparkles className="w-5 h-5 text-blue-500" />
            <h1 className="text-2xl font-bold tracking-tight">Insights</h1>
          </div>
          <p className="text-muted-foreground text-sm mt-1">
            {projectId
              ? `Detector findings for ${data?.project_name || "this project"}`
              : "Cross-project pattern detection — recurring vendors, pricing anomalies, contract clusters, multi-carrier accounts, M2M risk."}
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => load("refresh")}
          disabled={refreshing}
          className="gap-2"
        >
          {refreshing ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />}
          Refresh
        </Button>
      </div>

      {/* Summary chips */}
      <div className="flex flex-wrap items-center gap-2">
        {SEVERITY_ORDER.map((sev) => {
          const v = SEVERITY_VISUAL[sev];
          const Icon = v.icon;
          const count = (data?.findings || []).filter((f) => f.severity === sev).length;
          const active = activeSeverities.has(sev);
          return (
            <button
              key={sev}
              onClick={() => toggleSev(sev)}
              className={`flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-xs font-medium transition-colors ${
                active
                  ? `${v.bg} ${v.text} border-current`
                  : "border-input bg-muted/30 text-muted-foreground hover:bg-muted/50"
              }`}
            >
              <Icon className="w-3.5 h-3.5" />
              {v.label}
              <span className={`ml-1 ${active ? "" : "text-muted-foreground"}`}>{count}</span>
            </button>
          );
        })}
        <div className="grow" />
        {!projectId && (
          <Input
            value={clientFilter}
            onChange={(e) => setClientFilter(e.target.value)}
            onBlur={() => load("refresh")}
            onKeyDown={(e) => { if (e.key === "Enter") load("refresh"); }}
            placeholder="Filter by client name…"
            className="h-8 w-56 text-xs"
          />
        )}
      </div>

      {/* Stats banner */}
      {data && (
        <Card className="px-4 py-3 bg-card/50">
          <div className="flex items-center gap-6 text-xs flex-wrap">
            <Stat label="Findings" value={data.summary?.total ?? 0} />
            <Stat label="Rows scanned" value={data.row_count ?? 0} />
            {data.project_count !== undefined && <Stat label="Projects" value={data.project_count} />}
            {data.summary?.by_kind && (
              <div className="flex items-center gap-2 text-muted-foreground">
                <span>By kind:</span>
                {Object.entries(data.summary.by_kind).map(([k, n]) => (
                  <Badge key={k} variant="secondary" className="text-[10px] font-normal">
                    {KIND_VISUAL[k as PatternFinding["kind"]]?.label || k}: {n}
                  </Badge>
                ))}
              </div>
            )}
          </div>
        </Card>
      )}

      {/* Finding cards */}
      <div className="space-y-6">
        {SEVERITY_ORDER.map((sev) => {
          const items = grouped[sev];
          if (items.length === 0) return null;
          const v = SEVERITY_VISUAL[sev];
          const SevIcon = v.icon;
          return (
            <section key={sev}>
              <div className="flex items-center gap-2 mb-3">
                <SevIcon className={`w-4 h-4 ${v.text}`} />
                <h2 className="text-sm font-medium">{v.label}</h2>
                <span className="text-xs text-muted-foreground">({items.length})</span>
              </div>
              <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
                {items.map((f, i) => (
                  <FindingCard key={`${f.kind}-${i}`} finding={f} onDiscuss={onDiscussInChat} />
                ))}
              </div>
            </section>
          );
        })}
        {totalShown === 0 && (
          <div className="text-center py-12 text-muted-foreground">
            <Sparkles className="w-8 h-8 mx-auto mb-2 opacity-40" />
            <p className="text-sm">No findings match the current filter.</p>
          </div>
        )}
      </div>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="flex items-baseline gap-1.5">
      <span className="font-semibold text-foreground tabular-nums">{value}</span>
      <span className="text-muted-foreground">{label}</span>
    </div>
  );
}

function FindingCard({
  finding,
  onDiscuss,
}: {
  finding: PatternFinding;
  onDiscuss?: (f: PatternFinding) => void;
}) {
  const v = KIND_VISUAL[finding.kind] || { icon: Info, label: finding.kind, tint: "blue" };
  const KindIcon = v.icon;
  const sev = SEVERITY_VISUAL[finding.severity];

  // Format the metric subset that's most useful at-a-glance per kind.
  const metricLine = formatMetric(finding);

  return (
    <Card className={`p-4 ring-1 ${sev.ring} ${sev.bg} hover:shadow-sm transition-shadow`}>
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-start gap-2.5 min-w-0">
          <div className={`p-1.5 rounded-md ${sev.bg} border border-current/10 ${sev.text} shrink-0`}>
            <KindIcon className="w-3.5 h-3.5" />
          </div>
          <div className="min-w-0">
            <p className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground/70">
              {v.label}
            </p>
            <h3 className="text-sm font-semibold mt-0.5 leading-snug">{finding.title}</h3>
          </div>
        </div>
      </div>
      <p className="text-xs text-muted-foreground mt-2 leading-relaxed line-clamp-3">
        {finding.detail}
      </p>
      {metricLine && (
        <div className="text-[11px] font-mono mt-2 text-muted-foreground/80 bg-muted/30 rounded px-2 py-1">
          {metricLine}
        </div>
      )}
      <div className="flex items-center justify-between mt-3 pt-3 border-t border-border/50">
        <p className="text-[11px] text-muted-foreground">
          {finding.evidence_row_ids.length > 0
            ? `${finding.evidence_row_ids.length} evidence row${finding.evidence_row_ids.length === 1 ? "" : "s"}`
            : "no row-level evidence"}
        </p>
        {onDiscuss && (
          <Button
            variant="ghost"
            size="sm"
            className="h-7 text-xs gap-1"
            onClick={() => onDiscuss(finding)}
          >
            Discuss in chat
            <ArrowRight className="w-3 h-3" />
          </Button>
        )}
      </div>
    </Card>
  );
}

function formatMetric(f: PatternFinding): string | null {
  const m = f.metric || {};
  switch (f.kind) {
    case "pricing_anomaly":
      return `rate=$${m.row_rate} · group avg=$${m.group_mean} · z=${m.z_score} · n=${m.group_size}`;
    case "contract_cluster":
      return `${m.cluster_size} contracts · ${m.first_expiration} → ${m.last_expiration} · in ${m.days_until_first}d`;
    case "multi_carrier_account":
      return `${m.phone} · carriers=${(m.carriers as string[] | undefined)?.join(", ") || "?"} · ${m.row_count} rows`;
    case "m2m_no_contract":
      return `${m.row_count} rows · MRC=$${m.total_mrc?.toLocaleString?.() ?? m.total_mrc} · est savings≈$${m.estimated_savings_mid}`;
    case "recurring_vendor":
      return m.client && m.carriers
        ? `${m.client} · ${(m.carriers as string[]).join(", ")}`
        : m.client && m.carrier
        ? `${m.client} · ${m.carrier} on ${m.project_count} projects`
        : null;
    default:
      return null;
  }
}
