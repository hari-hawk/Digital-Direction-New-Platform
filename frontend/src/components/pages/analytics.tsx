"use client";

import { useState, useEffect } from "react";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  BarChart3,
  Loader2,
  CheckCircle2,
  AlertTriangle,
  TrendingUp,
  Pencil,
} from "lucide-react";
import { apiGetAnalyticsStats, type AnalyticsStats } from "@/lib/api";

const CATEGORY_META: Record<string, { label: string; target: string; color: string }> = {
  structured: { label: "Structured", target: ">98%", color: "text-blue-400" },
  semi_structured: { label: "Semi-Structured", target: ">90%", color: "text-violet-400" },
  fuzzy: { label: "Fuzzy", target: ">80%", color: "text-amber-400" },
  contract: { label: "Contract", target: ">75%", color: "text-rose-400" },
};

function humanize(field: string): string {
  return field
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase())
    .replace("Mrc", "MRC")
    .replace("Ld ", "LD ")
    .replace("Btn", "BTN")
    .replace("Usoc", "USOC")
    .replace("Mtm", "MTM");
}

function fillBar(rate: number) {
  const color =
    rate >= 90
      ? "bg-emerald-500"
      : rate >= 70
        ? "bg-amber-500"
        : rate >= 40
          ? "bg-orange-500"
          : "bg-red-500";
  return (
    <div className="flex items-center gap-2 flex-1">
      <div className="h-2 flex-1 rounded-full bg-muted/30 overflow-hidden">
        <div className={`h-full rounded-full ${color}`} style={{ width: `${rate}%` }} />
      </div>
      <span className="text-xs font-mono w-12 text-right text-muted-foreground">{rate}%</span>
    </div>
  );
}

