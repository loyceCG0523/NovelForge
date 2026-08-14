"use client";

import { useEffect, useRef } from "react";

/**
 * 串行执行状态刷新，避免 setInterval 请求重叠造成旧响应覆盖新状态。
 * 浏览器重新获得焦点或标签页重新可见时会立即补刷一次。
 */
export function useLiveRefresh({ enabled, refresh, onError, intervalMs = 2500 }) {
  const refreshRef = useRef(refresh);
  const errorRef = useRef(onError);

  useEffect(() => {
    refreshRef.current = refresh;
    errorRef.current = onError;
  }, [refresh, onError]);

  useEffect(() => {
    if (!enabled) return undefined;

    let disposed = false;
    let running = false;
    let timer = null;

    function schedule() {
      if (disposed) return;
      window.clearTimeout(timer);
      timer = window.setTimeout(run, intervalMs);
    }

    async function run() {
      if (disposed || running) return;
      running = true;
      try {
        await refreshRef.current?.();
      } catch (error) {
        if (!disposed) errorRef.current?.(error);
      } finally {
        running = false;
        schedule();
      }
    }

    function refreshWhenVisible() {
      if (document.visibilityState !== "visible") return;
      window.clearTimeout(timer);
      run();
    }

    // The page has already loaded the active task once before enabling polling.
    // Start with the regular delay so mounting does not duplicate that request.
    schedule();
    window.addEventListener("focus", refreshWhenVisible);
    document.addEventListener("visibilitychange", refreshWhenVisible);
    return () => {
      disposed = true;
      window.clearTimeout(timer);
      window.removeEventListener("focus", refreshWhenVisible);
      document.removeEventListener("visibilitychange", refreshWhenVisible);
    };
  }, [Boolean(enabled), intervalMs]);
}
