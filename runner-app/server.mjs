import http from "node:http";
import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";
import { spawn, execFile, execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const APP_DIR = path.dirname(__filename);
const ROOT = path.resolve(APP_DIR, "..");
const PUBLIC_DIR = path.join(APP_DIR, "public");
const PORT = Number(process.env.SKILLSBENCH_RUNNER_PORT || 5198);

const state = {
  tasks: [],
  skillLibraries: [],
  groups: [],
  runs: [],
  repairJobs: [],
  stageJobs: [],
  history: [],
  clients: new Set(),
};

const MAX_LOG_LINES = 2500;
const MAX_TRAJECTORY_BYTES = 5_000_000;
const MAX_TRAJECTORY_EVENTS = 5000;
const MAX_REPAIR_TEXT_BYTES = 1_500_000;
const MAX_REPAIR_JSON_BYTES = 8_000_000;
const PROMPT_MODE_STANDARD = "standard";
const PROMPT_MODE_FORCE_ALL_SKILLS = "force-all-skills";
const PROVIDER_TYPES = new Set(["deepseek", "anthropic", "openai", "custom"]);
const SKILL_MODES = new Set(["no-skill", "with-skill", "force-skill"]);
const REASONING_EFFORTS = new Set(["off", "minimal", "low", "medium", "high", "max", "xhigh"]);
const FORCE_ALL_SKILLS_PROMPT_PATH = path.join(APP_DIR, "prompts", "force-all-skills.md");
const STOP_AUDIT_PATH = path.join(ROOT, "jobs", "web-runner", "runner-stop-audit.log");
const REPAIR_STAGE_PRESET_PATH = path.join(ROOT, ".runner-config", "repair-stage-presets.json");
const MAX_REPAIR_STAGE_PRESETS = 100;

function nowIso() {
  return new Date().toISOString();
}

function makeId(prefix) {
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

function sanitizeSegment(value) {
  return String(value || "run")
    .replace(/[^a-zA-Z0-9._-]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 80) || "run";
}

function toPosixRelative(absPath) {
  return path.relative(ROOT, absPath).replace(/\\/g, "/");
}

function resolveInsideRoot(relPath) {
  const resolved = path.resolve(ROOT, relPath);
  const relative = path.relative(ROOT, resolved);
  // startsWith(ROOT) 会把 /workspace2 误判为 /workspace 的子路径；用
  // path.relative 处理 Windows 盘符和 Linux 分隔符，避免路径越界。
  if (relative === ".." || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
    throw new Error("Path escapes workspace root");
  }
  return resolved;
}

function normalizePromptMode(value) {
  const mode = String(value || PROMPT_MODE_STANDARD).trim();
  return mode === PROMPT_MODE_FORCE_ALL_SKILLS ? mode : PROMPT_MODE_STANDARD;
}

function normalizeProvider(value) {
  const provider = String(value || "deepseek").trim().toLowerCase();
  if (!PROVIDER_TYPES.has(provider)) {
    throw new Error(`Unsupported provider: ${provider}`);
  }
  return provider;
}

function normalizeSkillMode(value) {
  const mode = String(value || "with-skill").trim().toLowerCase();
  if (!SKILL_MODES.has(mode)) {
    throw new Error(`Unsupported skill mode: ${mode}`);
  }
  return mode;
}

function normalizeReasoningEffort(value) {
  const effort = String(value || "off").trim().toLowerCase();
  if (!REASONING_EFFORTS.has(effort)) {
    throw new Error(`Unsupported reasoning effort: ${effort}`);
  }
  // DeepSeek-compatible endpoints reject the OpenAI-style "minimal" enum.
  // Keep the UI option for other providers, but map it before the BenchFlow CLI.
  return effort;
}

function effectiveReasoningEffort(provider, model, baseUrl, effort) {
  const normalized = normalizeReasoningEffort(effort);
  if (normalized === "off") return "";
  const target = `${provider} ${model} ${baseUrl}`.toLowerCase();
  if (normalized === "minimal" && (provider === "deepseek" || target.includes("deepseek"))) return "low";
  return normalized;
}

function listSkillNamesSync(skillsDir) {
  const absSkillsDir = resolveInsideRoot(skillsDir);
  if (!fs.existsSync(absSkillsDir)) return [];
  return fs.readdirSync(absSkillsDir, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => entry.name)
    .filter((name) => fs.existsSync(path.join(absSkillsDir, name, "SKILL.md")))
    .sort((a, b) => a.localeCompare(b));
}

function forceAllSkillsInstruction(skillNames) {
  const list = skillNames.map((name, index) => `${index + 1}. /${name}`).join("\n");
  const template = fs.readFileSync(FORCE_ALL_SKILLS_PROMPT_PATH, "utf8");
  return template.replace("{{skill_list}}", list).trim();
}

function injectInstructionIntoTaskMarkdown(text, instruction) {
  const match = text.match(/^---\s*\r?\n[\s\S]*?\r?\n---\s*\r?\n?/);
  if (!match) return `${instruction}\n\n${text}`;
  return `${match[0]}\n${instruction}\n\n${text.slice(match[0].length).trimStart()}`;
}

function setSectionTimeout(frontmatter, sectionName, timeoutSec) {
  const lines = frontmatter.split(/\r?\n/);
  const topLevelPattern = /^[A-Za-z0-9_-]+:\s*(?:#.*)?$/;
  const sectionPattern = new RegExp(`^${sectionName}:\\s*(?:#.*)?$`);
  const timeoutValue = `${Number(timeoutSec).toFixed(1)}`;

  let sectionStart = lines.findIndex((line) => sectionPattern.test(line));
  if (sectionStart === -1) {
    const prefix = lines.length && lines[lines.length - 1].trim() ? [""] : [];
    return [...lines, ...prefix, `${sectionName}:`, `  timeout_sec: ${timeoutValue}`].join("\n");
  }

  let sectionEnd = lines.length;
  for (let index = sectionStart + 1; index < lines.length; index += 1) {
    if (topLevelPattern.test(lines[index])) {
      sectionEnd = index;
      break;
    }
  }

  for (let index = sectionStart + 1; index < sectionEnd; index += 1) {
    if (/^\s+timeout_sec:\s*/.test(lines[index])) {
      const indent = lines[index].match(/^\s*/)?.[0] || "  ";
      lines[index] = `${indent}timeout_sec: ${timeoutValue}`;
      return lines.join("\n");
    }
  }

  lines.splice(sectionStart + 1, 0, `  timeout_sec: ${timeoutValue}`);
  return lines.join("\n");
}

function setSectionScalar(frontmatter, sectionName, key, value) {
  const lines = frontmatter.split(/\r?\n/);
  const topLevelPattern = /^[A-Za-z0-9_-]+:\s*(?:#.*)?$/;
  const sectionPattern = new RegExp(`^${sectionName}:\\s*(?:#.*)?$`);
  const rendered = `${key}: ${value}`;

  let sectionStart = lines.findIndex((line) => sectionPattern.test(line));
  if (sectionStart === -1) {
    const prefix = lines.length && lines[lines.length - 1].trim() ? [""] : [];
    return [...lines, ...prefix, `${sectionName}:`, `  ${rendered}`].join("\n");
  }

  let sectionEnd = lines.length;
  for (let index = sectionStart + 1; index < lines.length; index += 1) {
    if (topLevelPattern.test(lines[index])) {
      sectionEnd = index;
      break;
    }
  }

  const keyPattern = new RegExp(`^\\s+${key}:\\s*`);
  for (let index = sectionStart + 1; index < sectionEnd; index += 1) {
    if (keyPattern.test(lines[index])) {
      const indent = lines[index].match(/^\s*/)?.[0] || "  ";
      lines[index] = `${indent}${rendered}`;
      return lines.join("\n");
    }
  }

  lines.splice(sectionStart + 1, 0, `  ${rendered}`);
  return lines.join("\n");
}

function injectRunTimeoutsIntoTaskMarkdown(text, timeoutSec) {
  if (!timeoutSec) return text;
  const match = text.match(/^---\s*\r?\n([\s\S]*?)\r?\n---(\s*\r?\n?)/);
  let frontmatter = match ? match[1] : "";
  frontmatter = setSectionTimeout(frontmatter, "agent", timeoutSec);
  frontmatter = setSectionTimeout(frontmatter, "verifier", timeoutSec);
  frontmatter = setSectionScalar(frontmatter, "environment", "build_timeout_sec", Number(timeoutSec).toFixed(1));
  if (!match) return `---\n${frontmatter}\n---\n\n${text}`;
  return `---\n${frontmatter}\n---${match[2]}${text.slice(match[0].length)}`;
}

function injectDockerImageIntoTaskMarkdown(text, dockerImage) {
  if (!dockerImage) return text;
  const match = text.match(/^---\s*\r?\n([\s\S]*?)\r?\n---(\s*\r?\n?)/);
  const frontmatter = match
    ? setSectionScalar(match[1], "environment", "docker_image", dockerImage)
    : `environment:\n  docker_image: ${dockerImage}`;
  if (!match) return `---\n${frontmatter}\n---\n\n${text}`;
  return `---\n${frontmatter}\n---${match[2]}${text.slice(match[0].length)}`;
}

function stripComposeBuildForPrebuiltImage(text) {
  const lines = text.split(/\r?\n/);
  const kept = [];
  let skippingBuildIndent = null;

  for (const line of lines) {
    const indent = line.match(/^\s*/)?.[0].length || 0;
    if (skippingBuildIndent !== null) {
      if (line.trim() && indent > skippingBuildIndent) continue;
      skippingBuildIndent = null;
    }

    if (/^\s+build:\s*$/.test(line)) {
      skippingBuildIndent = indent;
      continue;
    }
    if (/^\s{4}image:\s*\$\{MAIN_IMAGE_NAME\}\s*$/.test(line)) continue;
    if (line.includes("${HOME}/.config/gcloud:/root/.config/gcloud")) continue;
    kept.push(line);
  }

  return kept.join("\n");
}

function removeComposeBuildFromOverlay(overlayTaskAbs) {
  const composePath = path.join(overlayTaskAbs, "environment", "docker-compose.yaml");
  if (!fs.existsSync(composePath)) return;
  const original = fs.readFileSync(composePath, "utf8");
  const updated = stripComposeBuildForPrebuiltImage(original);
  if (updated !== original) fs.writeFileSync(composePath, updated, "utf8");
}

function loadPrebuiltImages() {
  try {
    const file = path.join(APP_DIR, "prebuilt-images.json");
    const parsed = JSON.parse(fs.readFileSync(file, "utf8"));
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function prebuiltImageForTask(task) {
  const images = loadPrebuiltImages();
  const value = images[task?.name] || images[task?.key];
  return typeof value === "string" ? value.trim() : "";
}

function createTaskOverlay(task, skillsDir, promptMode, agentTimeoutSec, prebuiltImage = "") {
  const shouldForceSkills = promptMode === PROMPT_MODE_FORCE_ALL_SKILLS;
  const shouldOverrideTimeout = Boolean(agentTimeoutSec);
  const shouldUsePrebuiltImage = Boolean(prebuiltImage);
  if (!shouldForceSkills && !shouldOverrideTimeout && !shouldUsePrebuiltImage) return task.taskDir;

  let skillNames = [];
  if (shouldForceSkills) {
    skillNames = listSkillNamesSync(skillsDir);
    if (!skillNames.length) {
      throw new Error("force-all-skills mode requires a non-empty skills library");
    }
  }

  const sourceTaskDir = resolveInsideRoot(task.taskDir);
  const overlayMode = shouldForceSkills ? "force" : shouldUsePrebuiltImage ? "image" : "timeout";
  const taskSlug = sanitizeSegment(task.name).slice(0, 24);
  const overlayParentRel = `.runner-task-overlays/${taskSlug}-${overlayMode}-${makeId("ov")}`;
  const overlayParentAbs = resolveInsideRoot(overlayParentRel);
  const overlayTaskAbs = path.join(overlayParentAbs, task.name);

  fs.mkdirSync(overlayParentAbs, { recursive: true });
  fs.cpSync(sourceTaskDir, overlayTaskAbs, { recursive: true, force: true });

  const taskMdPath = path.join(overlayTaskAbs, "task.md");
  let updated = fs.readFileSync(taskMdPath, "utf8");
  if (shouldUsePrebuiltImage) {
    updated = injectDockerImageIntoTaskMarkdown(updated, prebuiltImage);
    removeComposeBuildFromOverlay(overlayTaskAbs);
  }
  if (shouldOverrideTimeout) {
    updated = injectRunTimeoutsIntoTaskMarkdown(updated, agentTimeoutSec);
  }
  if (shouldForceSkills) {
    updated = injectInstructionIntoTaskMarkdown(updated, forceAllSkillsInstruction(skillNames));
  }
  fs.writeFileSync(taskMdPath, updated, "utf8");

  return toPosixRelative(overlayTaskAbs);
}

async function exists(filePath) {
  try {
    await fsp.access(filePath);
    return true;
  } catch {
    return false;
  }
}

function parseFrontmatter(text) {
  const match = text.match(/^---\s*\r?\n([\s\S]*?)\r?\n---/);
  const meta = {};
  if (!match) return meta;

  const lines = match[1].split(/\r?\n/);
  const stack = [meta];
  const indents = [-1];

  for (const rawLine of lines) {
    if (!rawLine.trim() || rawLine.trim().startsWith("#")) continue;
    const indent = rawLine.match(/^\s*/)?.[0].length ?? 0;
    const line = rawLine.trim();
    if (line.startsWith("- ")) continue;
    const pair = line.match(/^([A-Za-z0-9_-]+):\s*(.*)$/);
    if (!pair) continue;

    while (indent <= indents[indents.length - 1] && stack.length > 1) {
      stack.pop();
      indents.pop();
    }

    const key = pair[1];
    let value = pair[2].trim();
    const parent = stack[stack.length - 1];

    if (!value) {
      parent[key] = {};
      stack.push(parent[key]);
      indents.push(indent);
    } else {
      value = value.replace(/^['"]|['"]$/g, "");
      const numeric = Number(value);
      parent[key] = Number.isFinite(numeric) && /^-?\d+(\.\d+)?$/.test(value) ? numeric : value;
    }
  }

  return meta;
}

async function readSkillDirectoryInfo(skillsDir) {
  try {
    const entries = await fsp.readdir(skillsDir, { withFileTypes: true });
    let count = 0;
    let resourceCount = 0;
    let pureSkillMdOnly = true;
    for (const entry of entries) {
      if (!entry.isDirectory()) continue;
      const skillDir = path.join(skillsDir, entry.name);
      if (!(await exists(path.join(skillDir, "SKILL.md")))) continue;
      count += 1;
      const skillEntries = await fsp.readdir(skillDir, { withFileTypes: true });
      const extras = skillEntries.filter((item) => item.name !== "SKILL.md" && !item.name.startsWith("."));
      resourceCount += extras.length;
      if (extras.length) pureSkillMdOnly = false;
    }
    return {
      skillCount: count,
      skillResourceCount: resourceCount,
      pureSkillMdOnly: count > 0 && pureSkillMdOnly,
    };
  } catch {
    return {
      skillCount: 0,
      skillResourceCount: 0,
      pureSkillMdOnly: false,
    };
  }
}

async function scanSkillLibraries() {
  const librariesRoot = path.join(ROOT, "skill-libraries");
  const libraries = [];
  if (!(await exists(librariesRoot))) {
    state.skillLibraries = [];
    return [];
  }

  const taskEntries = await fsp.readdir(librariesRoot, { withFileTypes: true });
  for (const taskEntry of taskEntries) {
    if (!taskEntry.isDirectory()) continue;
    const taskName = taskEntry.name;
    const taskLibraryRoot = path.join(librariesRoot, taskName);
    const variantEntries = await fsp.readdir(taskLibraryRoot, { withFileTypes: true });
    for (const variantEntry of variantEntries) {
      if (!variantEntry.isDirectory()) continue;
      const skillsDir = path.join(taskLibraryRoot, variantEntry.name);
      const info = await readSkillDirectoryInfo(skillsDir);
      if (info.skillCount <= 0) continue;
      libraries.push({
        id: `${taskName}:${variantEntry.name}`,
        taskName,
        variant: variantEntry.name,
        label: `${taskName}/${variantEntry.name}`,
        skillsDir: toPosixRelative(skillsDir),
        ...info,
      });
    }
  }

  libraries.sort((a, b) => {
    const taskOrder = a.taskName.localeCompare(b.taskName);
    if (taskOrder) return taskOrder;
    const rank = { initial: 0, "s0-repaired": 1, mip: 2 };
    return (rank[a.variant] ?? 99) - (rank[b.variant] ?? 99) || a.variant.localeCompare(b.variant);
  });
  state.skillLibraries = libraries;
  return libraries;
}

function librariesForTask(task) {
  const taskName = task?.name || "";
  return state.skillLibraries.filter((library) => (
    library.taskName === taskName ||
    taskName.startsWith(`${library.taskName}-`) ||
    library.taskName.startsWith(`${taskName}-`)
  ));
}

function defaultLibraryForTask(task) {
  const libraries = librariesForTask(task);
  return libraries.find((library) => library.variant === "initial") || taskBundledLibrary(task) || libraries[0] || null;
}

function taskBundledLibrary(task) {
  if (!task?.hasSkills) return null;
  return {
    id: `${task.key}:task-bundled`,
    taskName: task.name,
    variant: "task-bundled",
    label: `${task.name}/task-bundled`,
    skillsDir: task.skillsDir,
    skillCount: task.skillCount,
    skillResourceCount: task.skillResourceCount,
    pureSkillMdOnly: task.pureSkillMdOnly,
  };
}

function availableLibrariesForTask(task) {
  const libraries = librariesForTask(task);
  const bundled = taskBundledLibrary(task);
  if (!bundled) return libraries;
  const bundledDir = String(bundled.skillsDir || "").replace(/\\/g, "/");
  const hasBundled = libraries.some((library) => String(library.skillsDir || "").replace(/\\/g, "/") === bundledDir);
  return hasBundled ? libraries : [bundled, ...libraries];
}

function resolveLibraryForTask(task, requestedId) {
  const available = availableLibrariesForTask(task);
  const requested = available.find((library) => library.id === requestedId);
  const initial = available.find((library) => library.variant === "initial");
  return requested || initial || available[0] || null;
}

function describeSkillsLibraryFromResult(result) {
  const requested = result?.requested_skills_dir || result?.skills_dir || "";
  const effective = result?.effective_skills_dir || "";
  const normalizedRequested = String(requested || "").replace(/\\/g, "/");
  const normalizedEffective = String(effective || "").replace(/\\/g, "/");
  const found = state.skillLibraries.find((library) => (
    normalizedRequested === library.skillsDir ||
    normalizedRequested.endsWith(`/${library.skillsDir}`) ||
    normalizedEffective === library.skillsDir ||
    normalizedEffective.endsWith(`/${library.skillsDir}`)
  ));
  if (found) {
    return {
      id: found.id,
      label: found.label,
      skillsDir: found.skillsDir,
    };
  }
  if (normalizedRequested) {
    return {
      id: "",
      label: normalizedRequested,
      skillsDir: normalizedRequested,
    };
  }
  return {
    id: "",
    label: "",
    skillsDir: "",
  };
}

function normalizeInferencePath(value) {
  return String(value || "").trim().replace(/\\/g, "/").replace(/\/+$/g, "");
}

function pathMatchesCandidate(observed, candidate) {
  const left = normalizeInferencePath(observed);
  const right = normalizeInferencePath(candidate);
  if (!left || !right) return false;
  return left === right || left.endsWith(`/${right}`) || right.endsWith(`/${left}`);
}

function resultSkillPathCandidates(result) {
  const candidates = [
    result?.requested_skills_dir,
    result?.effective_skills_dir,
    result?.skills_dir,
    result?.skillsDir,
    result?.skill_library,
    result?.skillLibrary,
  ];

  const scenes = Array.isArray(result?.scenes) ? result.scenes : [];
  for (const scene of scenes) {
    candidates.push(scene?.skills_dir, scene?.skillsDir);
    const roles = Array.isArray(scene?.roles) ? scene.roles : [];
    for (const role of roles) {
      candidates.push(role?.skills_dir, role?.skillsDir);
    }
  }

  return [...new Set(candidates.map(normalizeInferencePath).filter(Boolean))];
}

function taskCandidatesFromResult(result, rolloutDir) {
  const taskName = String(result?.task_name || path.basename(rolloutDir).split("__")[0] || "").trim();
  const sourcePath = normalizeInferencePath(result?.source?.path);
  const exactSource = state.tasks.filter((task) => sourcePath && pathMatchesCandidate(sourcePath, task.taskDir));
  const exactName = state.tasks.filter((task) => task.name === taskName);
  const looseName = state.tasks.filter((task) => taskName && (task.name.startsWith(taskName) || taskName.startsWith(task.name)));
  const ordered = [...exactSource, ...exactName, ...looseName];
  const seen = new Set();
  return ordered.filter((task) => {
    if (!task || seen.has(task.key)) return false;
    seen.add(task.key);
    return true;
  });
}

function inferLibraryForTask(task, result) {
  if (!task) return null;
  const libraries = availableLibrariesForTask(task);
  const pathCandidates = resultSkillPathCandidates(result);
  for (const candidate of pathCandidates) {
    const found = libraries.find((library) => pathMatchesCandidate(candidate, library.skillsDir));
    if (found) {
      return {
        library: found,
        confidence: 0.96,
        reason: `result.json 中的 skills 路径匹配 ${found.skillsDir}`,
        observedPath: candidate,
      };
    }
  }

  if (result?.skill_source === "task_bundled" || result?.skill_mode === "with-skill") {
    const bundled = taskBundledLibrary(task);
    if (bundled) {
      return {
        library: bundled,
        confidence: result?.skill_source === "task_bundled" ? 0.82 : 0.62,
        reason: result?.skill_source === "task_bundled"
          ? "result.json 标记为 task_bundled，回退到该 task 的 bundled skills"
          : "result.json 没有可匹配 skills 路径，回退到该 task 的默认 skills",
        observedPath: pathCandidates[0] || "",
      };
    }
  }

  const fallback = defaultLibraryForTask(task);
  if (!fallback) return null;
  return {
    library: fallback,
    confidence: 0.5,
    reason: "未找到可匹配 skills 元数据，使用该 task 的默认 skills 库作为候选",
    observedPath: pathCandidates[0] || "",
  };
}

async function isRolloutDir(dir) {
  return Boolean(
    dir &&
    (await exists(path.join(dir, "result.json")) ||
      await exists(path.join(dir, "trajectory", "acp_trajectory.jsonl")) ||
      await exists(path.join(dir, "agent", "acp_trajectory.jsonl")))
  );
}

async function nearestRolloutAncestor(startPath) {
  let current = startPath;
  const rootParent = path.dirname(ROOT);
  for (let depth = 0; depth < 8 && current && current !== rootParent; depth += 1) {
    if (await isRolloutDir(current)) return current;
    const next = path.dirname(current);
    if (next === current) break;
    current = next;
  }
  return null;
}

const REPAIR_TIMEOUT_TOKENS = [
  "timeout",
  "timed out",
  "time limit",
  "deadline exceeded",
  "idle timeout",
  "agent timeout",
];
const REPAIR_ENVIRONMENT_TOKENS = [
  "environment",
  "configuration",
  "config error",
  "infrastructure",
  "sandbox",
  "container",
  "docker",
  "image pull",
  "permission denied",
  "access denied",
  "api key",
  "authentication",
  "unauthorized",
  "forbidden",
  "rate limit",
  "quota",
  "connection refused",
  "network error",
  "transport error",
  "provider error",
  "proxy error",
];
const REPAIR_TIMEOUT_CATEGORIES = new Set(["timeout", "idle_timeout", "agent_timeout", "verifier_timeout"]);
const REPAIR_ENVIRONMENT_CATEGORIES = new Set([
  "environment", "environment_error", "configuration", "configuration_error", "config_error",
  "infrastructure", "infrastructure_error", "sandbox", "sandbox_error", "setup_error",
  "transport_error", "api_error", "authentication_error", "permission_error", "provider_error",
  "network_error", "quota_error", "rate_limit",
]);

function repairNumericOutcome(value) {
  if (typeof value === "boolean") return value ? 1 : 0;
  if (typeof value === "number" && Number.isFinite(value)) return value > 0 ? 1 : 0;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value.trim().replace(/%$/, ""));
    if (Number.isFinite(parsed)) return parsed > 0 ? 1 : 0;
  }
  return null;
}

function repairExplicitOutcome(result) {
  if (repairNumericOutcome(result?.success) !== null) return repairNumericOutcome(result.success);
  const rewards = result?.rewards;
  if (rewards && typeof rewards === "object") {
    for (const key of ["reward", "score", "success"]) {
      const value = repairNumericOutcome(rewards[key]);
      if (value !== null) return value;
    }
  }
  for (const key of ["score", "score_excl_errors"]) {
    const value = repairNumericOutcome(result?.[key]);
    if (value !== null) return value;
  }
  return null;
}

function repairSignalText(result) {
  const fields = [
    "error", "error_category", "export_error", "partial_trajectory", "trajectory_source",
    "idle_timeout_info", "agent_timeout_info", "sandbox_startup_info", "transport_error_info",
    "api_error_info", "suspected_api_error_info",
  ];
  return fields
    .map((key) => result?.[key])
    .filter((value) => value !== undefined && value !== null && value !== "")
    .map((value) => typeof value === "string" ? value : JSON.stringify(value))
    .join(" ")
    .toLowerCase();
}

function repairErrorCategory(result) {
  return String(result?.error_category || "")
    .trim()
    .toLowerCase()
    .replace(/[- ]/g, "_");
}

function classifyRolloutForRepair(result, rolloutDir) {
  const outcome = repairExplicitOutcome(result);
  if (outcome === null) {
    return {
      selected: false,
      reason: "missing_explicit_outcome",
      reasonLabel: "缺少明确 success/reward/score",
      success: null,
    };
  }
  const category = repairErrorCategory(result);
  const signals = repairSignalText(result);
  let reason = "task_failure";
  let reasonLabel = "明确任务失败";
  if (outcome === 1) {
    reason = "success";
    reasonLabel = "任务成功，不作为失败证据";
  } else if (REPAIR_TIMEOUT_CATEGORIES.has(category) || REPAIR_TIMEOUT_TOKENS.some((token) => signals.includes(token))) {
    reason = "timeout";
    reasonLabel = "超时，不作为失败证据";
  } else if (REPAIR_ENVIRONMENT_CATEGORIES.has(category) || REPAIR_ENVIRONMENT_TOKENS.some((token) => signals.includes(token))) {
    reason = "environment_or_configuration_error";
    reasonLabel = "环境/配置错误，不作为失败证据";
  }
  return {
    selected: reason === "task_failure",
    reason,
    reasonLabel,
    success: outcome,
    errorCategory: String(result?.error_category || ""),
  };
}

async function discoverRolloutDirsFromInput(inputPath, _limit = Number.POSITIVE_INFINITY) {
  const resolved = resolveArtifactPath(inputPath);
  const stat = await fsp.stat(resolved).catch(() => null);
  if (!stat) throw new Error(`Trace path does not exist: ${inputPath}`);

  const start = stat.isDirectory() ? resolved : path.dirname(resolved);
  const ancestor = await nearestRolloutAncestor(start);
  if (ancestor) return [ancestor];

  const rollouts = [];
  const queue = [start];
  const seen = new Set();
  let scanned = 0;
  while (queue.length) {
    const dir = queue.shift();
    const key = path.resolve(dir);
    if (seen.has(key)) continue;
    seen.add(key);
    scanned += 1;

    if (await isRolloutDir(dir)) {
      rollouts.push(dir);
      continue;
    }

    let entries = [];
    try {
      entries = await fsp.readdir(dir, { withFileTypes: true });
    } catch {
      continue;
    }
    for (const entry of entries.sort((a, b) => a.name.localeCompare(b.name))) {
      if (!entry.isDirectory()) continue;
      if (entry.name === "node_modules" || entry.name === ".venv" || entry.name === ".git") continue;
      queue.push(path.join(dir, entry.name));
      if (queue.length > 5000) break;
    }
  }
  return rollouts;
}

async function inferRepairStageInputs(body) {
  await refreshTasks();
  const rawPaths = Array.isArray(body.tracePaths)
    ? body.tracePaths
    : String(body.tracePaths || body.jobPaths || body.evidencePaths || "").split(/\r?\n/);
  const tracePaths = rawPaths
    .map((item) => String(item || "").trim())
    .filter(Boolean);
  if (!tracePaths.length) throw new Error("At least one trace/job path is required");

  const warnings = [];
  const selectionLimit = Math.min(Math.max(Number(body.maxTraces || 5), 1), 30);
  const rolloutMap = new Map();
  for (const tracePath of tracePaths) {
    const discovered = await discoverRolloutDirsFromInput(tracePath);
    const found = [];
    for (const rolloutDir of discovered) {
      if (
        await exists(path.join(rolloutDir, "trajectory", "acp_trajectory.jsonl"))
        || await exists(path.join(rolloutDir, "agent", "acp_trajectory.jsonl"))
      ) {
        found.push(rolloutDir);
      }
    }
    if (!found.length) warnings.push(`No rollout result found under ${tracePath}`);
    for (const rolloutDir of found) rolloutMap.set(toPosixRelative(rolloutDir), rolloutDir);
  }

  const rollouts = [];
  for (const [relRollout, rolloutDir] of [...rolloutMap.entries()].sort(([a], [b]) => a.localeCompare(b))) {
    const result = await readJsonFileIfExists(path.join(rolloutDir, "result.json")) || {};
    const selection = classifyRolloutForRepair(result, rolloutDir);
    const taskCandidates = taskCandidatesFromResult(result, rolloutDir);
    const preferredTask = taskCandidates[0] || null;
    const inferredLibrary = preferredTask ? inferLibraryForTask(preferredTask, result) : null;
    rollouts.push({
      path: relRollout,
      taskName: result?.task_name || path.basename(rolloutDir).split("__")[0] || "",
      skillMode: result?.skill_mode || "",
      skillSource: result?.skill_source || "",
      observedSkillPaths: resultSkillPathCandidates(result),
      taskCandidates: taskCandidates.slice(0, 5).map((task) => ({
        key: task.key,
        name: task.name,
        rootLabel: task.rootLabel,
        taskDir: task.taskDir,
      })),
      inferredSkillsLibrary: inferredLibrary ? {
        id: inferredLibrary.library.id,
        label: inferredLibrary.library.label,
        skillsDir: inferredLibrary.library.skillsDir,
        confidence: inferredLibrary.confidence,
        reason: inferredLibrary.reason,
        observedPath: inferredLibrary.observedPath,
      } : null,
      selection,
    });
  }

  if (!rollouts.length) {
    return {
      ok: true,
      selected: null,
      rollouts,
      selectedRollouts: [],
      candidateCount: 0,
      eligibleCount: 0,
      selectionLimit,
      warnings: warnings.concat("未发现可用 rollout result.json。"),
    };
  }

  const eligibleRollouts = rollouts.filter((item) => item.selection?.selected);
  const selectedRollouts = eligibleRollouts.slice(0, selectionLimit);
  for (const rollout of rollouts) {
    rollout.selectedForRepair = selectedRollouts.includes(rollout);
  }
  if (!eligibleRollouts.length) {
    return {
      ok: true,
      selected: null,
      rollouts,
      selectedRollouts: [],
      candidateCount: rollouts.length,
      eligibleCount: 0,
      selectionLimit,
      warnings: warnings.concat("未发现明确的任务失败轨迹；成功、超时和环境/配置错误已排除。"),
    };
  }

  const taskKeys = [...new Set(selectedRollouts.map((item) => item.taskCandidates[0]?.key).filter(Boolean))];
  const libraryIds = [...new Set(selectedRollouts.map((item) => item.inferredSkillsLibrary?.id).filter(Boolean))];
  const libraryDirs = [...new Set(selectedRollouts.map((item) => item.inferredSkillsLibrary?.skillsDir).filter(Boolean))];
  if (taskKeys.length > 1) warnings.push(`轨迹包含多个 task 候选：${taskKeys.join(", ")}`);
  if (libraryIds.length > 1 || libraryDirs.length > 1) warnings.push(`轨迹包含多个 skills 库候选：${libraryDirs.join(", ")}`);

  const first = selectedRollouts[0];
  const selectedTask = taskKeys.length === 1 ? state.tasks.find((task) => task.key === taskKeys[0]) : null;
  const selectedLibrary = libraryIds.length <= 1 && libraryDirs.length <= 1 ? first.inferredSkillsLibrary : null;
  const minConfidence = Math.min(...selectedRollouts.map((item) => Number(item.inferredSkillsLibrary?.confidence || 0)));
  const selected = selectedTask && selectedLibrary ? {
    taskKey: selectedTask.key,
    taskName: selectedTask.name,
    taskDir: selectedTask.taskDir,
    sourceSkillsLibraryId: selectedLibrary.id,
    sourceSkillsLibraryLabel: selectedLibrary.label,
    sourceSkillsDir: selectedLibrary.skillsDir,
    confidence: Number.isFinite(minConfidence) ? minConfidence : selectedLibrary.confidence,
    reason: selectedLibrary.reason,
  } : null;

  if (!selectedTask) warnings.push("无法唯一确定 task，请手动选择。");
  if (!selectedLibrary) warnings.push("无法唯一确定 skills 库，请手动选择。");

  return {
    ok: true,
    selected,
    rollouts,
    selectedRollouts: selectedRollouts.map((item) => item.path),
    candidateCount: rollouts.length,
    eligibleCount: eligibleRollouts.length,
    selectionLimit,
    warnings,
  };
}

async function readDockerFrom(taskDir) {
  const dockerfile = path.join(taskDir, "environment", "Dockerfile");
  try {
    const text = await fsp.readFile(dockerfile, "utf8");
    return text.split(/\r?\n/).find((line) => line.trim().startsWith("FROM "))?.trim() || "";
  } catch {
    return "";
  }
}

async function scanTaskRoot(rootLabel, relRoot) {
  const absRoot = path.join(ROOT, relRoot);
  if (!(await exists(absRoot))) return [];
  const entries = await fsp.readdir(absRoot, { withFileTypes: true });
  const tasks = [];

  for (const entry of entries) {
    if (!entry.isDirectory()) continue;
    const taskDir = path.join(absRoot, entry.name);
    const taskMd = path.join(taskDir, "task.md");
    if (!(await exists(taskMd))) continue;

    const text = await fsp.readFile(taskMd, "utf8");
    const fm = parseFrontmatter(text);
    const skillsDir = path.join(taskDir, "environment", "skills");
    const skillInfo = await readSkillDirectoryInfo(skillsDir);
    const dockerFrom = await readDockerFrom(taskDir);

    const task = {
      key: `${rootLabel}:${entry.name}`,
      name: entry.name,
      rootLabel,
      taskDir: toPosixRelative(taskDir),
      skillsDir: toPosixRelative(skillsDir),
      hasSkills: skillInfo.skillCount > 0,
      skillCount: skillInfo.skillCount,
      skillResourceCount: skillInfo.skillResourceCount,
      pureSkillMdOnly: skillInfo.pureSkillMdOnly,
      difficulty: fm.metadata?.difficulty || "",
      category: fm.metadata?.category || "",
      subcategory: fm.metadata?.subcategory || "",
      agentTimeoutSec: fm.agent?.timeout_sec || null,
      verifierTimeoutSec: fm.verifier?.timeout_sec || null,
      dockerFrom,
      localImage: dockerFrom.includes("csl-skillsbench-"),
    };
    task.skillLibraries = availableLibrariesForTask(task);
    task.defaultSkillsLibraryId = defaultLibraryForTask(task)?.id || task.skillLibraries[0]?.id || "";
    tasks.push(task);
  }

  return tasks.sort((a, b) => a.name.localeCompare(b.name));
}

async function refreshTasks() {
  await scanSkillLibraries();
  const taskRoots = [
    ["tasks", "tasks"],
    ["tasks-extra", "tasks-extra"],
    ["localimages", "tmp/easy5-localimages"],
  ];
  const all = [];
  for (const [label, relRoot] of taskRoots) {
    all.push(...(await scanTaskRoot(label, relRoot)));
  }
  state.tasks = all;
  return all;
}

function publicRun(run) {
  return {
    id: run.id,
    groupId: run.groupId,
    runNo: run.runNo,
    taskName: run.taskName,
    taskKey: run.taskKey,
    status: run.status,
    pid: run.process?.pid || null,
    startedAt: run.startedAt,
    endedAt: run.endedAt,
    jobsDir: run.jobsDir,
    provider: run.provider,
    baseUrl: run.baseUrl,
    model: run.model,
    skillMode: run.skillMode,
    effectiveSkillMode: run.effectiveSkillMode,
    promptMode: run.promptMode,
    reasoningEffort: run.reasoningEffort,
    agentTimeoutSec: run.agentTimeoutSec,
    runTaskDir: run.runTaskDir,
    skillsLibraryId: run.skillsLibraryId,
    skillsLibraryLabel: run.skillsLibraryLabel,
    skillsDir: run.skillsDir,
    prebuiltImage: run.prebuiltImage,
    artifactDir: run.artifactDir,
    exitCode: run.exitCode,
    summary: run.summary,
    error: run.error,
    logLineCount: run.logs.length,
  };
}

function publicGroup(group) {
  const runs = state.runs.filter((run) => run.groupId === group.id);
  const completed = runs.filter((run) => ["passed", "failed", "error", "stopped"].includes(run.status)).length;
  const passed = runs.filter((run) => run.status === "passed").length;
  return {
    id: group.id,
    taskKey: group.task.key,
    taskName: group.task.name,
    rootLabel: group.task.rootLabel,
    status: group.status,
    repeats: group.repeats,
    parallel: group.parallel,
    agent: group.agent,
    provider: group.provider,
    baseUrl: group.baseUrl,
    model: group.model,
    skillMode: group.skillMode,
    effectiveSkillMode: group.effectiveSkillMode,
    promptMode: group.promptMode,
    reasoningEffort: group.reasoningEffort,
    agentTimeoutSec: group.agentTimeoutSec,
    runTaskDir: group.runTaskDir,
    skillsLibraryId: group.skillsLibraryId,
    skillsLibraryLabel: group.skillsLibraryLabel,
    skillsDir: group.skillsDir,
    prebuiltImage: group.prebuiltImage,
    jobsRoot: group.jobsRoot,
    createdAt: group.createdAt,
    startedAt: group.startedAt,
    endedAt: group.endedAt,
    completed,
    passed,
    total: runs.length,
  };
}

function publicRepairJob(job) {
  return {
    id: job.id,
    taskKey: job.task.key,
    taskName: job.task.name,
    status: job.status,
    pid: job.process?.pid || null,
    createdAt: job.createdAt,
    startedAt: job.startedAt,
    endedAt: job.endedAt,
    exitCode: job.exitCode,
    error: job.error,
    sourceSkillsLibraryId: job.sourceSkillsLibraryId,
    sourceSkillsLibraryLabel: job.sourceSkillsLibraryLabel,
    sourceSkillsDir: job.sourceSkillsDir,
    outputVariant: job.outputVariant,
    outputSkillsDir: job.outputSkillsDir,
    reportDir: job.reportDir,
    reportPath: job.reportPath,
    strongProvider: job.strongProvider,
    strongBaseUrl: job.strongBaseUrl,
    strongModel: job.strongModel,
    strongReasoningEffort: job.strongReasoningEffort,
    weakModel: job.weakModel,
    maxRollouts: job.maxRollouts,
    jobPaths: job.jobPaths,
    usedFallback: job.manifest?.usedFallback ?? null,
    appliedFiles: job.manifest?.appliedFiles || [],
    logLineCount: job.logs.length,
  };
}

function publicStageJob(job) {
  return {
    id: job.id,
    command: job.command,
    stage: job.stage || "",
    traceIndex: job.traceIndex ?? null,
    taskKey: job.task?.key || "",
    taskName: job.task?.name || "",
    status: job.status,
    pid: job.process?.pid || null,
    createdAt: job.createdAt,
    startedAt: job.startedAt,
    endedAt: job.endedAt,
    exitCode: job.exitCode,
    error: job.error,
    outputVariant: job.outputVariant || "",
    outputDir: job.outputDir,
    outputSkillsDir: job.outputSkillsDir || "",
    sourceSkillsDir: job.sourceSkillsDir || "",
    strongBaseUrl: job.strongBaseUrl || "",
    strongModel: job.strongModel || "",
    strongReasoningEffort: job.strongReasoningEffort || "minimal",
    separateReviewLlm: Boolean(job.separateReviewLlm),
    reviewBaseUrl: job.reviewBaseUrl || "",
    reviewModel: job.reviewModel || "",
    reviewReasoningEffort: job.reviewReasoningEffort || "minimal",
    stage7RepairMode: job.stage7RepairMode || "per_suggestion",
    stage7SkillPackageSize: job.stage7SkillPackageSize || 3,
    logLineCount: job.logs.length,
  };
}

function snapshot() {
  return {
    type: "snapshot",
    tasks: state.tasks,
    groups: state.groups.map(publicGroup),
    runs: state.runs.map(publicRun),
    repairJobs: state.repairJobs.map(publicRepairJob),
    stageJobs: state.stageJobs.map(publicStageJob),
    config: {
      cwd: ROOT,
      port: PORT,
      skillLibraries: state.skillLibraries,
      hasDeepseekApiKey: Boolean(process.env.DEEPSEEK_API_KEY || process.env.ANTHROPIC_AUTH_TOKEN || process.env.ANTHROPIC_API_KEY),
    },
  };
}

async function directTimestampSummary(parentDir) {
  try {
    const entries = await fsp.readdir(parentDir, { withFileTypes: true });
    for (const entry of entries) {
      if (!entry.isDirectory() || !/^\d{4}-\d{2}-\d{2}__/.test(entry.name)) continue;
      if (await exists(path.join(parentDir, entry.name, "summary.json"))) return true;
    }
  } catch {
    // Ignore unreadable job directories.
  }
  return false;
}

async function findSummaryFiles(dir, files = []) {
  let entries = [];
  try {
    entries = await fsp.readdir(dir, { withFileTypes: true });
  } catch {
    return files;
  }

  for (const entry of entries) {
    const fullPath = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      await findSummaryFiles(fullPath, files);
    } else if (entry.name === "summary.json") {
      files.push(fullPath);
    }
  }
  return files;
}

async function deriveTaskNameFromArtifact(artifactDir, fallback) {
  if (!artifactDir) return fallback;
  try {
    const entries = await fsp.readdir(artifactDir, { withFileTypes: true });
    const names = entries
      .filter((entry) => entry.isDirectory() && entry.name.includes("__"))
      .map((entry) => entry.name.split("__")[0]);
    return [...new Set(names)].join(", ") || fallback;
  } catch {
    return fallback;
  }
}

function readFirstRolloutResult(artifactDir) {
  if (!artifactDir) return null;
  try {
    const entries = fs.readdirSync(artifactDir, { withFileTypes: true });
    const rolloutDirs = entries
      .filter((entry) => entry.isDirectory() && entry.name.includes("__"))
      .map((entry) => path.join(artifactDir, entry.name))
      .sort();
    for (const rolloutDir of rolloutDirs) {
      const result = readJsonIfExists(path.join(rolloutDir, "result.json"));
      if (result) return result;
    }
  } catch {
    return null;
  }
  return null;
}

async function scanHistory() {
  const jobsRoot = path.join(ROOT, "jobs");
  if (!(await exists(jobsRoot))) {
    state.history = [];
    return [];
  }

  const summaries = await findSummaryFiles(jobsRoot);
  const rows = [];

  for (const summaryPath of summaries) {
    const parent = path.dirname(summaryPath);
    const parentName = path.basename(parent);
    const isTimestamp = /^\d{4}-\d{2}-\d{2}__/.test(parentName);

    if (!isTimestamp && (await directTimestampSummary(parent))) {
      continue;
    }

    const summary = readJsonIfExists(summaryPath);
    if (!summary) continue;

    const artifactDir = isTimestamp ? parent : findLatestTimestampDir(parent);
    const relSummary = toPosixRelative(summaryPath);
    const relArtifact = artifactDir ? toPosixRelative(artifactDir) : null;
    const fallbackName = isTimestamp ? path.basename(path.dirname(parent)) : path.basename(parent);
    const taskName = await deriveTaskNameFromArtifact(artifactDir, fallbackName);
    const stat = fs.statSync(summaryPath);
    const result = readFirstRolloutResult(artifactDir);
    const skillsLibrary = describeSkillsLibraryFromResult(result);

    rows.push({
      id: relSummary,
      type: "run",
      groupKey: null,
      taskName,
      jobName: summary.job_name || parentName,
      agent: summary.agent || "",
      model: summary.model || "",
      score: summary.score || "",
      passed: Number(summary.passed ?? summary.pass ?? 0),
      failed: Number(summary.failed ?? summary.fail ?? 0),
      errored: Number(summary.errored ?? summary.error ?? 0),
      total: Number(summary.total ?? 0),
      totalToolCalls: Number(summary.total_tool_calls ?? 0),
      totalTokens: Number(summary.total_tokens ?? 0),
      totalTimeSec: Number(summary.total_time_sec ?? summary.elapsed_sec ?? 0),
      skillsLibraryId: skillsLibrary.id,
      skillsLibraryLabel: skillsLibrary.label,
      skillsDir: skillsLibrary.skillsDir,
      summaryPath: relSummary,
      artifactDir: relArtifact,
      modifiedAt: stat.mtime.toISOString(),
      summary,
    });
  }

  const grouped = buildHistoryGroups(rows);
  const groupedKeys = new Set(grouped.map((group) => group.groupKey));
  const ungroupedRows = rows.filter((row) => !row.groupKey || !groupedKeys.has(row.groupKey));
  state.history = [...grouped, ...ungroupedRows]
    .sort((a, b) => new Date(b.modifiedAt) - new Date(a.modifiedAt))
    .slice(0, 700);
  return state.history;
}

function detectRepeatGroup(summaryPath) {
  const parts = summaryPath.split("/");
  const runIndex = parts.findIndex((part) => /^run-\d+$/.test(part));
  if (runIndex <= 0) return null;
  return parts.slice(0, runIndex).join("/");
}

function buildHistoryGroups(rows) {
  const groups = new Map();
  for (const row of rows) {
    const groupKey = detectRepeatGroup(row.summaryPath);
    if (!groupKey) continue;
    row.groupKey = groupKey;
    if (!groups.has(groupKey)) groups.set(groupKey, []);
    groups.get(groupKey).push(row);
  }

  const result = [];
  for (const [groupKey, items] of groups) {
    if (items.length < 2) continue;
    const sorted = [...items].sort((a, b) => String(a.summaryPath).localeCompare(String(b.summaryPath)));
    const passed = sorted.filter((item) => item.score && String(item.score).startsWith("100")).length;
    const total = sorted.length;
    const totalTimeSec = sorted.reduce((sum, item) => sum + (Number(item.totalTimeSec) || 0), 0);
    const totalTokens = sorted.reduce((sum, item) => sum + (Number(item.totalTokens) || 0), 0);
    const totalToolCalls = sorted.reduce((sum, item) => sum + (Number(item.totalToolCalls) || 0), 0);
    const latest = sorted.reduce((max, item) => new Date(item.modifiedAt) > new Date(max.modifiedAt) ? item : max, sorted[0]);
    const agents = [...new Set(sorted.map((item) => item.agent).filter(Boolean))];
    const models = [...new Set(sorted.map((item) => item.model).filter(Boolean))];
    const skillsLibraries = [...new Set(sorted.map((item) => item.skillsLibraryLabel).filter(Boolean))];
    const taskNames = [...new Set(sorted.map((item) => item.taskName).filter(Boolean))];
    const score = `${passed}/${total} (${(passed / total * 100).toFixed(1)}%)`;

    result.push({
      id: `group:${groupKey}`,
      type: "group",
      groupKey,
      taskName: taskNames.join(", ") || path.basename(groupKey),
      jobName: path.basename(groupKey),
      agent: agents.join(", "),
      model: models.join(", "),
      skillsLibraryLabel: skillsLibraries.join(", "),
      skillsDir: [...new Set(sorted.map((item) => item.skillsDir).filter(Boolean))].join(", "),
      score,
      passed,
      failed: sorted.filter((item) => !String(item.score).startsWith("100")).length,
      errored: sorted.reduce((sum, item) => sum + (Number(item.errored) || 0), 0),
      total,
      totalToolCalls,
      totalTokens,
      totalTimeSec,
      summaryPath: groupKey,
      artifactDir: groupKey,
      modifiedAt: latest.modifiedAt,
      runs: sorted.map((item, index) => ({
        index: index + 1,
        id: item.id,
        type: "run",
        taskName: item.taskName,
        jobName: item.jobName,
        agent: item.agent,
        model: item.model,
        skillsLibraryLabel: item.skillsLibraryLabel,
        skillsDir: item.skillsDir,
        score: item.score,
        passed: String(item.score).startsWith("100"),
        failed: item.failed,
        errored: item.errored,
        total: item.total,
        summaryPath: item.summaryPath,
        artifactDir: item.artifactDir,
        totalTimeSec: item.totalTimeSec,
        totalTokens: item.totalTokens,
        totalToolCalls: item.totalToolCalls,
        modifiedAt: item.modifiedAt,
      })),
      summary: {
        type: "repeat-group",
        groupKey,
        taskName: taskNames.join(", ") || path.basename(groupKey),
        passed,
        total,
        successRate: passed / total,
        runs: sorted.map((item) => ({
          score: item.score,
          skillsLibraryLabel: item.skillsLibraryLabel,
          skillsDir: item.skillsDir,
          summaryPath: item.summaryPath,
          artifactDir: item.artifactDir,
          totalTimeSec: item.totalTimeSec,
          totalTokens: item.totalTokens,
          totalToolCalls: item.totalToolCalls,
        })),
      },
    });
  }
  return result;
}

function emit(type, payload = {}) {
  const event = JSON.stringify({ type, ...payload, at: nowIso() });
  for (const res of state.clients) {
    res.write(`data: ${event}\n\n`);
  }
}

function appendLog(run, chunk) {
  const lines = String(chunk).replace(/\r/g, "").split("\n");
  for (const line of lines) {
    if (!line) continue;
    const record = { at: nowIso(), text: line };
    run.logs.push(record);
    if (run.logs.length > MAX_LOG_LINES) run.logs.splice(0, run.logs.length - MAX_LOG_LINES);
    emit("run_log", { runId: run.id, line: record });
  }
}

function appendRepairLog(job, chunk) {
  const lines = String(chunk).replace(/\r/g, "").split("\n");
  for (const line of lines) {
    if (!line) continue;
    const record = { at: nowIso(), text: line };
    job.logs.push(record);
    if (job.logs.length > MAX_LOG_LINES) job.logs.splice(0, job.logs.length - MAX_LOG_LINES);
    emit("repair_log", { jobId: job.id, line: record });
  }
}

function appendStageLog(job, chunk) {
  const lines = String(chunk).replace(/\r/g, "").split("\n");
  for (const line of lines) {
    if (!line) continue;
    const record = { at: nowIso(), text: line };
    job.logs.push(record);
    if (job.logs.length > MAX_LOG_LINES) job.logs.splice(0, job.logs.length - MAX_LOG_LINES);
    emit("repair_stage_log", { jobId: job.id, line: record });
  }
}

function readJsonIfExists(filePath) {
  try {
    return JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch {
    return null;
  }
}

function runCurrentUserDpapi(script, inputBase64) {
  try {
    return execFileSync(
      "powershell.exe",
      ["-NoProfile", "-NonInteractive", "-Command", script],
      {
        // Node 与 Windows PowerShell 对 stdin/stdout 的默认字符编码不同。
        // 桥接层只传 ASCII Base64，实际设置始终以原始 UTF-8 字节参与 DPAPI。
        input: inputBase64,
        encoding: "utf8",
        windowsHide: true,
        timeout: 15_000,
        maxBuffer: 2_000_000,
      },
    ).trim();
  } catch (error) {
    const stderr = String(error?.stderr || "").trim().slice(0, 1000);
    throw new Error(`Windows DPAPI operation failed: ${error.message}${stderr ? `; ${stderr}` : ""}`);
  }
}

function protectPresetSettings(settings) {
  const script = [
    "Add-Type -AssemblyName System.Security",
    "$inputBase64=[Console]::In.ReadToEnd().Trim()",
    "$bytes=[Convert]::FromBase64String($inputBase64)",
    "$protected=[Security.Cryptography.ProtectedData]::Protect($bytes,$null,[Security.Cryptography.DataProtectionScope]::CurrentUser)",
    "[Convert]::ToBase64String($protected)",
  ].join("; ");
  const inputBase64 = Buffer.from(JSON.stringify(settings), "utf8").toString("base64");
  return runCurrentUserDpapi(script, inputBase64);
}

function unprotectPresetSettings(protectedSettings) {
  const script = [
    "Add-Type -AssemblyName System.Security",
    "$inputBase64=[Console]::In.ReadToEnd().Trim()",
    "$bytes=[Convert]::FromBase64String($inputBase64)",
    "$plain=[Security.Cryptography.ProtectedData]::Unprotect($bytes,$null,[Security.Cryptography.DataProtectionScope]::CurrentUser)",
    "[Convert]::ToBase64String($plain)",
  ].join("; ");
  const outputBase64 = runCurrentUserDpapi(script, String(protectedSettings || ""));
  const text = Buffer.from(outputBase64, "base64").toString("utf8");
  return JSON.parse(text);
}

function repairStagePresetStore() {
  const value = readJsonIfExists(REPAIR_STAGE_PRESET_PATH);
  return value && value.version === 1 && Array.isArray(value.presets)
    ? value
    : { version: 1, presets: [] };
}

async function writeRepairStagePresetStore(store) {
  await fsp.mkdir(path.dirname(REPAIR_STAGE_PRESET_PATH), { recursive: true });
  const temporary = `${REPAIR_STAGE_PRESET_PATH}.${makeId("tmp")}`;
  await fsp.writeFile(temporary, `${JSON.stringify(store, null, 2)}\n`, "utf8");
  try {
    await fsp.rm(REPAIR_STAGE_PRESET_PATH, { force: true });
    await fsp.rename(temporary, REPAIR_STAGE_PRESET_PATH);
  } finally {
    await fsp.rm(temporary, { force: true }).catch(() => {});
  }
}

function boundedNumber(value, fallback, min, max) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.min(Math.max(number, min), max) : fallback;
}

function normalizeRepairStagePresetSettings(raw = {}) {
  const tracePaths = Array.isArray(raw.tracePaths)
    ? raw.tracePaths
    : String(raw.tracePaths || "").split(/\r?\n/);
  return {
    taskKey: String(raw.taskKey || "").slice(0, 300),
    sourceSkillsLibraryId: String(raw.sourceSkillsLibraryId || "").slice(0, 500),
    outputVariant: String(raw.outputVariant || "").slice(0, 200),
    maxTraces: boundedNumber(raw.maxTraces, 5, 1, 30),
    tracePaths: tracePaths.map((item) => String(item || "").trim()).filter(Boolean).slice(0, 100),
    force: Boolean(raw.force),
    strongModel: String(raw.strongModel || "").slice(0, 300),
    strongBaseUrl: String(raw.strongBaseUrl || "").slice(0, 2000),
    strongApiKey: String(raw.strongApiKey || "").slice(0, 20_000),
    traceAnalysisWorkers: boundedNumber(raw.traceAnalysisWorkers, 5, 1, 20),
    maxPromptChars: boundedNumber(raw.maxPromptChars, 220_000, 20_000, 500_000),
    strongTimeout: boundedNumber(raw.strongTimeout, 1800, 30, 3600),
    stage7MaxOperations: boundedNumber(raw.stage7MaxOperations, 30, 1, 1000),
    stage7RepairMode: raw.stage7RepairMode === "skill_package" ? "skill_package" : "per_suggestion",
    stage7SkillPackageSize: boundedNumber(raw.stage7SkillPackageSize, 3, 1, 100),
    addSkillMergeThreshold: boundedNumber(raw.addSkillMergeThreshold, 0, 0, 1000),
    addSkillTargetCount: boundedNumber(raw.addSkillTargetCount, 0, 0, 1000),
    maxNewSkills: boundedNumber(raw.maxNewSkills, 2, 0, 1000),
    skillWordLimit: boundedNumber(raw.skillWordLimit, 1200, 100, 20_000),
    separateReviewLlm: Boolean(raw.separateReviewLlm),
    reviewModel: String(raw.reviewModel || "").slice(0, 300),
    reviewBaseUrl: String(raw.reviewBaseUrl || "").slice(0, 2000),
    reviewApiKey: String(raw.reviewApiKey || "").slice(0, 20_000),
  };
}

function repairStagePresetSummaries() {
  return repairStagePresetStore().presets
    .map(({ id, name, createdAt, updatedAt }) => ({ id, name, createdAt, updatedAt }))
    .sort((a, b) => String(b.updatedAt).localeCompare(String(a.updatedAt)));
}

async function saveRepairStagePreset(body) {
  const name = String(body.name || "").trim();
  if (!name) throw new Error("Preset name is required");
  if (name.length > 80) throw new Error("Preset name must be at most 80 characters");
  const settings = normalizeRepairStagePresetSettings(body.settings || {});
  const protectedSettings = protectPresetSettings(settings);
  const store = repairStagePresetStore();
  let preset = store.presets.find((item) => item.id === body.id);
  if (!preset) preset = store.presets.find((item) => String(item.name).toLowerCase() === name.toLowerCase());
  const timestamp = nowIso();
  if (preset) {
    preset.name = name;
    preset.updatedAt = timestamp;
    preset.protectedSettings = protectedSettings;
  } else {
    if (store.presets.length >= MAX_REPAIR_STAGE_PRESETS) throw new Error(`At most ${MAX_REPAIR_STAGE_PRESETS} presets are allowed`);
    preset = { id: makeId("stage-preset"), name, createdAt: timestamp, updatedAt: timestamp, protectedSettings };
    store.presets.push(preset);
  }
  await writeRepairStagePresetStore(store);
  return { id: preset.id, name: preset.name, createdAt: preset.createdAt, updatedAt: preset.updatedAt };
}

function loadRepairStagePreset(id) {
  const preset = repairStagePresetStore().presets.find((item) => item.id === id);
  if (!preset) throw new Error("Preset not found");
  return {
    id: preset.id,
    name: preset.name,
    createdAt: preset.createdAt,
    updatedAt: preset.updatedAt,
    settings: normalizeRepairStagePresetSettings(unprotectPresetSettings(preset.protectedSettings)),
  };
}

async function deleteRepairStagePreset(id) {
  const store = repairStagePresetStore();
  const before = store.presets.length;
  store.presets = store.presets.filter((item) => item.id !== id);
  if (store.presets.length === before) throw new Error("Preset not found");
  await writeRepairStagePresetStore(store);
}

async function readJsonFileIfExists(filePath) {
  try {
    return JSON.parse(await fsp.readFile(filePath, "utf8"));
  } catch {
    return null;
  }
}

function resolveArtifactPath(relPath) {
  const resolved = path.resolve(ROOT, relPath || "");
  const relative = path.relative(ROOT, resolved);
  if (relative.startsWith("..") || path.isAbsolute(relative)) {
    throw new Error("Path escapes workspace root");
  }
  return resolved;
}

async function fileInfo(filePath, baseDir) {
  try {
    const stat = await fsp.stat(filePath);
    if (!stat.isFile()) return null;
    return {
      path: toPosixRelative(filePath),
      name: path.relative(baseDir, filePath).replace(/\\/g, "/"),
      size: stat.size,
      modifiedAt: stat.mtime.toISOString(),
    };
  } catch {
    return null;
  }
}

async function listArtifactFiles(dir, baseDir = dir, depth = 0, out = []) {
  if (depth > 4 || out.length >= 240) return out;
  let entries = [];
  try {
    entries = await fsp.readdir(dir, { withFileTypes: true });
  } catch {
    return out;
  }

  for (const entry of entries) {
    if (out.length >= 240) break;
    const fullPath = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      await listArtifactFiles(fullPath, baseDir, depth + 1, out);
    } else if (entry.isFile()) {
      const info = await fileInfo(fullPath, baseDir);
      if (info) out.push(info);
    }
  }
  return out;
}

async function readTextPreview(filePath, maxBytes = 320_000) {
  try {
    const stat = await fsp.stat(filePath);
    if (!stat.isFile()) return null;
    const start = Math.max(0, stat.size - maxBytes);
    const handle = await fsp.open(filePath, "r");
    try {
      const buffer = Buffer.alloc(stat.size - start);
      await handle.read(buffer, 0, buffer.length, start);
      let text = buffer.toString("utf8");
      if (start > 0) {
        const firstNewline = text.indexOf("\n");
        if (firstNewline >= 0) text = text.slice(firstNewline + 1);
      }
      return {
        path: toPosixRelative(filePath),
        size: stat.size,
        truncated: start > 0,
        text,
      };
    } finally {
      await handle.close();
    }
  } catch {
    return null;
  }
}

async function readFileStart(filePath, maxBytes) {
  const stat = await fsp.stat(filePath);
  if (!stat.isFile()) throw new Error("Trajectory path is not a file");
  const bytesToRead = Math.min(stat.size, maxBytes);
  const handle = await fsp.open(filePath, "r");
  try {
    const buffer = Buffer.alloc(bytesToRead);
    const { bytesRead } = await handle.read(buffer, 0, bytesToRead, 0);
    return {
      text: buffer.subarray(0, bytesRead).toString("utf8"),
      size: stat.size,
      modifiedAt: stat.mtime.toISOString(),
      truncated: stat.size > maxBytes,
    };
  } finally {
    await handle.close();
  }
}

function truncateText(value, maxLength = 16_000) {
  if (typeof value !== "string") return value;
  if (value.length <= maxLength) return value;
  return `${value.slice(0, maxLength)}\n...[truncated ${value.length - maxLength} chars]`;
}

function compactValue(value, depth = 0) {
  if (typeof value === "string") return truncateText(value);
  if (value === null || typeof value !== "object") return value;
  if (depth >= 5) return Array.isArray(value) ? `[Array(${value.length})]` : "[Object]";
  if (Array.isArray(value)) {
    const items = value.slice(0, 60).map((item) => compactValue(item, depth + 1));
    if (value.length > items.length) items.push(`...[${value.length - items.length} more items]`);
    return items;
  }

  const out = {};
  const entries = Object.entries(value).slice(0, 80);
  for (const [key, item] of entries) out[key] = compactValue(item, depth + 1);
  const extra = Object.keys(value).length - entries.length;
  if (extra > 0) out.__truncatedKeys = extra;
  return out;
}

function firstText(value) {
  if (typeof value === "string") return value;
  if (!value || typeof value !== "object") return "";
  if (Array.isArray(value)) {
    for (const item of value) {
      const found = firstText(item);
      if (found) return found;
    }
    return "";
  }
  for (const key of ["text", "content", "message", "output", "title"]) {
    const found = firstText(value[key]);
    if (found) return found;
  }
  return "";
}

function collectTrajectoryText(value, options = {}) {
  const maxChars = Number(options.maxChars || 16000);
  const parts = [];
  const seen = new Set();
  const preferredKeys = [
    "text",
    "message",
    "content",
    "input",
    "output",
    "error",
    "reason",
    "title",
    "name",
    "status",
    "kind",
    "type",
  ];

  function add(text) {
    const value = String(text || "");
    if (!value) return;
    parts.push(value);
  }

  function visit(item, depth = 0) {
    if (parts.join("\n").length >= maxChars) return;
    if (item === null || item === undefined || depth > 10) return;
    if (typeof item === "string") {
      add(item);
      return;
    }
    if (typeof item !== "object") {
      if (typeof item === "number" || typeof item === "boolean") add(String(item));
      return;
    }
    if (seen.has(item)) return;
    seen.add(item);
    if (Array.isArray(item)) {
      for (const child of item) visit(child, depth + 1);
      return;
    }

    for (const key of preferredKeys) {
      if (Object.prototype.hasOwnProperty.call(item, key)) visit(item[key], depth + 1);
    }
    for (const [key, child] of Object.entries(item)) {
      if (!preferredKeys.includes(key)) visit(child, depth + 1);
    }
  }

  visit(value);
  const joined = parts
    .map((part) => String(part).trim())
    .filter(Boolean)
    .filter((part, index, all) => all.indexOf(part) === index)
    .join("\n");
  return joined.length > maxChars ? joined.slice(0, maxChars) : joined;
}

function previewLine(value, maxLength = 220) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  if (!text) return "";
  return text.length > maxLength ? `${text.slice(0, maxLength)}...` : text;
}

function normalizeTrajectoryEvent(record, index, line) {
  const extractedText = typeof record.text === "string"
    ? record.text
    : collectTrajectoryText(record.content !== undefined ? record.content : record);
  const text = extractedText || firstText(record.content);
  return {
    index,
    line,
    type: record.type || "event",
    kind: record.kind || "",
    status: record.status || "",
    title: record.title || record.name || "",
    toolCallId: record.tool_call_id || record.toolCallId || "",
    preview: previewLine(record.title || text || JSON.stringify(compactValue(record))),
    text: text
      ? { value: truncateText(text), length: text.length, truncated: text.length > 16_000, source: typeof record.text === "string" ? "text" : "extracted" }
      : null,
    content: record.content === undefined ? null : compactValue(record.content),
    payload: compactValue(record),
  };
}

async function findRolloutDirs(artifactDir) {
  const dirs = [];
  let entries = [];
  try {
    entries = await fsp.readdir(artifactDir, { withFileTypes: true });
  } catch {
    return dirs;
  }
  for (const entry of entries) {
    if (!entry.isDirectory()) continue;
    const candidate = path.join(artifactDir, entry.name);
    if (
      await exists(path.join(candidate, "result.json")) ||
      await exists(path.join(candidate, "trajectory", "acp_trajectory.jsonl")) ||
      await exists(path.join(candidate, "agent", "acp_trajectory.jsonl"))
    ) {
      dirs.push(candidate);
    }
  }
  return dirs.sort((a, b) => path.basename(a).localeCompare(path.basename(b)));
}

async function resolveTrajectoryPathForRollout(rolloutDir) {
  const candidates = [
    path.join(rolloutDir, "trajectory", "acp_trajectory.jsonl"),
    path.join(rolloutDir, "agent", "acp_trajectory.jsonl"),
  ];
  for (const candidate of candidates) {
    if (await exists(candidate)) return candidate;
  }
  return candidates[0];
}

async function readRolloutDetail(rolloutDir) {
  const trajectoryPath = await resolveTrajectoryPathForRollout(rolloutDir);
  const agentDir = path.join(rolloutDir, "agent");
  const verifierDir = path.join(rolloutDir, "verifier");
  const result = await readJsonFileIfExists(path.join(rolloutDir, "result.json"));
  return {
    name: path.basename(rolloutDir),
    path: toPosixRelative(rolloutDir),
    result,
    skillsLibrary: describeSkillsLibraryFromResult(result),
    config: await readJsonFileIfExists(path.join(rolloutDir, "config.json")),
    prompts: await readJsonFileIfExists(path.join(rolloutDir, "prompts.json")),
    timing: await readJsonFileIfExists(path.join(rolloutDir, "timing.json")),
    trajectory: await fileInfo(trajectoryPath, rolloutDir),
    agentFiles: await listArtifactFiles(agentDir, agentDir),
    verifierFiles: await listArtifactFiles(verifierDir, verifierDir),
  };
}

async function resolveRolloutDir(artifactDir, rolloutName) {
  const rollouts = await findRolloutDirs(artifactDir);
  if (!rollouts.length) throw new Error("No rollout directory found for this job");

  if (!rolloutName) return rollouts[0];

  const normalizedName = String(rolloutName).replace(/\\/g, "/");
  const found = rollouts.find((rolloutDir) => {
    const basename = path.basename(rolloutDir);
    const relativeToArtifact = path.relative(artifactDir, rolloutDir).replace(/\\/g, "/");
    const relativeToRoot = toPosixRelative(rolloutDir);
    return basename === normalizedName || relativeToArtifact === normalizedName || relativeToRoot === normalizedName;
  });
  if (!found) throw new Error(`Rollout not found: ${rolloutName}`);
  return found;
}

async function readTrajectory(relArtifactDir, rolloutName) {
  const artifactDir = resolveArtifactPath(relArtifactDir);
  const artifactStat = await fsp.stat(artifactDir);
  if (!artifactStat.isDirectory()) throw new Error("Artifact path is not a directory");

  const rolloutDir = await resolveRolloutDir(artifactDir, rolloutName);
  const trajectoryPath = await resolveTrajectoryPathForRollout(rolloutDir);
  const raw = await readFileStart(trajectoryPath, MAX_TRAJECTORY_BYTES);
  let lines = raw.text.split(/\r?\n/);
  if (raw.truncated && lines.length) lines = lines.slice(0, -1);
  lines = lines.filter((line) => line.trim());

  const events = [];
  let parseErrors = 0;
  for (let i = 0; i < lines.length && events.length < MAX_TRAJECTORY_EVENTS; i += 1) {
    try {
      const record = JSON.parse(lines[i]);
      events.push(normalizeTrajectoryEvent(record, events.length + 1, i + 1));
    } catch {
      parseErrors += 1;
    }
  }

  const counts = events.reduce((acc, event) => {
    acc[event.type] = (acc[event.type] || 0) + 1;
    return acc;
  }, {});

  return {
    artifactDir: toPosixRelative(artifactDir),
    rollout: path.basename(rolloutDir),
    rolloutPath: toPosixRelative(rolloutDir),
    trajectoryPath: toPosixRelative(trajectoryPath),
    size: raw.size,
    modifiedAt: raw.modifiedAt,
    truncated: raw.truncated || lines.length > events.length,
    maxBytes: MAX_TRAJECTORY_BYTES,
    maxEvents: MAX_TRAJECTORY_EVENTS,
    parseErrors,
    totalEvents: events.length,
    counts,
    result: await readJsonFileIfExists(path.join(rolloutDir, "result.json")),
    events,
  };
}

function pythonExecutable() {
  const configured = String(process.env.SKILLSBENCH_PYTHON || "").trim();
  if (configured && fs.existsSync(configured)) return configured;
  const windowsPython = path.join(ROOT, ".venv", "Scripts", "python.exe");
  if (fs.existsSync(windowsPython)) return windowsPython;
  const posixPython = path.join(ROOT, ".venv", "bin", "python");
  if (fs.existsSync(posixPython)) return posixPython;
  // Ubuntu 默认只提供 python3；Windows 仍优先使用 python/py。
  if (process.platform !== "win32") return "python3";
  return "python";
}

async function inferTaskPathFromRollout(rolloutDir) {
  const config = await readJsonFileIfExists(path.join(rolloutDir, "config.json"));
  const result = await readJsonFileIfExists(path.join(rolloutDir, "result.json"));
  return config?.task_path || config?.source?.path || result?.source?.path || null;
}

async function resolveAnalysisInputs(body = {}) {
  let rolloutDir = null;
  let artifactDir = null;

  if (body.artifactDir) {
    artifactDir = resolveArtifactPath(body.artifactDir);
    const stat = await fsp.stat(artifactDir);
    if (!stat.isDirectory()) throw new Error("artifactDir is not a directory");
    rolloutDir = await resolveRolloutDir(artifactDir, body.rollout);
  }

  let trajectoryPath = body.trajectoryPath
    ? resolveArtifactPath(body.trajectoryPath)
    : rolloutDir
      ? await resolveTrajectoryPathForRollout(rolloutDir)
      : null;
  if (!trajectoryPath) throw new Error("trajectoryPath or artifactDir is required");
  const trajectoryStat = await fsp.stat(trajectoryPath);
  if (!trajectoryStat.isFile()) throw new Error("trajectoryPath is not a file");

  rolloutDir ||= path.basename(path.dirname(trajectoryPath)) === "trajectory"
    ? path.dirname(path.dirname(trajectoryPath))
    : path.dirname(trajectoryPath);

  let resultPath = body.resultPath ? resolveArtifactPath(body.resultPath) : path.join(rolloutDir, "result.json");
  if (!(await exists(resultPath))) resultPath = null;

  let taskPath = body.taskPath || await inferTaskPathFromRollout(rolloutDir);
  let taskDir = taskPath ? resolveArtifactPath(taskPath) : null;
  if (taskDir && !(await exists(path.join(taskDir, "task.md")))) taskDir = null;

  const skillsDir = taskDir ? path.join(taskDir, "environment", "skills") : null;
  return {
    taskPath: taskDir ? toPosixRelative(taskDir) : "",
    trajectoryPath: toPosixRelative(trajectoryPath),
    resultPath: resultPath ? toPosixRelative(resultPath) : "",
    skillsDir: skillsDir && await exists(skillsDir) ? toPosixRelative(skillsDir) : "",
    rolloutPath: rolloutDir ? toPosixRelative(rolloutDir) : "",
    artifactDir: artifactDir ? toPosixRelative(artifactDir) : "",
  };
}

function runAnalysisScript(payload) {
  return new Promise((resolve, reject) => {
    const child = spawn(pythonExecutable(), ["scripts/trajectory_skill_analyzer.py", "--json-input"], {
      cwd: ROOT,
      windowsHide: true,
      stdio: ["pipe", "pipe", "pipe"],
    });

    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString("utf8");
      if (stdout.length > 12_000_000) {
        child.kill();
        reject(new Error("Analysis output exceeded limit"));
      }
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk.toString("utf8");
    });
    child.on("error", reject);
    child.on("close", (code) => {
      if (code !== 0) {
        reject(new Error(stderr || stdout || `Analyzer exited with code ${code}`));
        return;
      }
      try {
        resolve(JSON.parse(stdout));
      } catch (error) {
        reject(new Error(`Failed to parse analyzer output: ${error.message}; ${stderr}`));
      }
    });
    child.stdin.end(JSON.stringify(payload));
  });
}

async function readJobDetail(relArtifactDir) {
  const artifactDir = resolveArtifactPath(relArtifactDir);
  const stat = await fsp.stat(artifactDir);
  if (!stat.isDirectory()) throw new Error("Artifact path is not a directory");

  const rollouts = await findRolloutDirs(artifactDir);
  return {
    artifactDir: toPosixRelative(artifactDir),
    summary: await readJsonFileIfExists(path.join(artifactDir, "summary.json")),
    files: await listArtifactFiles(artifactDir),
    rollouts: await Promise.all(rollouts.map(readRolloutDetail)),
  };
}

function findLatestTimestampDir(parentDir) {
  try {
    const entries = fs.readdirSync(parentDir, { withFileTypes: true });
    return entries
      .filter((entry) => entry.isDirectory() && /^\d{4}-\d{2}-\d{2}__/.test(entry.name))
      .map((entry) => path.join(parentDir, entry.name))
      .sort()
      .at(-1) || null;
  } catch {
    return null;
  }
}

function loadRunSummary(run) {
  const summaryPath = path.join(ROOT, run.jobsDir, "summary.json");
  const summary = readJsonIfExists(summaryPath);
  const artifactDir = findLatestTimestampDir(path.join(ROOT, run.jobsDir));
  run.artifactDir = artifactDir ? toPosixRelative(artifactDir) : null;
  run.summary = summary;

  if (summary?.score && String(summary.score).startsWith("100")) {
    run.status = "passed";
  } else if (summary) {
    run.status = "failed";
  } else if (run.status !== "stopped") {
    run.status = run.exitCode === 0 ? "completed" : "error";
  }
}

function defaultBaseUrlForProvider(provider, agent) {
  if (provider === "anthropic") return "https://api.anthropic.com";
  if (provider === "openai") return "https://api.openai.com/v1";
  if (provider === "custom") return "";
  // Claude Code uses the Anthropic Messages surface. DeepSeek exposes that
  // compatibility surface under /anthropic; OpenHands/OpenCode can normalize
  // it back to the provider root below when required by their adapter.
  return agent === "claude-agent-acp" ? "https://api.deepseek.com/anthropic" : "https://api.deepseek.com";
}

function normalizeBaseUrlForAgent(agent, baseUrl, provider = "deepseek") {
  const value = String(baseUrl || defaultBaseUrlForProvider(provider, agent)).trim();
  if (agent === "openhands") return value.replace(/\/anthropic\/?$/i, "");
  return value;
}

function modelForAgentEnv(agent, model) {
  const value = String(model || "").trim();
  if (agent === "openhands" && value && !value.includes("/") && value.toLowerCase().startsWith("deepseek")) {
    return `deepseek/${value}`;
  }
  return value;
}

function isDeepSeekProvider(model, baseUrl) {
  const modelValue = String(model || "").trim().toLowerCase();
  const baseValue = String(baseUrl || "").trim().toLowerCase();
  return modelValue.startsWith("deepseek") || baseValue.includes("deepseek");
}

function modelForBenchArg(group) {
  const value = String(group.model || "").trim();
  if (group.agent === "openhands" && value && !value.includes("/") && value.toLowerCase().startsWith("deepseek")) {
    return `deepseek/${value}`;
  }
  return value;
}

function buildNonSecretAgentEnv(group) {
  const baseUrl = normalizeBaseUrlForAgent(group.agent, group.baseUrl, group.provider);
  const model = modelForAgentEnv(group.agent, group.model);
  const env = {
    BENCHFLOW_PROVIDER_BASE_URL: baseUrl,
    BENCHFLOW_PROVIDER_MODEL: group.model,
    BENCHFLOW_PROVIDER_TYPE: group.provider,
    SKILLSBENCH_PROVIDER: group.provider,
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: "1",
  };

  if (group.agent === "openhands") {
    env.LLM_BASE_URL = baseUrl;
    env.LLM_MODEL = model;
  } else if (group.agent === "claude-agent-acp") {
    env.ANTHROPIC_BASE_URL = baseUrl;
    env.ANTHROPIC_MODEL = model;
  }

  return Object.entries(env).filter(([, value]) => value);
}

function mergeNoProxy(value) {
  const required = ["localhost", "127.0.0.1", "::1", "host.docker.internal"];
  const entries = String(value || "")
    .split(",")
    .map((entry) => entry.trim())
    .filter(Boolean);
  const lower = new Set(entries.map((entry) => entry.toLowerCase()));
  for (const entry of required) {
    if (!lower.has(entry.toLowerCase())) entries.push(entry);
  }
  return entries.join(",");
}

function remapLoopbackProxy(value) {
  if (!value) return value;
  try {
    const url = new URL(value);
    if (["127.0.0.1", "localhost", "[::1]", "::1"].includes(url.hostname)) {
      url.hostname = "host.docker.internal";
      return url.toString();
    }
  } catch {
    // Keep non-URL proxy values unchanged.
  }
  return value;
}

function normalizeRunProxyEnv(env) {
  const policy = String(env.SKILLSBENCH_RUNNER_PROXY_MODE || "none").trim().toLowerCase();
  const proxyVars = ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"];

  env.NO_PROXY = mergeNoProxy(env.NO_PROXY || env.no_proxy);
  env.no_proxy = env.NO_PROXY;

  if (["none", "off", "disabled"].includes(policy)) {
    for (const key of proxyVars) delete env[key];
    return env;
  }

  if (["host", "local", "unchanged"].includes(policy)) {
    return env;
  }

  if (policy === "docker-host" || policy === "auto") {
    for (const key of proxyVars) {
      if (env[key]) env[key] = remapLoopbackProxy(env[key]);
    }
  }

  return env;
}

let dockerPreflightCache = { checkedAt: 0, ok: false, message: "" };

function compactCommandFailure(error, stdout, stderr) {
  return `${error?.message || ""}\n${stderr || ""}\n${stdout || ""}`
    .replace(/\r/g, "")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .slice(0, 8)
    .join(" | ")
    .slice(0, 1600);
}

function checkDockerAvailable() {
  const now = Date.now();
  if (now - dockerPreflightCache.checkedAt < 30_000 && dockerPreflightCache.message) {
    return dockerPreflightCache.ok
      ? Promise.resolve()
      : Promise.reject(new Error(dockerPreflightCache.message));
  }

  const dockerConfigDir = path.join(ROOT, ".docker-config");
  fs.mkdirSync(dockerConfigDir, { recursive: true });

  return new Promise((resolve, reject) => {
    execFile(
      "docker",
      ["info", "--format", "{{json .ServerVersion}}"],
      {
        cwd: ROOT,
        env: { ...process.env, DOCKER_CONFIG: dockerConfigDir },
        timeout: 15_000,
        windowsHide: true,
      },
      (error, stdout, stderr) => {
        if (!error) {
          const serverVersion = String(stdout || "").trim().replace(/^"|"$/g, "");
          dockerPreflightCache = {
            checkedAt: Date.now(),
            ok: true,
            message: `Docker server ${serverVersion || "available"}`,
          };
          resolve();
          return;
        }
        const detail = compactCommandFailure(error, stdout, stderr);
        const message = `Docker API is not accessible, so docker-sandbox tasks cannot start. ${detail}`;
        dockerPreflightCache = { checkedAt: Date.now(), ok: false, message };
        reject(new Error(message));
      },
    );
  });
}

function buildRunEnv(group) {
  const env = { ...process.env };
  const apiKey = group.apiKey || process.env.DEEPSEEK_API_KEY || process.env.ANTHROPIC_AUTH_TOKEN || process.env.ANTHROPIC_API_KEY || "";
  const baseUrl = normalizeBaseUrlForAgent(group.agent, group.baseUrl, group.provider);
  const deepSeekProvider = group.provider === "deepseek" || isDeepSeekProvider(group.model, baseUrl);
  const uvCacheDir = path.join(ROOT, ".uv-cache");
  const dockerConfigDir = path.join(ROOT, ".docker-config");
  const buildxConfigDir = path.join(ROOT, ".docker-buildx");
  fs.mkdirSync(uvCacheDir, { recursive: true });
  fs.mkdirSync(path.join(dockerConfigDir, "buildx", "instances"), { recursive: true });
  fs.mkdirSync(path.join(buildxConfigDir, "instances"), { recursive: true });

  env.PYTHONUTF8 = "1";
  env.UV_CACHE_DIR = uvCacheDir;
  env.UV_LINK_MODE = env.UV_LINK_MODE || "copy";
  env.DOCKER_CONFIG = dockerConfigDir;
  env.BUILDX_CONFIG = buildxConfigDir;
  env.ANTHROPIC_BASE_URL = baseUrl;
  // LiteLLM 会按 gpt-* 模型名选择 OpenAI provider；若缺少该变量，它会错误地
  // 回退到 api.openai.com，而不是用户指定的 OpenAI 兼容网关。
  env.OPENAI_BASE_URL = baseUrl;
  env.BENCHFLOW_PROVIDER_BASE_URL = baseUrl;
  env.BENCHFLOW_PROVIDER_MODEL = group.model;
  env.BENCHFLOW_PROVIDER_TYPE = group.provider;
  env.SKILLSBENCH_PROVIDER = group.provider;
  env.CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC = "1";
  delete env.CLAUDE_CODE_EFFORT_LEVEL;

  if (apiKey) {
    env.ANTHROPIC_AUTH_TOKEN = apiKey;
    env.ANTHROPIC_API_KEY = apiKey;
    // BenchFlow 会依据模型名称执行 provider 凭据预检。即使 Claude Code ACP
    // 最终通过 Anthropic 兼容端点请求，gpt-* 模型名仍要求此变量存在。
    env.OPENAI_API_KEY = apiKey;
    env.BENCHFLOW_PROVIDER_API_KEY = apiKey;
    env.LLM_API_KEY = apiKey;
    if (deepSeekProvider) {
      env.DEEPSEEK_API_KEY = apiKey;
      env.DEEPSEEK_BASE_URL = baseUrl;
    }
  }

  env.LLM_MODEL = modelForAgentEnv(group.agent, group.model);
  env.LLM_BASE_URL = baseUrl;

  return normalizeRunProxyEnv(env);
}

function buildBenchArgs(group, run) {
  const args = [
    "run",
    "bench",
    "eval",
    "run",
    "--tasks-dir",
    group.runTaskDir || group.task.taskDir,
    "--agent",
    group.agent,
    "--model",
    modelForBenchArg(group),
    "--sandbox",
    "docker",
    "--jobs-dir",
    run.jobsDir,
    "--skill-mode",
    group.effectiveSkillMode || (group.skillMode === "force-skill" ? "with-skill" : group.skillMode),
    "--concurrency",
    "1",
    "--agent-idle-timeout",
    group.agentIdleTimeout,
    "--sandbox-setup-timeout",
    "600",
  ];

  // 推理强度属于 BenchFlow 的评测参数，必须通过命令行显式传递。
  // 仅设置 Claude Code 环境变量无法保证所有 ACP 适配器都采用相同强度。
  const reasoningEffort = effectiveReasoningEffort(
    group.provider,
    group.model,
    group.baseUrl,
    group.reasoningEffort,
  );
  if (reasoningEffort) {
    args.push("--reasoning-effort", reasoningEffort);
  }

  if (group.skillMode !== "no-skill" && group.skillsDir) {
    args.push("--skills-dir", group.skillsDir);
  }
  for (const [key, value] of buildNonSecretAgentEnv(group)) {
    args.push("--agent-env", `${key}=${value}`);
  }

  return args;
}

function startRun(group, run) {
  run.status = "running";
  run.startedAt = nowIso();
  appendLog(run, `Starting ${run.taskName} run ${run.runNo}/${group.repeats}`);
  appendLog(run, `Provider: ${group.provider} -> ${group.baseUrl}`);
  appendLog(run, `Skill mode: ${group.skillMode} (BenchFlow: ${group.effectiveSkillMode})`);
  appendLog(run, `Prompt mode: ${group.promptMode || PROMPT_MODE_STANDARD}`);
  if (group.agentTimeoutSec) {
    appendLog(run, `Agent timeout override: ${group.agentTimeoutSec}s`);
  }
  if (group.runTaskDir && group.runTaskDir !== group.task.taskDir) {
    appendLog(run, `Prompt overlay task dir: ${group.runTaskDir}`);
  }
  appendLog(run, `Skills library: ${group.skillMode === "no-skill" ? "no-skill" : `${group.skillsLibraryLabel || "(custom)"} -> ${group.skillsDir || "(none)"}`}`);
  emit("run_update", { run: publicRun(run), group: publicGroup(group) });

  const args = buildBenchArgs(group, run);
  appendLog(run, `uv ${args.join(" ")}`);

  const child = spawn("uv", args, {
    cwd: ROOT,
    env: buildRunEnv(group),
    windowsHide: true,
  });
  run.process = child;
  appendLog(run, `Process started pid=${child.pid}`);

  child.stdout.on("data", (chunk) => appendLog(run, chunk));
  child.stderr.on("data", (chunk) => appendLog(run, chunk));
  child.on("error", (error) => {
    run.error = error.message;
    appendLog(run, `Process error: ${error.message}`);
  });
  child.on("close", (code) => {
    run.exitCode = code;
    run.endedAt = nowIso();
    run.process = null;
    appendLog(run, `Process exited with code ${code}`);
    loadRunSummary(run);
    emit("run_update", { run: publicRun(run), group: publicGroup(group) });
    scheduleGroup(group);
  });
}

function scheduleGroup(group) {
  const groupRuns = state.runs.filter((run) => run.groupId === group.id);
  const active = groupRuns.filter((run) => run.status === "running").length;
  const pending = groupRuns.filter((run) => run.status === "pending");

  if (group.status === "stopping") {
    if (active === 0) {
      group.status = "stopped";
      group.endedAt = nowIso();
      emit("group_update", { group: publicGroup(group) });
    }
    return;
  }

  let slots = Math.max(0, group.parallel - active);
  while (slots > 0 && pending.length > 0) {
    const run = pending.shift();
    startRun(group, run);
    slots -= 1;
  }

  const anyRunning = groupRuns.some((run) => run.status === "running");
  const anyPending = groupRuns.some((run) => run.status === "pending");
  if (!anyRunning && !anyPending) {
    group.status = "completed";
    group.endedAt = nowIso();
  } else {
    group.status = "running";
    group.startedAt ||= nowIso();
  }

  emit("group_update", { group: publicGroup(group) });
}

function killProcessTree(pid) {
  if (!pid) return;
  if (process.platform === "win32") {
    execFile("taskkill", ["/PID", String(pid), "/T", "/F"], () => {});
  } else {
    try {
      process.kill(-pid, "SIGTERM");
    } catch {
      try {
        process.kill(pid, "SIGTERM");
      } catch {
        // Process already gone.
      }
    }
  }
}

async function parseBody(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  if (!chunks.length) return {};
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

function sendJson(res, status, data) {
  res.writeHead(status, { "Content-Type": "application/json; charset=utf-8" });
  res.end(JSON.stringify(data));
}

function sendError(res, status, message) {
  sendJson(res, status, { error: message });
}

function isBlockedCurlStopRequest(req) {
  const userAgent = String(req.headers["user-agent"] || "").toLowerCase();
  const allow = String(req.headers["x-allow-runner-curl-stop"] || "") === "1";
  const expectedToken = String(process.env.SKILLSBENCH_RUNNER_STOP_TOKEN || "");
  const providedToken = String(req.headers["x-runner-stop-token"] || "");
  const tokenOk = expectedToken && providedToken === expectedToken;
  return userAgent.startsWith("curl/") && (!allow || !tokenOk);
}

async function auditStopRequest(req, kind, id, target, extra = {}) {
  const line = JSON.stringify({
    time: nowIso(),
    kind,
    id,
    method: req.method || "",
    url: req.url || "",
    taskName: target?.taskName || target?.task?.name || "",
    jobsRoot: target?.jobsRoot || target?.jobsDir || "",
    status: target?.status || "",
    remoteAddress: req.socket?.remoteAddress || "",
    userAgent: req.headers["user-agent"] || "",
    referer: req.headers.referer || "",
    hasAllowHeader: String(req.headers["x-allow-runner-curl-stop"] || "") === "1",
    hasStopToken: Boolean(req.headers["x-runner-stop-token"]),
    ...extra,
  });
  await fsp.mkdir(path.dirname(STOP_AUDIT_PATH), { recursive: true });
  await fsp.appendFile(STOP_AUDIT_PATH, `${line}\n`, "utf8");
}

function normalizeOptionalTimeoutSec(value) {
  if (value === undefined || value === null || value === "") return null;
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds <= 0) {
    throw new Error("agent timeout must be a positive number of seconds");
  }
  return Math.round(seconds);
}

function stopSentinelForJobsRoot(jobsRoot) {
  const normalized = String(jobsRoot || "").replace(/\\/g, "/");
  const marker = "jobs/web-runner/";
  const index = normalized.indexOf(marker);
  if (index === -1) return null;
  const rest = normalized.slice(index + marker.length).split("/").filter(Boolean);
  if (!rest.length) return null;
  return path.join(ROOT, "jobs", "web-runner", rest[0], "STOP");
}

function blockedByJobsRootStopSentinel(jobsRoot) {
  const sentinel = stopSentinelForJobsRoot(jobsRoot);
  return Boolean(sentinel && fs.existsSync(sentinel));
}

function normalizeOptions(body) {
  const task = state.tasks.find((item) => item.key === body.taskKey);
  if (!task) throw new Error("Unknown task");

  const repeats = Math.min(Math.max(Number(body.repeats || 1), 1), 50);
  const parallel = Math.min(Math.max(Number(body.parallel || 1), 1), 8);
  const model = String(body.model || "deepseek-v4-flash").trim();
  const agent = String(body.agent || "claude-agent-acp").trim();
  const provider = normalizeProvider(body.provider);
  const requestedSkillMode = normalizeSkillMode(body.skillMode);
  const skillMode = requestedSkillMode;
  const effectiveSkillMode = requestedSkillMode === "force-skill" ? "with-skill" : requestedSkillMode;
  const promptMode = requestedSkillMode === "force-skill"
    ? PROMPT_MODE_FORCE_ALL_SKILLS
    : normalizePromptMode(body.promptMode);
  const reasoningEffort = normalizeReasoningEffort(body.reasoningEffort);
  const agentTimeoutSec = normalizeOptionalTimeoutSec(body.agentTimeoutSec ?? body.taskTimeoutSec ?? body.timeoutSec);
  const agentIdleTimeout = String(body.agentIdleTimeout ?? "0").trim() || "0";
  const baseUrl = normalizeBaseUrlForAgent(agent, body.baseUrl, provider);
  if (!baseUrl) throw new Error("Custom provider requires a Base URL");
  const apiKey = String(body.apiKey || "").trim();
  const jobsRoot = String(body.jobsRoot || `jobs/web-runner/${sanitizeSegment(task.name)}-${new Date().toISOString().replace(/[:.]/g, "-")}`).trim();
  const skillsLibrary = resolveLibraryForTask(task, String(body.skillsLibraryId || "").trim());
  const skillsDir = skillsLibrary?.skillsDir || "";
  if (promptMode === PROMPT_MODE_FORCE_ALL_SKILLS && skillMode === "no-skill") {
    throw new Error("force-all-skills prompt mode requires with-skill");
  }
  const prebuiltImage = prebuiltImageForTask(task);
  const runTaskDir = createTaskOverlay(task, skillsDir, promptMode, agentTimeoutSec, prebuiltImage);

  resolveInsideRoot(task.taskDir);
  resolveInsideRoot(runTaskDir);
  if (skillsDir) resolveInsideRoot(skillsDir);
  resolveInsideRoot(jobsRoot);

  return {
    task,
    repeats,
    parallel,
    model,
    agent,
    provider,
    skillMode,
    effectiveSkillMode,
    promptMode,
    reasoningEffort,
    agentTimeoutSec,
    runTaskDir,
    skillsLibraryId: skillsLibrary?.id || "",
    skillsLibraryLabel: skillsLibrary?.label || "",
    skillsDir,
    agentIdleTimeout,
    prebuiltImage,
    baseUrl,
    apiKey,
    jobsRoot: jobsRoot.replace(/\\/g, "/"),
  };
}

function createGroup(options) {
  const group = {
    id: makeId("group"),
    createdAt: nowIso(),
    startedAt: null,
    endedAt: null,
    status: "pending",
    ...options,
    apiKey: options.apiKey,
  };
  state.groups.unshift(group);

  for (let i = 1; i <= options.repeats; i += 1) {
    const run = {
      id: makeId("run"),
      groupId: group.id,
      runNo: i,
      taskKey: options.task.key,
      taskName: options.task.name,
      status: "pending",
      startedAt: null,
      endedAt: null,
      process: null,
      exitCode: null,
      error: null,
      logs: [],
      provider: options.provider,
      baseUrl: options.baseUrl,
      skillMode: options.skillMode,
      effectiveSkillMode: options.effectiveSkillMode,
      summary: null,
      artifactDir: null,
      promptMode: options.promptMode,
      reasoningEffort: options.reasoningEffort,
      agentTimeoutSec: options.agentTimeoutSec,
      runTaskDir: options.runTaskDir,
      skillsLibraryId: options.skillsLibraryId,
      skillsLibraryLabel: options.skillsLibraryLabel,
      skillsDir: options.skillsDir,
      prebuiltImage: options.prebuiltImage,
      jobsDir: `${options.jobsRoot}/run-${i}`,
    };
    state.runs.unshift(run);
  }

  emit("snapshot", snapshot());
  scheduleGroup(group);
  return group;
}

function normalizeRepairOptions(body) {
  const task = state.tasks.find((item) => item.key === body.taskKey);
  if (!task) throw new Error("Unknown task");

  const sourceLibrary = resolveLibraryForTask(task, String(body.sourceSkillsLibraryId || body.skillsLibraryId || "").trim());
  const sourceSkillsDir = String(body.sourceSkillsDir || sourceLibrary?.skillsDir || "").trim();
  if (!sourceSkillsDir) throw new Error("No source skills library selected");
  resolveInsideRoot(task.taskDir);
  resolveInsideRoot(sourceSkillsDir);

  const rawPaths = Array.isArray(body.jobPaths)
    ? body.jobPaths
    : String(body.jobPaths || body.evidencePaths || "").split(/\r?\n/);
  const jobPaths = rawPaths
    .map((item) => String(item || "").trim())
    .filter(Boolean)
    .map((item) => item.replace(/\\/g, "/"));
  if (!jobPaths.length) throw new Error("At least one evidence job path is required");
  for (const jobPath of jobPaths) resolveInsideRoot(jobPath);

  const outputVariant = sanitizeSegment(body.outputVariant || body.variant || `auto-repair-${new Date().toISOString().replace(/[:.]/g, "-")}`);
  const outputSkillsDir = `skill-libraries/${task.name}/${outputVariant}`;
  const reportDir = `repair-runs/${task.name}/${outputVariant}`;
  resolveInsideRoot(outputSkillsDir);
  resolveInsideRoot(reportDir);

  const maxRollouts = Math.min(Math.max(Number(body.maxRollouts || 5), 1), 30);
  const strongProvider = String(body.strongProvider || "anthropic").trim();
  const strongBaseUrl = String(body.strongBaseUrl || "https://api.camel-hub.com").trim();
  const strongModel = String(body.strongModel || "gpt-5.5").trim();
  const strongReasoningEffort = normalizeReasoningEffort(body.strongReasoningEffort);
  const strongApiKey = String(body.strongApiKey || "").trim();
  const strongMaxTokens = Math.min(Math.max(Number(body.strongMaxTokens || 8000), 512), 32000);
  const strongTimeout = Math.min(Math.max(Number(body.strongTimeout || 240), 10), 900);
  const weakModel = String(body.weakModel || "deepseek-v4-flash").trim();
  const keywordMode = String(body.keywordMode || "rules").trim();

  return {
    task,
    sourceSkillsLibraryId: sourceLibrary?.id || "",
    sourceSkillsLibraryLabel: sourceLibrary?.label || sourceSkillsDir,
    sourceSkillsDir,
    outputVariant,
    outputSkillsDir,
    reportDir,
    maxRollouts,
    jobPaths,
    strongProvider,
    strongBaseUrl,
    strongModel,
    strongReasoningEffort,
    strongApiKey,
    strongMaxTokens,
    strongTimeout,
    weakModel,
    keywordMode,
    force: Boolean(body.force),
    judge: Boolean(body.judge),
  };
}

function buildRepairArgs(job) {
  const args = [
    "scripts/skill_repair_pipeline.py",
    "--task",
    job.task.taskDir,
    "--source-skills-dir",
    job.sourceSkillsDir,
    "--variant",
    job.outputVariant,
    "--report-root",
    "repair-runs",
    "--max-rollouts",
    String(job.maxRollouts),
    "--strong-provider",
    job.strongProvider,
    "--strong-base-url",
    job.strongBaseUrl,
    "--strong-model",
    job.strongModel,
    "--strong-reasoning-effort",
    job.strongReasoningEffort,
    "--strong-max-tokens",
    String(job.strongMaxTokens),
    "--strong-timeout",
    String(job.strongTimeout),
    "--weak-model",
    job.weakModel,
    "--keyword-mode",
    job.keywordMode,
    "--json-output",
  ];
  for (const jobPath of job.jobPaths) args.push("--job-path", jobPath);
  if (job.force) args.push("--force");
  if (job.judge) args.push("--judge");
  return args;
}

function buildRepairEnv(job) {
  const env = { ...process.env };
  env.PYTHONUTF8 = "1";
  env.STRONG_LLM_BASE_URL = job.strongBaseUrl;
  env.STRONG_LLM_MODEL = job.strongModel;
  env.STRONG_LLM_REASONING_EFFORT = job.strongReasoningEffort;
  env.WEAK_LLM_MODEL = job.weakModel;
  env.CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC = "1";
  if (job.strongApiKey) {
    env.STRONG_LLM_API_KEY = job.strongApiKey;
    env.ANTHROPIC_AUTH_TOKEN = job.strongApiKey;
    env.ANTHROPIC_API_KEY = job.strongApiKey;
  }
  return env;
}

function startRepairJob(job) {
  job.status = "running";
  job.startedAt = nowIso();
  appendRepairLog(job, `Starting skill repair for ${job.task.name}`);
  appendRepairLog(job, `Source skills: ${job.sourceSkillsLibraryLabel} -> ${job.sourceSkillsDir}`);
  appendRepairLog(job, `Output variant: ${job.outputVariant}`);
  emit("repair_update", { repairJob: publicRepairJob(job) });

  const args = buildRepairArgs(job);
  appendRepairLog(job, `${pythonExecutable()} ${args.join(" ")}`);
  const child = spawn(pythonExecutable(), args, {
    cwd: ROOT,
    env: buildRepairEnv(job),
    windowsHide: true,
  });
  job.process = child;
  appendRepairLog(job, `Process started pid=${child.pid}`);

  let stdout = "";
  child.stdout.on("data", (chunk) => {
    const text = chunk.toString("utf8");
    stdout += text;
    appendRepairLog(job, text);
  });
  child.stderr.on("data", (chunk) => appendRepairLog(job, chunk));
  child.on("error", (error) => {
    job.error = error.message;
    appendRepairLog(job, `Process error: ${error.message}`);
  });
  child.on("close", async (code) => {
    job.exitCode = code;
    job.endedAt = nowIso();
    job.process = null;
    appendRepairLog(job, `Process exited with code ${code}`);
    const manifestPath = path.join(ROOT, job.reportDir, "manifest.json");
    job.manifest = readJsonIfExists(manifestPath);
    if (job.manifest) {
      job.outputSkillsDir = job.manifest.outputSkillsDir || job.outputSkillsDir;
      job.reportPath = job.manifest.reportDir ? `${job.manifest.reportDir}/README.md` : `${job.reportDir}/README.md`;
    }
    if (!job.manifest && stdout.trim().startsWith("{")) {
      try {
        const parsed = JSON.parse(stdout.slice(stdout.indexOf("{")));
        job.outputSkillsDir = parsed.outputSkillsDir || job.outputSkillsDir;
        job.reportPath = parsed.report || job.reportPath;
      } catch {
        // Keep file-based manifest as the source of truth.
      }
    }
    job.status = code === 0 ? "completed" : "error";
    if (code !== 0 && !job.error) job.error = `Repair process exited with code ${code}`;
    await scanSkillLibraries();
    emit("repair_update", { repairJob: publicRepairJob(job) });
    emit("snapshot", snapshot());
  });
}

function createRepairJob(options) {
  const job = {
    id: makeId("repair"),
    createdAt: nowIso(),
    startedAt: null,
    endedAt: null,
    status: "pending",
    process: null,
    exitCode: null,
    error: null,
    logs: [],
    manifest: null,
    reportPath: `${options.reportDir}/README.md`,
    ...options,
  };
  state.repairJobs.unshift(job);
  emit("snapshot", snapshot());
  startRepairJob(job);
  return job;
}

function normalizeStageInitOptions(body) {
  const task = state.tasks.find((item) => item.key === body.taskKey);
  if (!task) throw new Error("Unknown task");

  const sourceLibrary = resolveLibraryForTask(task, String(body.sourceSkillsLibraryId || body.skillsLibraryId || "").trim());
  const sourceSkillsDir = String(body.sourceSkillsDir || sourceLibrary?.skillsDir || "").trim();
  if (!sourceSkillsDir) throw new Error("No source skills library selected");
  resolveInsideRoot(task.taskDir);
  resolveInsideRoot(sourceSkillsDir);

  const rawPaths = Array.isArray(body.tracePaths)
    ? body.tracePaths
    : String(body.tracePaths || body.jobPaths || body.evidencePaths || "").split(/\r?\n/);
  const tracePaths = rawPaths
    .map((item) => String(item || "").trim())
    .filter(Boolean)
    .map((item) => item.replace(/\\/g, "/"));
  if (!tracePaths.length) throw new Error("At least one trace/job path is required");
  for (const tracePath of tracePaths) resolveInsideRoot(tracePath);

  const outputVariant = sanitizeSegment(body.outputVariant || body.variant || `stage-debug-${new Date().toISOString().replace(/[:.]/g, "-")}`);
  const outputDir = `repair-runs/${task.name}/${outputVariant}`;
  const outputSkillsDir = `skill-libraries/${task.name}/${outputVariant}`;
  resolveInsideRoot(outputDir);
  resolveInsideRoot(outputSkillsDir);

  return {
    command: "init",
    task,
    sourceSkillsLibraryId: sourceLibrary?.id || "",
    sourceSkillsLibraryLabel: sourceLibrary?.label || sourceSkillsDir,
    sourceSkillsDir,
    tracePaths,
    outputVariant,
    outputDir,
    outputSkillsDir,
    maxTraces: Math.min(Math.max(Number(body.maxTraces || 5), 1), 30),
    maxPromptChars: Math.min(Math.max(Number(body.maxPromptChars || 220000), 20000), 500000),
    traceAnalysisWorkers: Math.min(Math.max(Number(body.traceAnalysisWorkers || 5), 1), 20),
    addSkillMergeThreshold: Math.min(Math.max(Number(body.addSkillMergeThreshold || 0), 0), 1000),
    addSkillTargetCount: Math.min(Math.max(Number(body.addSkillTargetCount || 0), 0), 1000),
    maxNewSkills: Math.min(Math.max(Number(body.maxNewSkills ?? 2), 0), 1000),
    skillWordLimit: Math.min(Math.max(Number(body.skillWordLimit || 1200), 100), 20000),
    stage7MaxOperations: Math.min(Math.max(Number(body.stage7MaxOperations || body.stage6MaxOperations || 30), 1), 1000),
    stage7RepairMode: String(body.stage7RepairMode || "per_suggestion") === "skill_package" ? "skill_package" : "per_suggestion",
    stage7SkillPackageSize: Math.min(Math.max(Number(body.stage7SkillPackageSize || 3), 1), 100),
    strongBaseUrl: String(body.strongBaseUrl || "https://api.camel-hub.com").trim(),
    strongModel: String(body.strongModel || "gpt-5.5").trim(),
    strongReasoningEffort: normalizeReasoningEffort(body.strongReasoningEffort || "minimal"),
    strongApiKey: String(body.strongApiKey || "").trim(),
    separateReviewLlm: Boolean(body.separateReviewLlm),
    reviewBaseUrl: String(body.reviewBaseUrl || "").trim(),
    reviewModel: String(body.reviewModel || "").trim(),
    reviewReasoningEffort: normalizeReasoningEffort(body.reviewReasoningEffort || "minimal"),
    reviewApiKey: String(body.reviewApiKey || "").trim(),
    strongTimeout: Math.min(Math.max(Number(body.strongTimeout || 1800), 30), 3600),
    force: Boolean(body.force),
  };
}

function normalizeStageRunOptions(command, body) {
  const outputDir = String(body.outputDir || "").trim().replace(/\\/g, "/");
  if (!outputDir) throw new Error("outputDir is required");
  const outputDirAbs = resolveInsideRoot(outputDir);
  const outputDirRel = toPosixRelative(outputDirAbs);
  if (!outputDirRel.startsWith("repair-runs/")) throw new Error("Only repair-runs output directories are supported");
  const manifest = readJsonIfExists(path.join(outputDirAbs, "stage_debug_manifest.json")) || {};
  const outputSkillsDir = String(body.outputSkillsDir || manifest.outputSkillsDir || "").trim().replace(/\\/g, "/");
  if (outputSkillsDir) resolveInsideRoot(outputSkillsDir);
  const stage = String(body.stage || "").trim();
  if ((command === "prompt" || command === "run" || command === "calculate") && !stage) throw new Error("stage is required");
  return {
    command,
    stage,
    traceIndex: body.traceIndex === "" || body.traceIndex === null || body.traceIndex === undefined ? null : Number(body.traceIndex),
    stageRunMode: String(body.stageRunMode || "step").trim() === "until-complete" ? "until-complete" : "step",
    task: state.tasks.find((item) => item.taskDir === manifest.taskDir) || null,
    sourceSkillsDir: manifest.skillsDir || "",
    outputVariant: outputDirRel.split("/").slice(2).join("/") || path.basename(outputDirRel),
    outputDir: outputDirRel,
    outputSkillsDir,
    maxPromptChars: Math.min(Math.max(Number(body.maxPromptChars || manifest.maxPromptChars || 220000), 20000), 500000),
    traceAnalysisWorkers: Math.min(Math.max(Number(body.traceAnalysisWorkers || manifest.traceAnalysisWorkers || 5), 1), 20),
    addSkillMergeThreshold: Math.min(Math.max(Number(body.addSkillMergeThreshold ?? manifest.addSkillMergeThreshold ?? 0), 0), 1000),
    addSkillTargetCount: Math.min(Math.max(Number(body.addSkillTargetCount ?? manifest.addSkillTargetCount ?? 0), 0), 1000),
    maxNewSkills: Math.min(Math.max(Number(body.maxNewSkills ?? manifest.maxNewSkillCount ?? 2), 0), 1000),
    skillWordLimit: Math.min(Math.max(Number(body.skillWordLimit || manifest.skillWordLimit || 1200), 100), 20000),
    stage7MaxOperations: Math.min(Math.max(Number(body.stage7MaxOperations || body.stage6MaxOperations || manifest.stage7MaxOperations || manifest.stage6MaxOperations || 30), 1), 1000),
    stage7RepairMode: String(body.stage7RepairMode || manifest.stage7RepairMode || "per_suggestion") === "skill_package" ? "skill_package" : "per_suggestion",
    stage7SkillPackageSize: Math.min(Math.max(Number(body.stage7SkillPackageSize || manifest.stage7SkillPackageSize || 3), 1), 100),
    strongBaseUrl: String(body.strongBaseUrl || manifest.strongBaseUrl || "https://api.camel-hub.com").trim(),
    strongModel: String(body.strongModel || manifest.strongModel || "gpt-5.5").trim(),
    strongReasoningEffort: normalizeReasoningEffort(body.strongReasoningEffort || manifest.strongReasoningEffort || "minimal"),
    strongApiKey: String(body.strongApiKey || "").trim(),
    separateReviewLlm: body.separateReviewLlm === undefined ? Boolean(manifest.separateReviewLlm) : Boolean(body.separateReviewLlm),
    reviewBaseUrl: String(body.reviewBaseUrl || manifest.reviewBaseUrl || "").trim(),
    reviewModel: String(body.reviewModel || manifest.reviewModel || "").trim(),
    reviewReasoningEffort: normalizeReasoningEffort(body.reviewReasoningEffort || manifest.reviewReasoningEffort || "minimal"),
    reviewApiKey: String(body.reviewApiKey || "").trim(),
    strongTimeout: Math.min(Math.max(Number(body.strongTimeout || 1800), 30), 3600),
    noApply: Boolean(body.noApply),
  };
}

function buildStageArgs(job) {
  const args = ["offline_skill_rca/run_stage_debug.py", job.command, "--output-dir", job.outputDir];
  if (job.command === "init") {
    args.push("--task-dir", job.task.taskDir);
    args.push("--skills-dir", job.sourceSkillsDir);
    args.push("--traces", ...job.tracePaths);
    args.push("--max-traces", String(job.maxTraces));
    if (job.force) args.push("--force");
  }
  if (job.command === "prompt" || job.command === "run" || job.command === "calculate") {
    args.push("--stage", job.stage);
    if (job.traceIndex !== null && job.traceIndex !== undefined && Number.isFinite(Number(job.traceIndex))) {
      args.push("--trace-index", String(job.traceIndex));
    }
    if (job.command === "run" && job.stage === "stage-08-transactional-skill-repair") {
      args.push("--stage-run-mode", job.stageRunMode || "step");
    }
  }
  if (job.outputSkillsDir) args.push("--output-skills-dir", job.outputSkillsDir);
  if (job.strongBaseUrl) args.push("--strong-base-url", job.strongBaseUrl);
  if (job.strongModel) args.push("--strong-model", job.strongModel);
  if (job.strongReasoningEffort) args.push("--strong-reasoning-effort", job.strongReasoningEffort);
  if (job.strongApiKey) args.push("--strong-api-key", job.strongApiKey);
  if (job.maxPromptChars) args.push("--max-prompt-chars", String(job.maxPromptChars));
  if (job.traceAnalysisWorkers) args.push("--trace-analysis-workers", String(job.traceAnalysisWorkers));
  if (job.addSkillMergeThreshold !== undefined) args.push("--add-skill-merge-threshold", String(job.addSkillMergeThreshold));
  if (job.addSkillTargetCount !== undefined) args.push("--add-skill-target-count", String(job.addSkillTargetCount));
  if (job.maxNewSkills !== undefined) args.push("--max-new-skills", String(job.maxNewSkills));
  if (job.skillWordLimit) args.push("--skill-word-limit", String(job.skillWordLimit));
  if (job.stage7MaxOperations) args.push("--stage7-max-operations", String(job.stage7MaxOperations));
  if (job.stage7RepairMode) args.push("--stage7-repair-mode", job.stage7RepairMode);
  if (job.stage7SkillPackageSize) args.push("--stage7-skill-package-size", String(job.stage7SkillPackageSize));
  args.push(job.separateReviewLlm ? "--use-separate-review-llm" : "--no-separate-review-llm");
  if (job.reviewBaseUrl) args.push("--review-base-url", job.reviewBaseUrl);
  if (job.reviewModel) args.push("--review-model", job.reviewModel);
  if (job.reviewReasoningEffort) args.push("--review-reasoning-effort", job.reviewReasoningEffort);
  if (job.noApply) args.push("--no-apply");
  return args;
}

function buildStageEnv(job) {
  const env = { ...process.env };
  env.PYTHONUTF8 = "1";
  env.OFFLINE_SKILL_RCA_BASE_URL = job.strongBaseUrl || env.OFFLINE_SKILL_RCA_BASE_URL || "";
  env.OFFLINE_SKILL_RCA_MODEL = job.strongModel || env.OFFLINE_SKILL_RCA_MODEL || "";
  env.OFFLINE_SKILL_RCA_REASONING_EFFORT = job.strongReasoningEffort || "minimal";
  env.OFFLINE_SKILL_RCA_TIMEOUT_SEC = String(job.strongTimeout || 1800);
  if (job.strongApiKey) {
    env.OFFLINE_SKILL_RCA_API_KEY = job.strongApiKey;
    env.LLM_API_KEY = job.strongApiKey;
    env.OPENAI_API_KEY = job.strongApiKey;
  }
  if (job.reviewApiKey) env.OFFLINE_SKILL_RCA_REVIEW_API_KEY = job.reviewApiKey;
  if (job.reviewBaseUrl) env.OFFLINE_SKILL_RCA_REVIEW_BASE_URL = job.reviewBaseUrl;
  if (job.reviewModel) env.OFFLINE_SKILL_RCA_REVIEW_MODEL = job.reviewModel;
  if (job.reviewReasoningEffort) env.OFFLINE_SKILL_RCA_REVIEW_REASONING_EFFORT = job.reviewReasoningEffort;
  if (job.separateReviewLlm) env.OFFLINE_SKILL_RCA_USE_SEPARATE_REVIEW_LLM = "1";
  return env;
}

function runStageCliJson(args, env = process.env, timeout = 60_000) {
  return new Promise((resolve, reject) => {
    execFile(
      pythonExecutable(),
      ["offline_skill_rca/run_stage_debug.py", ...args],
      { cwd: ROOT, env: { ...process.env, ...env, PYTHONUTF8: "1" }, timeout, windowsHide: true },
      (error, stdout, stderr) => {
        const text = String(stdout || "").trim();
        const start = text.indexOf("{");
        const end = text.lastIndexOf("}");
        let parsed = null;
        if (start >= 0 && end >= start) {
          try {
            parsed = JSON.parse(text.slice(start, end + 1));
          } catch {
            parsed = null;
          }
        }
        if (error) {
          reject(new Error(compactCommandFailure(error, stdout, stderr) || error.message));
          return;
        }
        if (!parsed) {
          reject(new Error(`Stage CLI did not return JSON: ${String(stdout || stderr).slice(0, 1200)}`));
          return;
        }
        resolve(parsed);
      },
    );
  });
}

async function readStageStatus(outputDir) {
  return runStageCliJson(["status", "--output-dir", outputDir]);
}

async function readTemplateVariable(name) {
  if (!/^[a-zA-Z0-9_]+$/.test(String(name || ""))) {
    throw new Error("Invalid template variable name");
  }
  return runStageCliJson(["template-variable", "--name", String(name)], process.env, 20_000);
}

function startStageJob(job) {
  job.status = "running";
  job.startedAt = nowIso();
  appendStageLog(job, `Starting stage command: ${job.command}${job.stage ? ` ${job.stage}` : ""}`);
  appendStageLog(job, `Output dir: ${job.outputDir}`);
  emit("repair_stage_update", { stageJob: publicStageJob(job) });

  const args = buildStageArgs(job);
  appendStageLog(job, `${pythonExecutable()} ${args.join(" ")}`);
  const child = spawn(pythonExecutable(), args, {
    cwd: ROOT,
    env: buildStageEnv(job),
    windowsHide: true,
  });
  job.process = child;
  appendStageLog(job, `Process started pid=${child.pid}`);

  child.stdout.on("data", (chunk) => appendStageLog(job, chunk));
  child.stderr.on("data", (chunk) => appendStageLog(job, chunk));
  child.on("error", (error) => {
    job.error = error.message;
    appendStageLog(job, `Process error: ${error.message}`);
  });
  child.on("close", async (code) => {
    job.exitCode = code;
    job.endedAt = nowIso();
    job.process = null;
    appendStageLog(job, `Process exited with code ${code}`);
    job.status = job.pauseRequested ? "paused" : code === 0 ? "completed" : "error";
    if (code !== 0 && !job.error) job.error = `Stage process exited with code ${code}`;
    try {
      job.latestStatus = (await readStageStatus(job.outputDir)).status;
    } catch (error) {
      appendStageLog(job, `Status refresh failed: ${error.message}`);
    }
    await scanSkillLibraries();
    emit("repair_stage_update", { stageJob: publicStageJob(job) });
    emit("snapshot", snapshot());
  });
}

function createStageJob(options) {
  const job = {
    id: makeId("stage"),
    createdAt: nowIso(),
    startedAt: null,
    endedAt: null,
    status: "pending",
    process: null,
    exitCode: null,
    error: null,
    logs: [],
    ...options,
  };
  state.stageJobs.unshift(job);
  emit("snapshot", snapshot());
  startStageJob(job);
  return job;
}

async function scanRepairReports() {
  const repairRoot = path.join(ROOT, "repair-runs");
  if (!(await exists(repairRoot))) return [];
  const manifests = [];
  async function walk(dir) {
    let entries = [];
    try {
      entries = await fsp.readdir(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      const fullPath = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        await walk(fullPath);
      } else if (entry.name === "manifest.json") {
        const manifest = await readJsonFileIfExists(fullPath);
        if (!manifest) continue;
        const stat = await fsp.stat(fullPath);
        manifests.push({
          id: toPosixRelative(fullPath),
          manifestPath: toPosixRelative(fullPath),
          reportPath: manifest.reportDir ? `${manifest.reportDir}/README.md` : toPosixRelative(path.join(path.dirname(fullPath), "README.md")),
          modifiedAt: stat.mtime.toISOString(),
          ...manifest,
        });
      }
    }
  }
  await walk(repairRoot);
  return manifests.sort((a, b) => new Date(b.modifiedAt) - new Date(a.modifiedAt)).slice(0, 200);
}

async function statFileIfExists(filePath) {
  try {
    const stat = await fsp.stat(filePath);
    if (!stat.isFile()) return null;
    return {
      exists: true,
      size: stat.size,
      modifiedAt: stat.mtime.toISOString(),
    };
  } catch {
    return null;
  }
}

async function readJsonFileLimitedIfExists(filePath, maxBytes = MAX_REPAIR_JSON_BYTES) {
  const info = await statFileIfExists(filePath);
  if (!info) return null;
  if (info.size > maxBytes) {
    return {
      __tooLarge: true,
      size: info.size,
      maxBytes,
      path: toPosixRelative(filePath),
    };
  }
  try {
    const data = JSON.parse(await fsp.readFile(filePath, "utf8"));
    return data;
  } catch (error) {
    return {
      __parseError: true,
      message: error.message,
      path: toPosixRelative(filePath),
    };
  }
}

async function readTextFileIfExists(filePath, maxBytes = MAX_REPAIR_TEXT_BYTES) {
  const info = await statFileIfExists(filePath);
  if (!info) return null;
  try {
    const handle = await fsp.open(filePath, "r");
    try {
      const readBytes = Math.min(info.size, maxBytes);
      const buffer = Buffer.alloc(readBytes);
      await handle.read(buffer, 0, readBytes, 0);
      return {
        path: toPosixRelative(filePath),
        name: path.basename(filePath),
        size: info.size,
        modifiedAt: info.modifiedAt,
        truncated: info.size > maxBytes,
        text: buffer.toString("utf8"),
      };
    } finally {
      await handle.close();
    }
  } catch (error) {
    return {
      path: toPosixRelative(filePath),
      name: path.basename(filePath),
      size: info.size,
      modifiedAt: info.modifiedAt,
      truncated: false,
      text: "",
      error: error.message,
    };
  }
}

function ensureRepairRunDir(relRunDir) {
  if (!relRunDir) throw new Error("runDir is required");
  const resolved = resolveArtifactPath(relRunDir);
  const relative = toPosixRelative(resolved);
  if (!relative.startsWith("repair-runs/")) {
    throw new Error("Only repair-runs paths can be read");
  }
  return { resolved, relative };
}

function repairRunIdentity(relativeRunDir) {
  const parts = relativeRunDir.split("/");
  return {
    taskName: parts[1] || path.basename(relativeRunDir),
    variant: parts.slice(2).join("/") || "",
  };
}

function latestModifiedAt(...infos) {
  const dates = infos
    .filter(Boolean)
    .map((info) => info.modifiedAt)
    .filter(Boolean)
    .sort((a, b) => new Date(b) - new Date(a));
  return dates[0] || "";
}

function usageFromResponse(response) {
  return response?.raw_response?.usage || response?.usage || null;
}

function aggregateUsage(interactions) {
  const totals = {};
  for (const item of interactions || []) {
    const usage = usageFromResponse(item.response) || {};
    for (const [key, value] of Object.entries(usage)) {
      if (typeof value === "number") totals[key] = (totals[key] || 0) + value;
    }
  }
  return Object.keys(totals).length ? totals : null;
}

async function listRepairTextFiles(dir, baseDir = dir, depth = 0, out = []) {
  if (depth > 5 || out.length >= 120 || !(await exists(dir))) return out;
  let entries = [];
  try {
    entries = await fsp.readdir(dir, { withFileTypes: true });
  } catch {
    return out;
  }

  for (const entry of entries) {
    if (out.length >= 120) break;
    const fullPath = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      await listRepairTextFiles(fullPath, baseDir, depth + 1, out);
    } else if (entry.isFile() && /\.(md|txt|json|csv)$/i.test(entry.name)) {
      const file = await readTextFileIfExists(fullPath);
      if (file) {
        file.relativeName = path.relative(baseDir, fullPath).replace(/\\/g, "/");
        out.push(file);
      }
    }
  }
  return out;
}

