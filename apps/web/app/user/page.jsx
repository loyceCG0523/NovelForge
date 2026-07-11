import { redirect } from "next/navigation";

export default function UserPage() {
  // 用户中心已合并到设置页，保留旧路由用于兼容历史入口。
  redirect("/personalization");
}
