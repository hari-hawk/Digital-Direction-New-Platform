"use client";

// ChatPage — dedicated cross-project conversational surface. Covers Hari's
// "overall search" use case while ChatDrawer covers project-scoped questions.
//
// Layout (Stitch-inspired): two-pane.
//   Left  — project picker (collapsible, lists known clients + projects with
//           row counts; clicking a project narrows context, leaving blank
//           keeps platform-wide scope).
//   Right — message thread + composer, identical message bubbles to ChatDrawer.
//
// The system prompt difference: when a project is picked, the request runs in
// "project" mode using the same context the drawer uses. When the picker is
// "All projects" or filtered to a client, the request runs in "platform" mode
// with cross-project pattern findings as context.

import { useEffect, useState, useRef, useCallback, useMemo } from "react";
import {
  Sparkles, Send, Loader2, AlertTriangle, AlertCircle, Info,
  FolderOpen, Users, Search, X,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  apiChatStream, apiListUploads, apiListClients,
  type ChatMessage, type ChatMeta, type UploadSummary, type ClientSummary,
} from "@/lib/api";
import { useAppStore } from "@/lib/store";
import { toast } from "sonner";

const PLATFORM_SUGGESTIONS = [
  "Which customers are paying for services on multiple carriers?",
  "Show me recurring vendors across our customer portfolio",
  "What's the total month-to-month exposure across all projects?",
  "Are there clusters of contracts expiring in the next quarter?",
  "Which projects have the most pricing anomalies?",
];

type Scope =
  | { kind: "platform" }
  | { kind: "client"; clientName: string }
  | { kind: "project"; projectId: string; projectName: string };