async function readRepairTranscriptInteractions(runDirAbs) {
  const transcriptDir = path.join(runDirAbs, "llm_transcript");
  if (!(await exists(transcriptDir))) return [];
  let entries = [];
  try {
    entries = await fsp.readdir(transcriptDir, { withFileTypes: true });
  } catch {
    return [];
  }
  const bases = entries
    .filter((entry) => entry.isFile() && entry.name.endsWith(".request.json"))
    .map((entry) => entry.name.slice(0, -".request.json".length))
    .sort((a, b) => a.localeCompare(b));

  const interactions = [];
  for (const base of bases) {
    const requestPath = path.join(transcriptDir, `${base}.request.json`);
    const responsePath = path.join(transcriptDir, `${base}.response.json`);
    const parsedPath = path.join(transcriptDir, `${base}.parsed.json`);
    const errorPath = path.join(transcriptDir, `${base}.error.json`);
    const requestInfo = await statFileIfExists(requestPath);
    const responseInfo = await statFileIfExists(responsePath);
    const parsedInfo = await statFileIfExists(parsedPath);
    const errorInfo = await statFileIfExists(errorPath);
    interactions.push({
      id: base,
      stage: base,
      request: await readJsonFileLimitedIfExists(requestPath),
      response: await readJsonFileLimitedIfExists(responsePath),
      parsed: await readJsonFileLimitedIfExists(parsedPath),
      error: await readJsonFileLimitedIfExists(errorPath),
      files: {
        request: requestInfo,
        response: responseInfo,
        parsed: parsedInfo,
        error: errorInfo,
      },
      modifiedAt: latestModifiedAt(responseInfo, parsedInfo, errorInfo, requestInfo),
    });
  }
  return interactions;
}

