import "./globals.css";

export const metadata = {
  title: "NovelForge",
  description: "长篇小说 Agent 创作系统"
};

export default function RootLayout({ children }) {
  // Next.js App Router 根布局，所有页面共享全局 CSS。
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