export function AnalyticsPage() {
  const [stats, setStats] = useState<AnalyticsStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    apiGetAnalyticsStats()
      .then(setStats)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <div className="p-8 space-y-8 max-w-6xl mx-auto">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Analytics</h1>
          <p className="text-muted-foreground text-sm mt-1">Extraction quality metrics and trends</p>
        </div>
        <div className="flex items-center gap-2 text-muted-foreground py-12 justify-center">
          <Loader2 className="w-4 h-4 animate-spin" />
          <span className="text-sm">Loading analytics...</span>
        </div>
      </div>
    );
  }

  if (error || !stats || stats.total_rows === 0) {
    return (
      <div className="p-8 space-y-8 max-w-6xl mx-auto">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Analytics</h1>
          <p className="text-muted-foreground text-sm mt-1">Extraction quality metrics and trends</p>
        </div>
        <Card className="p-12 bg-card/50 border-border/50 text-center">
          <BarChart3 className="w-10 h-10 mx-auto mb-4 text-muted-foreground/30" />
          <p className="text-lg font-medium text-muted-foreground">No analytics data yet</p>
          <p className="text-sm text-muted-foreground/70 mt-1">
            Run an extraction to see quality metrics.
          </p>
        </Card>
      </div>
    );
  }

  const categories = Object.entries(stats.category_fill_rates);

  // Group fields by category for tabbed view
  const fieldsByCategory: Record<string, typeof stats.field_fill_rates> = {};
  for (const f of stats.field_fill_rates) {
    if (!fieldsByCategory[f.category]) fieldsByCategory[f.category] = [];
    fieldsByCategory[f.category].push(f);
  }

  // Overall average fill rate
  const overallFill =
    stats.field_fill_rates.length > 0
      ? Math.round(
          stats.field_fill_rates.reduce((sum, f) => sum + f.fill_rate, 0) /
            stats.field_fill_rates.length,
        )
      : 0;

  return (
    <div className="p-8 space-y-6 max-w-6xl mx-auto">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Analytics</h1>
        <p className="text-muted-foreground text-sm mt-1">
          Extraction quality metrics across {stats.total_rows.toLocaleString()} rows
        </p>
      </div>

      {/* Category Summary Cards */}
      <div className="grid grid-cols-5 gap-4">
        <Card className="neu rounded-xl p-5">
          <div className="flex items-center gap-2 mb-3">
            <TrendingUp className="w-4 h-4 text-emerald-400" />
            <span className="text-sm text-muted-foreground">Overall</span>
          </div>
          <p className="text-2xl font-bold">{overallFill}%</p>
          <p className="text-xs text-muted-foreground mt-1">avg fill rate</p>
        </Card>

        {categories.map(([cat, data]) => {
          const meta = CATEGORY_META[cat] || { label: cat, target: "", color: "text-muted-foreground" };
          return (
            <Card key={cat} className="neu rounded-xl p-5">
              <div className="flex items-center gap-2 mb-3">
                <span className={`text-sm ${meta.color}`}>{meta.label}</span>
              </div>
              <p className="text-2xl font-bold">{data.avg_fill_rate}%</p>
              <p className="text-xs text-muted-foreground mt-1">
                {data.field_count} fields / target {meta.target}
              </p>
            </Card>
          );
        })}
      </div>

      {/* Field Fill Rates — Tabbed by Category */}
      <Card className="neu rounded-xl p-5">
        <h2 className="font-semibold mb-4">Field Fill Rates</h2>
        <Tabs defaultValue="all">
          <TabsList>
            <TabsTrigger value="all">All Fields</TabsTrigger>
            {Object.keys(fieldsByCategory).map((cat) => (
              <TabsTrigger key={cat} value={cat}>
                {CATEGORY_META[cat]?.label || cat}
              </TabsTrigger>
            ))}
          </TabsList>

          <TabsContent value="all" className="mt-4">
            <FieldList fields={stats.field_fill_rates} />
          </TabsContent>

          {Object.entries(fieldsByCategory).map(([cat, fields]) => (
            <TabsContent key={cat} value={cat} className="mt-4">
              <FieldList fields={fields} />
            </TabsContent>
          ))}
        </Tabs>
      </Card>

      {/* Bottom Row: Corrections */}
      <div className="grid grid-cols-2 gap-4">
        {/* Top Corrected Fields */}
        <Card className="neu rounded-xl p-5">
          <div className="flex items-center gap-2 mb-4">
            <Pencil className="w-4 h-4 text-amber-400" />
            <h2 className="font-semibold">Most Corrected Fields</h2>
          </div>
          {stats.top_corrected_fields.length === 0 ? (
            <p className="text-sm text-muted-foreground py-2">No corrections yet</p>
          ) : (
            <div className="space-y-2">
              {stats.top_corrected_fields.map((f) => (
                <div key={f.field} className="flex items-center justify-between py-1.5">
                  <span className="text-sm">{humanize(f.field)}</span>
                  <Badge variant="secondary" className="text-xs">
                    {f.corrections}
                  </Badge>
                </div>
              ))}
            </div>
          )}
        </Card>

        {/* Corrections by Carrier */}
        <Card className="neu rounded-xl p-5">
          <div className="flex items-center gap-2 mb-4">
            <AlertTriangle className="w-4 h-4 text-amber-400" />
            <h2 className="font-semibold">Corrections by Carrier</h2>
          </div>
          {stats.corrections_by_carrier.length === 0 ? (
            <p className="text-sm text-muted-foreground py-2">No corrections yet</p>
          ) : (
            <div className="space-y-2">
              {stats.corrections_by_carrier.map((c) => (
                <div key={c.carrier} className="flex items-center justify-between py-1.5">
                  <span className="text-sm">{c.carrier}</span>
                  <Badge variant="secondary" className="text-xs">
                    {c.corrections}
                  </Badge>
                </div>
              ))}
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}

/** Renders a list of fields with fill rate bars. */
function FieldList({ fields }: { fields: AnalyticsStats["field_fill_rates"] }) {
  return (
    <div className="space-y-1.5 max-h-[400px] overflow-y-auto pr-2">
      {fields.map((f) => (
        <div key={f.field} className="flex items-center gap-3 py-1">
          <span className="text-sm w-48 truncate shrink-0">{humanize(f.field)}</span>
          <Badge
            variant="outline"
            className={`text-[10px] w-24 justify-center shrink-0 ${
              CATEGORY_META[f.category]?.color || ""
            }`}
          >
            {CATEGORY_META[f.category]?.label || f.category}
          </Badge>
          {fillBar(f.fill_rate)}
        </div>
      ))}
    </div>
  );
}
