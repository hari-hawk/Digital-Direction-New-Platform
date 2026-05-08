"use client";

// ChatDrawer — sidebar drawer used on the Results page for project-scoped
// chat. Asks Claude questions grounded in the project's extracted rows +
// pattern findings. Streams tokens via apiChatStream.
//
// Layout (Stitch-inspired): right-edge slide-in panel, ~440px wide, message
// thread on top, sticky composer at bottom. Compact info chip at the top
// shows the pattern-finding count + row count so the user knows what
// context Claude has before they ask.

import { useState, useRef, useEffect, useCallback } from "react";
import { Sparkles, X, Send, Loader2, AlertTriangle, AlertCircle, Info, ChevronDown } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import {
  Sheet, SheetContent, SheetHeader, SheetTitle, SheetTrigger,
} from "@/components/ui/sheet";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  apiChatStream, apiPatternsForProject,
  type ChatMessage, type ChatMeta, type PatternFinding,
} from "@/lib/api";
import { toast } from "sonner";

const SUGGESTED_QUESTIONS = [
  "What are the top compliance risks in this project?",
  "Which services are paying above the average rate?",
  "List all contracts expiring in the next 6 months",
  "Show me services that are month-to-month with no contract",
  "Are there services on multiple carriers? (potential migration or duplicate billing)",
];

interface ChatDrawerProps {
  projectId: string;
  projectName?: string;
  trigger?: React.ReactNode;
}

export function ChatDrawer({ projectId, projectName, trigger }: ChatDrawerProps) {
  const [open, setOpen] = useState(false);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [meta, setMeta] = useState<ChatMeta | null>(null);
  const [findings, setFindings] = useState<PatternFinding[]>([]);
  const [findingsLoaded, setFindingsLoaded] = useState(false);
  const scrollEndRef = useRef<HTMLDivElement | null>(null);

  // Load project patterns the first time the drawer opens — keeps the
  // initial open snappy by not blocking on it, then refreshes with real data.
  useEffect(() => {
    if (!open || findingsLoaded) return;
    apiPatternsForProject(projectId)
      .then((r) => {
        setFindings(r.findings || []);
        setFindingsLoaded(true);
      })
      .catch(() => setFindingsLoaded(true));
  }, [open, projectId, findingsLoaded]);

  // Auto-scroll on new tokens. The streaming ref-based scroll keeps the
  // newest content visible without yanking the user up if they scrolled away.
  useEffect(() => {
    scrollEndRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, streaming]);

  const ask = useCallback(async (text: string) => {
    if (!text.trim() || streaming) return;
    const userMsg: ChatMessage = { role: "user", content: text.trim() };
    const next = [...messages, userMsg];
    setMessages(next);
    setInput("");
    setStreaming(true);
    // Append a placeholder assistant message we'll fill in token-by-token.
    setMessages((m) => [...m, { role: "assistant", content: "" }]);

    await apiChatStream(
      { messages: next, mode: "project", projectId },
      {
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
            if (last?.role === "assistant" && !last.content) {
              copy.pop();
            }
            return copy;
          });
        },
        onDone: () => setStreaming(false),
      },
    );
    setStreaming(false);
  }, [messages, projectId, streaming]);

  // Base UI's Trigger renders its own <button>, so we use the `render` prop
  // (the equivalent of Radix's `asChild`) to pass a Button when caller didn't
  // supply a custom trigger. When `trigger` IS supplied, we wrap it in a
  // div+onClick fallback so callers can pass arbitrary nodes.
  return (
    <Sheet open={open} onOpenChange={setOpen}>
      {trigger ? (
        <div onClick={() => setOpen(true)} className="inline-flex">
          {trigger}
        </div>
      ) : (
        <SheetTrigger
          render={
            <Button variant="outline" size="sm" className="gap-2">
              <Sparkles className="w-4 h-4 text-blue-500" />
              Ask AI
            </Button>
          }
        />
      )}
      <SheetContent
        side="right"
        className="w-full sm:max-w-[480px] flex flex-col p-0 gap-0"
      >
        <SheetHeader className="px-5 py-4 border-b shrink-0">
          <div className="flex items-center justify-between gap-3">
            <SheetTitle className="flex items-center gap-2 text-base">
              <Sparkles className="w-4 h-4 text-blue-500" />
              Ask about this project
            </SheetTitle>
          </div>
          {projectName && (
            <p className="text-xs text-muted-foreground -mt-1 line-clamp-1">{projectName}</p>
          )}
          {meta && (
            <div className="flex items-center gap-2 flex-wrap text-[11px] text-muted-foreground/80 -mt-0.5">
              <span>{meta.row_count ?? "?"} rows</span>
              {meta.findings && (
                <span>· {meta.findings.total} insights</span>
              )}
            </div>
          )}
        </SheetHeader>

        <ScrollArea className="flex-1 min-h-0">
          <div className="px-5 py-4 space-y-4">
            {messages.length === 0 && (
              <ChatEmptyState
                findings={findings}
                onSuggest={(q) => ask(q)}
              />
            )}
            {messages.map((m, i) => (
              <MessageBubble
                key={i}
                role={m.role}
                content={m.content}
                streaming={streaming && i === messages.length - 1 && m.role === "assistant"}
              />
            ))}
            <div ref={scrollEndRef} />
          </div>
        </ScrollArea>

        {/* Composer */}
        <div className="border-t p-3 shrink-0 bg-background">
          <form
            className="flex items-end gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              ask(input);
            }}
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
              placeholder="Ask about pricing, contracts, anomalies…"
              rows={2}
              className="flex-1 resize-none rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 min-h-[44px] max-h-32"
              disabled={streaming}
            />
            <Button
              type="submit"
              size="icon"
              disabled={!input.trim() || streaming}
              className="h-11 w-11 shrink-0"
            >
              {streaming ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <Send className="w-4 h-4" />
              )}
            </Button>
          </form>
          <p className="text-[10px] text-muted-foreground/60 mt-1.5 px-1">
            Claude reads the project's rows + pattern findings. Cites specific row IDs.
          </p>
        </div>
      </SheetContent>
    </Sheet>
  );
}

