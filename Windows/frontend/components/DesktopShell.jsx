"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useLayoutEffect, useMemo, useRef, useState, useTransition } from "react";

import { DesktopShellSlotsContext, recordDesktopDiagnostic } from "@/components/DesktopShellContext";
import { apiFetchCached, getStoredUser, prefetchApi, setStoredUser } from "@/lib/api";
import { applyTheme, getPreferredTheme } from "@/lib/themes";

const navItems = [
  { href: "/workbench/", label: "创作工作台", icon: "□" },
  { href: "/story-events/", label: "剧情事件", icon: "▥" },
  { href: "/chapters/", label: "章节管理", icon: "▤" },
  { href: "/projects/", label: "作品管理", icon: "▦" },
  { href: "/memory/", label: "结构化记忆", icon: "◫" },
  { href: "/research/", label: "资料检索", icon: "⌕" },
  { href: "/meme-library/", label: "热梗库", icon: "◇" },
  { href: "/sample-analysis/", label: "本地样本", icon: "▧" }
];

// Navigation ordering protection is tied to an actual compositor frame rather
// than a wall-clock timer. After a route commits we wait for two
// requestAnimationFrame boundaries — the well-known "after paint" signal: the
// first rAF belongs to the frame that draws the new DOM, the second confirms a
// frame containing it has been presented. Only then does a destination queued
// behind it start. This prevents "DOM already switched, window still shows the
// old page" mixing without the old fixed 80/700 ms serialization delay.
// Software compositing presents far less often (often ~5 fps), so waiting for
// whole frames there would serialize every click behind hundreds of ms; it
// confirms a single frame and then falls back to a short time budget.
const GPU_PAINT_CONFIRM_FRAMES = 2;
const SOFTWARE_PAINT_CONFIRM_FRAMES = 1;
const SOFTWARE_PAINT_CONFIRM_MAX_MS = 60;

function normalizePath(path) {
  return `/${String(path || "").split("?")[0].split("#")[0].replace(/^\/+|\/+$/g, "")}`;
}

