"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { getToken } from "@/lib/api";

export default function HomePage() {
  // 根路径只做分流：已登录进入工作台，未登录进入登录页。
  const router = useRouter();

  useEffect(() => {
    router.replace(getToken() ? "/workbench" : "/login");
  }, [router]);

  return <main className="route-loading">正在进入 NovelForge...</main>;
}