export function ChatPage() {
  const store = useAppStore();
  const [scope, setScope] = useState<Scope>({ kind: "platform" });
  const [uploads, setUploads] = useState<UploadSummary[]>([]);
  const [clients, setClients] = useState<ClientSummary[]>([]);
  const [filter, setFilter] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [meta, setMeta] = useState<ChatMeta | null>(null);
  const scrollEndRef = useRef<HTMLDivElement | null>(null);

  // Load picker data once on mount. Reuses store-level uploads when present
  // so the page hydrates instantly from the already-loaded list.
  useEffect(() => {
    if (store.uploads.length > 0) {
      // store has uploads already — derive a UploadSummary-shaped list
      setUploads(
        store.uploads.map((u) => ({
          upload_id: u.id,
          project_name: u.projectName,
          client_name: u.clientName ?? null,
          total_rows: u.files.length, // best-effort; backend total_rows is the truth
          status: u.status as string,
          files_total: u.files.length,
          files_processed: 0,
          created_at: null,
          updated_at: null,
          carriers: [],
          deleted_at: null,
        } as unknown as UploadSummary)),
      );
    }
    apiListUploads().then((r) => setUploads(r.uploads || []));
    apiListClients().then((r) => setClients(r.clients || [])).catch(() => {});
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    scrollEndRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, streaming]);

  // Reset thread when scope changes — context shifts, so the assistant should
  // start fresh rather than answer about the wrong scope.
  const setScopeAndReset = useCallback((s: Scope) => {
    setScope(s);
    setMessages([]);
    setMeta(null);
  }, []);

  const filtered = useMemo(() => {
    const q = filter.trim().toLowerCase();
    if (!q) return uploads;
    return uploads.filter(
      (u) =>
        (u.project_name || "").toLowerCase().includes(q) ||
        (u.client_name || "").toLowerCase().includes(q),
    );
  }, [uploads, filter]);

  // Group filtered uploads by client for the left-pane sectioning. Falls back
  // to "(no client)" so projects without a client_name still show up.
  const grouped = useMemo(() => {
    const map = new Map<string, UploadSummary[]>();
    for (const u of filtered) {
      const key = (u.client_name || "(no client)").trim();
      if (!map.has(key)) map.set(key, []);
      map.get(key)!.push(u);
    }
    // Stable order: known clients alphabetical, "(no client)" last.
    return [...map.entries()].sort(([a], [b]) => {
      if (a === "(no client)") return 1;
      if (b === "(no client)") return -1;
      return a.localeCompare(b);
    });
  }, [filtered]);

  const ask = useCallback(async (text: string) => {
    if (!text.trim() || streaming) return;
    const userMsg: ChatMessage = { role: "user", content: text.trim() };
    const next = [...messages, userMsg];
    setMessages(next);
    setInput("");
    setStreaming(true);
    setMessages((m) => [...m, { role: "assistant", content: "" }]);

    const args =
      scope.kind === "project"
        ? { messages: next, mode: "project" as const, projectId: scope.projectId }
        : scope.kind === "client"
        ? { messages: next, mode: "platform" as const, clientFilter: scope.clientName }
        : { messages: next, mode: "platform" as const };

    await apiChatStream(args, {
      onMeta: setMeta,
      onToken: (chunk) => {
        setMessages((m) => {
          const copy = [...m];
          const last = copy[copy.length - 1];
          if (last?.role === "assistant") {
            copy[copy.length - 1] = { ...last, content: last.content + chunk };
          }
          return copy;
        });
      },
      onError: (err) => {
        toast.error(`Chat error: ${err}`);
        setMessages((m) => {
          const copy = [...m];
          const last = copy[copy.length - 1];
          if (last?.role === "assistant" && !last.content) copy.pop();
          return copy;
        });
      },
      onDone: () => setStreaming(false),
    });
    setStreaming(false);
  }, [messages, streaming, scope]);

  const scopeLabel =
    scope.kind === "platform"
      ? "All projects"
      : scope.kind === "client"
      ? `Client: ${scope.clientName}`
      : `Project: ${scope.projectName}`;

  return (
    <div className="flex h-[calc(100vh-3.5rem)] overflow-hidden">
      {/* ── Left pane: project picker ─────────────────────────────────── */}
      <aside className="w-72 shrink-0 border-r flex flex-col bg-card/30">
        <div className="p-4 border-b">
          <p className="text-xs font-medium text-muted-foreground mb-2">SCOPE</p>
          <button
            onClick={() => setScopeAndReset({ kind: "platform" })}
            className={`w-full text-left rounded-md border px-3 py-2 text-sm transition-colors ${
              scope.kind === "platform"
                ? "border-blue-500/40 bg-blue-500/10"
                : "border-input hover:bg-accent"
            }`}
          >
            <div className="flex items-center gap-2">
              <Sparkles className="w-3.5 h-3.5 text-blue-500" />
              <span className="font-medium">All projects</span>
            </div>
            <p className="text-[11px] text-muted-foreground mt-0.5">
              Cross-project insights · {uploads.length} projects
            </p>
          </button>
        </div>

        <div className="px-4 py-2 border-b">
          <div className="relative">
            <Search className="w-3.5 h-3.5 absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder="Filter projects…"
              className="pl-8 h-8 text-xs"
            />
            {filter && (
              <button
                onClick={() => setFilter("")}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
              >
                <X className="w-3.5 h-3.5" />
              </button>
            )}
          </div>
        </div>

        <ScrollArea className="flex-1">
          <div className="p-3 space-y-3">
            {grouped.map(([client, ups]) => (
              <div key={client}>
                <button
                  onClick={() => setScopeAndReset({ kind: "client", clientName: client })}
                  className={`w-full flex items-center gap-2 px-2 py-1 text-[11px] font-medium uppercase tracking-wide rounded transition-colors ${
                    scope.kind === "client" && scope.clientName === client
                      ? "bg-blue-500/10 text-blue-600 dark:text-blue-400"
                      : "text-muted-foreground hover:bg-accent"
                  }`}
                >
                  <Users className="w-3 h-3" />
                  {client}
                  <span className="ml-auto text-muted-foreground/60 normal-case">{ups.length}</span>
                </button>
                <div className="ml-3 mt-1 space-y-0.5 border-l border-border pl-2">
                  {ups.map((u) => (
                    <button
                      key={u.upload_id}
                      onClick={() =>
                        setScopeAndReset({
                          kind: "project",
                          projectId: u.upload_id,
                          projectName: u.project_name || "(unnamed)",
                        })
                      }
                      className={`w-full text-left rounded-md px-2 py-1.5 text-xs transition-colors ${
                        scope.kind === "project" && scope.projectId === u.upload_id
                          ? "bg-blue-500/15 text-foreground"
                          : "hover:bg-accent"
                      }`}
                    >
                      <div className="flex items-center gap-1.5">
                        <FolderOpen className="w-3 h-3 text-muted-foreground/70 shrink-0" />
                        <span className="line-clamp-1">{u.project_name || "(unnamed)"}</span>
                      </div>
                      <p className="text-[10px] text-muted-foreground/70 ml-4">
                        {u.total_rows ?? 0} rows · {u.status}
                      </p>
                    </button>
                  ))}
                </div>
              </div>
            ))}
            {uploads.length === 0 && (
              <p className="text-xs text-muted-foreground p-2">
                No projects yet. Upload some files to start asking questions.
              </p>
            )}
          </div>
        </ScrollArea>
      </aside>

      {/* ── Right pane: thread + composer ─────────────────────────────── */}
      <main className="flex-1 flex flex-col min-w-0">
        <header className="px-6 py-3 border-b shrink-0 flex items-center justify-between">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <Sparkles className="w-4 h-4 text-blue-500" />
              <h1 className="text-base font-semibold">Ask AI</h1>
            </div>
            <p className="text-xs text-muted-foreground line-clamp-1">{scopeLabel}</p>
          </div>
          {meta && (
            <div className="text-[11px] text-muted-foreground/70 flex items-center gap-3">
              {meta.row_count !== undefined && <span>{meta.row_count} rows</span>}
              {meta.project_count !== undefined && <span>· {meta.project_count} projects</span>}
              {meta.findings && <span>· {meta.findings.total} insights</span>}
            </div>
          )}
        </header>

        <ScrollArea className="flex-1 min-h-0">
          <div className="max-w-3xl mx-auto px-6 py-6 space-y-5">
            {messages.length === 0 ? (
              <ChatEmpty scope={scope} onSuggest={ask} />
            ) : (
              messages.map((m, i) => (
                <Bubble
                  key={i}
                  role={m.role}
                  content={m.content}
                  streaming={streaming && i === messages.length - 1 && m.role === "assistant"}
                />
              ))
            )}
            <div ref={scrollEndRef} />
          </div>
        </ScrollArea>

        <div className="border-t p-4 shrink-0 bg-background">
          <form
            className="max-w-3xl mx-auto flex items-end gap-2"
            onSubmit={(e) => { e.preventDefault(); ask(input); }}
          >
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  ask(input);
                }
              }}
              placeholder={
                scope.kind === "project"
                  ? "Ask about this project's data, contracts, anomalies…"
                  : scope.kind === "client"
                  ? `Ask about ${scope.clientName}'s portfolio…`
                  : "Ask anything across all projects…"
              }
              rows={2}
              className="flex-1 resize-none rounded-lg border border-input bg-background px-4 py-2.5 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring min-h-[52px] max-h-40"
              disabled={streaming}
            />
            <Button type="submit" size="lg" disabled={!input.trim() || streaming}>
              {streaming ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
            </Button>
          </form>
          <p className="text-[10px] text-muted-foreground/60 mt-2 text-center">
            Grounded in extracted rows + deterministic pattern findings. Cites row IDs.
          </p>
        </div>
      </main>
    </div>
  );
}

