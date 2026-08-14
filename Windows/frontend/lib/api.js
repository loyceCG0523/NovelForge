// 桌面版由应用内的回环端口同时提供页面和 API，因此始终使用同源地址。
// 壳层优先复用固定端口以命中 Chromium 磁盘缓存，端口占用时会安全回退。
const API_BASE_URL = "";

// 页面之间共享短期只读缓存。桌面版 API 与前端同进程，数据量不大；这里主要
// 避免每次切换侧栏时重复等待相同请求，并合并同时发起的请求。
const apiResponseCache = new Map();
const apiRequestsInFlight = new Map();

export function getCachedApiData(path) {
  const entry = apiResponseCache.get(path);
  if (!entry) return undefined;
  if (entry.expiresAt <= Date.now()) {
    apiResponseCache.delete(path);
    return undefined;
  }
  return entry.data;
}

export function invalidateApiCache(pathPrefix = "") {
  for (const path of apiResponseCache.keys()) {
    if (!pathPrefix || path.startsWith(pathPrefix)) apiResponseCache.delete(path);
  }
}

export function getToken() {
  return "desktop-local";
}

export function isTaskInFlight(status) {
  return ["queued", "running"].includes(String(status || ""));
}

export function isTaskSettled(status) {
  return ["completed", "failed", "cancelled", "waiting"].includes(String(status || ""));
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
  return false;
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

  const method = String(options.method || "GET").toUpperCase();
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

  if (method !== "GET" && method !== "HEAD") invalidateApiCache();
  if (response.status === 204) return null;
  return response.json();
}

export async function apiFetchCached(path, cacheOptions = {}) {
  const {
    ttlMs = 30_000,
    force = false,
    ...fetchOptions
  } = cacheOptions;
  const method = String(fetchOptions.method || "GET").toUpperCase();
  if (method !== "GET" && method !== "HEAD") return apiFetch(path, fetchOptions);

  if (!force) {
    const cached = getCachedApiData(path);
    if (cached !== undefined) return cached;
    const pending = apiRequestsInFlight.get(path);
    if (pending) return pending;
  }

  const request = apiFetch(path, fetchOptions)
    .then((data) => {
      apiResponseCache.set(path, {
        data,
        expiresAt: Date.now() + Math.max(0, Number(ttlMs) || 0)
      });
      return data;
    })
    .finally(() => {
      if (apiRequestsInFlight.get(path) === request) apiRequestsInFlight.delete(path);
    });
  apiRequestsInFlight.set(path, request);
  return request;
}

export function prefetchApi(path, cacheOptions = {}) {
  return apiFetchCached(path, cacheOptions).catch(() => undefined);
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

export function subscribeSse(path, onEvent, onError) {
  // 使用带Authorization的fetch读取SSE；断线后按最后事件序号继续补拉。
  const controller = new AbortController();
  let lastSequence = 0;

  async function connect() {
    while (!controller.signal.aborted) {
      const separator = path.includes("?") ? "&" : "?";
      const target = `${API_BASE_URL}${path}${separator}after_sequence=${lastSequence}`;
      try {
        const headers = new Headers({ Accept: "text/event-stream" });
        const token = getToken();
        if (token) headers.set("Authorization", `Bearer ${token}`);
        const response = await fetch(target, { headers, cache: "no-store", signal: controller.signal });
        if (!response.ok || !response.body) throw new Error(`实时连接失败：${response.status}`);
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (!controller.signal.aborted) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
          let boundary = buffer.indexOf("\n\n");
          while (boundary >= 0) {
            const block = buffer.slice(0, boundary);
            buffer = buffer.slice(boundary + 2);
            const dataLine = block.split("\n").find((line) => line.startsWith("data:"));
            if (dataLine) {
              const event = JSON.parse(dataLine.slice(5).trim());
              lastSequence = Math.max(lastSequence, Number(event.sequence_no || 0));
              onEvent?.(event);
            }
            boundary = buffer.indexOf("\n\n");
          }
        }
      } catch (error) {
        if (!controller.signal.aborted) onError?.(error);
      }
      if (!controller.signal.aborted) {
        await new Promise((resolve) => window.setTimeout(resolve, 1200));
      }
    }
  }

  connect();
  return () => controller.abort();
}

export async function register(payload) {
  // 注册成功时后端会直接返回 token 和用户信息。
  return apiFetch("/api/auth/register", { method: "POST", body: JSON.stringify(payload) });
}

export async function login(payload) {
  // 登录接口与 register 返回同样的 TokenRead 结构。
  return apiFetch("/api/auth/login", { method: "POST", body: JSON.stringify(payload) });
}
