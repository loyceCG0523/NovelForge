const state = {
  services: [],
  selectedLog: "api",
  busy: false,
  operation: null
};

const labels = {
  running: "运行中",
  starting: "启动中",
  stopped: "已停止",
  external: "外部进程"
};

const icons = {
  web: "W",
  api: "A",
  agent_worker: "G",
  memory_worker: "M"
};

function formatDuration(seconds = 0) {
  if (!seconds) return "—";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const secs = seconds % 60;
  return hours ? `${hours}时 ${minutes}分` : minutes ? `${minutes}分 ${secs}秒` : `${secs}秒`;
}

function toast(message, tone = "") {
  const target = document.querySelector("#toast");
  target.textContent = message;
  target.dataset.tone = tone;
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => {
    target.textContent = "";
    target.dataset.tone = "";
  }, 4200);
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `请求失败：${response.status}`);
  return data;
}

function setBusy(value) {
  state.busy = value;
  document.querySelectorAll(
    "#startAll, #stopAll, #restartAll, [data-service][data-action]"
  ).forEach((button) => {
    button.disabled = value;
  });
}

function renderOperation(operation) {
  state.operation = operation;
  const panel = document.querySelector("#operationPanel");
  if (!operation || operation.status === "idle") {
    panel.className = "operation-panel hidden";
    setBusy(false);
    return;
  }

  const actionLabels = {
    start: "启动全部服务",
    stop: "停止全部服务",
    restart: "重启全部服务"
  };
  const statusLabels = {
    running: "执行中",
    completed: "已完成",
    failed: "有失败"
  };
  const processed = operation.completed_steps || 0;
  panel.className = `operation-panel ${operation.status}`;
  document.querySelector("#operationBadge").textContent =
    statusLabels[operation.status] || operation.status;
  document.querySelector("#operationTitle").textContent = operation.status === "running"
    ? operation.current_step
    : actionLabels[operation.action] || "整组操作";
  document.querySelector("#operationMeta").textContent =
    `${actionLabels[operation.action] || "整组操作"} · 步骤 ${processed}/${operation.total_steps || 0}`;
  document.querySelector("#operationPercent").textContent = `${operation.percent || 0}%`;
  const progress = document.querySelector("#operationProgress");
  progress.style.width = `${operation.percent || 0}%`;
  progress.parentElement.setAttribute("aria-valuenow", String(operation.percent || 0));
  document.querySelector("#operationSteps").innerHTML = (operation.steps || []).map((step) => `
    <div class="operation-step ${step.status}">
      <i></i><span>${step.label}</span>
    </div>
  `).join("");
  document.querySelector("#operationError").textContent = operation.error || "";
  setBusy(operation.status === "running");
}

async function refreshOperation(silent = true) {
  const previousStatus = state.operation?.status;
  try {
    const operation = await request("/api/operations/current");
    renderOperation(operation);
    if (previousStatus === "running" && operation.status === "completed") {
      toast("整组操作已完成", "success");
      refreshStatus();
    } else if (previousStatus === "running" && operation.status === "failed") {
      toast("整组操作完成，但有步骤失败", "error");
      refreshStatus();
    }
  } catch (error) {
    if (!silent) toast(error.message, "error");
  }
}

function renderOverview(data) {
  state.services = data.services || [];
  const overallLabel = document.querySelector("#overallLabel");
  const overallDot = document.querySelector("#overallDot");
  const running = state.services.filter((item) => item.status === "running").length;
  overallDot.className = `status-dot ${data.overall || "stopped"}`;
  overallLabel.textContent = data.overall === "running"
    ? "全部服务运行正常"
    : data.overall === "partial"
      ? `${running}/4 个服务运行`
      : "全部服务已停止";
  document.querySelector("#lastUpdate").textContent =
    `最近刷新 ${new Date(data.updated_at).toLocaleTimeString("zh-CN", { hour12: false })}`;
  document.querySelector("#redisStatus").textContent =
    data.queues?.status === "connected" ? "已连接" : "不可用";
  document.querySelector("#agentQueue").textContent =
    Number.isFinite(data.queues?.agent) ? `${data.queues.agent} 个等待` : "—";
  document.querySelector("#memoryQueue").textContent =
    Number.isFinite(data.queues?.memory) ? `${data.queues.memory} 个等待` : "—";

  document.querySelector("#serviceGrid").innerHTML = state.services.map((service) => `
    <article class="service-card ${service.status}">
      <div class="service-top">
        <span class="service-icon">${icons[service.key] || "S"}</span>
        <span class="service-state ${service.status}">
          <i></i>${labels[service.status] || service.status}
        </span>
      </div>
      <div>
        <h3>${service.title}</h3>
        <p>${service.description}</p>
      </div>
      <dl>
        <div><dt>PID</dt><dd>${service.pid || "—"}</dd></div>
        <div><dt>运行时长</dt><dd>${formatDuration(service.uptime_seconds)}</dd></div>
        <div><dt>日志大小</dt><dd>${Math.ceil((service.log_bytes || 0) / 1024)} KB</dd></div>
      </dl>
      <div class="service-actions">
        ${service.status === "stopped"
          ? `<button class="button primary compact" data-service="${service.key}" data-action="start">启动</button>`
          : `<button class="button secondary compact" data-service="${service.key}" data-action="restart">重启</button>
             <button class="button ghost compact" data-service="${service.key}" data-action="stop">停止</button>`}
        ${service.open_url ? `<a class="inline-link" href="${service.open_url}" target="_blank" rel="noreferrer">打开 ↗</a>` : ""}
      </div>
    </article>
  `).join("");

  document.querySelector("#logTabs").innerHTML = state.services.map((service) => `
    <button class="log-tab ${state.selectedLog === service.key ? "active" : ""}"
      data-log="${service.key}" role="tab" aria-selected="${state.selectedLog === service.key}">
      <i class="${service.status}"></i>${service.title}
    </button>
  `).join("");
  setBusy(state.operation?.status === "running");
}