function finalRepairInteraction(interactions) {
  if (!interactions?.length) return null;
  return (
    interactions.find((item) => item.id.includes("stage-08")) ||
    interactions.find((item) => item.id === "repair-llm") ||
    interactions[interactions.length - 1]
  );
}

async function summarizeRepairTraceRun(runDirAbs) {
  const runDir = toPosixRelative(runDirAbs);
  const { taskName, variant } = repairRunIdentity(runDir);
  const interactions = await readRepairTranscriptInteractions(runDirAbs);
  const finalInteraction = finalRepairInteraction(interactions);
  const inputBundlePath = path.join(runDirAbs, "input_bundle.json");
  const fullPath = path.join(runDirAbs, "offline_skill_rca_full.json");
  const manifestPath = path.join(runDirAbs, "applied_repair_manifest.json");
  const processPath = path.join(runDirAbs, "repair_process_zh.md");
  const reportPath = path.join(runDirAbs, "diagnosis_report.md");

  const requestInfo = finalInteraction?.files?.request || null;
  const responseInfo = finalInteraction?.files?.response || null;
  const parsedInfo = finalInteraction?.files?.parsed || null;
  const inputInfo = await statFileIfExists(inputBundlePath);
  const fullInfo = await statFileIfExists(fullPath);
  const processInfo = await statFileIfExists(processPath);
  const reportInfo = await statFileIfExists(reportPath);

  const request = finalInteraction?.request || null;
  const response = finalInteraction?.response || null;
  const parsed = finalInteraction?.parsed || null;
  const inputBundle = await readJsonFileLimitedIfExists(inputBundlePath);
  const full = await readJsonFileLimitedIfExists(fullPath);
  const manifest = await readJsonFileLimitedIfExists(manifestPath);
  const result = full && !full.__tooLarge && !full.__parseError ? full : parsed;

  const metadata = full?.metadata || {};
  const counts = {
    skills: inputBundle?.skill_library?.length ?? inputBundle?.skill_cards_static?.length ?? 0,
    failedTrajectories: inputBundle?.failed_trajectories?.length ?? 0,
    capabilityNodes: result?.capability_graph?.nodes?.length ?? result?.S0_capability_dag?.nodes?.length ?? 0,
    faultCards: result?.fault_cards?.length ?? 0,
    hypotheses: result?.root_cause_hypotheses?.length ?? 0,
    patches: result?.skill_patch_plan?.length ?? 0,
    drafts: result?.updated_skill_drafts?.length ?? 0,
    blockers: result?.non_skill_blockers?.length ?? 0,
  };

  return {
    id: runDir,
    runDir,
    taskName,
    variant,
    model: request?.model || metadata.strongModel || "",
    endpoint: request?.endpoint || metadata.strongBaseUrl || "",
    statusCode: response?.status_code ?? null,
    usage: aggregateUsage(interactions) || usageFromResponse(response),
    interactionCount: interactions.length,
    taskDir: inputBundle?.task_dir || metadata.taskDir || "",
    sourceSkillsDir: inputBundle?.skills_dir || metadata.sourceSkillsDir || "",
    outputSkillsDir: manifest?.output_dir || metadata.outputSkillsDir || "",
    modifiedAt: latestModifiedAt(responseInfo, parsedInfo, fullInfo, inputInfo, processInfo, reportInfo, requestInfo),
    counts,
    files: {
      request: requestInfo,
      response: responseInfo,
      parsed: parsedInfo,
      inputBundle: inputInfo,
      full: fullInfo,
      process: processInfo,
      report: reportInfo,
    },
  };
}

