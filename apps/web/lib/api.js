const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL || "http://127.0.0.1:8000";

export function getToken() {
  // Next.js 可能在服务端渲染阶段执行模块代码，访问 localStorage 前必须判断 window。
  if (typeof window === "undefined") return "";
  return localStorage.getItem("novelforge_token") || "";
}

export function setSession(token, user) {
  // 当前 MVP 使用 localStorage 保存会话；生产环境可替换为更严格的 Cookie 策略。
  localStorage.setItem("novelforge_token", token);
  localStorage.setItem("novelforge_user", JSON.stringify(user));
}

export function clearSession() {
  // 登录失效或用户退出时清理前端会话。
  localStorage.removeItem("novelforge_token");
  localStorage.removeItem("novelforge_user");
}

export function getStoredUser() {
  // 侧边栏用户信息从本地缓存读取，避免每次页面切换都请求 /me。
  if (typeof window === "undefined") return null;
  const raw = localStorage.getItem("novelforge_user");
  return raw ? JSON.parse(raw) : null;
}

export async function apiFetch(path, options = {}) {
  // 所有前端请求统一经过这里，集中处理 JSON、鉴权头和错误提示。
  const headers = new Headers(options.headers || {});
  if (!headers.has("Content-Type") && options.body) headers.set("Content-Type", "application/json");
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);

  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...options,
    headers,
    cache: "no-store"
  });

  if (!response.ok) {
    let message = `Request failed: ${response.status}`;
    try {
      const error = await response.json();
      message = typeof error.detail === "string" ? error.detail : JSON.stringify(error.detail || error);
    } catch {
      message = await response.text();
    }
    throw new Error(message);
  }

  if (response.status === 204) return null;
  return response.json();
}

export async function register(payload) {
  // 注册成功时后端会直接返回 token 和用户信息。
  return apiFetch("/api/auth/register", { method: "POST", body: JSON.stringify(payload) });
}

export async function login(payload) {
  // 登录接口与 register 返回同样的 TokenRead 结构。
  return apiFetch("/api/auth/login", { method: "POST", body: JSON.stringify(payload) });
}
