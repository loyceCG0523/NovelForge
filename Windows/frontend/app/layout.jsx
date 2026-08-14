import "./globals.css";

import DesktopShell from "@/components/DesktopShell";

const themeBootScript = `
  (() => {
    const allowed = new Set(["ink", "aurora", "velvet", "luna"]);
    const stored = localStorage.getItem("novelforge_theme");
    const theme = allowed.has(stored) ? stored : "ink";
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme === "velvet" ? "dark" : "light";
  })();
`;

export const metadata = {
  title: "NovelForge",
  description: "长篇小说 Agent 创作系统"
};

export default function RootLayout({ children }) {
  // Next.js App Router 根布局，所有页面共享全局 CSS。
  return (
    <html className="desktop-app" lang="zh-CN" data-theme="ink" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeBootScript }} />
      </head>
      <body><DesktopShell>{children}</DesktopShell></body>
    </html>
  );
}
