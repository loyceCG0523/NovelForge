import "./globals.css";

export const metadata = {
  title: "NovelForge",
  description: "长篇小说 Agent 创作系统"
};

export default function RootLayout({ children }) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