// ── Empty state for the dedicated page (richer than the drawer's) ───────────

function ChatEmpty({ scope, onSuggest }: { scope: Scope; onSuggest: (q: string) => void }) {
  return (
    <div className="space-y-6 py-8">
      <div className="text-center">
        <div className="inline-flex items-center justify-center w-12 h-12 rounded-full bg-blue-500/10 border border-blue-500/20 mb-3">
          <Sparkles className="w-5 h-5 text-blue-500" />
        </div>
        <h2 className="text-lg font-semibold">
          {scope.kind === "platform"
            ? "Ask anything about your portfolio"
            : scope.kind === "client"
            ? `Ask anything about ${scope.clientName}`
            : `Ask anything about ${scope.projectName}`}
        </h2>
        <p className="text-sm text-muted-foreground mt-1 max-w-xl mx-auto">
          The assistant reads your extracted rows, validation flags, and the
          pattern detectors' output. Every claim is grounded in numbers — no hallucination.
        </p>
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 max-w-2xl mx-auto">
        {PLATFORM_SUGGESTIONS.map((q) => (
          <button
            key={q}
            onClick={() => onSuggest(q)}
            className="text-left text-sm px-4 py-3 rounded-lg border border-input bg-card hover:bg-accent hover:border-blue-500/30 transition-colors"
          >
            {q}
          </button>
        ))}
      </div>
    </div>
  );
}

// ── Message bubble ──────────────────────────────────────────────────────────

function Bubble({
  role, content, streaming,
}: {
  role: "user" | "assistant";
  content: string;
  streaming: boolean;
}) {
  if (role === "user") {
    return (
      <div className="flex justify-end">
        <div className="rounded-2xl rounded-tr-sm bg-primary text-primary-foreground px-4 py-2.5 text-sm max-w-[80%]">
          {content}
        </div>
      </div>
    );
  }
  return (
    <div className="flex gap-3">
      <div className="w-8 h-8 rounded-full bg-blue-500/10 border border-blue-500/20 flex items-center justify-center shrink-0">
        <Sparkles className="w-4 h-4 text-blue-500" />
      </div>
      <div className="flex-1 min-w-0 pt-0.5">
        <div className="text-sm whitespace-pre-wrap leading-relaxed">
          {content}
          {streaming && (
            <span className="inline-block w-1.5 h-4 align-text-bottom bg-foreground/40 animate-pulse ml-0.5" />
          )}
          {!streaming && !content && (
            <span className="text-muted-foreground italic">Thinking…</span>
          )}
        </div>
      </div>
    </div>
  );
}