async function scanRepairTraceRuns() {
  const repairRoot = path.join(ROOT, "repair-runs");
  if (!(await exists(repairRoot))) return [];
  const runs = [];
  const seenRunDirs = new Set();

  async function walk(dir) {
    let entries = [];
    try {
      entries = await fsp.readdir(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      const fullPath = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        await walk(fullPath);
      } else if (entry.name.endsWith(".request.json") && path.basename(dir) === "llm_transcript") {
        const runDirAbs = path.dirname(dir);
        const key = runDirAbs.toLowerCase();
        if (seenRunDirs.has(key)) continue;
        seenRunDirs.add(key);
        runs.push(await summarizeRepairTraceRun(runDirAbs));
      }
    }
  }

  await walk(repairRoot);
  return runs.sort((a, b) => new Date(b.modifiedAt || 0) - new Date(a.modifiedAt || 0)).slice(0, 200);
}

async function readRepairTraceDetail(relRunDir) {
  const { resolved: runDirAbs, relative: runDir } = ensureRepairRunDir(relRunDir);
  const summary = await summarizeRepairTraceRun(runDirAbs);
  const interactions = await readRepairTranscriptInteractions(runDirAbs);
  const finalInteraction = finalRepairInteraction(interactions);
  const full = await readJsonFileLimitedIfExists(path.join(runDirAbs, "offline_skill_rca_full.json"));
  const parsed = finalInteraction?.parsed || null;

  return {
    summary,
    interactions,
    request: finalInteraction?.request || null,
    response: finalInteraction?.response || null,
    parsed,
    full,
    inputBundle: await readJsonFileLimitedIfExists(path.join(runDirAbs, "input_bundle.json")),
    artifacts: {
      prompt: await readTextFileIfExists(path.join(runDirAbs, "offline_skill_rca_prompt.txt")),
      diagnosisReport: await readTextFileIfExists(path.join(runDirAbs, "diagnosis_report.md")),
      repairProcess: await readTextFileIfExists(path.join(runDirAbs, "repair_process_zh.md")),
      coverageCsv: await readTextFileIfExists(path.join(runDirAbs, "skill_coverage_matrix.csv")),
    },
    jsonArtifacts: {
      capabilityGraph: await readJsonFileLimitedIfExists(path.join(runDirAbs, "capability_graph.json"))
        || await readJsonFileLimitedIfExists(path.join(runDirAbs, "s0_capability_dag.json")),
      faultCards: await readJsonFileLimitedIfExists(path.join(runDirAbs, "fault_cards.json")),
      coverageMatrix: await readJsonFileLimitedIfExists(path.join(runDirAbs, "skill_coverage_matrix.json")),
      hypotheses: await readJsonFileLimitedIfExists(path.join(runDirAbs, "root_cause_hypotheses.json")),
      patchPlan: await readJsonFileLimitedIfExists(path.join(runDirAbs, "skill_patch_plan.json")),
      patchReviews: await readJsonFileLimitedIfExists(path.join(runDirAbs, "patch_reviews.json")),
      updatedDrafts: await readJsonFileLimitedIfExists(path.join(runDirAbs, "updated_skill_drafts.json")),
      appliedManifest: await readJsonFileLimitedIfExists(path.join(runDirAbs, "applied_repair_manifest.json")),
      stageOutputs: await readJsonFileLimitedIfExists(path.join(runDirAbs, "stage_outputs.json")),
      traceAnalyses: await readJsonFileLimitedIfExists(path.join(runDirAbs, "trace_analyses.json")),
    },
    patchFiles: await listRepairTextFiles(path.join(runDirAbs, "patches")),
    draftFiles: await listRepairTextFiles(path.join(runDirAbs, "updated_skill_drafts")),
    runDir,
  };
}

