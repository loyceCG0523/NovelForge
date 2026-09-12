"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { apiFetch, clearSession, getStoredUser, getToken, setStoredUser } from "@/lib/api";
import { applyTheme, getPreferredTheme } from "@/lib/themes";

// 主导航只保留真正的产品功能；创作过程状态放到工作台内部展示。
const navItems = [
  { href: "/workbench", label: "创作工作台", icon: "□" },
  { href: "/story-events", label: "剧情事件", icon: "▥" },
  { href: "/chapters", label: "章节管理", icon: "▤" },
  { href: "/projects", label: "作品管理", icon: "▦" },
  { href: "/research", label: "资料检索", icon: "⌕" },
  { href: "/meme-library", label: "热梗库", icon: "◇" }
];

const AVATAR_CROP_SIZE = 220;

export default function AppShell({ title, actions, children }) {
  // AppShell 统一承载鉴权检查、侧边栏、顶部标题区和页面内容容器。
  const pathname = usePathname();
  const router = useRouter();
  const userMenuRef = useRef(null);
  const profileAvatarInputRef = useRef(null);
  const cropDragRef = useRef(null);
  const [collapsed, setCollapsed] = useState(false);
  const [user, setUser] = useState(null);
  const [userMenuOpen, setUserMenuOpen] = useState(false);
  const [profileDialogOpen, setProfileDialogOpen] = useState(false);
  const [profileForm, setProfileForm] = useState({ display_name: "", avatar_url: "" });
  const [profileMessage, setProfileMessage] = useState("");
  const [profileError, setProfileError] = useState("");
  const [savingProfile, setSavingProfile] = useState(false);
  const [cropSource, setCropSource] = useState("");
  const [cropImageSize, setCropImageSize] = useState({ width: 0, height: 0 });
  const [cropScale, setCropScale] = useState(1);
  const [cropOffset, setCropOffset] = useState({ x: 0, y: 0 });

  useEffect(() => {
    // 没有 token 时直接回到登录页，避免未登录用户访问业务页面。
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    const storedUser = getStoredUser();
    setUser(storedUser);
    applyTheme(getPreferredTheme(storedUser), { notify: false });
  }, [router]);

  useEffect(() => {
    function syncStoredUser(event) {
      const nextUser = event.detail || getStoredUser();
      setUser(nextUser);
      applyTheme(getPreferredTheme(nextUser), { notify: false });
    }

    window.addEventListener("novelforge:user-updated", syncStoredUser);
    return () => window.removeEventListener("novelforge:user-updated", syncStoredUser);
  }, []);

  useEffect(() => {
    function closeUserMenu(event) {
      if (!userMenuRef.current?.contains(event.target)) {
        setUserMenuOpen(false);
      }
    }

    document.addEventListener("mousedown", closeUserMenu);
    return () => document.removeEventListener("mousedown", closeUserMenu);
  }, []);

  const avatarUrl = user?.preferences?.profile?.avatar_url || "";
  const displayName = user?.display_name || "创作者";
  const planLabel = (user?.plan || "free").toLowerCase() === "pro" ? "Pro 会员" : "Free";

  function handleLogout() {
    clearSession();
    router.replace("/login");
  }

  function openProfileDialog() {
    setProfileForm({
      display_name: user?.display_name || "",
      avatar_url: user?.preferences?.profile?.avatar_url || ""
    });
    setCropSource("");
    setCropScale(1);
    setCropOffset({ x: 0, y: 0 });
    setCropImageSize({ width: 0, height: 0 });
    setProfileMessage("");
    setProfileError("");
    setProfileDialogOpen(true);
    setUserMenuOpen(false);
  }

  async function saveProfile() {
    setProfileMessage("");
    setProfileError("");
    if (!profileForm.display_name.trim()) {
      setProfileError("用户名不能为空。");
      return;
    }
    setSavingProfile(true);
    try {
      const avatarUrl = cropSource
        ? await renderAvatarCrop(cropSource, cropImageSize, cropScale, cropOffset)
        : profileForm.avatar_url;
      const savedUser = await apiFetch("/api/users/me/profile", {
        method: "PATCH",
        body: JSON.stringify({
          display_name: profileForm.display_name,
          avatar_url: avatarUrl
        })
      });
      setUser(savedUser);
      setStoredUser(savedUser);
      setProfileDialogOpen(false);
    } catch (err) {
      setProfileError(err.message);
    } finally {
      setSavingProfile(false);
    }
  }

  function handleProfileAvatarFile(event) {
    const file = event.target.files?.[0];
    event.target.value = "";
    setProfileError("");
    if (!file) return;
    if (!file.type.startsWith("image/")) {
      setProfileError("请选择图片文件作为头像。");
      return;
    }
    readImageFileAsDataUrl(file)
      .then((avatarDataUrl) => {
        setCropSource(avatarDataUrl);
        setCropImageSize({ width: 0, height: 0 });
        setCropScale(1);
        setCropOffset({ x: 0, y: 0 });
      })
      .catch((err) => setProfileError(err.message || "头像读取失败，请换一张图片重试。"));
  }

  function handleCropImageLoad(event) {
    setCropImageSize({
      width: event.currentTarget.naturalWidth,
      height: event.currentTarget.naturalHeight
    });
    setCropOffset({ x: 0, y: 0 });
  }

  function updateCropScale(value) {
    const nextScale = Number(value);
    setCropScale(nextScale);
    setCropOffset((current) => clampCropOffset(current, cropImageSize, nextScale));
  }

  function startCropDrag(event) {
    event.preventDefault();
    event.currentTarget.setPointerCapture?.(event.pointerId);
    cropDragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      originX: cropOffset.x,
      originY: cropOffset.y
    };
  }

  function moveCropDrag(event) {
    const drag = cropDragRef.current;
    if (!drag) return;
    const nextOffset = {
      x: drag.originX + event.clientX - drag.startX,
      y: drag.originY + event.clientY - drag.startY
    };
    setCropOffset(clampCropOffset(nextOffset, cropImageSize, cropScale));
  }

  function endCropDrag(event) {
    if (cropDragRef.current?.pointerId === event.pointerId) {
      event.currentTarget.releasePointerCapture?.(event.pointerId);
    }
    cropDragRef.current = null;
  }

  async function applyAvatarCrop() {
    try {
      const avatarDataUrl = await renderAvatarCrop(cropSource, cropImageSize, cropScale, cropOffset);
      setProfileForm((current) => ({ ...current, avatar_url: avatarDataUrl }));
      setCropSource("");
      setCropScale(1);
      setCropOffset({ x: 0, y: 0 });
    } catch (err) {
      setProfileError(err.message || "头像裁剪失败，请重试。");
    }
  }

  const cropDisplay = getCropDisplaySize(cropImageSize, cropScale);
  const cropImageStyle = cropSource && cropDisplay.width > 0
    ? {
        width: `${cropDisplay.width}px`,
        height: `${cropDisplay.height}px`,
        left: `${(AVATAR_CROP_SIZE - cropDisplay.width) / 2 + cropOffset.x}px`,
        top: `${(AVATAR_CROP_SIZE - cropDisplay.height) / 2 + cropOffset.y}px`
      }
    : undefined;

  return (
    <div className={`app-shell ${collapsed ? "sidebar-collapsed" : ""}`}>
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">N</div>
          <div className="brand-title">NovelForge</div>
          <button className="sidebar-toggle" onClick={() => setCollapsed((value) => !value)} aria-label="切换侧边栏">
            {collapsed ? "→" : "←"}
          </button>
        </div>

        <nav className="nav">
          {navItems.map((item) => (
            <Link key={item.href} className={`nav-item ${pathname === item.href ? "active" : ""}`} href={item.href}>
              <span className="nav-icon">{item.icon}</span>
              <span className="nav-label">{item.label}</span>
            </Link>
          ))}
        </nav>

        <div className="sidebar-user-wrap" ref={userMenuRef}>
          {userMenuOpen ? (
            <div className="sidebar-user-menu" role="menu">
              <button type="button" role="menuitem" onClick={openProfileDialog}>个人资料</button>
              <button type="button" role="menuitem" onClick={() => { setUserMenuOpen(false); router.push("/personalization"); }}>设置</button>
              <button type="button" role="menuitem" onClick={() => { setUserMenuOpen(false); router.push("/appearance"); }}>外观设置</button>
              <button type="button" role="menuitem" className="danger" onClick={handleLogout}>退出登录</button>
            </div>
          ) : null}
          <button className="sidebar-user" type="button" title="账号菜单" onClick={() => setUserMenuOpen((open) => !open)}>
            <div className="avatar">
              {avatarUrl ? <img src={avatarUrl} alt={`${displayName} 的头像`} /> : displayName.slice(0, 1)}
            </div>
            <div className="sidebar-user-text">
              <strong>{displayName}</strong>
              <span className={planLabel === "Free" ? "free" : "pro"}>{planLabel}</span>
            </div>
          </button>
        </div>
      </aside>

      {profileDialogOpen ? (
        <div className="modal-backdrop" role="presentation" onClick={() => setProfileDialogOpen(false)}>
          <div className="confirm-dialog profile-dialog" role="dialog" aria-modal="true" aria-labelledby="profile-dialog-title" onClick={(event) => event.stopPropagation()}>
            <div className="profile-dialog-head">
              <h2 id="profile-dialog-title">编辑个人资料</h2>
            </div>
            <input
              ref={profileAvatarInputRef}
              className="visually-hidden-input"
              type="file"
              accept="image/*"
              onChange={handleProfileAvatarFile}
            />
            {!cropSource ? (
              <button className="profile-avatar-editor" type="button" onClick={() => profileAvatarInputRef.current?.click()} aria-label="上传头像">
                <div className="profile-avatar-large">
                  {profileForm.avatar_url ? <img src={profileForm.avatar_url} alt="头像预览" /> : (profileForm.display_name || displayName).slice(0, 1)}
                </div>
                <span className="profile-avatar-camera">📷</span>
              </button>
            ) : (
              <div className="avatar-crop-panel">
                <div
                  className="avatar-crop-frame"
                  onPointerDown={startCropDrag}
                  onPointerMove={moveCropDrag}
                  onPointerUp={endCropDrag}
                  onPointerCancel={endCropDrag}
                >
                  <img src={cropSource} alt="头像裁剪" style={{ ...cropImageStyle, visibility: cropImageStyle ? "visible" : "hidden" }} onLoad={handleCropImageLoad} draggable={false} />
                  <span className="avatar-crop-mask" />
                </div>
                <div className="avatar-crop-controls">
                  <label>
                    缩放
                    <input type="range" min="1" max="2.6" step="0.05" value={cropScale} onChange={(event) => updateCropScale(event.target.value)} />
                  </label>
                  <div className="inline-actions">
                    <button className="secondary-button compact-button" type="button" onClick={() => profileAvatarInputRef.current?.click()}>重新选择</button>
                    <button className="primary-button compact-button" type="button" onClick={applyAvatarCrop}>应用裁剪</button>
                  </div>
                </div>
              </div>
            )}
            <div className="field floating-field">
              <label>用户名</label>
              <input value={profileForm.display_name} onChange={(event) => setProfileForm((current) => ({ ...current, display_name: event.target.value }))} placeholder="请输入用户名" />
            </div>
            <p className="profile-dialog-hint">点击头像上传图片，拖动并缩放后可裁剪成适合显示的头像。</p>
            {profileError ? <div className="error-box">{profileError}</div> : null}
            {profileMessage ? <div className="success-box">{profileMessage}</div> : null}
            <div className="inline-actions dialog-actions profile-dialog-actions">
              <button className="secondary-button" disabled={savingProfile} onClick={() => setProfileDialogOpen(false)}>取消</button>
              <button className="primary-button dark-button" disabled={savingProfile} onClick={saveProfile}>{savingProfile ? "保存中" : "保存"}</button>
            </div>
          </div>
        </div>
      ) : null}

      <main className="main">
        <header className="topbar">
          <div className="title-block">
            <h1 id="appshell-title-slot">{title}</h1>
          </div>
          <div className="top-actions" id="appshell-actions-slot">{actions}</div>
        </header>
        <div className="content">{children}</div>
      </main>
    </div>
  );
}

function readImageFileAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("头像文件读取失败。"));
    reader.onload = () => resolve(String(reader.result || ""));
    reader.readAsDataURL(file);
  });
}

function getCropDisplaySize(imageSize, scale) {
  if (!imageSize.width || !imageSize.height) return { width: 0, height: 0 };
  const baseScale = Math.max(AVATAR_CROP_SIZE / imageSize.width, AVATAR_CROP_SIZE / imageSize.height);
  return {
    width: imageSize.width * baseScale * scale,
    height: imageSize.height * baseScale * scale
  };
}

function clampCropOffset(offset, imageSize, scale) {
  const display = getCropDisplaySize(imageSize, scale);
  if (!display.width || !display.height) return { x: 0, y: 0 };
  const maxX = Math.max(0, (display.width - AVATAR_CROP_SIZE) / 2);
  const maxY = Math.max(0, (display.height - AVATAR_CROP_SIZE) / 2);
  return {
    x: Math.min(maxX, Math.max(-maxX, offset.x)),
    y: Math.min(maxY, Math.max(-maxY, offset.y))
  };
}

function renderAvatarCrop(source, imageSize, scale, offset) {
  return new Promise((resolve, reject) => {
    if (!source) {
      reject(new Error("请先选择头像图片。"));
      return;
    }
    if (!imageSize.width || !imageSize.height) {
      reject(new Error("头像图片尚未加载完成，请稍后再试。"));
      return;
    }
    const image = new Image();
    image.onerror = () => reject(new Error("无法裁剪该头像图片。"));
    image.onload = () => {
      const display = getCropDisplaySize(imageSize, scale);
      const left = (AVATAR_CROP_SIZE - display.width) / 2 + offset.x;
      const top = (AVATAR_CROP_SIZE - display.height) / 2 + offset.y;
      const sx = ((0 - left) / display.width) * image.naturalWidth;
      const sy = ((0 - top) / display.height) * image.naturalHeight;
      const sw = (AVATAR_CROP_SIZE / display.width) * image.naturalWidth;
      const sh = (AVATAR_CROP_SIZE / display.height) * image.naturalHeight;
      const size = 256;
      const canvas = document.createElement("canvas");
      canvas.width = size;
      canvas.height = size;
      const context = canvas.getContext("2d");
      if (!context) {
        reject(new Error("当前浏览器不支持头像处理。"));
        return;
      }
      context.fillStyle = "#ffffff";
      context.fillRect(0, 0, size, size);
      context.drawImage(image, sx, sy, sw, sh, 0, 0, size, size);
      resolve(canvas.toDataURL("image/jpeg", 0.86));
    };
    image.src = source;
  });
}
