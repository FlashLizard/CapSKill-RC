const $ = (id) => document.getElementById(id);

const state = {
  tasks: [],
  repairJobs: [],
  reports: [],
  logs: new Map(),
  reportTexts: new Map(),
  selectedRepairId: "",
  selectedReportPath: "",
};

const elements = {
  subtitle: $("repairSubtitle"),
  status: $("repairStatus"),
  taskSelect: $("repairTaskSelect"),
  skillsLibrary: $("repairSkillsLibrary"),
  variant: $("repairVariant"),
  maxRollouts: $("repairMaxRollouts"),
  jobPaths: $("repairJobPaths"),
  force: $("repairForce"),
  judge: $("repairJudge"),
  strongProvider: $("repairStrongProvider"),
  strongModel: $("repairStrongModel"),
  strongReasoningEffort: $("repairStrongReasoningEffort"),
  strongBaseUrl: $("repairStrongBaseUrl"),
  strongMaxTokens: $("repairStrongMaxTokens"),
  strongTimeout: $("repairStrongTimeout"),
  strongApiKey: $("repairStrongApiKey"),
  weakModel: $("repairWeakModel"),
  keywordMode: $("repairKeywordMode"),
  refresh: $("refreshRepair"),
  start: $("startRepair"),
  stop: $("stopRepair"),
  jobsList: $("repairJobsList"),
  reportsList: $("repairReportsList"),
  detailTitle: $("repairDetailTitle"),
  detail: $("repairDetail"),
};

const CONFIG_KEY = "skillsbench.runner.repairConfig.v1";
const query = new URLSearchParams(window.location.search);

