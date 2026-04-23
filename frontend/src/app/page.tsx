"use client";

import { useEffect } from "react";
import { AuthProvider, useAuth } from "@/lib/auth";
import { useAppStore } from "@/lib/store";
import { LoginScreen } from "@/components/login-screen";
import { AppShell, type Page } from "@/components/app-shell";
import { DashboardPage } from "@/components/pages/dashboard";
import { UploadPage } from "@/components/pages/upload";
import { ResultsPage } from "@/components/pages/results";
import { ReviewPage } from "@/components/pages/review";
import { AnalyticsPage } from "@/components/pages/analytics";
import { BinPage } from "@/components/pages/bin";
import { SettingsPage } from "@/components/pages/settings";

function AppContent() {
  const { isAuthenticated } = useAuth();
  const { setActiveUpload, loadUploadsFromAPI } = useAppStore();

  // Restore uploads from backend on page load/refresh
  useEffect(() => {
    if (isAuthenticated) {
      loadUploadsFromAPI();
    }
  }, [isAuthenticated, loadUploadsFromAPI]);

  if (!isAuthenticated) return <LoginScreen />;

  return (
    <AppShell>
      {(page: Page, navigate: (p: Page) => void) => {
        switch (page) {
          case "dashboard":
            return <DashboardPage onViewUpload={(id) => { setActiveUpload(id); navigate("results"); }} />;
          case "upload":
            return <UploadPage onViewResults={() => navigate("results")} />;
          case "results":
            return (
              <ResultsPage
                onReviewRow={() => navigate("review")}
                onBack={() => navigate("upload")}
              />
            );
          case "review":
            return <ReviewPage onBack={() => navigate("results")} />;
          case "analytics":
            return <AnalyticsPage />;
          case "bin":
            return <BinPage />;
          case "settings":
            return <SettingsPage />;
        }
      }}
    </AppShell>
  );
}

export default function Home() {
  return (
    <AuthProvider>
      <AppContent />
    </AuthProvider>
  );
}
