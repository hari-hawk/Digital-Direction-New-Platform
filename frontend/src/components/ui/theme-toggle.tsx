"use client";

import { useEffect, useState } from "react";
import { useTheme } from "next-themes";
import { Moon, Sun } from "lucide-react";

interface ThemeToggleProps {
  compact?: boolean;
}

export function ThemeToggle({ compact = false }: ThemeToggleProps) {
  const { resolvedTheme, setTheme } = useTheme();
  const [mounted, setMounted] = useState(false);

  useEffect(() => setMounted(true), []);

  const isDark = mounted ? resolvedTheme === "dark" : true;

  const toggle = () => setTheme(isDark ? "light" : "dark");

  if (compact) {
    return (
      <button
        type="button"
        onClick={toggle}
        aria-label={isDark ? "Switch to light theme" : "Switch to dark theme"}
        className="
          relative inline-flex h-6 w-6 items-center justify-center
          rounded-full text-muted-foreground hover:text-foreground transition-colors
        "
      >
        {isDark ? <Moon className="w-4 h-4" /> : <Sun className="w-4 h-4" />}
      </button>
    );
  }

  return (
    <button
      type="button"
      onClick={toggle}
      role="switch"
      aria-checked={!isDark}
      aria-label={isDark ? "Switch to light theme" : "Switch to dark theme"}
      className={`
        relative inline-flex h-7 w-12 shrink-0 items-center rounded-full
        border border-border/60
        transition-colors duration-300 ease-out
        ${isDark ? "bg-muted" : "bg-amber-100"}
        focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2
      `}
    >
      {/* Track labels (subtle sun / moon icons behind thumb) */}
      <span
        aria-hidden
        className={`
          absolute left-1.5 flex h-4 w-4 items-center justify-center
          text-amber-500 transition-opacity
          ${isDark ? "opacity-30" : "opacity-100"}
        `}
      >
        <Sun className="h-3 w-3" />
      </span>
      <span
        aria-hidden
        className={`
          absolute right-1.5 flex h-4 w-4 items-center justify-center
          text-sky-300 transition-opacity
          ${isDark ? "opacity-100" : "opacity-30"}
        `}
      >
        <Moon className="h-3 w-3" />
      </span>

      {/* Sliding thumb */}
      <span
        className={`
          pointer-events-none relative z-10 flex h-5 w-5 items-center justify-center
          rounded-full bg-background shadow-md ring-1 ring-border/40
          transition-transform duration-300 ease-out
          ${isDark ? "translate-x-6" : "translate-x-1"}
        `}
      >
        {isDark ? (
          <Moon className="h-3 w-3 text-sky-400" />
        ) : (
          <Sun className="h-3 w-3 text-amber-500" />
        )}
      </span>
    </button>
  );
}
