"use client";

import { useEffect, useState } from "react";

import AppShell from "@/components/AppShell";
import MetricCard from "@/components/MetricCard";
import { apiFetch, getStoredUser } from "@/lib/api";

export default function UserPage() {
  // 用户中心只展示账户概况；创作偏好已拆到个性化页面。
  const [user, setUser] = useState(null);
  const [projectCount, setProjectCount] = useState(0);

  useEffect(() => {
    // 用户基础信息来自本地会话，作品数量实时请求后端。
    setUser(getStoredUser());
    apiFetch("/api/novels").then((items) => setProjectCount(items.length)).catch(() => setProjectCount(0));
  }, []);

  return (
    <AppShell
      title="用户中心"
      subtitle="查看账号信息和当前工作空间状态"
      actions={<button className="secondary-button">编辑资料</button>}
    >
      <section className="grid-3">
        <MetricCard label="账号" value={user?.display_name || "创作者"} note={user?.email || "未读取到邮箱"} />
        <MetricCard label="套餐状态" value={user?.plan || "free"} note="后续接入计费与额度" tone="yellow" />
        <MetricCard label="作品数量" value={`${projectCount} 部`} note="当前账号下的小说项目" tone="green" />
      </section>

      <section className="panel">
        <div className="panel-header">
          <div><div className="panel-title">账号信息</div><div className="panel-subtitle">通知中心、安全设置暂不展示，保持页面轻量</div></div>
        </div>
        <div className="panel-body grid-3">
          <div className="mini-stat"><span>登录邮箱</span><strong>{user?.email || "-"}</strong></div>
          <div className="mini-stat"><span>显示名称</span><strong>{user?.display_name || "-"}</strong></div>
          <div className="mini-stat"><span>数据保留</span><strong>24 个月</strong></div>
        </div>
      </section>
    </AppShell>
  );
}
