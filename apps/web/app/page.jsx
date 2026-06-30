"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { getToken } from "@/lib/api";

export default function HomePage() {
  const router = useRouter();

  useEffect(() => {
    router.replace(getToken() ? "/workbench" : "/login");
  }, [router]);

  return <main className="route-loading">正在进入 NovelForge...</main>;
}
