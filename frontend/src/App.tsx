import { Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { RequireAuth } from "./features/auth/RequireAuth";
import { AdminPage } from "./pages/AdminPage";
import { ConnectorsPage } from "./pages/ConnectorsPage";
import { AgentPage } from "./pages/AgentPage";
import { ChatPage } from "./pages/ChatPage";
import { CitationExplorerPage } from "./pages/CitationExplorerPage";
import { DashboardPage } from "./pages/DashboardPage";
import { DocumentViewerPage } from "./pages/DocumentViewerPage";
import { DocumentsPage } from "./pages/DocumentsPage";
import { EvaluationPage } from "./pages/EvaluationPage";
import { KnowledgeExplorerPage } from "./pages/KnowledgeExplorerPage";
import { LoginPage } from "./pages/LoginPage";
import { ReportViewPage } from "./pages/ReportViewPage";
import { ReportsPage } from "./pages/ReportsPage";
import { RunInspectorPage } from "./pages/RunInspectorPage";
import { RunsPage } from "./pages/RunsPage";
import { SettingsPage } from "./pages/SettingsPage";

export function AppRoutes() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route element={<RequireAuth><Layout /></RequireAuth>}>
        <Route index element={<DashboardPage />} />
        <Route path="agent" element={<AgentPage />} />
        <Route path="chat" element={<ChatPage />} />
        <Route path="chat/:conversationId" element={<ChatPage />} />
        <Route path="documents" element={<DocumentsPage />} />
        <Route path="documents/:id" element={<DocumentViewerPage />} />
        <Route path="explorer" element={<KnowledgeExplorerPage />} />
        <Route path="citations" element={<CitationExplorerPage />} />
        <Route path="runs" element={<RunsPage />} />
        <Route path="runs/:id" element={<RunInspectorPage />} />
        <Route path="reports" element={<ReportsPage />} />
        <Route path="reports/:id" element={<ReportViewPage />} />
        <Route path="evaluation" element={<EvaluationPage />} />
        <Route path="settings" element={<SettingsPage />} />
        <Route path="connectors" element={<RequireAuth role="admin"><ConnectorsPage /></RequireAuth>} />
        <Route path="admin" element={<RequireAuth role="admin"><AdminPage /></RequireAuth>} />
        <Route path="*" element={<div className="empty"><h3>Page not found</h3></div>} />
      </Route>
    </Routes>
  );
}
