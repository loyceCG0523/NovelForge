(function () {
  const root = document.querySelector(".workbench-shell, .app");
  const sidebar = document.querySelector(".workbench-sidebar, .sidebar");

  if (!root || !sidebar) {
    return;
  }

  const storage = {
    get: function () {
      try {
        return window.localStorage.getItem("novelforge-sidebar-collapsed");
      } catch (error) {
        return null;
      }
    },
    set: function (value) {
      try {
        window.localStorage.setItem("novelforge-sidebar-collapsed", value);
      } catch (error) {
        // Storage can be unavailable in some local preview contexts.
      }
    }
  };

  if (storage.get() === "true") {
    root.classList.add("sidebar-collapsed");
  }

  const toggle = document.createElement("button");
  toggle.className = "sidebar-toggle-control";
  toggle.type = "button";
  toggle.setAttribute("aria-label", "展开或收起侧边栏");
  toggle.textContent = root.classList.contains("sidebar-collapsed") ? "→" : "←";

  const brand = sidebar.querySelector(".workbench-brand, .brand");
  if (brand) {
    brand.appendChild(toggle);
  } else {
    sidebar.prepend(toggle);
  }

  toggle.addEventListener("click", function () {
    root.classList.toggle("sidebar-collapsed");
    const collapsed = root.classList.contains("sidebar-collapsed");
    storage.set(collapsed ? "true" : "false");
    toggle.textContent = collapsed ? "→" : "←";
  });

  if (sidebar.classList.contains("sidebar") && !sidebar.querySelector(".sidebar-user-mini")) {
    const user = document.createElement("a");
    user.className = "sidebar-user-mini";
    user.href = "09-user.html";
    user.innerHTML = [
      '<div class="sidebar-mini-avatar">林</div>',
      '<div class="sidebar-mini-text"><strong>林深不知处</strong><span>Pro 创作者</span></div>',
      '<div class="sidebar-mini-arrow">⌄</div>'
    ].join("");
    sidebar.appendChild(user);
  }
})();
