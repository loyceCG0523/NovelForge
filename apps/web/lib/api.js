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

export function setStoredUser(user) {
  // 更新偏好后只刷新用户缓存，不改动现有 token。
  localStorage.setItem("novelforge_user", JSON.stringify(user));
  window.dispatchEvent(new CustomEvent("novelforge:user-updated", { detail: user }));
}

export function clearSession() {
  // 登录失效或用户退出时清理前端会话。
  localStorage.removeItem("novelforge_token");
  localStorage.removeItem("novelforge_user");
}

function handleUnauthorized(path, response) {
  if (response.status !== 401 || typeof window === "undefined") return false;
  if (path === "/api/auth/login" || path === "/api/auth/register") return false;

  clearSession();
  sessionStorage.setItem("novelforge_login_notice", "登录状态已失效，请重新登录。");
  window.location.replace("/login?reason=session_expired");
  return true;
}

export function getStoredUser() {
  // 侧边栏用户信息从本地缓存读取，避免每次页面切换都请求 /me。
  if (typeof window === "undefined") return null;
  const raw = localStorage.getItem("novelforge_user");
  return raw ? JSON.parse(raw) : null;
}

export function buildTimestampedDownloadFilename(title, extension, exportedAt = new Date()) {
  const pad = (value) => String(value).padStart(2, "0");
  const safeTitle = String(title || "novel").replace(/[\\/:*?"<>|]/g, "_").trim() || "novel";
  const timestamp = [
    exportedAt.getFullYear(),
    pad(exportedAt.getMonth() + 1),
    pad(exportedAt.getDate())
  ].join("") + `_${pad(exportedAt.getHours())}${pad(exportedAt.getMinutes())}`;
  return `${safeTitle}_${timestamp}.${String(extension || "txt").replace(/^\./, "")}`;
}

export async function apiFetch(path, options = {}) {
  // 所有前端请求统一经过这里，集中处理 JSON、鉴权头和错误提示。
  const headers = new Headers(options.headers || {});
  const isFormData = typeof FormData !== "undefined" && options.body instanceof FormData;
  if (!headers.has("Content-Type") && options.body && !isFormData) headers.set("Content-Type", "application/json");
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);

  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...options,
    headers,
    cache: "no-store"
  });

  if (!response.ok) {
    if (handleUnauthorized(path, response)) {
      throw new Error("登录状态已失效，正在返回登录页。");
    }
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

function getDownloadFilename(response, fallback) {
  // 后端使用 Content-Disposition 传文件名；解析失败时用前端兜底名。
  const disposition = response.headers.get("Content-Disposition") || "";
  const encodedMatch = disposition.match(/filename\*=UTF-8''([^;]+)/i);
  if (encodedMatch?.[1]) return decodeURIComponent(encodedMatch[1]);
  const plainMatch = disposition.match(/filename="?([^";]+)"?/i);
  return plainMatch?.[1] || fallback;
}

export async function apiDownload(path, fallbackFilename = "novelforge-export.txt") {
  // 文件下载也需要携带登录 Token，但响应体是 Blob，不走 apiFetch 的 JSON 解析。
  const headers = new Headers();
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);

  const response = await fetch(`${API_BASE_URL}${path}`, {
    headers,
    cache: "no-store"
  });

  if (!response.ok) {
    if (handleUnauthorized(path, response)) {
      throw new Error("登录状态已失效，正在返回登录页。");
    }
    let message = `Request failed: ${response.status}`;
    try {
      const error = await response.json();
      message = typeof error.detail === "string" ? error.detail : JSON.stringify(error.detail || error);
    } catch {
      message = await response.text();
    }
    throw new Error(message);
  }

  const blob = await response.blob();
  const filename = getDownloadFilename(response, fallbackFilename);
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
  return filename;
}

export async function register(payload) {
  // 注册成功时后端会直接返回 token 和用户信息。
  return apiFetch("/api/auth/register", { method: "POST", body: JSON.stringify(payload) });
}

export async function login(payload) {
  // 登录接口与 register 返回同样的 TokenRead 结构。
  return apiFetch("/api/auth/login", { method: "POST", body: JSON.stringify(payload) });
}
