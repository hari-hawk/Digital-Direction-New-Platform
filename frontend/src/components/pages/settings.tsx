"use client";

import { useState, useEffect } from "react";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { Loader2 } from "lucide-react";
import { apiListCarriers, type CarrierInfo } from "@/lib/api";

export function SettingsPage() {
  const [carriers, setCarriers] = useState<CarrierInfo[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    apiListCarriers()
      .then(({ carriers }) => setCarriers(carriers))
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  return (
    <div className="p-8 space-y-8 max-w-4xl mx-auto">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Settings</h1>
        <p className="text-muted-foreground text-sm mt-1">
          Carrier configurations and system settings
        </p>
      </div>

      <Card className="neu rounded-xl p-6">
        <h2 className="font-semibold mb-4">Carrier Configurations</h2>
        {loading ? (
          <div className="flex items-center gap-2 text-muted-foreground py-4">
            <Loader2 className="w-4 h-4 animate-spin" />
            <span className="text-sm">Loading carriers...</span>
          </div>
        ) : carriers.length === 0 ? (
          <p className="text-sm text-muted-foreground py-4">No carriers configured</p>
        ) : (
          <div className="space-y-4">
            {carriers.map((c) => (
              <div key={c.key} className="flex items-center justify-between py-3">
                <div>
                  <p className="font-medium">{c.name}</p>
                  <p className="text-xs text-muted-foreground mt-0.5">
                    {c.format_count} format variant{c.format_count !== 1 ? "s" : ""}
                  </p>
                </div>
                <Badge variant="secondary" className="text-emerald-400 bg-emerald-500/10">
                  active
                </Badge>
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card className="neu rounded-xl p-6">
        <h2 className="font-semibold mb-4">System</h2>
        <div className="space-y-3 text-sm">
          <div className="flex justify-between">
            <span className="text-muted-foreground">Extraction Model</span>
            <span className="font-mono">gemini-2.5-flash</span>
          </div>
          <Separator className="opacity-30" />
          <div className="flex justify-between">
            <span className="text-muted-foreground">Merge Model</span>
            <span className="font-mono">claude-sonnet-4-6</span>
          </div>
          <Separator className="opacity-30" />
          <div className="flex justify-between">
            <span className="text-muted-foreground">Concurrency</span>
            <span className="font-mono">200</span>
          </div>
          <Separator className="opacity-30" />
          <div className="flex justify-between">
            <span className="text-muted-foreground">Database</span>
            <span className="font-mono">PostgreSQL 16 + pgvector</span>
          </div>
          <Separator className="opacity-30" />
          <div className="flex justify-between">
            <span className="text-muted-foreground">State Store</span>
            <span className="font-mono">Redis 7</span>
          </div>
        </div>
      </Card>
    </div>
  );
}
