const state = {
  tasks: [],
  skillLibraries: [],
  groups: [],
  runs: [],
  history: [],
  modelConfigs: [],
  selectedModelConfigId: "",
  selectedSkillsLibraryId: "",
  selectedTaskKey: null,
  selectedRunId: null,
  selectedHistoryId: null,
  activeLogKey: "",
  expandedHistoryGroups: new Set(),
  jobDetails: new Map(),
  logs: new Map(),
  config: {},
};

const $ = (id) => document.getElementById(id);

const elements = {
  taskList: $("taskList"),
  taskSearch: $("taskSearch"),
  skillCountMode: $("skillCountMode"),
  skillCountMin: $("skillCountMin"),
  skillCountMax: $("skillCountMax"),
  skillCountMinLabel: $("skillCountMinLabel"),
  skillCountMaxField: $("skillCountMaxField"),
  onlyPureSkills: $("onlyPureSkills"),
  onlyLocalImage: $("onlyLocalImage"),
  taskFilterSummary: $("taskFilterSummary"),
  selectedTaskLabel: $("selectedTaskLabel"),
  groupsList: $("groupsList"),
  runsTable: $("runsTable"),
  logView: $("logView"),
  logTitle: $("logTitle"),
  workspaceLabel: $("workspaceLabel"),
  statRunning: $("statRunning"),
  statPassed: $("statPassed"),
  statFailed: $("statFailed"),
  agent: $("agent"),
  provider: $("provider"),
  model: $("model"),
  baseUrl: $("baseUrl"),
  apiKey: $("apiKey"),
  modelConfigSelect: $("modelConfigSelect"),
  modelConfigName: $("modelConfigName"),
  saveApiKey: $("saveApiKey"),
  saveModelConfig: $("saveModelConfig"),
  deleteModelConfig: $("deleteModelConfig"),
  modelConfigStatus: $("modelConfigStatus"),
  repeats: $("repeats"),
  parallel: $("parallel"),
  skillMode: $("skillMode"),
  reasoningEffort: $("reasoningEffort"),
  promptMode: $("promptMode"),
  skillsLibrary: $("skillsLibrary"),
  agentIdleTimeout: $("agentIdleTimeout"),
  jobsRoot: $("jobsRoot"),
  historySearch: $("historySearch"),
  historyList: $("historyList"),
};

const MODEL_CONFIG_STORAGE_KEY = "skillsbench.runner.modelConfigs.v1";

function formatTime(value) {
  if (!value) return "-";
  return new Date(value).toLocaleTimeString("zh-CN", { hour12: false });
}

function secondsBetween(start, end) {
  if (!start) return "-";
  const finish = end ? new Date(end) : new Date();
  return `${Math.max(0, Math.round((finish - new Date(start)) / 1000))}s`;
}

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function runStatusBadge(status) {
  if (status === "passed") return `<span class="badge pass">PASS</span>`;
  if (status === "failed" || status === "error") return `<span class="badge fail">${status.toUpperCase()}</span>`;
  if (status === "running") return `<span class="badge run">RUNNING</span>`;
  if (status === "partial") return `<span class="badge warn">PARTIAL</span>`;
  if (status === "completed") return `<span class="badge">DONE</span>`;
  if (status === "stopped") return `<span class="badge warn">STOPPED</span>`;
  return `<span class="badge">${status.toUpperCase()}</span>`;
}