// ── Empty state with pattern preview + suggested prompts ────────────────────

function ChatEmptyState({
  findings,
  onSuggest,
}: {
  findings: PatternFinding[];
  onSuggest: (q: string) => void;
}) {
  const top = findings.slice(0, 3);
  return (
    <div className="space-y-4">
      {top.length > 0 && (
        <Card className="p-3 bg-blue-500/5 border-blue-500/20">
          <p className="text-xs font-medium mb-2 flex items-center gap-1.5">
            <Sparkles className="w-3 h-3 text-blue-500" />
            Top insights detected
          </p>
          <div className="space-y-1.5">
            {top.map((f, i) => (
              <FindingChip key={i} finding={f} compact />
            ))}
            {findings.length > top.length && (
              <p className="text-[11px] text-muted-foreground pt-1">
                + {findings.length - top.length} more — ask Claude for details.
              </p>
            )}
          </div>
        </Card>
      )}
      <div>
        <p className="text-xs font-medium mb-2">Try asking…</p>
        <div className="space-y-1.5">
          {SUGGESTED_QUESTIONS.map((q) => (
            <button
              key={q}
              onClick={() => onSuggest(q)}
              className="w-full text-left text-xs px-3 py-2 rounded-md border border-input bg-card hover:bg-accent transition-colors"
            >
              {q}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

// ── Compact finding chip used in empty state ─────────────────────────────────

function FindingChip({ finding, compact }: { finding: PatternFinding; compact?: boolean }) {
  const Icon = finding.severity === "error" ? AlertCircle : finding.severity === "warning" ? AlertTriangle : Info;
  const color =
    finding.severity === "error" ? "text-red-500"
    : finding.severity === "warning" ? "text-amber-500"
    : "text-blue-500";
  return (
    <div className="flex items-start gap-2 text-[11px]">
      <Icon className={`w-3.5 h-3.5 ${color} shrink-0 mt-0.5`} />
      <span className={compact ? "line-clamp-2" : ""}>{finding.title}</span>
    </div>
  );
}

// ── Message bubble — rendered for both user and assistant ────────────────────

function MessageBubble({
  role,
  content,
  streaming,
}: {
  role: ChatRole;
  content: string;
  streaming: boolean;
}) {
  if (role === "user") {
    return (
      <div className="flex justify-end">
        <div className="rounded-2xl rounded-tr-sm bg-primary text-primary-foreground px-4 py-2 text-sm max-w-[85%]">
          {content}
        </div>
      </div>
    );
  }
  return (
    <div className="flex gap-2">
      <div className="w-7 h-7 rounded-full bg-blue-500/10 border border-blue-500/20 flex items-center justify-center shrink-0">
        <Sparkles className="w-3.5 h-3.5 text-blue-500" />
      </div>
      <div className="flex-1 min-w-0">
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

// Re-export for convenience — saves callers from importing the type from api.ts.
export type ChatRole = "user" | "assistant";