function escapeHtml(text) {
  return String(text ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function formatTime(value) {
  if (!value) return "-";
  return new Date(value).toLocaleTimeString("zh-CN", { hour12: false });
}

function runStatusBadge(status) {
  if (status === "completed") return `<span class="badge pass">DONE</span>`;
  if (status === "running") return `<span class="badge run">RUNNING</span>`;
  if (status === "pending") return `<span class="badge warn">PENDING</span>`;
  if (status === "stopping") return `<span class="badge warn">STOPPING</span>`;
  if (status === "error") return `<span class="badge fail">ERROR</span>`;
  if (status === "stopped") return `<span class="badge warn">STOPPED</span>`;
  return `<span class="badge">${escapeHtml(status || "-")}</span>`;
}

function saveConfig() {
  localStorage.setItem(CONFIG_KEY, JSON.stringify({
    strongProvider: elements.strongProvider.value,
    strongModel: elements.strongModel.value,
    strongReasoningEffort: elements.strongReasoningEffort.value,
    strongBaseUrl: elements.strongBaseUrl.value,
    strongMaxTokens: elements.strongMaxTokens.value,
    strongTimeout: elements.strongTimeout.value,
    weakModel: elements.weakModel.value,
    keywordMode: elements.keywordMode.value,
  }));
}

function loadConfig() {
  try {
    const config = JSON.parse(localStorage.getItem(CONFIG_KEY) || "{}");
    if (config.strongProvider) elements.strongProvider.value = config.strongProvider;
    if (config.strongModel) elements.strongModel.value = config.strongModel;
    if (config.strongReasoningEffort) elements.strongReasoningEffort.value = config.strongReasoningEffort;
    if (config.strongBaseUrl) elements.strongBaseUrl.value = config.strongBaseUrl;
    if (config.strongMaxTokens) elements.strongMaxTokens.value = config.strongMaxTokens;
    if (config.strongTimeout) elements.strongTimeout.value = config.strongTimeout;
    if (config.weakModel) elements.weakModel.value = config.weakModel;
    if (config.keywordMode) elements.keywordMode.value = config.keywordMode;
  } catch {
    // Ignore local storage corruption.
  }
}

function selectedTask() {
  return state.tasks.find((task) => task.key === elements.taskSelect.value) || null;
}

function skillsLibraryDisplay(library) {
  return library?.label || library?.skillsDir || library?.id || "-";
}

function defaultLibraryId(task) {
  const libraries = task?.skillLibraries || [];
  return task?.defaultSkillsLibraryId
    || libraries.find((library) => library.variant === "initial")?.id
    || libraries[0]?.id
    || "";
}

function renderSkillLibraries() {
  const task = selectedTask();
  const libraries = task?.skillLibraries || [];
  elements.skillsLibrary.innerHTML = libraries
    .map((library) => `<option value="${escapeHtml(library.id)}">${escapeHtml(skillsLibraryDisplay(library))}${library.variant === "initial" ? "（默认）" : ""}</option>`)
    .join("") || `<option value="">无可用 skills 库</option>`;
  elements.skillsLibrary.value = defaultLibraryId(task);
}

function renderTasks() {
  elements.taskSelect.innerHTML = state.tasks
    .map((task) => `<option value="${escapeHtml(task.key)}">${escapeHtml(task.rootLabel)}/${escapeHtml(task.name)} · ${task.skillCount} skills</option>`)
    .join("");
  const preferred = state.tasks.find((task) => task.name === "bike-rebalance") || state.tasks[0];
  if (preferred) elements.taskSelect.value = preferred.key;
  renderSkillLibraries();
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

function jobSummary(job) {
  return `
    <button class="repair-job-row${job.id === state.selectedRepairId ? " selected" : ""}" data-repair-id="${escapeHtml(job.id)}">
      <div>
        <strong>${escapeHtml(job.taskName)}</strong>
        <span>${escapeHtml(job.outputVariant || "-")}</span>
      </div>
      ${runStatusBadge(job.status)}
    </button>
  `;
}

function reportSummary(report) {
  const selected = report.reportPath === state.selectedReportPath;
  return `
    <button class="repair-job-row${selected ? " selected" : ""}" data-report-path="${escapeHtml(report.reportPath)}">
      <div>
        <strong>${escapeHtml((report.task || "").split("/").at(-1) || report.variant || "report")}</strong>
        <span>${escapeHtml(report.variant || report.outputSkillsDir || "-")}</span>
      </div>
      <span class="badge ${report.usedFallback ? "warn" : "pass"}">${report.usedFallback ? "fallback" : "llm"}</span>
    </button>
  `;
}

function renderLists() {
  elements.jobsList.innerHTML = state.repairJobs.map(jobSummary).join("") || `<div class="detail-empty">还没有运行中的 repair job。</div>`;
  elements.reportsList.innerHTML = state.reports.map(reportSummary).join("") || `<div class="detail-empty">还没有 repair report。</div>`;

  elements.jobsList.querySelectorAll("[data-repair-id]").forEach((button) => {
    button.addEventListener("click", async () => {
      state.selectedRepairId = button.dataset.repairId;
      state.selectedReportPath = "";
      await fetchRepairLogs(state.selectedRepairId);
      renderDetail();
      renderLists();
    });
  });
  elements.reportsList.querySelectorAll("[data-report-path]").forEach((button) => {
    button.addEventListener("click", async () => {
      state.selectedRepairId = "";
      state.selectedReportPath = button.dataset.reportPath;
      await fetchReport(state.selectedReportPath);
      renderDetail();
      renderLists();
    });
  });
}

function renderAppliedFiles(files) {
  return `
    <section class="detail-section">
      <div class="detail-section-title"><h4>写入文件</h4></div>
      ${(files || []).map((file) => `
        <div class="repair-file-row">
          <strong>${escapeHtml(file.relativePath || file.path)}</strong>
          <span>${escapeHtml(file.why || "")}</span>
        </div>
      `).join("") || `<div class="detail-empty">尚未产生写入文件。</div>`}
    </section>
  `;
}

function renderRepairJobDetail(job) {
  const logs = state.logs.get(job.id) || [];
  elements.detailTitle.textContent = `Repair · ${job.taskName}`;
  elements.stop.disabled = job.status !== "running";
  elements.detail.innerHTML = `
    <div class="detail-view">
      ${renderKeyValueGrid({
        status: job.status,
        variant: job.outputVariant,
        sourceSkills: job.sourceSkillsLibraryLabel || job.sourceSkillsDir,
        outputSkills: job.outputSkillsDir,
        report: job.reportPath,
        strongModel: `${job.strongProvider}/${job.strongModel}`,
        weakModel: job.weakModel,
        rollouts: job.maxRollouts,
        started: job.startedAt,
        ended: job.endedAt,
        exitCode: job.exitCode,
      })}
      ${job.error ? `<div class="detail-error">${escapeHtml(job.error)}</div>` : ""}
      ${renderAppliedFiles(job.appliedFiles)}
      <section class="detail-section">
        <div class="detail-section-title"><h4>日志</h4></div>
        <pre class="repair-log">${escapeHtml(logs.map((line) => `[${formatTime(line.at)}] ${line.text}`).join("\n") || "等待日志...")}</pre>
      </section>
    </div>
  `;
  const pre = elements.detail.querySelector(".repair-log");
  if (pre && ["running", "pending"].includes(job.status)) pre.scrollTop = pre.scrollHeight;
}

function renderReportDetail(reportPath) {
  const report = state.reports.find((item) => item.reportPath === reportPath);
  const text = state.reportTexts.get(reportPath) || "正在读取 report...";
  elements.stop.disabled = true;
  elements.detailTitle.textContent = "Repair Report";
  elements.detail.innerHTML = `
    <div class="detail-view">
      ${report ? renderKeyValueGrid({
        task: report.task,
        variant: report.variant,
        sourceSkills: report.sourceSkillsDir,
        outputSkills: report.outputSkillsDir,
        usedFallback: report.usedFallback,
        strongModel: report.strongModel ? `${report.strongModel.provider}/${report.strongModel.model}` : "",
        report: report.reportPath,
      }) : ""}
      <pre class="repair-report-text">${escapeHtml(text)}</pre>
    </div>
  `;
}

function renderDetail() {
  const job = state.repairJobs.find((item) => item.id === state.selectedRepairId);
  if (job) {
    renderRepairJobDetail(job);
    return;
  }
  if (state.selectedReportPath) {
    renderReportDetail(state.selectedReportPath);
    return;
  }
  elements.stop.disabled = true;
  elements.detailTitle.textContent = "详情";
  elements.detail.innerHTML = `<div class="detail-empty">选择或启动一个 repair job。</div>`;
}

async function fetchTasks() {
  const response = await fetch("/api/tasks");
  const data = await response.json();
  state.tasks = data.tasks || [];
  renderTasks();
}

async function fetchRepairState() {
  const response = await fetch("/api/repair/jobs");
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "读取 repair jobs 失败");
  state.repairJobs = data.repairJobs || [];
  state.reports = data.reports || [];
  renderLists();
  renderDetail();
}

async function fetchRepairLogs(jobId) {
  const response = await fetch(`/api/repair/jobs/${encodeURIComponent(jobId)}/logs`);
  const data = await response.json();
  if (!response.ok) return;
  state.logs.set(jobId, data.logs || []);
}

async function fetchReport(reportPath) {
  if (!reportPath || state.reportTexts.has(reportPath)) return;
  const response = await fetch(`/api/repair/report?reportPath=${encodeURIComponent(reportPath)}`);
  const data = await response.json();
  state.reportTexts.set(reportPath, response.ok ? data.text || "" : data.error || "读取 report 失败");
}

function repairPayload() {
  return {
    taskKey: elements.taskSelect.value,
    sourceSkillsLibraryId: elements.skillsLibrary.value,
    outputVariant: elements.variant.value.trim(),
    jobPaths: elements.jobPaths.value,
    maxRollouts: Number(elements.maxRollouts.value || 6),
    force: elements.force.checked,
    judge: elements.judge.checked,
    strongProvider: elements.strongProvider.value,
    strongModel: elements.strongModel.value.trim(),
    strongReasoningEffort: elements.strongReasoningEffort.value,
    strongBaseUrl: elements.strongBaseUrl.value.trim(),
    strongApiKey: elements.strongApiKey.value.trim(),
    strongMaxTokens: Number(elements.strongMaxTokens.value || 8000),
    strongTimeout: Number(elements.strongTimeout.value || 240),
    weakModel: elements.weakModel.value.trim(),
    keywordMode: elements.keywordMode.value,
  };
}

async function startRepair() {
  const payload = repairPayload();
  if (!payload.taskKey) {
    alert("请选择 task。");
    return;
  }
  if (!payload.jobPaths.trim()) {
    alert("请填写至少一个 jobs / artifact / rollout 路径。");
    return;
  }
  saveConfig();
  elements.status.textContent = "启动中...";
  const response = await fetch("/api/repair/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok) {
    elements.status.textContent = "";
    alert(data.error || "启动修复失败");
    return;
  }
  elements.strongApiKey.value = "";
  state.selectedRepairId = data.repairJob.id;
  state.selectedReportPath = "";
  elements.status.textContent = "已启动";
  await fetchRepairState();
}

async function stopRepair() {
  if (!state.selectedRepairId) return;
  await fetch(`/api/repair/jobs/${encodeURIComponent(state.selectedRepairId)}/stop`, { method: "POST" });
}

function connectEvents() {
  const source = new EventSource("/api/events");
  source.onmessage = (event) => {
    const data = JSON.parse(event.data);
    if (data.type === "snapshot") {
      state.repairJobs = data.repairJobs || state.repairJobs;
    }
    if (data.type === "repair_update") {
      const idx = state.repairJobs.findIndex((job) => job.id === data.repairJob.id);
      if (idx >= 0) state.repairJobs[idx] = data.repairJob;
      else state.repairJobs.unshift(data.repairJob);
      if (!state.selectedRepairId) state.selectedRepairId = data.repairJob.id;
      if (["completed", "error", "stopped"].includes(data.repairJob.status)) fetchRepairState();
    }
    if (data.type === "repair_log") {
      const logs = state.logs.get(data.jobId) || [];
      logs.push(data.line);
      if (logs.length > 2500) logs.splice(0, logs.length - 2500);
      state.logs.set(data.jobId, logs);
    }
    renderLists();
    renderDetail();
  };
  source.onerror = () => {
    elements.status.textContent = "事件连接中断，浏览器会自动重连";
  };
}

elements.taskSelect.addEventListener("change", renderSkillLibraries);
elements.refresh.addEventListener("click", fetchRepairState);
elements.start.addEventListener("click", startRepair);
elements.stop.addEventListener("click", stopRepair);

for (const item of [
  elements.strongProvider,
  elements.strongModel,
  elements.strongReasoningEffort,
  elements.strongBaseUrl,
  elements.strongMaxTokens,
  elements.strongTimeout,
  elements.weakModel,
  elements.keywordMode,
]) {
  item.addEventListener("change", saveConfig);
}

loadConfig();
if (query.get("jobPath")) elements.jobPaths.value = query.get("jobPath");
if (query.get("artifactDir")) elements.jobPaths.value = query.get("artifactDir");
await fetchTasks();
await fetchRepairState();
connectEvents();