async function scanStageDebugRuns() {
  const repairRoot = path.join(ROOT, "repair-runs");
  if (!(await exists(repairRoot))) return [];
  const runs = [];

  async function walk(dir) {
    let entries = [];
    try {
      entries = await fsp.readdir(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      const fullPath = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        await walk(fullPath);
      } else if (entry.name === "stage_debug_manifest.json") {
        const manifest = await readJsonFileIfExists(fullPath);
        const stat = await fsp.stat(fullPath);
        const runDirAbs = path.dirname(fullPath);
        const runDir = toPosixRelative(runDirAbs);
        const identity = repairRunIdentity(runDir);
        const stageOutputsInfo = await statFileIfExists(path.join(runDirAbs, "stage_outputs.json"));
        const finalInfo = await statFileIfExists(path.join(runDirAbs, "diagnosis_report.md"));
        const appliedInfo = await statFileIfExists(path.join(runDirAbs, "applied_repair_manifest.json"));
        runs.push({
          id: runDir,
          runDir,
          taskName: identity.taskName,
          variant: identity.variant,
          manifest,
          taskDir: manifest?.taskDir || "",
          sourceSkillsDir: manifest?.skillsDir || "",
          outputSkillsDir: manifest?.outputSkillsDir || "",
          model: manifest?.strongModel || "",
          endpoint: manifest?.strongBaseUrl || "",
          modifiedAt: latestModifiedAt(stageOutputsInfo, finalInfo, appliedInfo, { modifiedAt: stat.mtime.toISOString() }),
          files: {
            manifest: {
              path: toPosixRelative(fullPath),
              size: stat.size,
              modifiedAt: stat.mtime.toISOString(),
            },
            stageOutputs: stageOutputsInfo,
            finalReport: finalInfo,
            appliedManifest: appliedInfo,
          },
        });
      }
    }
  }

  await walk(repairRoot);
  return runs.sort((a, b) => new Date(b.modifiedAt || 0) - new Date(a.modifiedAt || 0)).slice(0, 200);
}

