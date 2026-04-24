"use client";

import { useCallback, useEffect, useState } from "react";
import { motion } from "framer-motion";
import { Users, Inbox, ChevronRight, Loader2, AlertTriangle, Database } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import {
  apiListClients,
  apiGetClient,
  type ClientSummary,
  type ClientDetail,
} from "@/lib/api";

export function ClientsPage() {
  const [clients, setClients] = useState<ClientSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<ClientDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const { clients } = await apiListClients();
      setClients(clients);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const openClient = useCallback(async (id: string) => {
    setSelectedId(id);
    setDetailLoading(true);
    try {
      const d = await apiGetClient(id);
      setDetail(d);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setDetailLoading(false);
    }
  }, []);

  return (
    <div className="h-full overflow-auto">
      <div className="mx-auto max-w-6xl px-6 py-6 space-y-5">
        <header className="flex items-start justify-between gap-4">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight flex items-center gap-2">
              <Users className="w-6 h-6 text-sky-400" />
              Clients
            </h1>
            <p className="text-sm text-muted-foreground mt-1">
              Master-data store for each client. Analyst-confirmed values here
              override extracted values on every future upload for the client.
            </p>
          </div>
          <Button variant="outline" size="sm" onClick={refresh} disabled={loading}>
            Refresh
          </Button>
        </header>

        <Separator className="opacity-50" />

        {error && (
          <div className="rounded-lg border border-rose-500/40 bg-rose-500/10 px-4 py-3 text-sm text-rose-300 flex items-start gap-2">
            <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />
            <span>{error}</span>
          </div>
        )}

        {loading ? (
          <div className="py-20 text-center text-sm text-muted-foreground flex items-center justify-center gap-2">
            <Loader2 className="w-4 h-4 animate-spin" /> Loading clients…
          </div>
        ) : clients.length === 0 ? (
          <div className="py-20 text-center space-y-2">
            <Inbox className="w-10 h-10 mx-auto text-muted-foreground/60" />
            <p className="text-sm text-muted-foreground">No clients yet.</p>
            <p className="text-xs text-muted-foreground/70">
              Upload a project with a client name — the system will auto-create
              the client and link the project. Corrections applied in Review
              will accumulate here as master-data facts.
            </p>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-[300px_1fr] gap-4">
            {/* Client list */}
            <div className="space-y-2">
              {clients.map((c) => {
                const selected = c.id === selectedId;
                return (
                  <motion.button
                    key={c.id}
                    onClick={() => openClient(c.id)}
                    aria-current={selected}
                    className={`
                      w-full text-left neu rounded-xl px-4 py-3 flex items-center justify-between
                      transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-500/60
                      ${selected ? "ring-1 ring-sky-500/50" : ""}
                    `}
                    whileTap={{ scale: 0.99 }}
                  >
                    <div className="min-w-0 flex-1">
                      <p className="font-medium text-sm truncate">{c.name}</p>
                      <div className="flex items-center gap-2 mt-1 text-xs text-muted-foreground">
                        <span>
                          {c.project_count} project{c.project_count === 1 ? "" : "s"}
                        </span>
                        <span>·</span>
                        <span
                          className={
                            c.reference_data_count > 0 ? "text-emerald-400" : ""
                          }
                        >
                          {c.reference_data_count} fact
                          {c.reference_data_count === 1 ? "" : "s"}
                        </span>
                      </div>
                    </div>
                    <ChevronRight
                      className={`w-4 h-4 shrink-0 transition-transform ${
                        selected ? "text-sky-400 translate-x-0.5" : "text-muted-foreground"
                      }`}
                    />
                  </motion.button>
                );
              })}
            </div>

            {/* Detail pane */}
            <div className="neu rounded-xl p-5 min-h-[360px]">
              {!selectedId ? (
                <div className="h-full flex items-center justify-center text-center text-sm text-muted-foreground">
                  <div>
                    <Database className="w-10 h-10 mx-auto mb-3 text-muted-foreground/40" />
                    <p>Select a client to view their master-data facts.</p>
                  </div>
                </div>
              ) : detailLoading || !detail ? (
                <div className="h-full flex items-center justify-center text-sm text-muted-foreground gap-2">
                  <Loader2 className="w-4 h-4 animate-spin" /> Loading…
                </div>
              ) : (
                <div className="space-y-4">
                  <div>
                    <h2 className="text-lg font-semibold">{detail.name}</h2>
                    {detail.notes && (
                      <p className="text-xs text-muted-foreground mt-1">{detail.notes}</p>
                    )}
                  </div>

                  {detail.reference_data.length === 0 ? (
                    <div className="py-10 text-center text-sm text-muted-foreground">
                      <p>No master-data facts recorded yet.</p>
                      <p className="text-xs text-muted-foreground/70 mt-1">
                        When an analyst corrects a location or contract field
                        in Review, the correction is saved here.
                      </p>
                    </div>
                  ) : (
                    <>
                      <div className="text-xs text-muted-foreground uppercase tracking-wider">
                        {detail.reference_data.length} fact
                        {detail.reference_data.length === 1 ? "" : "s"}
                      </div>
                      <ul className="space-y-2">
                        {detail.reference_data.map((rd) => (
                          <li
                            key={rd.id}
                            className="rounded-lg border border-border/50 px-4 py-3 bg-card/40"
                          >
                            <div className="flex items-center gap-2 flex-wrap mb-2">
                              <Badge
                                variant="secondary"
                                className="text-[10px] uppercase"
                              >
                                {rd.kind}
                              </Badge>
                              {rd.carrier && (
                                <Badge className="text-[10px] bg-sky-500/10 text-sky-300 border border-sky-500/20">
                                  {rd.carrier}
                                </Badge>
                              )}
                              {rd.account_number && (
                                <span className="text-[10px] font-mono text-muted-foreground">
                                  acct {rd.account_number}
                                </span>
                              )}
                              {rd.source && (
                                <span className="text-[10px] text-muted-foreground ml-auto">
                                  {rd.source}
                                  {rd.confirmed_by ? ` · ${rd.confirmed_by}` : ""}
                                </span>
                              )}
                            </div>
                            <div className="grid grid-cols-1 md:grid-cols-2 gap-x-4 gap-y-1 text-xs">
                              <div>
                                <span className="text-muted-foreground">Key: </span>
                                <span className="font-mono">
                                  {JSON.stringify(rd.key_fields)}
                                </span>
                              </div>
                              <div>
                                <span className="text-muted-foreground">Values: </span>
                                <span className="font-mono">
                                  {Object.entries(rd.values)
                                    .map(([k, v]) => `${k}=${String(v)}`)
                                    .join(", ")}
                                </span>
                              </div>
                            </div>
                          </li>
                        ))}
                      </ul>
                    </>
                  )}
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
