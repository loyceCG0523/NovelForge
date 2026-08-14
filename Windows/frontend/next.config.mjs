/** @type {import('next').NextConfig} */
const nextConfig = {
  // 开启 React 严格模式，尽早暴露副作用和生命周期问题。
  reactStrictMode: true,
  // 关闭开发模式右下角的 Next 指示球，避免遮挡业务操作。
  devIndicators: false,
  // 导出纯静态页面，安装后由 NovelForge 本地进程提供，不依赖 Node.js 服务。
  output: "export",
  trailingSlash: true,
  // All route payloads are immutable build artifacts; live SQLite data is
  // loaded by the client APIs. Reusing the App Router payload avoids fetching
  // the same static RSC files again on every desktop tab revisit.
  experimental: {
    staleTimes: {
      dynamic: 300,
      static: 300
    }
  }
};

export default nextConfig;