function ensureStageReadableFile(relPath) {
  if (!relPath) throw new Error("path is required");
  const resolved = resolveArtifactPath(relPath);
  const relative = toPosixRelative(resolved);
  const isRepairRunFile = relative.startsWith("repair-runs/");
  const isPromptTemplate = relative.startsWith("offline_skill_rca/prompt_templates/") && relative.endsWith(".txt");
  if (!isRepairRunFile && !isPromptTemplate) {
    throw new Error("Only repair-runs files and prompt templates can be read");
  }
  return { resolved, relative };
}

function ensureStageWritableFile(relPath) {
  const { resolved, relative } = ensureStageReadableFile(relPath);
  const isPromptFile = relative.startsWith("repair-runs/") && relative.includes("/prompts/") && relative.endsWith(".prompt.txt");
  const isPromptTemplate = relative.startsWith("offline_skill_rca/prompt_templates/") && relative.endsWith(".txt");
  if (!isPromptFile && !isPromptTemplate) {
    throw new Error("Only stage prompt files and prompt templates can be edited");
  }
  return { resolved, relative };
}

async function handleApi(req, res, url) {
  if (req.method === "GET" && url.pathname === "/api/health") {
    return sendJson(res, 200, {
      ok: true,
      node: process.version,
      platform: process.platform,
      workspace: ROOT,
      python: pythonExecutable(),
      taskCount: state.tasks.length,
      skillLibraryCount: state.skillLibraries.length,
    });
  }

  if (req.method === "GET" && url.pathname === "/api/tasks") {
    await refreshTasks();
    return sendJson(res, 200, { tasks: state.tasks, skillLibraries: state.skillLibraries });
  }

  if (req.method === "GET" && url.pathname === "/api/runs") {
    return sendJson(res, 200, {
      groups: state.groups.map(publicGroup),
      runs: state.runs.map(publicRun),
    });
  }

  if (req.method === "GET" && url.pathname === "/api/repair/jobs") {
    return sendJson(res, 200, {
      repairJobs: state.repairJobs.map(publicRepairJob),
      reports: await scanRepairReports(),
    });
  }

  if (req.method === "GET" && url.pathname === "/api/repair/report") {
    const reportPath = url.searchParams.get("reportPath");
    if (!reportPath) return sendError(res, 400, "reportPath is required");
    const resolved = resolveArtifactPath(reportPath);
    const relative = toPosixRelative(resolved);
    if (!relative.startsWith("repair-runs/") || path.basename(resolved) !== "README.md") {
      return sendError(res, 403, "Only repair-runs README.md reports can be read");
    }
    const text = await fsp.readFile(resolved, "utf8");
    return sendJson(res, 200, { reportPath: relative, text });
  }

  if (req.method === "GET" && url.pathname === "/api/repair-traces") {
    return sendJson(res, 200, { runs: await scanRepairTraceRuns() });
  }

  if (req.method === "GET" && url.pathname === "/api/repair-trace") {
    const runDir = url.searchParams.get("runDir");
    if (!runDir) return sendError(res, 400, "runDir is required");
    return sendJson(res, 200, { trace: await readRepairTraceDetail(runDir) });
  }

  if (req.method === "GET" && url.pathname === "/api/repair-stage/runs") {
    return sendJson(res, 200, {
      runs: await scanStageDebugRuns(),
      jobs: state.stageJobs.map(publicStageJob),
    });
  }

  if (req.method === "GET" && url.pathname === "/api/repair-stage/presets") {
    const id = String(url.searchParams.get("id") || "").trim();
    return sendJson(res, 200, id ? { preset: loadRepairStagePreset(id) } : { presets: repairStagePresetSummaries() });
  }

  if (req.method === "POST" && url.pathname === "/api/repair-stage/presets") {
    const body = await parseBody(req);
    const preset = await saveRepairStagePreset(body);
    return sendJson(res, 200, { ok: true, preset, presets: repairStagePresetSummaries() });
  }

  if (req.method === "DELETE" && url.pathname === "/api/repair-stage/presets") {
    const id = String(url.searchParams.get("id") || "").trim();
    if (!id) return sendError(res, 400, "Preset id is required");
    await deleteRepairStagePreset(id);
    return sendJson(res, 200, { ok: true, presets: repairStagePresetSummaries() });
  }

  if (req.method === "GET" && url.pathname === "/api/repair-stage/status") {
    const outputDir = url.searchParams.get("outputDir");
    if (!outputDir) return sendError(res, 400, "outputDir is required");
    const relative = toPosixRelative(resolveInsideRoot(outputDir));
    if (!relative.startsWith("repair-runs/")) return sendError(res, 403, "Only repair-runs output directories are supported");
    return sendJson(res, 200, await readStageStatus(relative));
  }

  if (req.method === "GET" && url.pathname === "/api/repair-stage/template-variable") {
    const name = url.searchParams.get("name");
    if (!name) return sendError(res, 400, "name is required");
    return sendJson(res, 200, await readTemplateVariable(name));
  }

  if (req.method === "GET" && url.pathname === "/api/repair-stage/file") {
    const relPath = url.searchParams.get("path");
    const { resolved, relative } = ensureStageReadableFile(relPath);
    const info = await statFileIfExists(resolved);
    if (!info) return sendError(res, 404, "File not found");
    if (info.size > MAX_REPAIR_TEXT_BYTES) return sendError(res, 413, "File is too large to read in the stage editor");
    const text = await fsp.readFile(resolved, "utf8");
    return sendJson(res, 200, { path: relative, info, text });
  }

  if (req.method === "POST" && url.pathname === "/api/repair-stage/file") {
    const body = await parseBody(req);
    const { resolved, relative } = ensureStageWritableFile(body.path);
    const text = String(body.text ?? "");
    if (Buffer.byteLength(text, "utf8") > MAX_REPAIR_TEXT_BYTES) return sendError(res, 413, "Text is too large");
    await fsp.mkdir(path.dirname(resolved), { recursive: true });
    await fsp.writeFile(resolved, text, "utf8");
    return sendJson(res, 200, { ok: true, path: relative });
  }

  if (req.method === "POST" && url.pathname === "/api/repair-stage/infer-inputs") {
    const body = await parseBody(req);
    return sendJson(res, 200, await inferRepairStageInputs(body));
  }

  if (req.method === "POST" && url.pathname === "/api/repair-stage/init") {
    const body = await parseBody(req);
    const job = createStageJob(normalizeStageInitOptions(body));
    return sendJson(res, 200, { job: publicStageJob(job) });
  }

  if (req.method === "POST" && url.pathname === "/api/repair-stage/prompt") {
    const body = await parseBody(req);
    const job = createStageJob(normalizeStageRunOptions("prompt", body));
    return sendJson(res, 200, { job: publicStageJob(job) });
  }

  if (req.method === "POST" && url.pathname === "/api/repair-stage/run") {
    const body = await parseBody(req);
    const job = createStageJob(normalizeStageRunOptions("run", body));
    return sendJson(res, 200, { job: publicStageJob(job) });
  }

  if (req.method === "POST" && url.pathname === "/api/repair-stage/calculate") {
    const body = await parseBody(req);
    const job = createStageJob(normalizeStageRunOptions("calculate", body));
    return sendJson(res, 200, { job: publicStageJob(job) });
  }

  if (req.method === "POST" && url.pathname === "/api/repair-stage/finalize") {
    const body = await parseBody(req);
    const job = createStageJob(normalizeStageRunOptions("finalize", body));
    return sendJson(res, 200, { job: publicStageJob(job) });
  }

  if (req.method === "POST" && url.pathname === "/api/repair-stage/pause") {
    const body = await parseBody(req);
    const outputDir = String(body.outputDir || "").trim().replace(/\\/g, "/");
    if (!outputDir) return sendError(res, 400, "outputDir is required");
    const relative = toPosixRelative(resolveInsideRoot(outputDir));
    if (!relative.startsWith("repair-runs/")) return sendError(res, 403, "Only repair-runs output directories are supported");
    return sendJson(res, 200, await runStageCliJson(["pause", "--output-dir", relative], process.env, 20_000));
  }

  const stageLogs = url.pathname.match(/^\/api\/repair-stage\/jobs\/([^/]+)\/logs$/);
  if (req.method === "GET" && stageLogs) {
    const job = state.stageJobs.find((item) => item.id === stageLogs[1]);
    if (!job) return sendError(res, 404, "Stage job not found");
    return sendJson(res, 200, { job: publicStageJob(job), logs: job.logs });
  }

  const stopStageJob = url.pathname.match(/^\/api\/repair-stage\/jobs\/([^/]+)\/stop$/);
  if (req.method === "POST" && stopStageJob) {
    const job = state.stageJobs.find((item) => item.id === stopStageJob[1]);
    if (!job) return sendError(res, 404, "Stage job not found");
    if (job.status === "running") {
      job.status = "stopping";
      job.pauseRequested = job.stage === "stage-08-transactional-skill-repair";
      appendStageLog(job, "Stop requested");
      killProcessTree(job.process?.pid);
      if (job.pauseRequested) {
        // 先终止可能正在等待 LLM 的 Python 进程，再用一个短命令把 active
        // interaction 标为 interrupted。状态落盘后，后续“运行至完成”会从同一
        // repair/review 逻辑位置重试，候选库不会出现半提交。
        await new Promise((resolve) => setTimeout(resolve, 350));
        try {
          await runStageCliJson(["pause", "--output-dir", job.outputDir], buildStageEnv(job), 20_000);
          appendStageLog(job, "Stage 7 pause state persisted");
        } catch (error) {
          appendStageLog(job, `Pause state persistence failed: ${error.message}`);
        }
      }
    }
    emit("repair_stage_update", { stageJob: publicStageJob(job) });
    return sendJson(res, 200, { job: publicStageJob(job) });
  }

  if (req.method === "POST" && url.pathname === "/api/repair/start") {
    const body = await parseBody(req);
    const repairJob = createRepairJob(normalizeRepairOptions(body));
    return sendJson(res, 200, { repairJob: publicRepairJob(repairJob) });
  }

  const repairLogs = url.pathname.match(/^\/api\/repair\/jobs\/([^/]+)\/logs$/);
  if (req.method === "GET" && repairLogs) {
    const job = state.repairJobs.find((item) => item.id === repairLogs[1]);
    if (!job) return sendError(res, 404, "Repair job not found");
    return sendJson(res, 200, { repairJob: publicRepairJob(job), logs: job.logs });
  }

  const stopRepairJob = url.pathname.match(/^\/api\/repair\/jobs\/([^/]+)\/stop$/);
  if (req.method === "POST" && stopRepairJob) {
    const job = state.repairJobs.find((item) => item.id === stopRepairJob[1]);
    if (!job) return sendError(res, 404, "Repair job not found");
    if (job.status === "running") {
      job.status = "stopping";
      appendRepairLog(job, "Stop requested");
      killProcessTree(job.process?.pid);
    }
    emit("repair_update", { repairJob: publicRepairJob(job) });
    return sendJson(res, 200, { repairJob: publicRepairJob(job) });
  }

  if (req.method === "GET" && url.pathname === "/api/history") {
    const history = await scanHistory();
    return sendJson(res, 200, { history });
  }

  if (req.method === "GET" && url.pathname === "/api/job-detail") {
    const artifactDir = url.searchParams.get("artifactDir");
    if (!artifactDir) return sendError(res, 400, "artifactDir is required");
    return sendJson(res, 200, { detail: await readJobDetail(artifactDir) });
  }

  if (req.method === "GET" && url.pathname === "/api/trajectory") {
    const artifactDir = url.searchParams.get("artifactDir");
    const rollout = url.searchParams.get("rollout");
    if (!artifactDir) return sendError(res, 400, "artifactDir is required");
    return sendJson(res, 200, { trajectory: await readTrajectory(artifactDir, rollout) });
  }

  if (req.method === "GET" && url.pathname === "/api/analysis-context") {
    const context = await resolveAnalysisInputs({
      taskPath: url.searchParams.get("taskPath"),
      trajectoryPath: url.searchParams.get("trajectoryPath"),
      resultPath: url.searchParams.get("resultPath"),
      artifactDir: url.searchParams.get("artifactDir"),
      rollout: url.searchParams.get("rollout"),
    });
    return sendJson(res, 200, { context });
  }

  if (req.method === "POST" && url.pathname === "/api/analyze-trajectory") {
    const body = await parseBody(req);
    const context = await resolveAnalysisInputs(body);
    const analysis = await runAnalysisScript({
      ...context,
      judge: body.judge || {},
      keywordExtraction: body.keywordExtraction || {},
    });
    return sendJson(res, 200, { context, analysis });
  }

  if (req.method === "GET" && url.pathname === "/api/events") {
    res.writeHead(200, {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      Connection: "keep-alive",
    });
    res.write(`data: ${JSON.stringify(snapshot())}\n\n`);
    state.clients.add(res);
    req.on("close", () => state.clients.delete(res));
    return;
  }

  if (req.method === "POST" && url.pathname === "/api/start") {
    const body = await parseBody(req);
    if (blockedByJobsRootStopSentinel(body.jobsRoot)) {
      return sendError(res, 409, `Jobs root is paused by STOP sentinel: ${body.jobsRoot}`);
    }
    try {
      await checkDockerAvailable();
    } catch (error) {
      return sendError(res, 503, error.message || "Docker API is not accessible");
    }
    const group = createGroup(normalizeOptions(body));
    return sendJson(res, 200, { group: publicGroup(group) });
  }

  const stopGroup = url.pathname.match(/^\/api\/groups\/([^/]+)\/stop$/);
  if (req.method === "POST" && stopGroup) {
    const group = state.groups.find((item) => item.id === stopGroup[1]);
    if (!group) return sendError(res, 404, "Group not found");
    const blockedCurlStop = isBlockedCurlStopRequest(req);
    await auditStopRequest(req, "group", group.id, group, { rejected: blockedCurlStop });
    if (blockedCurlStop) {
      return sendError(res, 403, "curl stop requests require x-allow-runner-curl-stop: 1");
    }
    group.status = "stopping";
    const runs = state.runs.filter((run) => run.groupId === group.id);
    for (const run of runs) {
      if (run.status === "pending") {
        run.status = "stopped";
        run.endedAt = nowIso();
      }
      if (run.status === "running") {
        appendLog(run, "Stop requested");
        killProcessTree(run.process?.pid);
      }
    }
    emit("snapshot", snapshot());
    return sendJson(res, 200, { group: publicGroup(group) });
  }

  const logRun = url.pathname.match(/^\/api\/runs\/([^/]+)\/logs$/);
  if (req.method === "GET" && logRun) {
    const run = state.runs.find((item) => item.id === logRun[1]);
    if (!run) return sendError(res, 404, "Run not found");
    return sendJson(res, 200, { run: publicRun(run), logs: run.logs });
  }

  const stopRun = url.pathname.match(/^\/api\/runs\/([^/]+)\/stop$/);
  if (req.method === "POST" && stopRun) {
    const run = state.runs.find((item) => item.id === stopRun[1]);
    if (!run) return sendError(res, 404, "Run not found");
    const blockedCurlStop = isBlockedCurlStopRequest(req);
    await auditStopRequest(req, "run", run.id, run, { rejected: blockedCurlStop });
    if (blockedCurlStop) {
      return sendError(res, 403, "curl stop requests require x-allow-runner-curl-stop: 1");
    }
    if (run.status === "pending") {
      run.status = "stopped";
      run.endedAt = nowIso();
    }
    if (run.status === "running") {
      appendLog(run, "Stop requested");
      killProcessTree(run.process?.pid);
    }
    emit("run_update", { run: publicRun(run) });
    return sendJson(res, 200, { run: publicRun(run) });
  }

  return sendError(res, 404, "Unknown API route");
}

