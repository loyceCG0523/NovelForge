"use client";

import { createContext } from "react";

export const DesktopShellSlotsContext = createContext(null);

export function recordDesktopDiagnostic(name, increment = 1) {
  if (typeof window === "undefined") return;
  const diagnostics = window.__novelforgeDiagnostics || {};
  diagnostics[name] = Number(diagnostics[name] || 0) + increment;
  window.__novelforgeDiagnostics = diagnostics;
}
