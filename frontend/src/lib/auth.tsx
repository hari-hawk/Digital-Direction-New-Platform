"use client";

import { createContext, useContext, useState, useEffect, ReactNode } from "react";

interface AuthContextType {
  isAuthenticated: boolean;
  login: (passphrase: string) => boolean;
  logout: () => void;
}

const AuthContext = createContext<AuthContextType | null>(null);

// Simple passphrase hash check — not bcrypt, but sufficient for a non-public tool
// In production, this would verify against a backend endpoint
const PASSPHRASE_HASH = "dd2026"; // Simple passphrase for POC

export function AuthProvider({ children }: { children: ReactNode }) {
  const [isAuthenticated, setIsAuthenticated] = useState(false);

  useEffect(() => {
    const stored = sessionStorage.getItem("dd-auth");
    if (stored === "true") setIsAuthenticated(true);
  }, []);

  const login = (passphrase: string): boolean => {
    if (passphrase === PASSPHRASE_HASH) {
      setIsAuthenticated(true);
      sessionStorage.setItem("dd-auth", "true");
      return true;
    }
    return false;
  };

  const logout = () => {
    setIsAuthenticated(false);
    sessionStorage.removeItem("dd-auth");
  };

  return (
    <AuthContext.Provider value={{ isAuthenticated, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be inside AuthProvider");
  return ctx;
}
