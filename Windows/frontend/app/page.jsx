"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

export default function HomePage() {
  // Windows 本地版没有账号系统，直接进入创作工作台。
  const router = useRouter();

  useEffect(() => {
    router.replace("/workbench");
  }, [router]);

  return <div className="route-loading">正在进入 NovelForge...</div>;
}
