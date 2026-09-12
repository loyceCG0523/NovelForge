"use client";

import { SWRConfig } from "swr";

import AppShell from "@/components/AppShell";
import { apiFetch } from "@/lib/api";

// (main) 路由组的共享布局：AppShell 常驻，组内页面切换路由时外壳不再重挂，
// 鉴权、侧边栏状态（折叠/账号菜单）跨页面保持。各页面通过 AppShellRegion
// 把自己的标题和操作按钮投射到顶栏插槽。登录/注册页不在本组内，不受影响。
//
// SWRConfig 为组内所有页面提供统一的数据缓存：切回已访问过的页面时先秒显
// 缓存数据，再在后台静默刷新（stale-while-revalidate），加载态只在首次出现。
export default function MainLayout({ children }) {
  return (
    <SWRConfig
      value={{
        fetcher: apiFetch,
        revalidateOnFocus: false,
        keepPreviousData: true,
      }}
    >
      <AppShell>{children}</AppShell>
    </SWRConfig>
  );
}