export default function DesktopShell({ children }) {
  const pathname = usePathname();
  const router = useRouter();
  const localMenuRef = useRef(null);
  const titleSlotRef = useRef(null);
  const actionsSlotRef = useRef(null);
  const diagnosticMountedRef = useRef(false);
  const navigationFrameRef = useRef(null);
  const navigationWatchdogRef = useRef(null);
  const paintConfirmFrameRef = useRef(null);
  // The one href whose router.push is in flight, or committed but not yet
  // visually painted.  Next App Router transitions are not cancellable from
  // userland, so starting another push here creates competing RSC transitions
  // whose completion order need not match click order.  Keep exactly one push
  // active and retain only the latest requested destination behind it.
  const pushedHrefRef = useRef("");
  // The latest destination the user asked for that we have not pushed yet.
  const pendingHrefRef = useRef("");
  const flushNavigationRef = useRef(null);
  const prefetchedRoutesRef = useRef(new Set());
  const hoverPrefetchRef = useRef({ href: "", timer: null });
  const [slots, setSlots] = useState(null);
  const [collapsed, setCollapsed] = useState(false);
  const [localMenuOpen, setLocalMenuOpen] = useState(false);
  const [user, setUser] = useState(() => getStoredUser());
  const [isNavigating, startNavigation] = useTransition();

  useLayoutEffect(() => {
    setSlots({ title: titleSlotRef.current, actions: actionsSlotRef.current });
  }, []);

  useEffect(() => {
    if (!diagnosticMountedRef.current) {
      diagnosticMountedRef.current = true;
      recordDesktopDiagnostic("desktopShellMounts");
    }
  }, []);

  useEffect(() => {
    const storedUser = getStoredUser();
    applyTheme(getPreferredTheme(storedUser), { notify: false });
    apiFetchCached("/api/auth/me", { ttlMs: 60_000 })
      .then((localUser) => {
        setUser(localUser);
        setStoredUser(localUser);
        applyTheme(getPreferredTheme(localUser), { notify: false });
      })
      .catch(() => {});
    prefetchApi("/api/novels", { ttlMs: 300_000 });
    // This desktop export is fully local and immutable. Prime every page tree
    // during idle time, one route at a time, so a first rapid sidebar sweep does
    // not create competing RSC/chunk requests. This mirrors a bundled SPA while
    // keeping the existing Next page modules and without delaying first paint.
    const localRoutes = [
      ...navItems.map((item) => item.href),
      "/personalization/",
      "/appearance/"
    ];
    let prefetchIndex = 0;
    let idleHandle = null;
    let timerHandle = null;
    let cancelled = false;
    const prefetchNext = () => {
      if (cancelled || prefetchIndex >= localRoutes.length) return;
      prefetchRoute(localRoutes[prefetchIndex]);
      prefetchIndex += 1;
      timerHandle = window.setTimeout(prefetchNext, 80);
    };
    if (window.requestIdleCallback) {
      idleHandle = window.requestIdleCallback(prefetchNext, { timeout: 800 });
    } else {
      timerHandle = window.setTimeout(prefetchNext, 400);
    }
    return () => {
      cancelled = true;
      if (idleHandle !== null && window.cancelIdleCallback) window.cancelIdleCallback(idleHandle);
      if (timerHandle !== null) window.clearTimeout(timerHandle);
    };
  }, []);

  function scheduleNavigation() {
    if (navigationFrameRef.current !== null) return;
    navigationFrameRef.current = window.requestAnimationFrame(() => {
      navigationFrameRef.current = null;
      flushNavigationRef.current?.();
    });
  }

  function armNavigationWatchdog(href) {
    window.clearTimeout(navigationWatchdogRef.current);
    navigationWatchdogRef.current = window.setTimeout(() => {
      if (pushedHrefRef.current !== href) return;
      // A pushed destination neither committed nor got superseded. Clear it so
      // any pending click can start from a clean slate.
      pushedHrefRef.current = "";
      recordDesktopDiagnostic("routeTransitionsTimedOut");
      if (pendingHrefRef.current) scheduleNavigation();
    }, 5000);
  }

  function confirmPaintedCommit() {
    // requestAnimationFrame fires right before Chromium presents the frame that
    // includes this commit, so it is the earliest moment the new page is
    // actually visible on screen. Waiting for it (instead of a fixed timer)
    // keeps navigation ordering protection without delaying the next click.
    // Software compositing presents infrequently; a short wall-clock budget
    // keeps a slow engine from serializing every click behind hundreds of ms.
    const rendering = String(window.__NOVELFORGE_RUNTIME__?.rendering || "gpu").toLowerCase();
    const isSoftware = rendering === "software";
    const framesNeeded = isSoftware ? SOFTWARE_PAINT_CONFIRM_FRAMES : GPU_PAINT_CONFIRM_FRAMES;
    const deadline = isSoftware ? window.performance.now() + SOFTWARE_PAINT_CONFIRM_MAX_MS : 0;
    let paintedFrames = 0;
    const confirm = () => {
      paintedFrames += 1;
      if (paintedFrames < framesNeeded && (deadline === 0 || window.performance.now() < deadline)) {
        paintConfirmFrameRef.current = window.requestAnimationFrame(confirm);
        return;
      }
      paintConfirmFrameRef.current = null;
      pushedHrefRef.current = "";
      if (pendingHrefRef.current) scheduleNavigation();
    };
    paintConfirmFrameRef.current = window.requestAnimationFrame(confirm);
  }

  flushNavigationRef.current = () => {
    const href = pendingHrefRef.current;
    if (!href) return;
    const currentPath = normalizePath(window.location.pathname);
    if (normalizePath(href) === currentPath) {
      pendingHrefRef.current = "";
      return;
    }
    const pushed = pushedHrefRef.current;
    if (pushed) {
      if (normalizePath(pushed) === currentPath) return; // committed; waiting for paint
      // The previous push is still in flight. Do not create a second competing
      // transition; requestNavigation already coalesced pendingHrefRef to the
      // newest click and the pathname effect will flush it after presentation.
      return;
    }
    pushedHrefRef.current = href;
    pendingHrefRef.current = "";
    recordDesktopDiagnostic("routeTransitionsStarted");
    startNavigation(() => router.push(href));
    armNavigationWatchdog(href);
  };

  function queueNavigation(href) {
    const requestedPath = normalizePath(href);
    const currentPath = normalizePath(window.location.pathname);
    const pushedPath = normalizePath(pushedHrefRef.current);
    if (requestedPath === currentPath) {
      if (pushedHrefRef.current && pushedPath !== currentPath) {
        // The latest click asks to stay on the page that is still visible, but
        // an older router.push cannot be canceled. Queue this current route so
        // it wins immediately after that unavoidable transition commits.
        if (pendingHrefRef.current !== href) {
          if (pendingHrefRef.current) recordDesktopDiagnostic("routeIntentsCoalesced");
          pendingHrefRef.current = href;
          recordDesktopDiagnostic("routeNavigationIntents");
        }
        return;
      }
      // A click on the currently painted tab is still the latest intent: it
      // must cancel any older destination that is only queued behind the paint
      // boundary.
      if (pendingHrefRef.current) {
        pendingHrefRef.current = "";
        recordDesktopDiagnostic("routeNavigationIntents");
        recordDesktopDiagnostic("routeIntentsCoalesced");
      }
      return;
    }
    if (pendingHrefRef.current && pendingHrefRef.current !== href) {
      recordDesktopDiagnostic("routeIntentsCoalesced");
    }
    pendingHrefRef.current = href;
    recordDesktopDiagnostic("routeNavigationIntents");
    scheduleNavigation();
  }

  function requestNavigation(event, href) {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    cancelRoutePrefetch(href);
    prefetchedRoutesRef.current.add(href);
    queueNavigation(href);
  }

  function prefetchRoute(href) {
    if (prefetchedRoutesRef.current.has(href)) return;
    prefetchedRoutesRef.current.add(href);
    router.prefetch(href);
    recordDesktopDiagnostic("intentRoutePrefetches");
  }

  function cancelRoutePrefetch(href = "") {
    const pending = hoverPrefetchRef.current;
    if (href && pending.href !== href) return;
    if (pending.timer !== null) window.clearTimeout(pending.timer);
    hoverPrefetchRef.current = { href: "", timer: null };
  }

  function scheduleRoutePrefetch(href) {
    if (prefetchedRoutesRef.current.has(href)) return;
    cancelRoutePrefetch();
    hoverPrefetchRef.current = {
      href,
      timer: window.setTimeout(() => {
        hoverPrefetchRef.current = { href: "", timer: null };
        prefetchRoute(href);
      }, 120)
    };
  }

  // Every committed pathname goes through the paint-boundary gate: the new page
  // must actually appear on screen before any destination queued behind it can
  // start. A fixed wall-clock "settle" timer is deliberately not used — waiting
  // for the next compositor frame is both correct and far snappier.
  useEffect(() => {
    const committedPath = normalizePath(pathname);
    const pushed = pushedHrefRef.current;
    if (pushed && normalizePath(pushed) === committedPath) {
      window.clearTimeout(navigationWatchdogRef.current);
      navigationWatchdogRef.current = null;
      recordDesktopDiagnostic("routeTransitionsCompleted");
      if (paintConfirmFrameRef.current !== null) {
        window.cancelAnimationFrame(paintConfirmFrameRef.current);
      }
      confirmPaintedCommit();
      return;
    }
    if (pendingHrefRef.current && normalizePath(pendingHrefRef.current) === committedPath) {
      // The latest intent already committed through another path (redirect).
      pendingHrefRef.current = "";
    }
    if (pendingHrefRef.current) scheduleNavigation();
  }, [pathname]);

  useEffect(() => () => {
    if (navigationFrameRef.current !== null) {
      window.cancelAnimationFrame(navigationFrameRef.current);
    }
    if (paintConfirmFrameRef.current !== null) {
      window.cancelAnimationFrame(paintConfirmFrameRef.current);
    }
    window.clearTimeout(navigationWatchdogRef.current);
    cancelRoutePrefetch();
  }, []);

  useEffect(() => {
    function syncStoredUser(event) {
      const nextUser = event.detail || getStoredUser();
      setUser(nextUser);
      applyTheme(getPreferredTheme(nextUser), { notify: false });
    }

    function closeLocalMenu(event) {
      if (!localMenuRef.current?.contains(event.target)) setLocalMenuOpen(false);
    }

    window.addEventListener("novelforge:user-updated", syncStoredUser);
    document.addEventListener("mousedown", closeLocalMenu);
    return () => {
      window.removeEventListener("novelforge:user-updated", syncStoredUser);
      document.removeEventListener("mousedown", closeLocalMenu);
    };
  }, []);

  const slotContext = useMemo(() => slots, [slots]);
  const displayName = user?.display_name || "本地创作者";
  const activePath = normalizePath(pathname);

  return (
    <DesktopShellSlotsContext.Provider value={slotContext}>
      <div className={`app-shell ${collapsed ? "sidebar-collapsed" : ""}`}>
        <aside className="sidebar">
          <div className="brand">
            <div className="brand-mark">N</div>
            <div className="brand-title">NovelForge</div>
            <button className="sidebar-toggle" onClick={() => setCollapsed((value) => !value)} aria-label="切换侧边栏">
              {collapsed ? "→" : "←"}
            </button>
          </div>

          <nav className="nav" aria-busy={isNavigating}>
            {navItems.map((item) => (
              <Link
                key={item.href}
                className={`nav-item ${activePath === normalizePath(item.href) ? "active" : ""}`}
                href={item.href}
                prefetch={false}
                aria-current={activePath === normalizePath(item.href) ? "page" : undefined}
                onClick={(event) => requestNavigation(event, item.href)}
                onPointerEnter={() => scheduleRoutePrefetch(item.href)}
                onPointerLeave={() => cancelRoutePrefetch(item.href)}
                onFocus={(event) => {
                  if (event.currentTarget.matches(":focus-visible")) prefetchRoute(item.href);
                }}
              >
                <span className="nav-icon">{item.icon}</span>
                <span className="nav-label">{item.label}</span>
              </Link>
            ))}
          </nav>

          <div className="sidebar-user-wrap" ref={localMenuRef}>
            {localMenuOpen ? (
              <div className="sidebar-user-menu" role="menu">
                <button type="button" role="menuitem" onPointerEnter={() => prefetchRoute("/personalization/")} onClick={() => { setLocalMenuOpen(false); queueNavigation("/personalization/"); }}>模型与创作设置</button>
                <button type="button" role="menuitem" onPointerEnter={() => prefetchRoute("/appearance/")} onClick={() => { setLocalMenuOpen(false); queueNavigation("/appearance/"); }}>外观设置</button>
              </div>
            ) : null}
            <button className="sidebar-user" type="button" title="本地应用设置" onClick={() => setLocalMenuOpen((open) => !open)}>
              <div className="avatar">N</div>
              <div className="sidebar-user-text">
                <strong>{displayName}</strong>
                <span className="pro">Windows 本地版</span>
              </div>
            </button>
          </div>
        </aside>

        <main className="main">
          <header className="topbar">
            <div className="title-block" ref={titleSlotRef}>{slots ? null : <h1>NovelForge</h1>}</div>
            <div className="top-actions" ref={actionsSlotRef} />
          </header>
          <div className="content">{children}</div>
        </main>
      </div>
    </DesktopShellSlotsContext.Provider>
  );
}