function contentType(filePath) {
  if (filePath.endsWith(".html")) return "text/html; charset=utf-8";
  if (filePath.endsWith(".css")) return "text/css; charset=utf-8";
  if (filePath.endsWith(".js")) return "application/javascript; charset=utf-8";
  if (filePath.endsWith(".json")) return "application/json; charset=utf-8";
  return "application/octet-stream";
}

async function serveStatic(req, res, url) {
  const requestPath = decodeURIComponent(url.pathname === "/" ? "/index.html" : url.pathname);
  const filePath = path.resolve(PUBLIC_DIR, `.${requestPath}`);
  if (!filePath.startsWith(PUBLIC_DIR)) {
    return sendError(res, 403, "Forbidden");
  }
  try {
    const data = await fsp.readFile(filePath);
    res.writeHead(200, { "Content-Type": contentType(filePath) });
    res.end(data);
  } catch {
    sendError(res, 404, "Not found");
  }
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);
  try {
    if (url.pathname.startsWith("/api/")) {
      await handleApi(req, res, url);
    } else {
      await serveStatic(req, res, url);
    }
  } catch (error) {
    console.error(error);
    sendError(res, 500, error.message || "Internal server error");
  }
});
await refreshTasks();

server.listen(PORT, () => {
  console.log(`SkillsBench Runner listening on http://localhost:${PORT}`);
  console.log(`Workspace: ${ROOT}`);
});
