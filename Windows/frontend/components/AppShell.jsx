"use client";

import { useContext, useEffect, useRef } from "react";
import { createPortal } from "react-dom";

import { DesktopShellSlotsContext, recordDesktopDiagnostic } from "@/components/DesktopShellContext";

/**
 * Page-facing compatibility component. The persistent frame lives in the root
 * layout; existing pages keep passing title/actions here and those nodes are
 * portaled into the frame without making the sidebar part of the page tree.
 */
export default function AppShell({ title, actions, children }) {
  const slots = useContext(DesktopShellSlotsContext);
  const diagnosticMountedRef = useRef(false);

  useEffect(() => {
    if (!diagnosticMountedRef.current) {
      diagnosticMountedRef.current = true;
      recordDesktopDiagnostic("pageShellBridgeMounts");
    }
  }, []);

  return (
    <>
      <span hidden data-novelforge-page-title={title} />
      {slots?.title ? createPortal(<h1>{title}</h1>, slots.title) : null}
      {slots?.actions && actions ? createPortal(actions, slots.actions) : null}
      {children}
    </>
  );
}
