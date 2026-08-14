/** @type {import('next').NextConfig} */
const nextConfig = {
  // 开启 React 严格模式，尽早暴露副作用和生命周期问题。
  reactStrictMode: true,
  // 关闭开发模式右下角的 Next 指示球，避免遮挡业务操作。
  devIndicators: false
};

export default nextConfig;
