export const THEME_STORAGE_KEY = "novelforge_theme";
export const DEFAULT_THEME = "ink";

export const THEMES = [
  {
    id: "ink",
    name: "Ink",
    chineseName: "墨痕",
    tagline: "让界面退后，让文字站到纸面中央。",
    description: "黑白、纸张与书写感。克制的边界、温暖的纸色和朱砂强调，适合长时间专注创作。",
    suitableFor: "专注创作 · 作者工作台",
    palette: ["#1d1c19", "#fffdf7", "#d8d0c2", "#9b4232"]
  },
  {
    id: "aurora",
    name: "Aurora",
    chineseName: "极光",
    tagline: "把灵感放进一片会发光的天幕。",
    description: "靛青、星紫与极光青交叠，玻璃质感承载内容，梦幻但不牺牲正文可读性。",
    suitableFor: "网文 · 科幻 · 奇幻小说",
    palette: ["#17162f", "#6c5ce7", "#45c8cb", "#f4a9d8"]
  },
  {
    id: "velvet",
    name: "Velvet",
    chineseName: "绒夜",
    tagline: "像坐进深夜书房，安静地写完下一章。",
    description: "深墨、酒红与旧金构成沉浸夜色，弱化屏幕眩光，强调优雅、稳定的长篇阅读体验。",
    suitableFor: "长篇阅读 · 夜间写作",
    palette: ["#0f1017", "#211a22", "#b36b82", "#c8a46a"]
  },
  {
    id: "luna",
    name: "Luna",
    chineseName: "月影",
    tagline: "柔和月光下，情绪有足够的空间慢慢发生。",
    description: "雾蓝、月白与灰紫形成安静的低对比氛围，圆润、柔和，适合情感和治愈类故事。",
    suitableFor: "治愈 · 情感类小说",
    palette: ["#26344d", "#f7f9fc", "#7189b5", "#c6a6ad"]
  }
];

const THEME_IDS = new Set(THEMES.map((theme) => theme.id));

export function normalizeTheme(theme) {
  return THEME_IDS.has(theme) ? theme : DEFAULT_THEME;
}

export function getStoredTheme() {
  if (typeof window === "undefined") return DEFAULT_THEME;
  return normalizeTheme(window.localStorage.getItem(THEME_STORAGE_KEY));
}

export function getPreferredTheme(user) {
  const accountTheme = user?.preferences?.appearance?.theme;
  return accountTheme ? normalizeTheme(accountTheme) : getStoredTheme();
}

export function applyTheme(theme, { persist = true, notify = true } = {}) {
  const normalized = normalizeTheme(theme);
  if (typeof document !== "undefined") {
    document.documentElement.dataset.theme = normalized;
    document.documentElement.style.colorScheme = normalized === "velvet" ? "dark" : "light";
  }
  if (typeof window !== "undefined" && persist) {
    window.localStorage.setItem(THEME_STORAGE_KEY, normalized);
  }
  if (typeof window !== "undefined" && notify) {
    window.dispatchEvent(new CustomEvent("novelforge:theme-updated", { detail: normalized }));
  }
  return normalized;
}