function selectLogTab(key) {
  state.selectedLog = key;
  document.querySelectorAll("[data-log]").forEach((tab) => {
    const active = tab.dataset.log === key;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
  });
}

async function refreshStatus(silent = true) {
  try {
    renderOverview(await request("/api/status"));
  } catch (error) {
    if (!silent) toast(error.message, "error");
  }
}

async function refreshLog() {
  if (!state.selectedLog) return;
  try {
    const data = await request(`/api/logs/${state.selectedLog}?lines=400`);
    const output = document.querySelector("#logOutput");
    const logStatus = document.querySelector("#logStatus");
    const statusCopy = {
      running: ["正在运行", "日志正在实时刷新"],
      starting: ["正在启动", "等待服务进入就绪状态"],
      stopping: ["正在停止", "等待进程安全退出，历史日志仍会保留"],
      stopped: ["已停止", "以下内容为该服务的历史日志"],
      external: ["由外部进程运行", "控制台无法采集该进程的实时日志"]
    };
    const [statusTitle, statusDetail] =
      statusCopy[data.status] || [data.status || "未知状态", "日志状态未知"];
    logStatus.className = `log-status ${data.status || "stopped"}`;
    logStatus.querySelector("strong").textContent = `${data.title || "服务"} · ${statusTitle}`;
    logStatus.querySelector("span").textContent = statusDetail;
    const nextText = data.text || "该服务还没有日志。";
    if (output.textContent !== nextText) {
      output.textContent = nextText;
      if (document.querySelector("#autoScroll").checked) {
        output.scrollTop = output.scrollHeight;
      }
    }
  } catch (error) {
    document.querySelector("#logOutput").textContent = error.message;
  }
}

async function controlAll(action) {
  if (state.busy) return;
  setBusy(true);
  try {
    const data = await request(`/api/all/${action}`, { method: "POST" });
    renderOperation(data.operation);
    toast({ start: "开始启动全部服务", stop: "开始停止全部服务", restart: "开始重启全部服务" }[action]);
  } catch (error) {
    setBusy(false);
    toast(error.message, "error");
  }
}

async function controlService(key, action) {
  if (state.busy) return;
  setBusy(true);
  try {
    await request(`/api/services/${key}/${action}`, { method: "POST" });
    await refreshStatus(false);
    await refreshLog();
    toast("服务状态已更新", "success");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    setBusy(false);
  }
}

document.addEventListener("click", (event) => {
  const serviceButton = event.target.closest("[data-service][data-action]");
  if (serviceButton) {
    controlService(serviceButton.dataset.service, serviceButton.dataset.action);
    return;
  }
  const logTab = event.target.closest("[data-log]");
  if (logTab) {
    selectLogTab(logTab.dataset.log);
    refreshLog();
  }
});

document.querySelector("#startAll").addEventListener("click", () => controlAll("start"));
document.querySelector("#stopAll").addEventListener("click", () => controlAll("stop"));
document.querySelector("#restartAll").addEventListener("click", () => controlAll("restart"));
document.querySelector("#clearLog").addEventListener("click", async () => {
  if (!state.selectedLog || state.busy) return;
  setBusy(true);
  try {
    await request(`/api/logs/${state.selectedLog}/clear`, { method: "POST" });
    await refreshLog();
    toast("当前日志已清空", "success");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    setBusy(false);
  }
});

window.setInterval(() => {
  document.querySelector("#clock").textContent =
    new Date().toLocaleTimeString("zh-CN", { hour12: false });
}, 1000);
window.setInterval(() => refreshStatus(), 1500);
window.setInterval(() => refreshLog(), 2000);
window.setInterval(() => refreshOperation(), 400);
refreshStatus(false);
refreshLog();
refreshOperation(false);
