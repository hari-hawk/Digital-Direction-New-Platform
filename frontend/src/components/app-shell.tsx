"use client";

import { useEffect, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { useAuth } from "@/lib/auth";
import { useAppStore } from "@/lib/store";
import {
  LayoutDashboard,
  Upload,
  FileSearch,
  BarChart3,
  Settings,
  LogOut,
  Satellite,
  ChevronLeft,
  Command,
  Trash2,
  Wallet,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger, TooltipProvider } from "@/components/ui/tooltip";
import { Separator } from "@/components/ui/separator";
import { ThemeToggle } from "@/components/ui/theme-toggle";
import { apiGetSpend, type SpendStatus } from "@/lib/api";

import { Table2 } from "lucide-react";

export type Page = "dashboard" | "upload" | "results" | "review" | "analytics" | "bin" | "settings";

const navItems: { id: Page; label: string; icon: React.ElementType }[] = [
  { id: "dashboard", label: "Dashboard", icon: LayoutDashboard },
  { id: "upload", label: "Upload", icon: Upload },
  { id: "results", label: "Results", icon: Table2 },
  { id: "review", label: "Review", icon: FileSearch },
  { id: "analytics", label: "Analytics", icon: BarChart3 },
  { id: "bin", label: "Bin", icon: Trash2 },
  { id: "settings", label: "Settings", icon: Settings },
];

interface AppShellProps {
  children: (page: Page, navigate: (page: Page) => void) => React.ReactNode;
}

export function AppShell({ children }: AppShellProps) {
  const { logout } = useAuth();
  const { setActiveUpload, setSelectedRow } = useAppStore();
  const [activePage, setActivePage] = useState<Page>("dashboard");

  const navigateTo = (page: Page) => {
    // When clicking sidebar nav (not programmatic navigation), clear active context
    // so Results shows the project list, Review shows "select a row"
    if (page === "results") setActiveUpload(null);
    if (page === "review") setSelectedRow(null);
    setActivePage(page);
  };
  const [collapsed, setCollapsed] = useState(false);
  const [spend, setSpend] = useState<SpendStatus | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const s = await apiGetSpend();
        if (!cancelled) setSpend(s);
      } catch {
        /* best-effort; ignore */
      }
    };
    load();
    const t = setInterval(load, 30_000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, []);

  const spendTone =
    spend && spend.cap_usd > 0 && spend.pct_used >= spend.warn_at_pct
      ? "text-amber-400"
      : "text-muted-foreground";

  return (
    <TooltipProvider>
      <div className="flex h-screen overflow-hidden">
        {/* Sidebar */}
        <motion.aside
          animate={{ width: collapsed ? 72 : 240 }}
          transition={{ duration: 0.2, ease: "easeInOut" }}
          className="flex flex-col border-r border-border/50 bg-card/30 backdrop-blur-xl"
        >
          {/* Logo + top controls (collapse + theme toggle) */}
          <div className="flex items-center gap-3 px-4 h-16 shrink-0">
            <div className="flex items-center justify-center w-9 h-9 rounded-xl bg-emerald-500/10 border border-emerald-500/20 shrink-0">
              <Satellite className="w-5 h-5 text-emerald-400" />
            </div>
            <AnimatePresence>
              {!collapsed && (
                <motion.span
                  initial={{ opacity: 0, width: 0 }}
                  animate={{ opacity: 1, width: "auto" }}
                  exit={{ opacity: 0, width: 0 }}
                  className="font-semibold text-sm whitespace-nowrap overflow-hidden flex-1"
                >
                  Digital Direction
                </motion.span>
              )}
            </AnimatePresence>
            {!collapsed && (
              <Tooltip>
                <TooltipTrigger
                  onClick={() => setCollapsed(true)}
                  aria-label="Collapse sidebar"
                  className="text-muted-foreground hover:text-foreground transition-colors p-1 rounded-md hover:bg-muted/50"
                >
                  <ChevronLeft className="w-4 h-4" />
                </TooltipTrigger>
                <TooltipContent side="right">Collapse sidebar</TooltipContent>
              </Tooltip>
            )}
          </div>

          {/* Expand button (visible only when collapsed) + theme toggle */}
          <div className={`px-3 pb-2 flex items-center ${collapsed ? "flex-col gap-2" : "justify-between"}`}>
            {collapsed ? (
              <Tooltip>
                <TooltipTrigger
                  onClick={() => setCollapsed(false)}
                  aria-label="Expand sidebar"
                  className="text-muted-foreground hover:text-foreground transition-colors p-2 rounded-md hover:bg-muted/50 w-full flex justify-center"
                >
                  <ChevronLeft className="w-4 h-4 rotate-180" />
                </TooltipTrigger>
                <TooltipContent side="right">Expand sidebar</TooltipContent>
              </Tooltip>
            ) : (
              <span className="text-xs font-medium text-muted-foreground uppercase tracking-wider">
                Theme
              </span>
            )}
            <ThemeToggle compact={collapsed} />
          </div>

          <Separator className="opacity-50" />

          {/* Nav items */}
          <nav className="flex-1 py-4 px-3 space-y-1">
            {navItems.map((item) => {
              const isActive = activePage === item.id;
              const Icon = item.icon;

              return (
                <Tooltip key={item.id}>
                  <TooltipTrigger
                      onClick={() => navigateTo(item.id)}
                      className={`
                        w-full flex items-center gap-3 px-3 py-2.5 rounded-xl text-sm font-medium
                        transition-all duration-150 relative group
                        ${isActive
                          ? "bg-emerald-500/10 text-emerald-400"
                          : "text-muted-foreground hover:text-foreground hover:bg-muted/50"
                        }
                      `}
                    >
                      {isActive && (
                        <motion.div
                          layoutId="active-nav"
                          className="absolute inset-0 rounded-xl bg-emerald-500/10 border border-emerald-500/20"
                          transition={{ type: "spring", stiffness: 300, damping: 25 }}
                        />
                      )}
                      <Icon className="w-5 h-5 shrink-0 relative z-10" />
                      <AnimatePresence>
                        {!collapsed && (
                          <motion.span
                            initial={{ opacity: 0 }}
                            animate={{ opacity: 1 }}
                            exit={{ opacity: 0 }}
                            className="relative z-10 whitespace-nowrap"
                          >
                            {item.label}
                          </motion.span>
                        )}
                      </AnimatePresence>
                  </TooltipTrigger>
                  {collapsed && (
                    <TooltipContent side="right">{item.label}</TooltipContent>
                  )}
                </Tooltip>
              );
            })}
          </nav>

          {/* Bottom actions */}
          <div className="p-3 space-y-1">
            <Separator className="opacity-50 mb-3" />

            {spend && spend.cap_usd > 0 && (
              <Tooltip>
                <TooltipTrigger
                  className={`w-full flex items-center gap-3 px-3 py-2 rounded-xl text-xs ${spendTone} cursor-default`}
                >
                  <Wallet className="w-4 h-4 shrink-0" />
                  <AnimatePresence>
                    {!collapsed && (
                      <motion.div
                        initial={{ opacity: 0 }}
                        animate={{ opacity: 1 }}
                        exit={{ opacity: 0 }}
                        className="flex-1 min-w-0"
                      >
                        <div className="flex items-baseline justify-between gap-2">
                          <span className="tabular-nums whitespace-nowrap">
                            ${spend.total_usd.toFixed(2)}
                          </span>
                          <span className="text-[10px] text-muted-foreground whitespace-nowrap">
                            of ${spend.cap_usd.toFixed(0)}
                          </span>
                        </div>
                        <div className="mt-1 h-1 rounded-full bg-muted overflow-hidden">
                          <div
                            className={`h-full ${
                              spend.pct_used >= spend.warn_at_pct
                                ? "bg-amber-400"
                                : "bg-emerald-400"
                            }`}
                            style={{ width: `${Math.min(spend.pct_used, 100)}%` }}
                          />
                        </div>
                      </motion.div>
                    )}
                  </AnimatePresence>
                </TooltipTrigger>
                {collapsed && (
                  <TooltipContent side="right">
                    LLM spend: ${spend.total_usd.toFixed(2)} of ${spend.cap_usd.toFixed(0)}
                  </TooltipContent>
                )}
              </Tooltip>
            )}

            <Tooltip>
              <TooltipTrigger
                onClick={logout}
                className="w-full flex items-center gap-3 px-3 py-2.5 rounded-xl text-sm text-muted-foreground hover:text-rose-400 hover:bg-rose-500/10 transition-colors"
              >
                <LogOut className="w-5 h-5 shrink-0" />
                <AnimatePresence>
                  {!collapsed && (
                    <motion.span
                      initial={{ opacity: 0 }}
                      animate={{ opacity: 1 }}
                      exit={{ opacity: 0 }}
                      className="whitespace-nowrap"
                    >
                      Sign out
                    </motion.span>
                  )}
                </AnimatePresence>
              </TooltipTrigger>
              {collapsed && <TooltipContent side="right">Sign out</TooltipContent>}
            </Tooltip>
          </div>
        </motion.aside>

        {/* Main content */}
        <main className="flex-1 overflow-auto">
          <AnimatePresence mode="wait">
            <motion.div
              key={activePage}
              initial={{ opacity: 0, x: 10 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0, x: -10 }}
              transition={{ duration: 0.15 }}
              className="h-full"
            >
              {children(activePage, setActivePage)}
            </motion.div>
          </AnimatePresence>
        </main>
      </div>
    </TooltipProvider>
  );
}
