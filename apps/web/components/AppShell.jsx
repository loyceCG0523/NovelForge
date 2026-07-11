"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { clearSession, getStoredUser, getToken } from "@/lib/api";

// 主导航只保留真正的产品功能；创作过程状态放到工作台内部展示。
const navItems = [
  { href: "/workbench", label: "创作工作台", icon: "□" },
  { href: "/story-events", label: "剧情事件", icon: "▥" },
  { href: "/chapters", label: "章节管理", icon: "▤" },
  { href: "/projects", label: "作品管理", icon: "▦" },
  { href: "/sample-analysis", label: "样本分析", icon: "▧" },
  { href: "/personalization", label: "设置", icon: "●" }
];

export default function AppShell({ title, subtitle, actions, children }) {
  // AppShell 统一承载鉴权检查、侧边栏、顶部标题区和页面内容容器。
  const pathname = usePathname();
  const router = useRouter();
  const [collapsed, setCollapsed] = useState(false);
  const [user, setUser] = useState(null);

  useEffect(() => {
    // 没有 token 时直接回到登录页，避免未登录用户访问业务页面。
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    setUser(getStoredUser());
  }, [router]);

  function handleLogout() {
    // 退出只清理前端会话，后续如果引入服务端会话可在这里追加 revoke 请求。
    clearSession();
    router.replace("/login");
  }

  return (
    <div className={`app-shell ${collapsed ? "sidebar-collapsed" : ""}`}>
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">N</div>
          <div className="brand-title">NovelForge</div>
          <button className="sidebar-toggle" onClick={() => setCollapsed((value) => !value)} aria-label="切换侧边栏">
            {collapsed ? "→" : "←"}
          </button>
        </div>

        <nav className="nav">
          {navItems.map((item) => (
            <Link key={item.href} className={`nav-item ${pathname === item.href ? "active" : ""}`} href={item.href}>
              <span className="nav-icon">{item.icon}</span>
              <span className="nav-label">{item.label}</span>
            </Link>
          ))}
        </nav>

        <div className="sidebar-user">
          <div className="avatar">{user?.display_name?.slice(0, 1) || "N"}</div>
          <div className="sidebar-user-text">
            <strong>{user?.display_name || "创作者"}</strong>
            <span>Pro 创作者</span>
          </div>
          <button className="logout-button" onClick={handleLogout} aria-label="退出登录">×</button>
        </div>
      </aside>

      <main className="main">
        <header className="topbar">
          <div className="title-block">
            <h1>{title}</h1>
            <p>{subtitle}</p>
          </div>
          <div className="top-actions">{actions}</div>
        </header>
        <div className="content">{children}</div>
      </main>
    </div>
  );
}
