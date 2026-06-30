const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL || "http://127.0.0.1:8000";

export function getToken() {
  if (typeof window === "undefined") return "";
  return localStorage.getItem("novelforge_token") || "";
}

export function setSession(token, user) {
  localStorage.setItem("novelforge_token", token);
  localStorage.setItem("novelforge_user", JSON.stringify(user));
}

export function clearSession() {
  localStorage.removeItem("novelforge_token");
  localStorage.removeItem("novelforge_user");
}

export function getStoredUser() {
  if (typeof window === "undefined") return null;
  const raw = localStorage.getItem("novelforge_user");
  return raw ? JSON.parse(raw) : null;
}

export async function apiFetch(path, options = {}) {
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
  return apiFetch("/api/auth/register", { method: "POST", body: JSON.stringify(payload) });
}

export async function login(payload) {
  return apiFetch("/api/auth/login", { method: "POST", body: JSON.stringify(payload) });
}
