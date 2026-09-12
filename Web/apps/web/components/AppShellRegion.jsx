"use client";

import { useEffect, useLayoutEffect, useState } from "react";
import { createPortal } from "react-dom";

// SSR 阶段没有 document，退回 useEffect 避免 Next.js 告警；
// 浏览器端用 useLayoutEffect，确保插槽内容在绘制前就绪，切换页面时顶栏不闪空。
const useIsomorphicLayoutEffect = typeof window !== "undefined" ? useLayoutEffect : useEffect;

function useShellSlot(slotId) {
  const [target, setTarget] = useState(null);
  useIsomorphicLayoutEffect(() => {
    setTarget(document.getElementById(slotId));
  }, [slotId]);
  return target;
}

/**
 * 页面用它在常驻 AppShell 的顶栏中注册自己的标题和操作区。
 * 外壳由 app/(main)/layout.jsx 挂载并保持，页面切换时只替换本组件投射的内容，
 * 侧边栏、鉴权状态等不再随路由重建。页面卸载时投射内容自动移除。
 */
export default function AppShellRegion({ title, actions }) {
  const titleTarget = useShellSlot("appshell-title-slot");
  const actionsTarget = useShellSlot("appshell-actions-slot");
  return (
    <>
      {titleTarget ? createPortal(title, titleTarget) : null}
      {actions && actionsTarget ? createPortal(actions, actionsTarget) : null}
    </>
  );
}
