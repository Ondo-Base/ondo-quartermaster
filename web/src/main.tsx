import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { ShellProvider } from "./components/Shell";
import { AdminConsole, AuditLog } from "./pages/Admin";
import { PairAgent, SignIn, SigningIn, Verify } from "./pages/Auth";
import { Files } from "./pages/Files";
import { Landing } from "./pages/Landing";
import { TaskRun } from "./pages/TaskRun";
import { WorkflowPage } from "./pages/Workflow";
import { Workspace } from "./pages/Workspace";
import { RequireVerified, SessionProvider } from "./session";
import "./styles.css";

function App() {
  return (
    <SessionProvider>
      <ShellProvider>
        <Routes>
          <Route path="/" element={<Landing />} />
          <Route path="/signin" element={<SignIn />} />
          <Route path="/verify" element={<Verify />} />
          <Route path="/pair" element={<RequireVerified><PairAgent /></RequireVerified>} />
          <Route path="/signing-in" element={<RequireVerified><SigningIn /></RequireVerified>} />
          <Route path="/app" element={<RequireVerified><Workspace /></RequireVerified>} />
          <Route path="/app/runs/:id" element={<RequireVerified><TaskRun /></RequireVerified>} />
          <Route path="/app/files" element={<RequireVerified><Files /></RequireVerified>} />
          <Route path="/app/workflows/:id" element={<RequireVerified><WorkflowPage /></RequireVerified>} />
          <Route path="/admin" element={<RequireVerified admin><AdminConsole /></RequireVerified>} />
          <Route path="/admin/audit" element={<RequireVerified admin><AuditLog /></RequireVerified>} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </ShellProvider>
    </SessionProvider>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter><App /></BrowserRouter>
  </StrictMode>,
);