function escapeHtml(text) {
  return String(text ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function makeId(prefix) {
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

function loadModelConfigs() {
  try {
    const raw = JSON.parse(localStorage.getItem(MODEL_CONFIG_STORAGE_KEY) || "[]");
    state.modelConfigs = Array.isArray(raw)
      ? raw
        .filter((item) => item && typeof item === "object" && item.id && item.name)
        .map((item) => ({
          id: String(item.id),
          name: String(item.name),
          agent: String(item.agent || "claude-agent-acp"),
          provider: String(item.provider || "deepseek"),
          model: String(item.model || ""),
          baseUrl: String(item.baseUrl || ""),
          apiKey: typeof item.apiKey === "string" ? item.apiKey : "",
          skillMode: String(item.skillMode || "with-skill"),
          promptMode: String(item.promptMode || "standard"),
          reasoningEffort: String(item.reasoningEffort || "off"),
          agentIdleTimeout: String(item.agentIdleTimeout ?? "0"),
          createdAt: item.createdAt || new Date().toISOString(),
          updatedAt: item.updatedAt || item.createdAt || new Date().toISOString(),
        }))
      : [];
  } catch {
    state.modelConfigs = [];
  }
}

function persistModelConfigs() {
  localStorage.setItem(MODEL_CONFIG_STORAGE_KEY, JSON.stringify(state.modelConfigs));
}

function setModelConfigStatus(message) {
  elements.modelConfigStatus.textContent = message;
  if (!message) return;
  window.clearTimeout(setModelConfigStatus.timer);
  setModelConfigStatus.timer = window.setTimeout(() => {
    elements.modelConfigStatus.textContent = "";
  }, 3000);
}

function renderModelConfigs() {
  const options = [
    `<option value="">未选择配置</option>`,
    ...state.modelConfigs
      .slice()
      .sort((a, b) => a.name.localeCompare(b.name, "zh-CN"))
      .map((config) => {
        const label = `${config.name} · ${config.model || "-"} · ${config.agent || "-"}`;
        return `<option value="${escapeHtml(config.id)}">${escapeHtml(label)}</option>`;
      }),
  ];
  elements.modelConfigSelect.innerHTML = options.join("");
  elements.modelConfigSelect.value = state.selectedModelConfigId;
}

function currentModelConfigName() {
  return elements.modelConfigName.value.trim()
    || elements.model.value.trim()
    || "未命名配置";
}

function currentModelConfigPayload(existing = null) {
  const now = new Date().toISOString();
  const name = currentModelConfigName();
  const config = {
    id: existing?.id || makeId("model-config"),
    name,
    agent: elements.agent.value,
    provider: elements.provider.value,
    model: elements.model.value.trim(),
    baseUrl: elements.baseUrl.value.trim(),
    apiKey: elements.saveApiKey.checked ? elements.apiKey.value : "",
    skillMode: elements.skillMode.value,
    promptMode: elements.promptMode.value,
    reasoningEffort: elements.reasoningEffort.value,
    agentIdleTimeout: elements.agentIdleTimeout.value.trim() || "0",
    createdAt: existing?.createdAt || now,
    updatedAt: now,
  };
  return config;
}

function applyModelConfig(config) {
  elements.agent.value = config.agent || "claude-agent-acp";
  elements.provider.value = config.provider || "deepseek";
  elements.model.value = config.model || "";
  elements.baseUrl.value = config.baseUrl || "";
  elements.apiKey.value = config.apiKey || "";
  elements.skillMode.value = config.skillMode || "with-skill";
  elements.promptMode.value = config.promptMode || "standard";
  elements.reasoningEffort.value = config.reasoningEffort || "off";
  elements.agentIdleTimeout.value = config.agentIdleTimeout || "0";
  elements.modelConfigName.value = config.name || "";
  elements.saveApiKey.checked = Boolean(config.apiKey);
  state.selectedModelConfigId = config.id;
  renderModelConfigs();
}

function saveModelConfig() {
  const selected = state.modelConfigs.find((config) => config.id === state.selectedModelConfigId);
  const name = currentModelConfigName();
  const sameName = state.modelConfigs.find(
    (config) => config.name.trim().toLowerCase() === name.toLowerCase(),
  );
  const existing = selected || sameName || null;
  const next = currentModelConfigPayload(existing);
  const idx = state.modelConfigs.findIndex((config) => config.id === next.id);
  if (idx >= 0) state.modelConfigs[idx] = next;
  else state.modelConfigs.push(next);
  state.selectedModelConfigId = next.id;
  persistModelConfigs();
  renderModelConfigs();
  setModelConfigStatus(elements.saveApiKey.checked ? "已保存配置和 API Key" : "已保存配置");
}

function deleteModelConfig() {
  const id = elements.modelConfigSelect.value;
  if (!id) {
    setModelConfigStatus("请选择要删除的配置");
    return;
  }
  const before = state.modelConfigs.length;
  state.modelConfigs = state.modelConfigs.filter((config) => config.id !== id);
  state.selectedModelConfigId = "";
  persistModelConfigs();
  renderModelConfigs();
  if (before !== state.modelConfigs.length) setModelConfigStatus("已删除配置");
}

function selectedTask() {
  return state.tasks.find((task) => task.key === state.selectedTaskKey) || null;
}

function skillLibrariesForTask(task) {
  return Array.isArray(task?.skillLibraries) ? task.skillLibraries : [];
}

function defaultSkillsLibraryId(task) {
  const libraries = skillLibrariesForTask(task);
  return task?.defaultSkillsLibraryId
    || libraries.find((library) => library.variant === "initial")?.id
    || libraries[0]?.id
    || "";
}

function selectedSkillsLibrary(task = selectedTask()) {
  const libraries = skillLibrariesForTask(task);
  if (!libraries.length) return null;
  const selected = libraries.find((library) => library.id === state.selectedSkillsLibraryId);
  return selected || libraries.find((library) => library.id === defaultSkillsLibraryId(task)) || libraries[0];
}

function skillsLibraryDisplay(value) {
  if (!value) return "初始 skills 库";
  return value.label || value.skillsLibraryLabel || value.skillsDir || value.id || "初始 skills 库";
}

function promptModeLabel(value) {
  if (value === "force-all-skills") return "强制全部 skill";
  return "普通";
}

function renderSkillsLibrarySelect() {
  const task = selectedTask();
  const libraries = skillLibrariesForTask(task);
  if (!libraries.length) {
    elements.skillsLibrary.innerHTML = `<option value="">无可用 skills 库</option>`;
    elements.skillsLibrary.value = "";
    elements.skillsLibrary.disabled = true;
    return;
  }

  if (!libraries.some((library) => library.id === state.selectedSkillsLibraryId)) {
    state.selectedSkillsLibraryId = defaultSkillsLibraryId(task);
  }

  elements.skillsLibrary.innerHTML = libraries
    .map((library) => {
      const suffix = library.variant === "initial" ? "（默认）" : "";
      return `<option value="${escapeHtml(library.id)}">${escapeHtml(skillsLibraryDisplay(library) + suffix)}</option>`;
    })
    .join("");
  elements.skillsLibrary.value = state.selectedSkillsLibraryId;
  elements.skillsLibrary.disabled = elements.skillMode.value === "no-skill";
}

function findHistoryEntry(id) {
  for (const item of state.history) {
    if (item.id === id) return item;
    if (item.type === "group") {
      const child = (item.runs || []).find((run) => run.id === id);
      if (child) return { ...child, parentGroup: item };
    }
  }
  return null;
}

function historyGroupStatus(item) {
  if (item.type !== "group") {
    return item.score && String(item.score).startsWith("100") ? "passed" : "failed";
  }
  if (!item.total) return "completed";
  if (item.passed === item.total) return "passed";
  if (item.passed > 0) return "partial";
  return "failed";
}

function numberOrNull(value) {
  const trimmed = String(value ?? "").trim();
  if (!trimmed) return null;
  const number = Number(trimmed);
  return Number.isFinite(number) ? Math.max(0, Math.floor(number)) : null;
}

function updateSkillCountControls() {
  const mode = elements.skillCountMode.value;
  const usesPrimary = mode !== "any";
  const usesRangeMax = mode === "range";
  elements.skillCountMin.disabled = !usesPrimary;
  elements.skillCountMax.disabled = !usesRangeMax;
  elements.skillCountMaxField.hidden = !usesRangeMax;
  elements.skillCountMinLabel.textContent = mode === "max" ? "最多" : mode === "range" ? "最少" : "数量";
  elements.skillCountMin.placeholder = mode === "exact" ? "4" : mode === "max" ? "8" : "4";
}

function matchesSkillCount(task) {
  const mode = elements.skillCountMode.value;
  if (mode === "any") return true;
  const count = Number(task.skillCount || 0);
  const primary = numberOrNull(elements.skillCountMin.value);
  const max = numberOrNull(elements.skillCountMax.value);
  if (mode === "exact") return primary === null || count === primary;
  if (mode === "min") return primary === null || count >= primary;
  if (mode === "max") return primary === null || count <= primary;
  if (mode === "range") {
    if (primary !== null && count < primary) return false;
    if (max !== null && count > max) return false;
  }
  return true;
}

function filteredTasks() {
  updateSkillCountControls();
  const query = elements.taskSearch.value.trim().toLowerCase();
  return state.tasks.filter((task) => {
    if (!matchesSkillCount(task)) return false;
    if (elements.onlyPureSkills.checked && !task.pureSkillMdOnly) return false;
    if (elements.onlyLocalImage.checked && !task.localImage) return false;
    if (!query) return true;
    const haystack = `${task.name} ${task.rootLabel} ${task.category} ${task.subcategory} ${task.difficulty} ${task.skillCount} skills ${task.pureSkillMdOnly ? "pure skillmd 纯 skill" : "resources"}`.toLowerCase();
    return haystack.includes(query);
  });
}

function renderTasks() {
  const tasks = filteredTasks();
  elements.taskFilterSummary.textContent = `显示 ${tasks.length}/${state.tasks.length} 个 task`;
  elements.taskList.innerHTML = tasks
    .map((task) => {
      const selected = task.key === state.selectedTaskKey ? " selected" : "";
      const local = task.localImage ? `<span class="badge pass">local</span>` : "";
      const pure = task.pureSkillMdOnly
        ? `<span class="badge pass">pure</span>`
        : task.hasSkills ? `<span class="badge">${Number(task.skillResourceCount || 0)} res</span>` : "";
      return `
        <button class="task-item${selected}" data-task-key="${escapeHtml(task.key)}">
          <div class="task-name">
            <span>${escapeHtml(task.name)}</span>
            <span class="badge">${task.skillCount} skills</span>
          </div>
          <div class="task-meta">
            ${escapeHtml(task.rootLabel)} · ${escapeHtml(task.difficulty || "unknown")} · ${escapeHtml(task.category || "uncategorized")}
            ${pure}
            ${local}
          </div>
        </button>
      `;
    })
    .join("") || `<div class="task-meta" style="padding:12px;">没有匹配任务。</div>`;

  elements.taskList.querySelectorAll(".task-item").forEach((button) => {
    button.addEventListener("click", () => {
      state.selectedTaskKey = button.dataset.taskKey;
      updateJobsRootPlaceholder();
      renderAll();
    });
  });
}

function renderStats() {
  const running = state.runs.filter((run) => run.status === "running").length;
  const passed = state.runs.filter((run) => run.status === "passed").length;
  const failed = state.runs.filter((run) => ["failed", "error"].includes(run.status)).length;
  elements.statRunning.textContent = running;
  elements.statPassed.textContent = passed;
  elements.statFailed.textContent = failed;
}

function renderGroups() {
  elements.groupsList.innerHTML = state.groups
    .slice(0, 8)
    .map((group) => {
      const done = group.total > 0 && group.completed >= group.total;
      const displayStatus = done ? (group.passed === group.total ? "passed" : "completed") : group.status;
      return `
        <div class="group-row">
          <div>
            <div class="group-title">${escapeHtml(group.taskName)}</div>
            <div class="group-subtitle">${escapeHtml(group.jobsRoot)} · ${escapeHtml(group.provider || "-")}/${escapeHtml(group.model || "-")} · ${escapeHtml(group.skillMode || "with-skill")} · ${escapeHtml(promptModeLabel(group.promptMode))} · ${group.completed}/${group.total} · ${formatTime(group.createdAt)}</div>
          </div>
          ${runStatusBadge(displayStatus)}
          <button class="icon-button" title="停止运行组" data-stop-group="${escapeHtml(group.id)}">■</button>
        </div>
      `;
    })
    .join("") || `<div class="task-meta" style="padding:12px;">尚未启动任务。</div>`;

  elements.groupsList.querySelectorAll("[data-stop-group]").forEach((button) => {
    button.addEventListener("click", () => stopGroup(button.dataset.stopGroup));
  });
}

function renderRuns() {
  const runs = [...state.runs].sort((a, b) => {
    if (a.groupId === b.groupId) return a.runNo - b.runNo;
    return String(b.startedAt || b.id).localeCompare(String(a.startedAt || a.id));
  });
  elements.runsTable.innerHTML = runs
    .map((run) => {
      const selected = run.id === state.selectedRunId ? " selected" : "";
      const score = run.summary?.score || "-";
      return `
        <div class="run-row${selected}" data-run-id="${escapeHtml(run.id)}">
          <div>#${run.runNo}</div>
          <div>
            <div>${escapeHtml(run.taskName)}</div>
            <div class="run-path">${escapeHtml(run.jobsDir)} · ${escapeHtml(run.provider || "-")}/${escapeHtml(run.model || "-")} · ${escapeHtml(run.skillMode || "with-skill")} · ${escapeHtml(skillsLibraryDisplay(run))} · ${escapeHtml(promptModeLabel(run.promptMode))}</div>
          </div>
          <div>${runStatusBadge(run.status)}</div>
          <div>${escapeHtml(score)}</div>
          <div>${secondsBetween(run.startedAt, run.endedAt)}</div>
        </div>
      `;
    })
    .join("") || `<div class="task-meta" style="padding:12px;">没有 run。</div>`;

  elements.runsTable.querySelectorAll(".run-row").forEach((row) => {
    row.addEventListener("click", async () => {
      state.selectedRunId = row.dataset.runId;
      state.selectedHistoryId = null;
      await fetchRunLogs(state.selectedRunId);
      renderAll();
    });
  });
}

function renderGroupDetail(item) {
  const runs = (item.runs || [])
    .map((run) => `#${run.index} ${run.score || "-"} · ${run.agent || item.agent || "-"} · ${run.summaryPath}`)
    .join("\n");
  return [
    `Group: ${item.taskName}`,
    `Path: ${item.groupKey || item.artifactDir}`,
    `Skills Library: ${item.skillsLibraryLabel || item.skillsDir || "-"}`,
    `Prompt Mode: ${promptModeLabel(item.promptMode)}`,
    `Success: ${item.passed}/${item.total} (${item.total ? (item.passed / item.total * 100).toFixed(1) : "0.0"}%)`,
    `Errored: ${item.errored || 0}`,
    `Tool calls: ${item.totalToolCalls || 0}`,
    "",
    "内部 jobs:",
    runs || "(empty)",
    "",
    "点击 group 行可展开/收起内部 job；点击内部 job 可查看 result、config、prompts，并进入单独轨迹页。",
  ].join("\n");
}

function trajectoryHref(artifactDir, rolloutName) {
  const params = new URLSearchParams();
  params.set("artifactDir", artifactDir || "");
  if (rolloutName) params.set("rollout", rolloutName);
  return `/trajectory.html?${params.toString()}`;
}

function analysisHref(artifactDir, rolloutName) {
  const params = new URLSearchParams();
  params.set("artifactDir", artifactDir || "");
  if (rolloutName) params.set("rollout", rolloutName);
  return `/analysis.html?${params.toString()}`;
}

function renderKeyValueGrid(values) {
  return `
    <div class="detail-grid">
      ${Object.entries(values).map(([key, value]) => `
        <div>
          <span>${escapeHtml(key)}</span>
          <strong>${escapeHtml(value === undefined || value === null || value === "" ? "-" : String(value))}</strong>
        </div>
      `).join("")}
    </div>
  `;
}

function renderJsonDetails(title, value, open = false) {
  return `
    <details class="detail-json"${open ? " open" : ""}>
      <summary>${escapeHtml(title)}</summary>
      <pre>${escapeHtml(JSON.stringify(value ?? null, null, 2))}</pre>
    </details>
  `;
}

function renderJobDetailHtml(item, detail) {
  const header = {
    taskName: item.taskName || item.parentGroup?.taskName,
    jobName: item.jobName,
    agent: item.agent || item.parentGroup?.agent,
    model: item.model || item.parentGroup?.model,
    skillsLibrary: item.skillsLibraryLabel || item.parentGroup?.skillsLibraryLabel || item.skillsDir || item.parentGroup?.skillsDir,
    score: item.score,
    passed: item.passed,
    failed: item.failed,
    errored: item.errored,
    total: item.total,
    summaryPath: item.summaryPath,
    artifactDir: item.artifactDir,
    modifiedAt: item.modifiedAt,
  };
  if (!detail) {
    return `
      <div class="detail-view">
        <h3>Job 详情</h3>
        ${renderKeyValueGrid(header)}
        <div class="detail-empty">正在读取 job 详情...</div>
      </div>
    `;
  }
  if (detail.error) {
    return `
      <div class="detail-view">
        <h3>Job 详情</h3>
        ${renderKeyValueGrid(header)}
        <div class="detail-error">读取 job 详情失败：${escapeHtml(detail.error)}</div>
      </div>
    `;
  }

  const rollouts = (detail.rollouts || []).map((rollout) => {
    const trajectory = rollout.trajectory;
    const href = trajectory ? trajectoryHref(detail.artifactDir || item.artifactDir, rollout.name) : "";
    const analysis = trajectory ? analysisHref(detail.artifactDir || item.artifactDir, rollout.name) : "";
    return `
      <section class="detail-section">
        <div class="detail-section-title">
          <div>
            <h4>${escapeHtml(rollout.name)}</h4>
            <p>${escapeHtml(rollout.path || "")}</p>
          </div>
          ${trajectory ? `
            <div class="detail-actions">
              <a class="secondary-button detail-link" href="${escapeHtml(href)}" target="_blank" rel="noreferrer">查看轨迹</a>
              <a class="secondary-button detail-link" href="${escapeHtml(analysis)}" target="_blank" rel="noreferrer">分析轨迹</a>
            </div>
          ` : `<span class="muted">无轨迹文件</span>`}
        </div>
        ${trajectory ? renderKeyValueGrid({
          "trajectory": trajectory.name,
          "size": formatBytes(trajectory.size),
          "modified": trajectory.modifiedAt,
          "skills library": rollout.skillsLibrary?.label || rollout.skillsLibrary?.skillsDir || "-",
        }) : ""}
        ${renderJsonDetails("result.json", rollout.result, true)}
        ${renderJsonDetails("config.json", rollout.config)}
        ${renderJsonDetails("prompts.json", rollout.prompts)}
        ${rollout.timing ? renderJsonDetails("timing.json", rollout.timing) : ""}
      </section>
    `;
  }).join("");

  const files = (detail.files || [])
    .filter((file) => !file.name.endsWith("trajectory/acp_trajectory.jsonl"))
    .slice(0, 80);

  return `
    <div class="detail-view">
      <h3>Job 详情</h3>
      ${renderKeyValueGrid(header)}
      ${renderJsonDetails("summary.json", detail.summary, true)}
      ${rollouts || `<div class="detail-empty">没有找到 rollout 目录。</div>`}
      ${files.length ? `
        <details class="detail-json">
          <summary>artifact files (${files.length})</summary>
          <pre>${escapeHtml(files.map((file) => `${file.name} · ${formatBytes(file.size)}`).join("\n"))}</pre>
        </details>
      ` : ""}
    </div>
  `;
}

function setLogText(text) {
  elements.logView.classList.remove("log-view-detail");
  elements.logView.textContent = text;
}

function setLogHtml(html) {
  elements.logView.classList.add("log-view-detail");
  elements.logView.innerHTML = html;
}

function renderLogContent(key, renderContent, options = {}) {
  const sameTarget = state.activeLogKey === key;
  const previousTop = elements.logView.scrollTop;
  renderContent();
  state.activeLogKey = key;

  if (options.followBottom) {
    elements.logView.scrollTop = elements.logView.scrollHeight;
    return;
  }

  if (sameTarget) {
    const maxTop = Math.max(0, elements.logView.scrollHeight - elements.logView.clientHeight);
    elements.logView.scrollTop = Math.min(previousTop, maxTop);
    return;
  }

  elements.logView.scrollTop = 0;
}

function renderLog() {
  if (state.selectedHistoryId) {
    const item = findHistoryEntry(state.selectedHistoryId);
    if (item) {
      elements.logTitle.textContent = item.type === "group"
        ? `历史组 · ${item.taskName}`
        : `Job 详情 · ${item.taskName || item.parentGroup?.taskName || "run"}`;
      const logKey = `history:${item.type}:${item.id}`;
      renderLogContent(logKey, () => {
        if (item.type === "group") setLogText(renderGroupDetail(item));
        else setLogHtml(renderJobDetailHtml(item, state.jobDetails.get(item.id)));
      });
      return;
    }
  }

  const run = state.runs.find((item) => item.id === state.selectedRunId);
  if (!run) {
    elements.logTitle.textContent = "日志";
    renderLogContent("empty", () => setLogText("选择一个 run 查看实时日志。"));
    return;
  }

  elements.logTitle.textContent = `日志 · ${run.taskName} #${run.runNo}`;
  const logs = state.logs.get(run.id) || [];
  renderLogContent(
    `run:${run.id}`,
    () => setLogText(logs.map((line) => `[${formatTime(line.at)}] ${line.text}`).join("\n") || "等待日志..."),
    { followBottom: true },
  );
}

function filteredHistory() {
  const query = elements.historySearch.value.trim().toLowerCase();
  if (!query) return state.history;
  return state.history.filter((item) => {
    const haystack = `${item.type} ${item.taskName} ${item.agent} ${item.model} ${item.skillsLibraryLabel} ${item.skillsDir} ${item.score} ${item.summaryPath} ${item.artifactDir}`.toLowerCase();
    return haystack.includes(query);
  });
}

function renderHistory() {
  const items = filteredHistory().slice(0, 120);
  const rows = [];
  for (const item of items) {
      const selected = item.id === state.selectedHistoryId ? " selected" : "";
      const isGroup = item.type === "group";
      const successRate = isGroup && item.total ? `${item.passed}/${item.total}` : item.score;
      const status = historyGroupStatus(item);
      const label = isGroup ? "GROUP" : "RUN";
      const expanded = isGroup && state.expandedHistoryGroups.has(item.id);
      rows.push(`
        <div class="history-row${selected}" data-history-id="${escapeHtml(item.id)}" data-history-kind="${escapeHtml(item.type)}">
          <div class="history-main">
            <div class="history-title">
              <span class="badge">${label}</span>
              ${isGroup ? `<span class="expand-mark">${expanded ? "▾" : "▸"}</span>` : ""}
              ${escapeHtml(item.taskName || "(unknown)")}
            </div>
            <div class="history-subtitle">${escapeHtml(item.summaryPath)}</div>
          </div>
          <div>${runStatusBadge(status)}</div>
          <div>${escapeHtml(successRate || "-")}</div>
          <div>${escapeHtml(item.skillsLibraryLabel || item.skillsDir || "-")}</div>
          <div>${escapeHtml(item.agent || "-")}</div>
          <div>${escapeHtml(item.model || "-")}</div>
          <div>${item.totalTimeSec ? `${Math.round(item.totalTimeSec)}s` : "-"}</div>
        </div>
      `);

      if (expanded) {
        for (const run of item.runs || []) {
          const childSelected = run.id === state.selectedHistoryId ? " selected" : "";
          const childStatus = run.score && String(run.score).startsWith("100") ? "passed" : "failed";
          rows.push(`
            <div class="history-row history-child-row${childSelected}" data-history-id="${escapeHtml(run.id)}" data-history-kind="run">
              <div class="history-main">
                <div class="history-title"><span class="badge">JOB ${run.index}</span> ${escapeHtml(run.jobName || item.taskName || "(unknown)")}</div>
                <div class="history-subtitle">${escapeHtml(run.summaryPath)}</div>
              </div>
              <div>${runStatusBadge(childStatus)}</div>
              <div>${escapeHtml(run.score || "-")}</div>
              <div>${escapeHtml(run.skillsLibraryLabel || run.skillsDir || item.skillsLibraryLabel || "-")}</div>
              <div>${escapeHtml(run.agent || item.agent || "-")}</div>
              <div>${escapeHtml(run.model || item.model || "-")}</div>
              <div>${run.totalTimeSec ? `${Math.round(run.totalTimeSec)}s` : "-"}</div>
            </div>
          `);
        }
      }
  }

  elements.historyList.innerHTML = rows.join("") || `<div class="task-meta" style="padding:12px;">没有扫描到已完成结果。</div>`;

  elements.historyList.querySelectorAll(".history-row").forEach((row) => {
    row.addEventListener("click", async () => {
      const entry = findHistoryEntry(row.dataset.historyId);
      if (!entry) return;
      if (entry.type === "group") {
        if (state.expandedHistoryGroups.has(entry.id)) state.expandedHistoryGroups.delete(entry.id);
        else state.expandedHistoryGroups.add(entry.id);
        state.selectedHistoryId = entry.id;
        state.selectedRunId = null;
        renderAll();
        return;
      } else {
        state.selectedHistoryId = entry.id;
        state.selectedRunId = null;
        renderAll();
        await fetchJobDetail(entry);
        renderAll();
      }
    });
  });
}

function updateJobsRootPlaceholder() {
  const task = selectedTask();
  const library = selectedSkillsLibrary(task);
  elements.selectedTaskLabel.textContent = task ? `${task.rootLabel}/${task.name} · ${skillsLibraryDisplay(library)}` : "未选择任务";
  if (task) {
    elements.jobsRoot.placeholder = `jobs/web-runner/${task.name}-${new Date().toISOString().replace(/[:.]/g, "-")}`;
  }
}

function renderAll() {
  renderStats();
  renderTasks();
  renderSkillsLibrarySelect();
  renderGroups();
  renderHistory();
  renderRuns();
  renderLog();
  updateJobsRootPlaceholder();
}

async function fetchTasks() {
  const response = await fetch("/api/tasks");
  const data = await response.json();
  state.tasks = data.tasks || [];
  state.skillLibraries = data.skillLibraries || [];
  if (!state.selectedTaskKey && state.tasks.length) {
    const preferred = state.tasks.find((task) => task.key === "localimages:r2r-mpc-control") || state.tasks[0];
    state.selectedTaskKey = preferred.key;
  }
  renderAll();
}

async function fetchRunLogs(runId) {
  const response = await fetch(`/api/runs/${encodeURIComponent(runId)}/logs`);
  if (!response.ok) return;
  const data = await response.json();
  state.logs.set(runId, data.logs || []);
}

async function fetchHistory() {
  const response = await fetch("/api/history");
  const data = await response.json();
  state.history = data.history || [];
  renderAll();
}

async function fetchJobDetail(item) {
  if (!item?.artifactDir || state.jobDetails.has(item.id)) return;
  state.jobDetails.set(item.id, null);
  renderLog();
  const response = await fetch(`/api/job-detail?artifactDir=${encodeURIComponent(item.artifactDir)}`);
  const data = await response.json();
  if (!response.ok) {
    state.jobDetails.set(item.id, { error: data.error || "读取 job 详情失败" });
    return;
  }
  state.jobDetails.set(item.id, data.detail || null);
}

async function startRun() {
  const task = selectedTask();
  if (!task) {
    alert("请先选择任务。");
    return;
  }

  const body = {
    taskKey: task.key,
    agent: elements.agent.value,
    provider: elements.provider.value,
    model: elements.model.value,
    baseUrl: elements.baseUrl.value,
    apiKey: elements.apiKey.value,
    repeats: Number(elements.repeats.value || 1),
    parallel: Number(elements.parallel.value || 1),
    skillMode: elements.skillMode.value,
    promptMode: elements.skillMode.value === "force-skill" ? "force-all-skills" : elements.promptMode.value,
    reasoningEffort: elements.reasoningEffort.value,
    skillsLibraryId: selectedSkillsLibrary(task)?.id || elements.skillsLibrary.value,
    agentIdleTimeout: elements.agentIdleTimeout.value,
    jobsRoot: elements.jobsRoot.value,
  };

  if (body.promptMode === "force-all-skills" && body.skillMode === "no-skill") {
    alert("强制运行全部 skill 模式需要使用 with-skill。");
    return;
  }

  const response = await fetch("/api/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) {
    alert(data.error || "启动失败");
    return;
  }
  elements.apiKey.value = "";
}

async function stopGroup(groupId) {
  await fetch(`/api/groups/${encodeURIComponent(groupId)}/stop`, { method: "POST" });
}

function connectEvents() {
  const source = new EventSource("/api/events");
  source.onmessage = (event) => {
    const data = JSON.parse(event.data);
    if (data.type === "snapshot") {
      state.tasks = data.tasks || state.tasks;
      state.skillLibraries = data.config?.skillLibraries || state.skillLibraries;
      state.groups = data.groups || [];
      state.runs = data.runs || [];
      state.config = data.config || {};
      elements.workspaceLabel.textContent = state.config.cwd || "";
    }
    if (data.type === "group_update") {
      const idx = state.groups.findIndex((group) => group.id === data.group.id);
      if (idx >= 0) state.groups[idx] = data.group;
      else state.groups.unshift(data.group);
    }
    if (data.type === "run_update") {
      const idx = state.runs.findIndex((run) => run.id === data.run.id);
      if (idx >= 0) state.runs[idx] = data.run;
      else state.runs.unshift(data.run);
      if (data.group) {
        const groupIdx = state.groups.findIndex((group) => group.id === data.group.id);
        if (groupIdx >= 0) state.groups[groupIdx] = data.group;
        else state.groups.unshift(data.group);
      }
      if (!state.selectedRunId) state.selectedRunId = data.run.id;
      if (["passed", "failed", "error", "stopped"].includes(data.run.status)) {
        fetchHistory();
      }
    }
    if (data.type === "run_log") {
      const logs = state.logs.get(data.runId) || [];
      logs.push(data.line);
      if (logs.length > 2500) logs.splice(0, logs.length - 2500);
      state.logs.set(data.runId, logs);
      if (!state.selectedRunId) state.selectedRunId = data.runId;
    }
    renderAll();
  };
  source.onerror = () => {
    elements.workspaceLabel.textContent = "事件连接中断，浏览器会自动重连";
  };
}

$("refreshTasks").addEventListener("click", fetchTasks);
$("refreshHistory").addEventListener("click", fetchHistory);
$("startRun").addEventListener("click", startRun);
$("stopLatest").addEventListener("click", () => {
  const active = state.groups.find((group) => ["running", "pending"].includes(group.status));
  if (active) stopGroup(active.id);
});
$("clearLog").addEventListener("click", () => {
  if (state.selectedRunId) state.logs.set(state.selectedRunId, []);
  renderLog();
});

elements.modelConfigSelect.addEventListener("change", () => {
  const id = elements.modelConfigSelect.value;
  state.selectedModelConfigId = id;
  if (!id) {
    elements.modelConfigName.value = "";
    elements.saveApiKey.checked = false;
    return;
  }
  const config = state.modelConfigs.find((item) => item.id === id);
  if (config) {
    applyModelConfig(config);
    setModelConfigStatus("已加载配置");
  }
});

elements.saveModelConfig.addEventListener("click", saveModelConfig);
elements.deleteModelConfig.addEventListener("click", deleteModelConfig);

for (const el of [
  elements.taskSearch,
  elements.skillCountMode,
  elements.skillCountMin,
  elements.skillCountMax,
  elements.onlyPureSkills,
  elements.onlyLocalImage,
]) {
  el.addEventListener("input", renderTasks);
  el.addEventListener("change", renderTasks);
}

elements.historySearch.addEventListener("input", renderHistory);

function syncSkillModeControls() {
  const force = elements.skillMode.value === "force-skill";
  elements.promptMode.disabled = force;
  if (force) elements.promptMode.value = "force-all-skills";
  else if (elements.promptMode.value === "force-all-skills") elements.promptMode.value = "standard";
  elements.skillsLibrary.disabled = elements.skillMode.value === "no-skill";
}

elements.skillMode.addEventListener("change", () => {
  syncSkillModeControls();
  renderAll();
});
elements.provider.addEventListener("change", () => {
  const defaults = {
    deepseek: "https://api.deepseek.com/anthropic",
    anthropic: "https://api.anthropic.com",
    openai: "https://api.openai.com/v1",
    custom: "",
  };
  const current = elements.baseUrl.value.trim();
  if (!current || Object.values(defaults).includes(current)) {
    elements.baseUrl.value = defaults[elements.provider.value] || "";
  }
});
elements.reasoningEffort.addEventListener("change", renderAll);
elements.promptMode.addEventListener("change", renderAll);
elements.skillsLibrary.addEventListener("change", () => {
  state.selectedSkillsLibraryId = elements.skillsLibrary.value;
  renderAll();
});

loadModelConfigs();
renderModelConfigs();
syncSkillModeControls();
connectEvents();
await fetchTasks();
await fetchHistory();
